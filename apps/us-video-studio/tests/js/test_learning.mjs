import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../../studio/web/learning.js',import.meta.url),'utf8');
const context=vm.createContext({URL});
vm.runInContext(source,context);
const L=context.StudioLearning;
const plain=v=>JSON.parse(JSON.stringify(v));

test('older backend reference 404 preserves learning access without claiming an empty reference list',async()=>{
  const requests=[];
  const unavailable=await L.loadProjectReferences(async path=>{requests.push(path);throw Object.assign(new Error('Not found'),{status:404});},'run-one');
  assert.deepEqual(requests,['/api/runs/run-one/references']);
  assert.equal(unavailable.capability,false);
  assert.equal(unavailable.references,null);
  const item={id:'one',partition:'calibration',eligible_for:['message']};
  const form=L.referenceForm(item,'run-one',null,null,unavailable.capability);
  const project=L.projectReferences(unavailable.references,unavailable.capability);
  assert.match(form,/项目参考工具待服务更新/);
  assert.match(project,/项目参考尚未加载/);
  assert.doesNotMatch(form,/<form|type="submit"/);
  assert.doesNotMatch(project,/还没有选择项目参考/);
});

test('reference capability fallback only handles 404 and retains other errors',async()=>{
  for(const status of [401,409,500,undefined]){
    const error=Object.assign(new Error('real failure'),{status});
    await assert.rejects(L.loadProjectReferences(async()=>{throw error;},'run-one'),caught=>caught===error);
  }
  const result=await L.loadProjectReferences(async()=>({snapshot:'exact',references:[{case_id:'one'}]}),'run-one');
  assert.equal(result.capability,true);
  assert.equal(result.snapshot,'exact');
  assert.equal(result.references[0].case_id,'one');
});

test('applying a reference requires an actual project and offers only qualified purposes',()=>{
  const item={id:'one',title:'Reference',partition:'calibration',eligible_for:['storyboard'],case_sha256:'abc'};
  assert.doesNotMatch(L.referenceForm(item,null,null),/<form/);
  assert.match(L.referenceForm(item,null,null),/新建或选择/);
  const html=L.referenceForm(item,'run-one',null);
  assert.match(html,/value="storyboard"/);
  assert.doesNotMatch(html,/value="message"/);
  assert.match(html,/保存免费/);
  assert.match(html,/需要更新/);
  assert.doesNotMatch(L.referenceForm({...item,partition:'holdout'},'run-one',null),/<form/);
});

test('project reference expression is escaped and source changes are visible',()=>{
  const expression='<script>evil()</script>';
  const form=L.referenceForm({id:'one',partition:'calibration',eligible_for:['message']},'run-one',null,{expression,purpose:'message'});
  assert.doesNotMatch(form,/<script>/);
  assert.match(form,/&lt;script&gt;/);
  const html=L.projectReferences([{case_id:'one',title:expression,expression,purpose:'message',status:'changed'}]);
  assert.doesNotMatch(html,/<script>/);
  assert.match(html,/参考内容已经变化/);
  assert.match(html,/不会自动换用新版/);
  assert.match(html,/移出本项目/);
});

test('untrusted case, method, fields and diff text cannot make markup or attributes',()=>{
  const bad='<img src=x onerror="alert(1)"><script>x</script>';
  const html=L.caseCard({id:bad,title:bad,partition:'holdout',tags:[bad],strategy:{situation:bad}})+L.methodCard({id:bad,decision:bad})+L.fields({quote:bad})+L.diffHtml(`+${bad}`);
  assert.doesNotMatch(html,/<img|<script|data-id="<|onerror="/);
  assert.match(html,/&lt;script&gt;/);
  assert.match(html,/仅供对照，不用于创作/);
});

test('reference links reject script, data and credential-bearing URLs',()=>{
  for(const url of ['javascript:alert(1)','data:text/html,hi','https://user:secret@example.com','file:///C:/secret','not a url']) assert.equal(L.safeURL(url),null);
  assert.equal(L.safeURL('https://www.tiktok.com/@test/video/123'),'https://www.tiktok.com/@test/video/123');
});

