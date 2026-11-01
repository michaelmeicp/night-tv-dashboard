#!/usr/bin/env python3
"""Validate public feeds and rebuild this artifact's reviewed snapshot, once.

No daemon, credentials, scheduling, runtime patching, or source-system writes.
Failed sources retain their last valid values AND original observation/fetch times.
"""
import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from weather_source import CURRENT_URL, FORECAST_URL, parse_weather

UTC = timezone.utc
SNAPSHOT = re.compile(r'(<script type="application/json" id="data-app-reviewed-snapshot">)(.*?)(</script>)', re.S)
QUOTE_URL = 'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,binancecoin&vs_currencies=usd&include_24hr_change=true&include_last_updated_at=true'


def iso(dt):
    return dt.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def instant(value):
    if not isinstance(value, str):
        raise ValueError('missing timestamp')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def number(value, low=0, high=1e10):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low < value < high:
        raise ValueError('invalid numeric observation')
    return value


def fetch(url):
    # No automatic retry, key, cookie, proxy, alternate domain, or denial bypass.
    with urlopen(Request(url, headers={'User-Agent': 'NightTVDashboard/1.0 (public data refresh)'}), timeout=20) as response:
        body = response.read(2_000_001)
        if len(body) > 2_000_000:
            raise ValueError('response too large')
        return body


def read_json(fetcher, url):
    return json.loads(fetcher(url))


def validate_time(value, now, oldest_days=40):
    dt = instant(value)
    if dt > now + timedelta(minutes=10) or dt < now - timedelta(days=oldest_days):
        raise ValueError('observation timestamp outside expected period')
    return value


def crypto_quote(payload, key, now):
    row = payload[key]
    value = number(row['usd'])
    timestamp = iso(datetime.fromtimestamp(number(row['last_updated_at'], 1, 1e11), UTC))
    validate_time(timestamp, now, 7)
    change = number(row['usd_24h_change'], -100, 10000)
    return value, timestamp, change


def crypto_history(payload, now):
    values = payload.get('prices')
    if not isinstance(values, list) or not 3 <= len(values) <= 100:
        raise ValueError('invalid history size')
    rows = []
    previous = None
    for item in values:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError('invalid history point')
        date = iso(datetime.fromtimestamp(number(item[0], 1, 1e15) / 1000, UTC))
        validate_time(date, now)
        if previous is not None and date <= previous:
            raise ValueError('history dates not strictly increasing')
        previous = date
        rows.append({'date': date, 'value': number(item[1])})
    return rows


def fx_history(payload, currency, now):
    if currency == 'HKD':
        if payload.get('base') != 'USD' or not isinstance(payload.get('rates'), dict):
            raise ValueError('wrong FX base or schema')
        rows = [{'date': date, 'value': number(rates[currency], 1, 20)} for date, rates in sorted(payload['rates'].items())]
    else:
        if not isinstance(payload, list):
            raise ValueError('invalid AMCM schema')
        rows = []
        for row in payload:
            if row.get('base') != 'USD' or row.get('quote') != 'MOP':
                raise ValueError('wrong AMCM currency')
            rows.append({'date': row['date'], 'value': number(row['rate'], 1, 20)})
        rows.sort(key=lambda row: row['date'])
    previous = None
    for row in rows:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', row['date']):
            raise ValueError('invalid FX date')
        validate_time(row['date'], now)
        if previous is not None and row['date'] <= previous:
            raise ValueError('duplicate FX date')
        previous = row['date']
    start = (now - timedelta(days=30)).date().isoformat()
    rows = [row for row in rows if row['date'] >= start]
    if not 3 <= len(rows) <= 40:
        raise ValueError('invalid FX history size')
    return rows


def put_history(snapshot, ident, points, url, fetched_at):
    meta = snapshot['histories'][ident]
    meta.update(period_start=points[0]['date'], period_end=points[-1]['date'], as_of=points[-1]['date'], fetched_at=fetched_at, source_url=url,
        period='Rolling 30-day window; published observations only')
    meta['change_pct'] = (points[-1]['value'] / points[0]['value'] - 1) * 100
    query = snapshot['queries']['history_' + ident]
    template = query['rows'][0]
    query['rows'] = [{**template, **point, 'source_url': url, 'fetch_timestamp': fetched_at} for point in points]
    query['source']['executedAt'] = fetched_at
    query['source']['notes'] = [meta.get('price_type', 'Published observations'), '真實日期與觀測值，未補週末或缺值。']
    query['source']['evidenceFlow'] = [{'title': '公開來源', 'detail': url}]


