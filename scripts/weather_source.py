"""Read the official, public Macau hourly weather and dated forecasts.

The SMG XML feeds are listed as fully open on the Macau open-data catalogue.
This is a one-shot build-time reader, not a browser proxy or a scheduler.
All returned observation/publication times retain their distinct meanings.
"""

from datetime import date, datetime, timedelta, timezone
import math
import re
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


CURRENT_URL = "https://xml.smg.gov.mo/c_actual_brief.xml"
FORECAST_URL = "https://xml.smg.gov.mo/c_forecast.xml"
CATALOGUE_URL = "https://data.gov.mo/Detail?id=070f7d9b-f734-4269-841c-0859912a5b15"
MACAU = timezone(timedelta(hours=8), "Asia/Macau")
MAX_XML_BYTES = 1_000_000
OBSERVATION_STALE_HOURS = 2


class WeatherValidationError(ValueError):
    """A feed is malformed or cannot support the claimed observation."""


def _aware_timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError) as exc:
        raise WeatherValidationError("Invalid fetch timestamp") from exc
    if stamp.tzinfo is None:
        raise WeatherValidationError("Fetch timestamp must include a timezone")
    return stamp


def _macau_timestamp(value, field):
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=MACAU)
    except (ValueError, AttributeError) as exc:
        raise WeatherValidationError("Invalid SMG " + field) from exc


def _required_text(root, path):
    value = root.findtext(path)
    if value is None or not value.strip():
        raise WeatherValidationError("Missing SMG field " + path)
    return value.strip()


def _parse_xml(raw, expected_root):
    if not isinstance(raw, (bytes, str)):
        raise WeatherValidationError("XML input must be bytes or text")
    payload = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(payload) > MAX_XML_BYTES:
        raise WeatherValidationError("Weather XML exceeds size limit")
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", payload, re.I):
        raise WeatherValidationError("DTD and entity declarations are not accepted")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise WeatherValidationError("Malformed SMG XML") from exc
    if root.tag != expected_root:
        raise WeatherValidationError("Unexpected SMG XML root")
    return root


def _number(value, minimum, maximum, field):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise WeatherValidationError("Invalid " + field) from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise WeatherValidationError("Out-of-range " + field)
    return int(result) if result.is_integer() else result


def _observation(custom, name, unit, minimum, maximum, optional=False):
    matches = [node for node in custom.findall(name) if node.findtext("Type") == "3"]
    if not matches and optional:
        return None
    if len(matches) != 1:
        raise WeatherValidationError("Expected one type-3 " + name + " observation")
    node = matches[0]
    if _required_text(node, "MeasureUnit") != unit:
        raise WeatherValidationError("Unexpected unit for " + name)
    return _number(_required_text(node, "Value"), minimum, maximum, name)


