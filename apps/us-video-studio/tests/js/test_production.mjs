import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url),ui=require('../../studio/web/production.js');
const hash='a'.repeat(64),data={capabilities:{image:{available:false,reason:'尚未验证'},video:{available:true,models:[{id:'wan3.0-video',resolutions:['720P'],durations:{min:2,max:30}}]}},sources:[{version:2,artifact_sha256:hash,ready:true}],materials:[{name:'a.png',sha256:hash,kind:'image',approved:true,url:'/api/runs/demo/materials/a.png'}]};
const values={kind:'video',source_version:'2',source_sha256:hash,reference_name:'a.png',reference_sha256:hash,prompt:'Precise input',model:'wan3.0-video',duration:'5',resolution:'720P'};
const draft={draft_id:'one',draft_sha256:hash,...ui.submission(values,data,'save'),source_ready:true,reference_approved:true,review:{verdict:'ready',findings:[]},attempts:[]};
test('draft binds exact source and reference bytes without implicit latest',()=>{const body=ui.submission(values,data,'save');assert.equal(body.source_sha256,hash);assert.equal(body.source_version,2);assert.equal(body.reference.sha256,hash);assert.throws(()=>ui.submission({...values,source_sha256:'changed'},data,'save'));assert.throws(()=>ui.submission({...values,reference_sha256:'changed'},data,'save'));assert.throws(()=>ui.submission({...values,source_version:''},data,'save'));});
test('video parameters and reference are explicit and validated',()=>{for(const changes of [{reference_name:''},{duration:'1'},{duration:'5.5'},{resolution:'fake'},{model:'fake'}])assert.throws(()=>ui.submission({...values,...changes},data,'save'));const image=ui.submission({...values,kind:'image',reference_name:''},data,'image');assert.equal(image.duration,null);assert.equal(image.resolution,null);assert.equal(image.reference,null);assert.equal(image.model,'codex-image');});
test('unknown pricing never becomes zero or free',()=>{assert.equal(ui.price(null),'费用未知');assert.equal(ui.price({amount:null,currency:'CNY'}),'费用未知');assert.equal(ui.price({amount:NaN}),'费用未知');assert.match(ui.price({amount:3,currency:'CNY'}),/CNY 3.00/);assert.match(ui.price({amount:0,currency:'CNY'}),/0.00/);});
test('generation requires verified capability review and exact reference approval',()=>{assert.equal(ui.gate(draft,data),'');for(const changes of [{kind:'image'},{review:null},{review:{verdict:'revise'}},{source_ready:false},{reference_approved:false},{reference:null},{can_generate:false},{attempts:[{status:'submitted'}]},{attempts:[{status:'unknown'}]}])assert.notEqual(ui.gate({...draft,...changes},data),'');});
test('network retry retains identical consent request and nonce',()=>{const p=ui.actionRequest('generate',draft,'old-nonce','same-request');assert.deepEqual(ui.actionRequest('generate',draft,'new-nonce','new-request',JSON.parse(JSON.stringify(p))),p);assert.equal(p.body.expected_sha256,hash);assert.equal(p.body.consent,true);assert.throws(()=>ui.actionRequest('review',draft,'n','r',p));assert.throws(()=>ui.actionRequest('generate',{...draft,draft_id:'other'},'n','r',p));});
test('media only renders same project material endpoints',()=>{assert.equal(ui.mediaUrl('/api/runs/demo/materials/a%20b.png','demo'),'/api/runs/demo/materials/a%20b.png');for(const url of ['https://evil.test/a.png','//evil.test/a.png','javascript:alert(1)','/api/runs/other/materials/a.png','/api/runs/demo/materials/../x','/api/runs/demo/materials/%2e%2e','/api/runs/demo/materials/a%2fb.png','/api/runs/demo/materials/a.png?x=1'])assert.equal(ui.mediaUrl(url,'demo'),'');});
test('records escape prompts findings model status messages and block external outputs',()=>{const html=ui.draftHtml({...draft,prompt:'<script>x</script>',review:{verdict:'ready',findings:['<img src=x>']},attempts:[{attempt_id:'x',status:'failed',message:'<iframe>',output:{url:'https://evil.test/a',name:'bad'}}]},data,'demo');assert.doesNotMatch(html,/<script>|<iframe>|<img src=x>|https:\/\/evil/);assert.match(html,/&lt;script/);assert.match(html,/&lt;img/);});
test('review and generation affordances distinguish pending and recovery',()=>{const html=ui.draftHtml({...draft,attempts:[{attempt_id:'pending',status:'unknown'}]},data,'demo');assert.match(html,/data-generate="one" disabled/);assert.match(html,/核实提供方提交记录/);assert.match(html,/不会重新生成/);assert.doesNotMatch(html,/自动重试/);});
test('network ambiguity including server failures retains request identity',()=>{for(const error of [new Error('Failed to fetch'),{status:500},{status:502},{status:429},{}])assert.equal(ui.uncertain(error),true);for(const status of [400,403,404,409,422])assert.equal(ui.uncertain({status}),false);const request=ui.actionRequest('generate',draft,'nonce','known-id');const persisted=JSON.parse(JSON.stringify({pending:request}));const recovered=ui.actionRequest('generate',{...draft,prompt:'Changed form',draft_sha256:'b'.repeat(64)},'new-nonce','new-id',persisted.pending);assert.equal(recovered.body.request_id,'known-id');assert.equal(recovered.body.expected_sha256,hash);});
test('running jobs block actions but unknown accounting never does',()=>{
 const active={...data,active:true};
 assert.notEqual(ui.reviewGate(draft,active),''); assert.notEqual(ui.gate(draft,active),'');
 const unknown={...data,active:false,unresolved_calls:1};
 assert.equal(ui.reviewGate(draft,unknown),''); assert.equal(ui.gate(draft,unknown),'');
});
test('every running provider phase prevents duplicate generation',()=>{for(const status of ['preparing','submitting','submitted','downloading'])assert.notEqual(ui.gate({...draft,attempts:[{status}]},data),'');assert.equal(ui.gate({...draft,attempts:[{status:'succeeded',usage_status:'unknown'}]},data),'');});
test('saved image stays accessible without manual accounting controls',()=>{const html=ui.draftHtml({...draft,kind:'image',attempts:[{attempt_id:'image',status:'succeeded',usage_status:'unknown',output:{name:'a.png',sha256:hash,url:'/api/runs/demo/materials/a.png'}}]},data,'demo');assert.doesNotMatch(html,/data-recover-usage|后续调用会暂停|核实图片用量/);assert.match(html,/src="\/api\/runs\/demo\/materials\/a.png"/);assert.doesNotMatch(html,/quantity="1"/);});
test('completed media precedes prompt while evidence and attempts remain available',()=>{
 const confirmed={...data,materials:[...data.materials,{name:'movie.mp4',sha256:hash,kind:'video'}]};
 const html=ui.draftHtml({...draft,attempts:[{attempt_id:'done',status:'succeeded',output:{name:'movie.mp4',sha256:hash,url:'/api/runs/demo/materials/movie.mp4'}}]},confirmed,'demo');
 assert.ok(html.indexOf('class="creator-result"')<html.indexOf('class="production-prompt"'));
 assert.match(html,/<video controls preload="metadata"/);
 assert.match(html,/制作描述、检查依据与费用说明/);
 assert.match(html,/制作与取回记录 · 1/);
 assert.match(html,/data-feedback-attempt="done"/);
});
test('missing result bytes never become a playable or downloadable success',()=>{
 const html=ui.draftHtml({...draft,attempts:[{attempt_id:'gone',status:'succeeded',output:{name:'gone.mp4',sha256:hash,url:'/api/runs/demo/materials/gone.mp4'}}]},data,'demo');
 assert.match(html,/媒体文件暂时无法核对/);
 assert.doesNotMatch(html,/<video controls|下载视频|data-feedback-attempt="gone"/);
});
test('caption is shown only from an intact current writing artifact',()=>{
 const writing={stage:'writing',has_doc:true,version:3,integrity_ok:true,blocks:[{type:'text',title:'发布文案',text:'<shop> lunch bag'}]};
 assert.match(ui.captionHtml([writing]),/&lt;shop&gt; lunch bag/);
 assert.match(ui.captionHtml([writing]),/data-copy-caption/);
 assert.doesNotMatch(ui.captionHtml([{...writing,stale:true}]),/data-copy-caption/);
 assert.equal(ui.captionHtml([]),'');
});
test('usage recovery accepts confirmed fractional video seconds but image integers only',()=>{assert.equal(ui.usageQuantity('5.75','video'),5.75);assert.equal(ui.usageQuantity('0','video'),0);assert.equal(ui.usageQuantity('2','image'),2);assert.throws(()=>ui.usageQuantity('2.5','image'));for(const value of ['',null,' ','NaN','Infinity','-1','10001']){assert.throws(()=>ui.usageQuantity(value,'video'));assert.throws(()=>ui.usageQuantity(value,'image'));}const html=ui.usageFormHtml('video');assert.match(html,/计费时长（秒）/);assert.match(html,/step="any"/);assert.doesNotMatch(html,/value="/);assert.match(html,/不按方案时长或下载文件推算/);assert.match(ui.usageFormHtml('image'),/step="1"/);});
test('unknown video usage is recoverable only after local and provider work stop',()=>{for(const status of ['failed','succeeded','retrieve_failed']){const attempt={attempt_id:'video',status,usage_status:'unknown'};assert.equal(ui.canRecoverUsage(attempt,{active:false}),true);assert.equal(ui.canRecoverUsage(attempt,{active:true}),false);const html=ui.draftHtml({...draft,attempts:[attempt]},data,'demo');assert.doesNotMatch(html,/data-recover-usage/);}for(const status of ['submitted','downloading','preparing','submitting'])assert.equal(ui.canRecoverUsage({status,usage_status:'unknown'},{active:false}),false);assert.equal(ui.canRecoverUsage({status:'succeeded',usage_status:'known'},{active:false}),false);const active=ui.draftHtml({...draft,attempts:[{attempt_id:'video',status:'submitted',usage_status:'unknown'}]},{...data,active:true},'demo');assert.doesNotMatch(active,/data-recover-usage/);});
test('review scope remains visible and all five textual checks have escaped details',()=>{const review={verdict:'ready',scope:'仅文字检查，未看像素 <script>x</script>',findings:['Written evidence'],checks:{opening:'Opening <img src=x>',progression:'Progression',payoff:'Payoff',product:'Product',execution:'Execution'}};const html=ui.reviewHtml(review);assert.match(html,/文字制作检查通过/);assert.match(html,/仅文字检查，未看像素/);assert.match(html,/<details><summary>查看五项检查依据/);for(const label of ['开场','推进','回报','商品作用','执行与限制'])assert.match(html,new RegExp(label));assert.doesNotMatch(html,/<script>|<img src=x>/);assert.match(html,/&lt;script/);assert.match(ui.reviewHtml({verdict:'ready'}),/未查看参考图像素；不代表图审或成片验收/);});
test('current work picks unresolved generation before a newer draft or older success',()=>{
 const movie={name:'movie.mp4',sha256:hash,kind:'video',url:'/api/runs/demo/materials/movie.mp4'};
 const done={...draft,draft_id:'old',attempts:[{attempt_id:'old-done',status:'succeeded',output:movie}]};
 const pending={...draft,draft_id:'pending',attempts:[{attempt_id:'pending-job',status:'unknown',task_id:'provider-task',message:'Provider response unclear'}]};
 const prepared={...draft,draft_id:'new',attempts:[]};
 const work=ui.currentProductionWork({...data,materials:[...data.materials,movie],drafts:[done,pending,prepared]},'video','demo');
 assert.equal(work.phase,'unknown');assert.equal(work.draft.draft_id,'pending');assert.equal(work.primaryAction,'refresh');
 const html=ui.draftHtml(pending,{...data,materials:[...data.materials,movie]},'demo');
 assert.match(html,/<div class="production-actions production-primary-actions"><button class="primary" data-refresh="pending-job">查询这次结果/);
 assert.doesNotMatch(html,/<video controls|下载视频/);
 assert.match(html,/data-generate="pending" disabled/);
});
test('completed video makes playback and download the main work, while a new generation stays secondary',()=>{
 const movie={name:'movie.mp4',sha256:hash,kind:'video',url:'/api/runs/demo/materials/movie.mp4'};
 const shown={...data,materials:[...data.materials,movie]};
 const finished={...draft,attempts:[{attempt_id:'done',status:'succeeded',output:movie}]};
 const work=ui.currentProductionWork({...shown,drafts:[finished]},'video','demo');
 assert.equal(work.phase,'completed');assert.equal(work.primaryAction,'download');
 const html=ui.draftHtml(finished,shown,'demo');
 assert.ok(html.indexOf('<video controls')<html.indexOf('production-primary-actions'));
 assert.match(html,/<div class="production-actions production-primary-actions"><a class="primary production-download"[^>]+>下载视频<\/a>/);
 assert.ok(html.indexOf('修改或再次制作')>html.indexOf('下载视频'));
});
test('returned image asks for exact image approval before treating it as complete',()=>{
 const picture={name:'returned.png',sha256:hash,kind:'image',url:'/api/runs/demo/materials/returned.png',approved:false};
 const shown={...data,materials:[...data.materials,picture]};
 const image={...draft,kind:'image',draft_id:'image-draft',attempts:[{attempt_id:'image-job',status:'succeeded',output:picture}]};
 let work=ui.currentProductionWork({...shown,drafts:[image]},'image','demo');
 assert.equal(work.phase,'image_pending');assert.equal(work.primaryAction,'approve_image');
 assert.match(ui.draftHtml(image,shown,'demo'),/class="production-actions production-primary-actions"><button class="primary" data-image="returned.png">查看并确认这张图/);
 work=ui.currentProductionWork({...shown,materials:[...data.materials,{...picture,approved:true}],drafts:[image]},'image','demo');
 assert.equal(work.phase,'completed');assert.equal(work.primaryAction,'download');
});
test('ready video shows the exact reference and estimate before generation',()=>{
 const prepared={...draft,estimate:{amount:3,currency:'CNY',notice:'Fixture estimate'},attempts:[]};
 const work=ui.currentProductionWork({...data,drafts:[prepared]},'video','demo');
 assert.equal(work.phase,'ready');assert.equal(work.primaryAction,'generate');
 const html=ui.draftHtml(prepared,data,'demo');
 assert.match(html,/src="\/api\/runs\/demo\/materials\/a.png"/);
 assert.match(html,/CNY 3.00/);
 assert.match(html,/<div class="production-actions production-primary-actions"><button class="primary" data-generate="one">核对并生成视频/);
 assert.ok(html.indexOf('核对并生成视频')<html.indexOf('制作描述、检查依据与费用说明'));
});
test('empty media view names the actual missing step instead of a fixed prepare prompt',()=>{
 const noScript=ui.currentProductionWork({...data,sources:[],drafts:[]},'video','demo');
 assert.equal(noScript.phase,'needs_plan');assert.match(ui.emptyWorkHtml(noScript),/查看当前方案/);
 const imageNeeded=ui.currentProductionWork({...data,materials:[],drafts:[]},'video','demo');
 assert.equal(imageNeeded.phase,'image_pending');assert.match(ui.emptyWorkHtml(imageNeeded),/查看并确认画面/);
 const prepare=ui.currentProductionWork({...data,drafts:[]},'video','demo');
 assert.equal(prepare.phase,'prepare');assert.match(ui.emptyWorkHtml(prepare),/准备视频输入/);
});
test('generation confirmation keeps exact scope, reference, cost and limits visible while full prompt is optional',()=>{
 const checked={...draft,prompt:'<raw prompt>',estimate:{amount:3,currency:'CNY',notice:'含声音；实际账单以提供方为准'},
  review:{verdict:'ready',findings:['请核对商品上的 <word>'],checks:{}}};
 const html=ui.confirmationHtml(checked,data,'demo',true);
 assert.match(html,/脚本第 2 版 · 1 条视频 · 5 秒 · 720P/);
 assert.match(html,/src="\/api\/runs\/demo\/materials\/a.png"/);
 assert.match(html,/CNY 3.00/);assert.match(html,/含声音；实际账单以提供方为准/);
 assert.match(html,/请核对商品上的 &lt;word&gt;/);
 assert.match(html,/<details class="production-confirm-prompt"><summary>查看实际发送的完整画面描述/);
 assert.match(html,/&lt;raw prompt&gt;/);assert.doesNotMatch(html,/<raw prompt>|<word>/);
 assert.match(html,/data-consent/);assert.match(html,/data-authorize disabled/);
});
