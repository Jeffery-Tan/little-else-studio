/* Shipped journey + app DOM reading with a synthetic backend image-analysis projection. */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {loadApp,makeDom} from './dom-fixture.mjs';
function page() {
  const calls=[];
  const p=loadApp({runtime:'real',dom:makeDom({ids:['stage','chat-input','send','toast','run-select'],body:''}),
    globals:{setTimeout:()=>0,clearTimeout:()=>{},setInterval:()=>0,clearInterval:()=>{},
      StudioReferenceVideo:{breakdownHtml:()=>{calls.push('reference-render');return '<article>Existing video reference</article>';},
        mountBreakdowns:()=>calls.push('reference-wire')}}});
  p.read=code=>vm.runInContext(code,p.ctx);p.calls=calls;
  p.read('state.runId="image-project";state.referenceTasks=[];state.materials=[];state.creatorView={analysisOnly:true};');
  return p;
}
function analysis() {
  return {kind:'image',analysis_only:true,status:'complete',goal:'图片分析',title:'图片分析已完成',
    total:2,observed:2,missing:[],detail:'本轮两张观察已保存。',job_id:'image-job',
    request_id:'image-request',source_message_id:'image-message',turn_id:'image-turn',
    interpretation_id:'image-interpretation',report:'| 图 | 可见与未知 |\n| --- | --- |\n| way to carry.png | 肩带图示，功能未知 |\n| texture.png | <script>unsafe()</script> |'};
}

test('the real analysis reader presents the original image report rather than an empty video reference',()=>{
  const p=page(),value={brief:{analysis:analysis()},rail:[]},before=JSON.stringify(value);
  p.ctx.tl=value;p.read('state.timeline=tl;renderReferenceAnalysis()');
  const host=p.document.getElementById('stage');
  assert.ok(host.querySelector('[data-image-analysis-report]'));
  assert.equal(host.querySelector('h1').textContent,'图片分析');
  assert.equal(host.querySelectorAll('th').length,2);
  assert.equal(host.querySelectorAll('td').length,4);
  assert.match(host.textContent,/way to carry\.png/);
  assert.equal(host.querySelector('script'),null);
  assert.deepEqual(p.calls,[]);
  assert.equal(JSON.stringify(value),before);
  assert.equal(p.observed.filter(row=>row.method!=='GET').length,0);
});

test('an unfinished image reader displays exact backend title and detail without inventing completion',()=>{
  const p=page(),a={...analysis(),status:'incomplete',report:'',observed:1,missing:['texture.png'],
    title:'第二张图片尚未完成',detail:'已保存第一张观察；第二张返回格式不符合要求。'};
  p.ctx.tl={brief:{analysis:a},rail:[]};p.read('state.timeline=tl;renderReferenceAnalysis()');
  const host=p.document.getElementById('stage');
  assert.match(host.textContent,/第二张图片尚未完成/);
  assert.match(host.textContent,/已保存第一张观察/);
  assert.doesNotMatch(host.textContent,/图片分析已完成|参考视频|完整方案/);
  assert.equal(host.querySelector('table'),null);assert.deepEqual(p.calls,[]);
});

test('project switches and non-only or creative projections retain the old video reader and clear the old image report',()=>{
  const p=page();p.ctx.tl={brief:{analysis:analysis()},rail:[]};
  p.read('state.timeline=tl;renderReferenceAnalysis()');
  for(const tl of [{brief:{},rail:[]},{brief:{analysis:{...analysis(),analysis_only:false}},rail:[]},
    {brief:{analysis:analysis()},rail:[{stage:'writing',has_doc:true}]}]){
    p.ctx.tl=tl;p.read('state.runId="video-project";state.timeline=tl;renderReferenceAnalysis()');
    const host=p.document.getElementById('stage');
    assert.equal(host.querySelector('[data-image-analysis-report]'),null);
    assert.equal(host.querySelector('table'),null);
    assert.match(host.textContent,/Existing video reference/);
  }
  assert.equal(p.calls.length,6);
  assert.equal(p.observed.filter(row=>row.method!=='GET').length,0);
});