test('purpose and tag filtering preserves partition labels instead of promoting evidence',()=>{
  const cases=[{id:'cal',eligible_for:['message'],tags:['pace'],partition:'calibration',evidence:[{observation:'具体理由'}]},{id:'hold',eligible_for:[],tags:['pace'],partition:'holdout'}];
  assert.deepEqual(plain(L.filtered(cases,'具体理由','message','pace')).map(c=>c.id),['cal']);
  assert.deepEqual(plain(L.filtered(cases,'','','pace')).map(c=>c.id),['cal','hold']);
  assert.equal(L.filtered(cases,'','storyboard','').length,0);
});

test('feedback freezes the displayed artifact and preserves user quote verbatim',()=>{
  const artifact={stage:'writing',version:3,artifact_sha256:'abc'};
  const payload=L.feedbackPayload(artifact,'  这段太空\n只是开头  ','persona_dialogue');
  artifact.version=4;artifact.artifact_sha256='new';
  assert.deepEqual(plain(payload),{stage:'writing',version:3,artifact_sha256:'abc',quote:'  这段太空\n只是开头  ',scope:'persona_dialogue',action:'diagnose',observation:'  这段太空\n只是开头  ',hypothesis:null});
  assert.throws(()=>L.feedbackPayload(null,'原话','direction'));
  assert.throws(()=>L.feedbackPayload(artifact,' ','direction'));
  assert.throws(()=>L.feedbackPayload(artifact,'原话','made-up-scope'));
});

test('method directory and usage record separate reading from claimed implementation',()=>{
  const bad='<script>not evidence</script>';
  const card=L.contextCard({role:'writer',purpose:'message',snapshot:{
    skills:[],seed_case_ids:[],method_directory:{methods:[{id:'method-one',revision:2,retrieval_summary:bad}]},
  }});
  assert.match(card,/提供过，不代表选用/);
  assert.doesNotMatch(card,/<script>/);
  assert.match(card,/&lt;script&gt;/);
  const usage=L.methodUsageCard({method_id:'method-one',revision:2,decision:'chosen',
    reason:bad,artifact:{stage:'writing',version:3,passage:bad}});
  assert.match(usage,/制片使用声明/);
  assert.match(usage,/尚未独立核实/);
  assert.doesNotMatch(usage,/<script>/);
});

test('preview posts one candidate, never applies it or invokes an agent',async()=>{
  const calls=[];
  const actions=L.createActions(async(url,opts)=>{calls.push({url,...opts});return{id:'p',status:'pending'};},'run-one');
  const result=await actions.preview('source',{title:'参考',url:'https://www.tiktok.com/@a/video/1',tags:[]});
  assert.equal(calls.length,1);
  assert.equal(calls[0].url,'/api/runs/run-one/learning/proposals');
  assert.equal(result.status,'pending');
  assert.deepEqual(JSON.parse(calls[0].body),{type:'source',payload:{title:'参考',url:'https://www.tiktok.com/@a/video/1',tags:[]}});
});

test('apply uses reviewed base hash once and exposes conflict without refresh, retry or rebase',async()=>{
  const calls=[];
  const conflict=Object.assign(new Error('changed'),{status:409});
  const actions=L.createActions(async(url,opts)=>{calls.push({url,...opts});throw conflict;},'run-one');
  await assert.rejects(actions.apply({id:'proposal-a',status:'pending',base_library_sha256:'old-hash'}),error=>error===conflict);
  assert.equal(calls.length,1);
  assert.equal(calls[0].url,'/api/runs/run-one/learning/proposals/proposal-a/apply');
  assert.deepEqual(JSON.parse(calls[0].body),{expected_library_sha256:'old-hash'});
});

test('missing run or applied proposal cannot produce a write request',()=>{
  let calls=0;
  const request=()=>{calls++;};
  assert.throws(()=>L.createActions(request,null).preview('source',{}));
  assert.throws(()=>L.createActions(request,'run-one').apply({id:'p',status:'applied',base_library_sha256:'h'}));
  assert.throws(()=>L.createActions(request,'run-one').apply({id:'p',status:'pending'}));
  assert.equal(calls,0);
});

