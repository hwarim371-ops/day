const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
function fixture(job) {
  const calls=[], banner={hidden:true};
  const ctx=vm.createContext({window:{},document:{getElementById:()=>banner},crypto:{randomUUID:()=> 'test-id'},setTimeout:callback=>callback()});
  ctx.google={script:{get run(){let success;return {
    withSuccessHandler(fn){success=fn;return this;}, withFailureHandler(){return this;},
    uiRequest(path,payload,id){calls.push({path,payload,id});success({ok:true,value:path==='/api/job'?job:{queued:true,job_id:'job-1'}});}
  };}}};
  vm.runInContext(fs.readFileSync('cloud/apps_script/transport.js','utf8'),ctx);
  return {api:ctx.window.bookingCloudApi,calls,banner};
}
(async()=>{
  for(const state of ['completed','issues']){
    const f=fixture({state,response:{keys:['a'],revision:'new'}});
    const payload={collect_after:true,wait_for_completion:true,edits:[{key:'a'}]};
    const [result,other]=await Promise.all([f.api('/api/apply',payload),f.api('/api/apply',payload)]);
    assert.equal(result.collection_state,state);assert.equal(other.keys[0],'a');
    assert.equal(f.calls.filter(c=>c.path==='/api/apply').length,1);
    assert.equal(f.calls.some(c=>c.path==='/api/start'),false);
    assert.equal(f.banner.hidden,true);
  }
  const failed=fixture({state:'failed',message:'DB not published'});
  await assert.rejects(()=>failed.api('/api/apply',{collect_after:true,wait_for_completion:true}),/DB not published/);
  const pending=fixture({state:'completed',response:{keys:['a']}});
  const order=fixture({state:'completed'});
  await order.api('/api/list-order',{keys:['a'],order_revision:''});
  assert.equal(order.calls.length,1);
  assert.equal(order.banner.hidden,true);
  const result=await pending.api('/api/apply',{collect_after:true});
  assert.equal(result.queued,true);assert.equal(pending.calls.length,1);
  console.log('PASS one combined job, completed/issues response, duplicate dispatch prevention, failure propagation, existing queued mode');
})().catch(error=>{console.error(error);process.exitCode=1;});
