const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ctx = vm.createContext({});
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../scripts/refresh-state.js'),'utf8'), ctx);
const {dataFreshness,checkPublishedData,weatherDetail} = ctx;
const now = Date.parse('2026-11-01T02:00:00Z');
const fresh = {value:27,quote_timestamp:'2026-11-01T01:00:00Z',stale_after_seconds:7200,refresh_key:'weather'};

test('freshness is based on observation time, never successful fetch time',()=>{
  assert.equal(dataFreshness(fresh,now,{}),'已核實快照');
  assert.equal(dataFreshness({...fresh,quote_timestamp:'2026-10-31T23:00:00Z',fetch_timestamp:new Date(now).toISOString()},now,{}),'已過期');
  assert.equal(dataFreshness({...fresh,quote_timestamp:null},now,{}),'已過期');
});
test('source failure retains explicit stale or last-good status',()=>{
  assert.equal(dataFreshness(fresh,now,{weather:{status:'error'}}),'更新失敗 · 保留舊值');
  assert.equal(dataFreshness(fresh,now+86400000,{weather:{status:'error'}}),'更新失敗 · 已過期');
  assert.equal(dataFreshness({value:null,status:'delisted'},now,{}),'已除牌');
  assert.equal(dataFreshness({value:null},now,{}),'未核實');
  assert.equal(dataFreshness({value:100},now,{}),'人工快照');
});
test('daily FX tolerates a weekend but expires after the stated four days',()=>{
  const row={value:8,quote_timestamp:'2026-10-30',stale_after_seconds:345600,refresh_key:'fx'};
  assert.equal(dataFreshness(row,now,{}),'已核實快照');
  assert.equal(dataFreshness(row,now+4*86400000,{}),'已過期');
});
test('weather never relabels yesterday forecast as today after midnight',()=>{
  const rows=[{valid_date:'2026-11-01',value:{low:25,high:30}}];
  assert.equal(weatherDetail(rows,'2026-11-01'),'2026-11-01 預報 25–30°C');
  assert.equal(weatherDetail(rows,'2026-11-02'),'今日預報未更新');
});
const digest='a'.repeat(64), next='b'.repeat(64);
const options={url:new URL('https://example.com/data-version.json'),currentDigest:digest,currentGeneratedAt:'2026-11-01T01:00:00Z'};
test('same-origin published version detects new data and ignores stale CDN versions',async()=>{
  const check=manifest=>checkPublishedData({...options,fetcher:async(url,opts)=>{
    assert.equal(url,options.url);assert.equal(opts.credentials,'omit');assert.equal(opts.cache,'no-store');
    return {ok:true,json:async()=>manifest};
  }});
  assert.equal(await check({schema:1,snapshot_sha256:digest,generated_at:'2026-11-01T01:00:00Z'}),'current');
  assert.equal(await check({schema:1,snapshot_sha256:next,generated_at:'2026-11-01T02:00:00Z'}),'available');
  assert.equal(await check({schema:1,snapshot_sha256:next,generated_at:'2026-10-31T02:00:00Z'}),'current');
});
test('failed or corrupt version requests cannot replace the page',async()=>{
  await assert.rejects(()=>checkPublishedData({...options,fetcher:async()=>{throw new Error('offline')}}),/offline/);
  await assert.rejects(()=>checkPublishedData({...options,fetcher:async()=>({ok:false,status:403})}),e=>e.stop===true);
  await assert.rejects(()=>checkPublishedData({...options,fetcher:async()=>({ok:true,json:async()=>({schema:1,snapshot_sha256:'bad'})})}),/invalid_published_manifest/);
});