test('review shows exact source and change before its explicit save button',()=>{
  const html=L.proposalHtml({id:'p',type:'feedback',status:'pending',payload:{quote:'原话',stage:'writing',version:2,artifact_sha256:'hash'},base_library_sha256:'before',candidate_sha256:'after',diff:'-旧\n+新'});
  assert.match(html,/原话/);assert.match(html,/writing/);assert.match(html,/before/);assert.match(html,/after/);
  assert.ok(html.indexOf('候选差异')<html.indexOf('data-learning-action="apply"'));
  assert.doesNotMatch(L.proposalHtml({status:'applied'}),/data-learning-action="apply"/);
});

test('unknown market outcome remains unverified; internal filesystem path is not an HTTP link',()=>{
  const html=L.fields({market_effect:null,media:{path:'C:/secret'},evidence:[{source:'J:/evidence/observation.md',observation:'仅看8秒'}]});
  assert.match(html,/未记录 \/ 未验证/);assert.match(html,/仅看8秒/);
  assert.doesNotMatch(html,/C:\/secret|href=/);
});

test('proposal review uses human status and scope; full artifact audit follows the diff',()=>{
  const html=L.proposalHtml({type:'feedback',status:'pending',payload:{quote:'原话',scope:'persona_dialogue',action:'diagnose',stage:'writing',version:3,hypothesis:'待核实',artifact:{content:'LONG_ARTIFACT_BODY'}},diff:'+CHANGE'});
  assert.match(html,/待审阅/);assert.doesNotMatch(html,/待研究/);
  assert.match(html,/人物与说话方式/);assert.match(html,/先诊断/);assert.match(html,/脚本与文案 · v3/);
  assert.ok(html.indexOf('+CHANGE')<html.indexOf('LONG_ARTIFACT_BODY'));
  assert.match(html,/<details[^>]*>.*LONG_ARTIFACT_BODY/s);
  assert.match(L.caseCard({id:'pending-case',partition:'pending'}),/待补充观察/);
});