def refresh(snapshot, fetcher=fetch, now=None):
    fixed_now = now
    clock = lambda: fixed_now or datetime.now(UTC)
    now = clock()
    result = copy.deepcopy(snapshot)
    attempted_at = iso(now)
    status = copy.deepcopy(result.get('refresh', {}).get('sources', {}))
    market = {row['id']: row for row in result['queries']['market']['rows']}

    def run(key, url, callback):
        old = status.get(key, {})
        started = iso(clock())
        try:
            callback()
            status[key] = {'status': 'success', 'last_attempt_at': started, 'last_success_at': iso(clock()), 'source_url': url}
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, OverflowError) as error:
            code = 'http_' + str(error.code) if isinstance(error, HTTPError) else 'invalid_data' if isinstance(error, (ValueError, KeyError, TypeError, OverflowError)) else 'network_error'
            status[key] = {**old, 'status': 'error', 'last_attempt_at': started, 'error': code, 'source_url': url}

    def quotes():
        payload = read_json(fetcher, QUOTE_URL)
        # Validate both before accepting either: partial/corrupt responses are atomic.
        parsed = {ident: crypto_quote(payload, key, now) for ident, key in [('btc', 'bitcoin'), ('bnb', 'binancecoin')]}
        completed = iso(clock())
        for ident, (value, timestamp, change) in parsed.items():
            market[ident].update(value=value, display=f'{value:,.2f}', quote_timestamp=timestamp, source_quote_time=timestamp,
                fetch_timestamp=completed, change_24h_percent=change, source_url=QUOTE_URL,
                detail=datetime.fromisoformat(timestamp.replace('Z', '+00:00')).astimezone(timezone(timedelta(hours=8))).strftime('%m/%d %H:%M') + f' 報價 · 24h {change:+.2f}%', status='snapshot')
    run('crypto_quotes', QUOTE_URL, quotes)

    for ident, coin in [('btc', 'bitcoin'), ('bnb', 'binancecoin')]:
        url = f'https://api.coingecko.com/api/v3/coins/{coin}/market_chart?vs_currency=usd&days=30&interval=daily'
        def history(ident=ident, url=url):
            points = crypto_history(read_json(fetcher, url), now)
            put_history(result, ident, points, url, iso(clock()))
        run('history_' + ident, url, history)

    start = (now - timedelta(days=30)).date().isoformat()
    for ident, currency, url in [
        ('usd_hkd', 'HKD', f'https://api.frankfurter.dev/v1/{start}..?base=USD&symbols=HKD'),
        ('usd_mop', 'MOP', f'https://api.frankfurter.dev/v2/rates?base=USD&quotes=MOP&from={start}&providers=AMCM'),
    ]:
        def rates(ident=ident, currency=currency, url=url):
            points = fx_history(read_json(fetcher, url), currency, now)
            completed = iso(clock())
            put_history(result, ident, points, url, completed)
            last = points[-1]
            market[ident].update(value=last['value'], display=f"{last['value']:.4f}", quote_timestamp=last['date'],
                source_quote_time=last['date'], fetch_timestamp=completed, source_url=url,
                detail=last['date'] + ' 每日參考匯率')
        run('history_' + ident, url, rates)

    def weather():
        current, forecast = fetcher(CURRENT_URL), fetcher(FORECAST_URL)
        query = parse_weather(current, forecast, iso(clock()))
        # Do not accept future station observations.
        validate_time(query['rows'][0]['quote_timestamp'], now, 7)
        result['queries']['weather'] = query
    run('weather', CURRENT_URL, weather)

    for row in market.values():
        if row['id'] in ['btc', 'bnb']:
            row['refresh_key'], row['stale_after_seconds'] = 'crypto_quotes', 1800
        elif row['id'] in ['usd_hkd', 'usd_mop']:
            row['refresh_key'], row['stale_after_seconds'] = 'history_' + row['id'], 4 * 86400
    for row in result['queries']['weather']['rows']:
        row['refresh_key'] = 'weather'
        if row['id'] == 'macau_temperature':
            row['stale_after_seconds'] = 2 * 3600
    for name in ['market', 'watchlist']:
        if name == 'market' and any(entry['status'] == 'success' for key, entry in status.items() if key != 'weather'):
            result['queries'][name]['source']['executedAt'] = max(entry['last_success_at'] for key, entry in status.items() if key != 'weather' and entry['status'] == 'success')
        result['queries'][name]['source']['notes'] = [
            '每列 quote_timestamp 為來源報價時間；fetch_timestamp 僅為成功擷取時間，失敗時不改寫。',
            '加密貨幣、匯率與天氣可透過本專案一次性更新工具重建；股票、基金、原油與油價保留原快照或缺口。',
            '沒有自動排程或瀏覽器即時行情；每日匯率非即時買賣價。股票變動不含股息。']
    result['refresh'] = {'mode': 'validated-build', 'attempted_at': attempted_at, 'sources': status}
    result['generatedAt'] = iso(clock())
    return result


def rebuild(html, snapshot):
    encoded = json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False).replace('<', '\\u003c')
    updated, count = SNAPSHOT.subn(lambda m: m[1] + encoded + m[3], html, count=1)
    if count != 1:
        raise ValueError('reviewed snapshot not found')
    updated, count = re.subn(r'(<meta name="data-app-snapshot-sha256" content=")[^"]+', lambda m: m[1] + hashlib.sha256(encoded.encode()).hexdigest(), updated, count=1)
    if count != 1:
        raise ValueError('snapshot hash not found')
    return updated


def version_manifest(html, snapshot):
    digest = re.search(r'<meta name="data-app-snapshot-sha256" content="([^"]+)', html)[1]
    return json.dumps({'schema': 1, 'snapshot_sha256': digest, 'generated_at': snapshot['generatedAt']}, indent=2) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index', type=Path, default=Path(__file__).resolve().parents[1] / 'index.html')
    parser.add_argument('--check', action='store_true', help='Fetch and validate without writing the artifact')
    args = parser.parse_args()
    html = args.index.read_text()
    match = SNAPSHOT.search(html)
    if not match:
        raise ValueError('reviewed snapshot not found')
    result = refresh(json.loads(match[2]))
    output = rebuild(html, result)
    if not args.check:
        with tempfile.NamedTemporaryFile(mode='w', dir=args.index.parent, delete=False) as handle:
            handle.write(output)
            temporary = Path(handle.name)
        temporary.replace(args.index)
        args.index.with_name('data-version.json').write_text(version_manifest(output, result))
    print(json.dumps({'generatedAt': result['generatedAt'], 'written': not args.check, 'sources': result['refresh']['sources']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