def parse_weather(current_xml, forecast_xml, fetched_at):
    """Return a Data query ``{rows, source, methods}`` from two official feeds.

    ``macau_temperature`` is an hourly station observation; ``macau_forecast``
    is today's dated forecast. Extra forecasts retain date-specific IDs.
    No inferred temperature, timestamp or missing forecast range is created.
    A stale observation is retained with an explicit warning.
    """
    fetched = _aware_timestamp(fetched_at)
    current = _parse_xml(current_xml, "ActualWeatherBrief")
    forecast = _parse_xml(forecast_xml, "ActualForecast")
    custom = current.find("Custom")
    if custom is None:
        raise WeatherValidationError("Missing SMG current-weather payload")
    observed = _macau_timestamp(_required_text(custom, "ValidFor"), "observation time")
    published = _macau_timestamp(_required_text(current, "System/SysPubdate"), "publication time")
    forecast_published = _macau_timestamp(
        _required_text(forecast, "System/SysPubdate"), "forecast publication time"
    )
    # The hourly brief may be published a few minutes before its valid hour.
    future_limit = fetched + timedelta(minutes=15)
    if any(stamp > future_limit for stamp in (observed, published, forecast_published)):
        raise WeatherValidationError("SMG timestamp is implausibly in the future")
    if published > observed + timedelta(hours=2):
        raise WeatherValidationError("Observation and publication timestamps disagree")
    temperature = _observation(custom, "Temperature", "°C", -20, 60)
    humidity = _observation(custom, "Humidity", "%", 0, 100)
    wind = _observation(custom, "WindSpeed", "km/h", 0, 300, optional=True)
    stale = fetched - observed > timedelta(hours=OBSERVATION_STALE_HOURS)
    source = "澳門地球物理氣象局（SMG）"
    row = {
        "id": "macau_temperature",
        "label": "澳門氣溫｜官方整點觀測",
        "value": temperature,
        "unit": "°C",
        "humidity_percent": humidity,
        "quote_timestamp": observed.isoformat(),
        "quote_timezone": "Asia/Macau",
        "published_at": published.isoformat(),
        "fetch_timestamp": fetched.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_url": CURRENT_URL,
        "source": source,
        "status": "stale" if stale else "snapshot",
        "is_live": False,
        "warning": "官方觀測已逾2小時" if stale else "官方整點觀測，非連續即時讀數",
        "summary": custom.findtext("Icon/IconDescription", "").strip(),
        "asof": "觀測：" + observed.strftime("%m/%d %H:%M") + " 澳門",
    }
    if wind is not None:
        row["wind_kmh"] = wind
    today = fetched.astimezone(MACAU).date()
    forecast_rows = []
    seen_dates = set()
    for item in forecast.findall("Custom/WeatherForecast"):
        try:
            valid = date.fromisoformat(_required_text(item, "ValidFor"))
        except ValueError as exc:
            raise WeatherValidationError("Invalid forecast valid date") from exc
        if valid in seen_dates:
            raise WeatherValidationError("Duplicate forecast valid date")
        seen_dates.add(valid)
        summary = _required_text(item, "WeatherDescription")
        match = re.search(
            r"氣溫[^。\n]*?攝氏\s*(-?\d+(?:\.\d+)?)\s*(?:至|到|–|－)\s*(-?\d+(?:\.\d+)?)\s*度",
            summary,
        )
        bounds = {"low": None, "high": None}
        if match:
            low = _number(match.group(1), -20, 60, "forecast low")
            high = _number(match.group(2), -20, 60, "forecast high")
            if low > high:
                raise WeatherValidationError("Forecast low exceeds high")
            bounds = {"low": low, "high": high}
        forecast_rows.append({
            "id": "macau_forecast" if valid == today else "macau_forecast_" + valid.isoformat(),
            "label": "澳門" + valid.strftime("%m月%d日") + "預報",
            "value": bounds,
            "unit": "°C",
            "quote_timestamp": forecast_published.isoformat(),
            "quote_timestamp_note": "quote_timestamp為官方發布時間，valid_date為預報適用日期",
            "quote_timezone": "Asia/Macau",
            "published_at": forecast_published.isoformat(),
            "fetch_timestamp": row["fetch_timestamp"],
            "source_url": FORECAST_URL,
            "source": source,
            "status": "stale" if valid < today else "forecast",
            "is_live": False,
            "valid_date": valid.isoformat(),
            "summary": summary,
        })
    if not forecast_rows:
        raise WeatherValidationError("Missing dated forecasts")
    # Never relabel yesterday's forecast as today's, even if the feed is stale.
    if today not in seen_dates:
        row["detail"] = "今日預報未有可核實資料"
    else:
        current_forecast = next(r for r in forecast_rows if r["id"] == "macau_forecast")
        bounds = current_forecast["value"]
        if bounds["low"] is not None:
            row["detail"] = "今日預報 " + str(bounds["low"]) + "–" + str(bounds["high"]) + "°C"
        else:
            row["detail"] = "今日預報未提供可解析溫度範圍"
    rows = [row] + sorted(forecast_rows, key=lambda item: item["valid_date"])
    return {
        "rows": rows,
        "source": {
            "label": source,
            "executedAt": row["fetch_timestamp"],
            "notes": [
                "官方整點觀測與逐日預報分開標時；擷取時間不代表觀測時間。",
                "公開資料目錄：" + CATALOGUE_URL,
            ],
            "evidenceFlow": [{
                "title": "官方公開XML",
                "detail": "Type=3為即時錄得觀測；ValidFor為觀測時刻／預報適用日，SysPubdate為發布時間。",
            }],
        },
        "methods": [{
            "language": "text",
            "code": "一次擷取SMG公開XML，驗證數值、單位、時刻及適用日期；不推算缺值，不重標舊預報日期。",
        }],
    }


def fetch_url(url, timeout=20):
    """Read only the two allowlisted, public SMG XML URLs."""
    if url not in (CURRENT_URL, FORECAST_URL):
        raise WeatherValidationError("Weather URL is not allowlisted")
    request = Request(url, headers={"User-Agent": "night-tv-dashboard/1.0 (+public weather reader)"})
    with urlopen(request, timeout=timeout) as response:
        if response.geturl() != url:
            raise WeatherValidationError("Unexpected weather redirect")
        payload = response.read(MAX_XML_BYTES + 1)
    if len(payload) > MAX_XML_BYTES:
        raise WeatherValidationError("Weather XML exceeds size limit")
    return payload


def fetch_weather(fetched_at=None):
    """Fetch/validate public weather once, raising on failure for caller fallback."""
    current = fetch_url(CURRENT_URL)
    forecast = fetch_url(FORECAST_URL)
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return parse_weather(current, forecast, fetched_at)
