/* Authored status logic only. This never changes the protected Data runtime. */
function dataFreshness(row, now, sources) {
  if (row.value == null) return row.status === 'delisted' ? '已除牌' : '未核實';
  const state = sources?.[row.refresh_key];
  const stamp = row.quote_timestamp;
  const observed = typeof stamp === 'string' ? Date.parse(stamp.length === 10 ? stamp + 'T00:00:00Z' : stamp) : NaN;
  const stale = row.status === 'stale' || (row.stale_after_seconds && (!Number.isFinite(observed) || now - observed > row.stale_after_seconds * 1000));
  if (state?.status === 'error') return '更新失敗' + (stale ? ' · 已過期' : ' · 保留舊值');
  if (stale) return '已過期';
  return row.refresh_key ? '已核實快照' : '人工快照';
}

function weatherDetail(rows, today) {
  const forecast = rows.find(row => row.valid_date === today);
  if (!forecast) return '今日預報未更新';
  const {low, high} = forecast.value || {};
  return Number.isFinite(low) && Number.isFinite(high) ? forecast.valid_date + ' 預報 ' + low + '–' + high + '°C' : forecast.valid_date + ' 預報溫度未核實';
}

async function checkPublishedData({fetcher, url, currentDigest, currentGeneratedAt}) {
  const response = await fetcher(url, {cache: 'no-store', credentials: 'omit'});
  if (!response.ok) {
    const error = new Error('published_data_unavailable');
    error.stop = [401, 403, 451].includes(response.status);
    throw error;
  }
  const manifest = await response.json();
  if (manifest.schema !== 1 || !/^[a-f0-9]{64}$/.test(manifest.snapshot_sha256) || !Number.isFinite(Date.parse(manifest.generated_at))) {
    throw new Error('invalid_published_manifest');
  }
  // CDN edges can briefly serve an older manifest. Never reload backwards.
  return manifest.snapshot_sha256 === currentDigest || Date.parse(manifest.generated_at) <= Date.parse(currentGeneratedAt) ? 'current' : 'available';
}
