const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/web/app.js', 'utf8');
function section(start, end) {return source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start)));}
const ctx = vm.createContext({URL, $: () => ({value:'2026-09-27'})});
vm.runInContext(section('function normal(', 'function badge('), ctx);
vm.runInContext(section('function businessValue(', 'function renderSummary('), ctx);
vm.runInContext(section('function parsePlaceId(', 'function openAddCamp('), ctx);
vm.runInContext(section('function cleanHistory(', 'async function openTrend('), ctx);
const entry = {rooms:['A','B'], price_amount:50000, result:{status:'정상', date:'2026-09-27', total:2, reserved:1}};
assert.equal(ctx.businessValue(entry).rate, .5);
assert.equal(ctx.businessValue(entry).revenue, 50000);
for (const changed of [
  {...entry, archived:true}, {...entry, changed:true},
  {...entry, proposal:{changed:true}},
  {...entry, result:{...entry.result, stale:true}},
  {...entry, result:{...entry.result, date:'2026-09-26'}},
  {...entry, result:{...entry.result, status:'객실 확인 필요'}},
  {...entry, result:{...entry.result, total:1}},
  {...entry, result:{...entry.result, reserved:3}},
]) assert.equal(ctx.businessValue(changed).revenue, null);
assert.equal(ctx.businessValue({...entry, price_amount:null}).revenue, null);
assert.equal(ctx.status({...entry, changed:true, result:{...entry.result, missing:1, status:'일부 매칭 실패'}})[0], '객실 1개 미확인');
assert.equal(ctx.status({...entry, changed:true, result:{...entry.result, status:'오류: timeout'}})[0], '수집 접속 오류');
assert.equal(ctx.status({...entry, changed:true, result:{...entry.result, superseded:true}})[0], '저장 완료 · 재수집 대기');
for (const link of ['123456789','https://m.place.naver.com/accommodation/123456789/room','https://map.naver.com/p/entry/place/123456789?x=y']) assert.equal(ctx.parsePlaceId(link),'123456789');
for (const link of ['https://evil.example/place/123456789','https://m.place.naver.com.evil.example/place/123456789','javascript:123456789','https://booking.naver.com/booking/3/bizes/12345']) assert.equal(ctx.parsePlaceId(link),'');
console.log('PASS financial validity, unknown exclusion, price missing, saved versus collected status, place links');
const records=ctx.cleanHistory([
  {date:'2026-09-01', reserved:1, total:2, revenue:50000},
  {date:'2026-09-01', reserved:2, total:2, revenue:100000},
  {date:'2026-09-02', reserved:1, total:2, revenue:null},
  {date:'2026-09-03', reserved:3, total:2, revenue:150000},
  {date:'2026-02-30', reserved:1, total:2, revenue:50000},
]);
assert.equal(records.length,2);
assert.equal(records[0].rate,1);
assert.equal(records[0].revenue,100000);
assert.equal(records[1].revenue,null);
console.log('PASS same-date history deduplication, weighted inputs, invalid date/count exclusion, missing revenue not zero');