test('only registered document links become native reading actions; unknown relative paths remain text',()=>{
  const text='[研究](../../../tracks/us/creative/learning/commerce-methods-research.md#method) [执行](execution.md) [未知](../../outside/secret.md) [目录](README.md)';
  const html=L.documentHtml(text,['commerce-methods-research','execution'],L.esc);
  assert.match(html,/data-id="commerce-methods-research"/);assert.match(html,/data-id="execution"/);
  assert.match(html,/未知（secret.md）/);assert.match(html,/目录（README.md）/);
  assert.doesNotMatch(html,/href=|outside|\.\.\//);
  const unavailable=L.documentHtml('[研究](learning/commerce-methods-research.md)',[],L.esc);
  assert.doesNotMatch(unavailable,/data-learning-action/);
});

test('document link labels and unsupported URL schemes remain escaped',()=>{
  const html=L.documentHtml('[<img onerror="x">](learning/execution.md) [bad](javascript:alert)', ['execution'],L.esc);
  assert.doesNotMatch(html,/<img|href="javascript:/);assert.match(html,/&lt;img/);
});

test('reference cards use registered local media, remain silent, and invent no preview',()=>{
  const html=L.caseCard({id:'real-reference',title:'装包参考',partition:'calibration',media:{path:'J:/registered/reference.mp4'},evidence:[{observation:'观察到前袋放入小物。'}]});
  assert.match(html,/<video[^>]*muted[^>]*preload="metadata"[^>]*src="\/api\/learning\/cases\/real-reference\/media#t=0\.1"/);
  assert.doesNotMatch(html,/autoplay|https?:\/\//);
  assert.match(html,/观察到前袋放入小物/);
  assert.ok(html.indexOf('<video')<html.indexOf('<h4>装包参考'));
  assert.doesNotMatch(L.caseCard({id:'unobserved',title:'待研究参考'}),/<img|<video/);
  assert.match(L.caseCard({id:'unobserved',title:'待研究参考'}),/暂无本地预览/);
});

test('skill cards lead with Chinese method names and keep fingerprints in collapsed audit',()=>{
  for(const [id,title] of [['us-commerce-strategy','购买策略'],['us-creator-video','视频创作'],['us-photo-copy','图文创作']]){
    const html=L.skillCard({id,version:'VERSION_TOKEN',sha256:'HASH_TOKEN'});
    const main=html.split('<details')[0].replace(/<[^>]*>/g,'');
    assert.match(main,new RegExp(title));assert.doesNotMatch(main,/us-|HASH_TOKEN|VERSION_TOKEN/);
    assert.match(html,/HASH_TOKEN/);assert.doesNotMatch(html,/<details[^>]*\sopen[\s>]/);
  }
});

test('run context leads with actual role methods and never makes missing context look observed',()=>{
  const html=L.contextCard({role:'writer',snapshot_sha256:'HASH_TOKEN',snapshot:{skills:[{id:'us-creator-video',sha256:'SKILL_HASH'}],seed_case_ids:['reference-one'],library:{methods:[{decision:'让动作兑现说法'}]},text:'EXACT_CONTEXT_BODY'}});
  const main=html.split('<details')[0].replace(/<[^>]*>/g,'');
  assert.match(main,/编剧的参考/);assert.match(main,/视频创作/);assert.match(main,/让动作兑现说法/);
  assert.doesNotMatch(main,/HASH_TOKEN|EXACT_CONTEXT_BODY|reference-one/);
  assert.match(html,/EXACT_CONTEXT_BODY/);assert.match(html,/HASH_TOKEN/);
  assert.match(L.contextCard({role:'writer'}),/本次没有提供创作方法文本/);
  assert.match(L.contextCard({role:'writer'}),/未检索参考案例/);
});

test('proposal starts with a readable effect and keeps exact diff available without default expansion',()=>{
  const html=L.proposalHtml({type:'feedback',status:'pending',payload:{quote:'只调开头',stage:'writing',version:2,scope:'pace',action:'diagnose'},diff:'-before\n+after'});
  const main=html.split('<details')[0];
  assert.match(main,/所选稿件的反馈记录/);assert.match(main,/只调开头/);assert.doesNotMatch(main,/-before|\+after/);
  assert.match(html,/<details[^>]*learning-diff-review[^>]*><summary>查看完整候选差异<\/summary>/);
  assert.match(html,/-before/);assert.match(html,/\+after/);
  assert.doesNotMatch(html,/<details[^>]*\sopen[\s>]/);
});

function openHarness(fetch) {
  const nodes=new Map();
  const make=()=>({open:false,innerHTML:'',textContent:'',value:'',dataset:{},
    setAttribute(){},addEventListener(){},querySelectorAll(){return[];},
    querySelector(selector){if(!nodes.has(selector))nodes.set(selector,make());return nodes.get(selector);},
    showModal(){this.open=true;},remove(){},focus(){} });
  const opener=make();
  const c=vm.createContext({URL,fetch,state:{runId:null},document:{getElementById:()=>opener,createElement:make,body:{append(){}}}});
  vm.runInContext(source,c);
  return {learning:c.StudioLearning,nodes};
}

test('homepage can open an exact reference through the public library entry',async()=>{
  const calls=[];
  const h=openHarness(async url=>{calls.push(url);return{ok:true,json:async()=>({cases:[{id:'real-reference',title:'真实参考标题',evidence:[{observation:'只观察到这段动作'}],partition:'calibration'}]})};});
  await h.learning.open({tab:'cases',caseId:'real-reference'});
  assert.deepEqual(calls,['/api/learning']);
  assert.match(h.nodes.get('.learning-main').innerHTML,/真实参考标题/);
  assert.match(h.nodes.get('.learning-main').innerHTML,/只观察到这段动作/);
});

test('public library open catches connection failures and displays readable recovery text',async()=>{
  const h=openHarness(async()=>{throw new TypeError('fetch failed');});
  await assert.doesNotReject(h.learning.open({tab:'cases'}));
  assert.match(h.nodes.get('.learning-status').textContent,/暂时无法连接资料库/);
});
