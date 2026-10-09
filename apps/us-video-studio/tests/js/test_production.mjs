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
test('generation requires capability and exact reference approval without reviewer verdict',()=>{assert.equal(ui.gate(draft,data),'');assert.equal(ui.gate({...draft,review:null},data),'');assert.equal(ui.gate({...draft,review:{verdict:'revise'}},data),'');for(const changes of [{kind:'image'},{source_ready:false},{reference_approved:false},{reference:null},{can_generate:false},{attempts:[{status:'submitted'}]},{attempts:[{status:'unknown'}]}])assert.notEqual(ui.gate({...draft,...changes},data),'');});
test('unavailable generation capability leads to settings, while another active job offers no repair action',()=>{
 const unavailable={...data,capabilities:{...data.capabilities,video:{available:false,reason:'视频连接尚未配置'}},drafts:[draft]};
 const blocked=ui.currentProductionWork(unavailable,'video','demo');
 assert.equal(blocked.phase,'blocked');assert.equal(blocked.primaryAction,'settings');
 assert.match(ui.draftHtml(draft,unavailable,'demo'),/data-model-settings>打开模型设置/);
 const active=ui.currentProductionWork({...data,active:true,drafts:[draft]},'video','demo');
 assert.equal(active.phase,'blocked');assert.equal(active.primaryAction,'none');
});
test('network retry retains identical consent request and nonce',()=>{const p=ui.actionRequest('generate',draft,'old-nonce','same-request');assert.deepEqual(ui.actionRequest('generate',draft,'new-nonce','new-request',JSON.parse(JSON.stringify(p))),p);assert.equal(p.body.expected_sha256,hash);assert.equal(p.body.consent,true);assert.throws(()=>ui.actionRequest('review',draft,'n','r',p));assert.throws(()=>ui.actionRequest('generate',{...draft,draft_id:'other'},'n','r',p));});
test('image confirmation names the selected controller model and pins its settings revision',()=>{
 const image={...draft,kind:'image',model:'codex-image',estimate:{amount:null,currency:null},prompt:'Exact frame'};
 const shown={...data,capabilities:{...data.capabilities,image:{available:true,controller_model:'gpt-6-sol',connection_revision:3}}};
 assert.match(ui.confirmationHtml(image,shown,'demo'),/Codex Image \/ gpt-6-sol/);
 const request=ui.actionRequest('generate',image,'nonce','one',null,3);
 assert.equal(request.body.expected_connection_revision,3);
 const intent={kind:'generate',connection_revision:3,drafts:[{draft_id:image.draft_id,expected_sha256:hash}]};
 assert.match(ui.batchConfirmationHtml(intent,{...shown,drafts:[image]},'demo'),/Codex Image \/ gpt-6-sol/);
 assert.equal(ui.batchConfirmationHtml(intent,{...shown,capabilities:{...shown.capabilities,image:{...shown.capabilities.image,connection_revision:4}},drafts:[image]},'demo'),'');
});
test('media only renders same project material endpoints',()=>{assert.equal(ui.mediaUrl('/api/runs/demo/materials/a%20b.png','demo'),'/api/runs/demo/materials/a%20b.png');for(const url of ['https://evil.test/a.png','//evil.test/a.png','javascript:alert(1)','/api/runs/other/materials/a.png','/api/runs/demo/materials/../x','/api/runs/demo/materials/%2e%2e','/api/runs/demo/materials/a%2fb.png','/api/runs/demo/materials/a.png?x=1'])assert.equal(ui.mediaUrl(url,'demo'),'');});
test('canvas shows the current media without approval, generation or version controls',()=>{
 const movie={name:'movie.mp4',sha256:hash,kind:'video',url:'/api/runs/demo/materials/movie.mp4'};
 const complete={...draft,attempts:[{attempt_id:'done',status:'succeeded',output:movie}]};
 const html=ui.canvasHtml({...data,materials:[...data.materials,movie],drafts:[complete]},'video','demo',[]);
 assert.match(html,/<video controls/);
 assert.doesNotMatch(html,/<button|data-generate|data-review|production-history|脚本第 \d+ 版/);
});
test('storyboard shows every saved image input and labels uploaded photos as references',()=>{
 const imageOne={...draft,kind:'image',draft_id:'morning',prompt:'Morning <street> shot',attempts:[]};
 const imageTwo={...draft,kind:'image',draft_id:'evening',prompt:'Evening street shot',attempts:[]};
 const shown={...data,capabilities:{...data.capabilities,image:{available:true}},drafts:[imageOne,imageTwo]};
 const html=ui.canvasHtml(shown,'image','demo',[]);
 assert.match(html,/分镜首帧输入（2）/);
 assert.match(html,/镜头 01/);assert.match(html,/镜头 02/);
 assert.match(html,/Morning &lt;street&gt; shot/);assert.match(html,/Evening street shot/);
 assert.equal((html.match(/<details class="production-shot"/g)||[]).length,2);
 assert.doesNotMatch(html,/<details class="production-shot"[^>]+open/);
 assert.match(html,/上传商品原图，仅供生成参考/);
 assert.doesNotMatch(html,/production-canvas-media|<street>/);
 assert.match(html,/data-feedback-image-draft="morning"/);
 assert.match(html,/data-feedback-image-draft="evening"/);
 assert.doesNotMatch(html,/data-review-image-draft/);
});

test('photo canvas uses page wording while retaining server target count and stable identities',()=>{
 const image={...draft,kind:'image',draft_id:'photo-page-2',shot_id:'source-page-2',prompt:'Current page image',attempts:[]};
 const shown={...data,capabilities:{...data.capabilities,image:{available:true}},drafts:[image],
  shots:{image:{total:4,complete_list:true,shots:[
   {ordinal:2,shot_id:'source-page-2',draft_id:'photo-page-2',source_ready:true,stage:'input_ready',stage_label:'输入已准备'},
   {ordinal:4,shot_id:'source-page-4',source_ready:false,stage:'unprepared',stage_label:'输入尚未准备',blocker:'unprepared'}]}}};
 const html=ui.canvasHtml(shown,'image','demo',[],'photo');
 assert.match(html,/图文图片（4）/);
 assert.match(html,/目标 4 页，已准备输入 1 页，已生成 0 张/);
 assert.match(html,/第 02 页/);assert.match(html,/第 04 页/);
 assert.match(html,/data-shot-id="source-page-2"/);assert.match(html,/data-shot-id="source-page-4"/);
 assert.match(html,/这一页还没有制作输入/);
 assert.doesNotMatch(html,/分镜首帧|面镜头|这一镜|镜头 0/);
});

test('shared source readiness counts usable inputs while historical results and unresolved tasks stay readable',()=>{
 const saved={name:'historical.png',sha256:hash,kind:'image',approved:false,url:'/api/runs/demo/materials/historical.png'};
 const complete={...draft,kind:'image',draft_id:'old-shot-01',shot_id:'shot-01',source_ready:false,
  design_issue:'原输入结果可查看；补设计后才能继续制作。',is_current:true,
  attempts:[{attempt_id:'completed-01',status:'succeeded',output:saved}]};
 const unresolved={...complete,draft_id:'old-shot-08',shot_id:'shot-08',
  attempts:[{attempt_id:'original-08',status:'unknown'}]};
 const shots=Array.from({length:10},(_,index)=>({ordinal:index+1,shot_id:`shot-${String(index+1).padStart(2,'0')}`,
  source_ready:false,stage:'unprepared',stage_label:'输入需要补设计'}));
 Object.assign(shots[0],{draft_id:complete.draft_id,reason:complete.design_issue});
 Object.assign(shots[7],{draft_id:unresolved.draft_id,stage:'unverified',stage_label:'结果待核实',
  blocker:'attempt_unresolved',attempt_id:'original-08',attempt_status:'unknown',
  reason:'原任务提交结果尚未确认。'});
 const shown={...data,capabilities:{image:{available:true}},materials:[saved],drafts:[complete,unresolved],
  shots:{image:{total:10,complete_list:true,shots}}};
 const html=ui.canvasHtml(shown,'image','demo',[]);
 assert.match(html,/目标 10 面镜头，已准备输入 0 面，已生成 1 张/);
 assert.match(html,/<img src="\/api\/runs\/demo\/materials\/historical.png"/);
 assert.match(html,/结果待核实/);assert.match(html,/原任务提交结果尚未确认/);
 assert.match(html,/先查询原任务，不要重新提交/);
 assert.doesNotMatch(html,/attempt_unresolved|data-generate|data-approve/);
 assert.equal(saved.approved,false);
 // A query state does not invalidate a separately verified current input.
 shots[7].source_ready=true;
 assert.match(ui.canvasHtml(shown,'image','demo',[]),/已准备输入 1 面/);
});
test('one batch review pins every eligible shot and shows exact images before approval',()=>{
 const image={...draft,kind:'image',draft_id:'first',prompt:'First <frame>',estimate:{amount:2,currency:'CNY',notice:'Fixture'},attempts:[]};
 const second={...image,draft_id:'second',prompt:'Second frame'};
 const shown={...data,capabilities:{...data.capabilities,image:{available:true}},drafts:[image,second]};
 assert.deepEqual(ui.batchGenerationDrafts(shown,'demo').map(row=>row.draft_id),['first','second']);
 const confirm=ui.batchConfirmationHtml({kind:'generate',drafts:[
  {draft_id:'first',expected_sha256:hash},{draft_id:'second',expected_sha256:hash}]},shown,'demo');
 assert.match(confirm,/一次生成所选 2 张首帧/);assert.match(confirm,/估算合计 CNY 4.00/);
 // Scenario E: the one-time confirmation names what it is bound to, so a later
 // edit to any shot invalidates it visibly rather than silently.
 assert.match(confirm,/来源：脚本第 2 版/);
 assert.match(confirm,/输入：本次所选 2 面准确分镜/);
 assert.match(confirm,/模型：Codex Image/);
 assert.match(confirm,/费用：估算 CNY 4\.00/);
 const unknownCost=ui.batchConfirmationHtml({kind:'generate',drafts:[
  {draft_id:'first',expected_sha256:hash},{draft_id:'second',expected_sha256:hash}]},
  {...shown,drafts:[{...image,estimate:{amount:null,currency:null,notice:'费用未知'}},second]},'demo');
 assert.match(unknownCost,/费用：未知/);
 assert.doesNotMatch(unknownCost,/CNY 0\.00/);
 assert.match(confirm,/First &lt;frame&gt;/);assert.match(confirm,/确认并生成 2 张/);
 assert.equal(ui.batchConfirmationHtml({kind:'generate',drafts:[{draft_id:'first',expected_sha256:'0'.repeat(64)}]},shown,'demo'),'');
 const output={name:'generated.png',sha256:hash,url:'/api/runs/demo/materials/generated.png',approved:false};
 const complete={...image,attempts:[{attempt_id:'attempt',status:'succeeded',output}]};
 const produced={...shown,drafts:[complete,second],materials:[...data.materials,output]};
 assert.deepEqual(ui.batchGenerationDrafts(produced,'demo').map(row=>row.draft_id),['second']);
 assert.deepEqual(ui.batchApprovalImages(produced,'demo').map(row=>row.output.name),['generated.png']);
 const approve=ui.batchConfirmationHtml({kind:'approve',images:[{name:'generated.png',sha256:hash}]},produced,'demo');
 assert.match(approve,/src="\/api\/runs\/demo\/materials\/generated.png"/);
 assert.match(approve,/确认这 1 张图片/);
});
test('five video shots keep their approved references, costs, stop state and assembly order',()=>{
 const references=Array.from({length:5},(_,index)=>({name:`frame-${index+1}.png`,sha256:hash,
  kind:'image',approved:true,url:`/api/runs/demo/materials/frame-${index+1}.png`}));
 const clips=Array.from({length:5},(_,index)=>({...draft,draft_id:`clip-${index+1}`,
  reference:{name:references[index].name,sha256:hash},prompt:`Scene ${index+1} <bag>`,
  duration:3,estimate:{amount:1.8,currency:'CNY',notice:'原价估算'},attempts:[]}));
 const sequence=clips.map((clip,index)=>({index:index+1,image_draft_id:`frame-${index+1}`,
  reference:{name:references[index].name,sha256:hash},approved:true,
  video_draft_id:clip.draft_id,video_draft_sha256:clip.draft_sha256}));
 const shown={...data,materials:[...data.materials,...references],drafts:clips,video_sequence:sequence};
 assert.deepEqual(ui.videoBatchGenerationDrafts(shown,'demo').map(item=>item.draft_id),clips.map(item=>item.draft_id));
 const confirm=ui.batchConfirmationHtml({kind:'video_generate',drafts:clips.map(item=>({draft_id:item.draft_id,expected_sha256:item.draft_sha256}))},shown,'demo');
 assert.match(confirm,/一次生成整组视频段/);assert.match(confirm,/CNY 9.00/);
 assert.match(confirm,/Scene 1 &lt;bag&gt;/);assert.match(confirm,/确认并生成 5 段/);
 const unknown={...clips[0],attempts:[{attempt_id:'uncertain',status:'unknown'}]};
 assert.deepEqual(ui.videoBatchGenerationDrafts({...shown,drafts:[unknown,...clips.slice(1)]},'demo'),[]);
 const outputs=clips.map((clip,index)=>({name:`clip-${index+1}.mp4`,sha256:hash,kind:'video',
  url:`/api/runs/demo/materials/clip-${index+1}.mp4`}));
 const completed=clips.map((clip,index)=>({...clip,attempts:[{attempt_id:`attempt-${index+1}`,
  status:'succeeded',output:outputs[index]}]}));
 const edit_plan={request_id:'script-plan',plan_sha256:hash,duration_seconds:15,
  cuts:clips.map((_,index)=>({clip_index:index+1,audio_clip_index:index+1,start_seconds:0,
   audio_start_seconds:0,duration_seconds:3})),overlays:[{text:'one bag.',start_seconds:0,duration_seconds:2}]};
 const ready={...shown,drafts:completed,materials:[...shown.materials,...outputs],edit_plan};
 const assemblyClips=ui.assemblyClips(ready,'demo');
 assert.equal(assemblyClips.length,5);assert.deepEqual(assemblyClips.map(item=>item.name),outputs.map(item=>item.name));
 const assemblyConfirm=ui.batchConfirmationHtml({kind:'assemble',clips:assemblyClips,
  edit_plan_id:edit_plan.request_id,edit_plan_sha256:edit_plan.plan_sha256},ready,'demo');
 assert.match(assemblyConfirm,/5 个切点/);assert.match(assemblyConfirm,/one bag\./);
 assert.equal(ui.batchConfirmationHtml({kind:'assemble',clips:assemblyClips,
  edit_plan_id:edit_plan.request_id,edit_plan_sha256:'0'.repeat(64)},ready,'demo'),'');
 const assembled={name:'full.mp4',sha256:hash,kind:'video',url:'/api/runs/demo/materials/full.mp4'};
 const final={...ready,materials:[...ready.materials,assembled],assemblies:[{clips:assemblyClips,
  edit_plan_sha256:edit_plan.plan_sha256,output:assembled}]};
 assert.equal(ui.currentAssembly(final,'demo').output.name,'full.mp4');
 const html=ui.canvasHtml(final,'video','demo',[]);
 assert.match(html,/下载成片/);assert.match(html,/当前脚本的成片剪辑计划/);
 assert.match(html,/15 秒 · 5 个画面切点/);
 assert.equal((html.match(/<details class="production-shot"/g)||[]).length,5);
 assert.doesNotMatch(html,/<bag>|data-generate/);
});
test('a generated image never appears as another storyboard frame',()=>{
 const picture={name:'generated.png',sha256:hash,kind:'image',url:'/api/runs/demo/materials/generated.png',approved:false};
 const done={...draft,kind:'image',draft_id:'done',attempts:[{attempt_id:'one',status:'succeeded',output:picture}]};
 const pending={...draft,kind:'image',draft_id:'pending',attempts:[]};
 const shown={...data,materials:[...data.materials,picture],drafts:[done,pending]};
 const pendingWork=ui.currentProductionWork({...shown,drafts:[pending]},'image','demo');
 assert.equal(pendingWork.outputUrl,'');
 assert.doesNotMatch(ui.draftHtml(pending,shown,'demo'),/generated\.png/);
 const html=ui.canvasHtml(shown,'image','demo',[]);
 assert.equal((html.match(/generated\.png/g)||[]).length,1);
 assert.match(html,/镜头 02[\s\S]*<small>本次制作还不能提交<\/small>/);
});
test('a failed shot shows its own reason and its real stage in place',()=>{
 const picture={name:'generated.png',sha256:hash,kind:'image',url:'/api/runs/demo/materials/generated.png',approved:false};
 const failed={...draft,kind:'image',draft_id:'shot-two',prompt:'Second frame',
  attempts:[{attempt_id:'try-2',status:'failed',message:'Provider rejected this frame description.'}]};
 const shots={kind:'image',total:2,complete_list:true,ready:false,delivered:false,failed:true,pending:false,
  unready_shots:[],shots:[
   {ordinal:1,shot_id:'shot-01',label:'Bag on the table',draft_id:'shot-one',revision:1,source_ready:true,
    stage:'completed',stage_label:'已完成',blocker:null,next_action:'review_result',reason:'',
    attempt_status:'succeeded',output:{name:'generated.png',sha256:hash}},
   {ordinal:2,shot_id:'shot-02',label:'Zip opens',draft_id:'shot-two',revision:1,source_ready:true,
    stage:'failed',stage_label:'失败',blocker:'generation_failed',next_action:'diagnose_attempt',
    reason:'Provider rejected this frame description.',attempt_status:'failed',attempt_id:'try-2'}]};
 const shown={...data,capabilities:{...data.capabilities,image:{available:true}},
  materials:[...data.materials,picture],drafts:[failed],shots:{image:shots}};
 const html=ui.canvasHtml(shown,'image','demo',[]);
 assert.match(html,/Provider rejected this frame description\./);
 assert.match(html,/<small>失败<\/small>/);
 assert.match(html,/production-shot-cause/);
 assert.match(html,/镜头 01[\s\S]*这面镜头还没有制作输入/);
});
test('a revised shot replaces its input instead of adding a shot',()=>{
 const old={...draft,kind:'image',draft_id:'prompt-v1',prompt:'Old framing',shot_id:'shot-01',
  is_current:false,superseded_by:'prompt-v2',attempts:[]};
 const fresh={...draft,kind:'image',draft_id:'prompt-v2',prompt:'Natural framing',shot_id:'shot-01',
  is_current:true,supersedes:'prompt-v1',attempts:[]};
 const shots={kind:'image',total:1,complete_list:true,ready:true,delivered:false,failed:false,pending:false,
  unready_shots:[],shots:[{ordinal:1,shot_id:'shot-01',label:'Natural framing',draft_id:'prompt-v2',
   revision:2,source_ready:true,stage:'input_ready',stage_label:'输入已准备',blocker:null,
   next_action:'authorise_generation',reason:'',attempt:null}]};
 const shown={...data,capabilities:{...data.capabilities,image:{available:true}},
  drafts:[old,fresh],shots:{image:shots}};
 const html=ui.canvasHtml(shown,'image','demo',[]);
 assert.equal((html.match(/data-draft-id=/g)||[]).length,1,'the replaced input is not a second shot');
 assert.doesNotMatch(html,/<span>Old framing<\/span>/);
 assert.match(html,/已被替换的历史输入 · 1/);
 assert.match(html,/Old framing/);
 assert.deepEqual(ui.batchGenerationDrafts(shown,'demo').map(row=>row.draft_id),['prompt-v2']);
});
test('video canvas keeps reference material separate from a generated video',()=>{
 const html=ui.canvasHtml({...data,drafts:[draft]},'video','demo',[]);
 assert.match(html,/这里还没有生成的视频/);
 assert.match(html,/参考素材 · 尚未生成视频/);
 assert.doesNotMatch(html,/production-canvas-media/);
});
test('records escape prompts findings model status messages and block external outputs',()=>{const html=ui.draftHtml({...draft,prompt:'<script>x</script>',review:{verdict:'ready',findings:['<img src=x>']},attempts:[{attempt_id:'x',status:'failed',message:'<iframe>',output:{url:'https://evil.test/a',name:'bad'}}]},data,'demo');assert.doesNotMatch(html,/<script>|<iframe>|<img src=x>|https:\/\/evil/);assert.match(html,/&lt;script/);assert.match(html,/&lt;img/);});
test('review and generation affordances distinguish pending and recovery',()=>{const html=ui.draftHtml({...draft,attempts:[{attempt_id:'pending',status:'unknown'}]},data,'demo');assert.doesNotMatch(html,/data-generate/);assert.match(html,/核实提供方提交记录/);assert.match(html,/不会重新生成/);assert.doesNotMatch(html,/自动重试/);});
test('network ambiguity including server failures retains request identity',()=>{for(const error of [new Error('Failed to fetch'),{status:500},{status:502},{status:429},{}])assert.equal(ui.uncertain(error),true);for(const status of [400,403,404,409,422])assert.equal(ui.uncertain({status}),false);const request=ui.actionRequest('generate',draft,'nonce','known-id');const persisted=JSON.parse(JSON.stringify({pending:request}));const recovered=ui.actionRequest('generate',{...draft,prompt:'Changed form',draft_sha256:'b'.repeat(64)},'new-nonce','new-id',persisted.pending);assert.equal(recovered.body.request_id,'known-id');assert.equal(recovered.body.expected_sha256,hash);});
test('running jobs block actions but unknown accounting never does',()=>{
 const active={...data,active:true};
 assert.notEqual(ui.reviewGate(draft,active),''); assert.notEqual(ui.gate(draft,active),'');
 const unknown={...data,active:false,unresolved_calls:1};
 assert.equal(ui.reviewGate(draft,unknown),''); assert.equal(ui.gate(draft,unknown),'');
});
test('every running provider phase prevents duplicate generation',()=>{for(const status of ['preparing','submitting','submitted','downloading'])assert.notEqual(ui.gate({...draft,attempts:[{status}]},data),'');assert.equal(ui.gate({...draft,attempts:[{status:'succeeded',usage_status:'unknown'}]},data),'');});
test('an unresolved first image blocks a misleading batch of only the remaining shots',()=>{
 const first={...draft,kind:'image',draft_id:'first',attempts:[{attempt_id:'old',status:'unknown'}]};
 const second={...draft,kind:'image',draft_id:'second',attempts:[]};
 const current={...data,capabilities:{...data.capabilities,image:{available:true}},drafts:[first,second]};
 assert.equal(ui.batchBlocker(current,'demo').draft.draft_id,'first');
 assert.equal(ui.batchGenerationDrafts(current,'demo').length,0);
 const resolved={...current,drafts:[{...first,attempts:[{attempt_id:'old',status:'retry_authorized'}]},second]};
 assert.equal(ui.batchBlocker(resolved,'demo'),null);
 assert.equal(ui.batchGenerationDrafts(resolved,'demo').length,2);
});
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
 assert.doesNotMatch(html,/data-generate/);
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
 assert.ok(html.indexOf('再次制作或高级编辑')>html.indexOf('下载视频'));
});
test('failed retry keeps the preceding playable result as the main work',()=>{
 const movie={name:'movie.mp4',sha256:hash,kind:'video',url:'/api/runs/demo/materials/movie.mp4'};
 const shown={...data,materials:[...data.materials,movie]};
 const first={attempt_id:'first',status:'succeeded',at:'2026-09-24T09:00:00Z',output:movie};
 const retry={attempt_id:'retry',status:'failed',at:'2026-09-24T10:00:00Z',message:'口型与旁白冲突'};
 const changed={...draft,attempts:[first,retry]};
 const work=ui.currentProductionWork({...shown,drafts:[changed]},'video','demo');
 assert.equal(work.phase,'failed');assert.equal(work.primaryAction,'discuss');
 assert.equal(work.displayAttempt.attempt_id,'first');assert.equal(work.resultIsPrevious,true);
 const html=ui.draftHtml(changed,shown,'demo');
 assert.match(html,/<video controls preload="metadata" src="\/api\/runs\/demo\/materials\/movie.mp4"/);
 assert.match(html,/上一份已生成结果/);assert.match(html,/口型与旁白冲突/);
 assert.match(html,/data-feedback-attempt="first"/);assert.doesNotMatch(html,/data-feedback-attempt="retry"/);
 assert.match(html,/production-primary-actions"><button class="primary" data-production-diagnose="one">让团队检查并提出修正/);
 assert.match(html,/data-production-feedback="one">自己描述问题/);
 assert.ok(html.indexOf('手动调整制作输入')>html.indexOf('再次制作或高级编辑'));
});
test('failed generation offers one-click diagnosis while old reviewer record remains history',()=>{
 const checked={...draft,review:{verdict:'revise',findings:['开场证据不足']}};
 const checkWork=ui.currentProductionWork({...data,drafts:[checked]},'video','demo');
 assert.equal(checkWork.phase,'ready');assert.equal(ui.canDiagnoseWork(checkWork),false);
 const review=ui.draftHtml(checked,data,'demo');
 assert.doesNotMatch(review,/data-review|data-production-diagnose/);
 assert.match(review,/data-generate="one"/);
 const failed={...draft,attempts:[{attempt_id:'failed',status:'failed',message:'提供方返回失败'}]};
 assert.equal(ui.canDiagnoseWork(ui.currentProductionWork({...data,drafts:[failed]},'video','demo')),true);
 const unknown={...draft,attempts:[{attempt_id:'unknown',status:'unknown',task_id:'original'}]};
 const uncertain=ui.draftHtml(unknown,data,'demo');
 assert.match(uncertain,/data-refresh="unknown">查询这次结果/);
 assert.doesNotMatch(uncertain,/data-production-diagnose/);
 assert.equal(ui.canDiagnoseWork(ui.currentProductionWork({...data,drafts:[unknown]},'video','demo')),false);
 const cancelled={...draft,attempts:[{attempt_id:'stop',status:'cancelled'}]};
 assert.doesNotMatch(ui.draftHtml(cancelled,data,'demo'),/data-production-diagnose/);
 const noFindings={...checked,review:{verdict:'revise',findings:[]}};
 assert.doesNotMatch(ui.draftHtml(noFindings,data,'demo'),/data-production-diagnose/);
});
test('new failed draft can show a verified result from an earlier draft',()=>{
 const movie={name:'movie.mp4',sha256:hash,kind:'video',url:'/api/runs/demo/materials/movie.mp4'};
 const first={...draft,draft_id:'first-draft',source_version:1,attempts:[{attempt_id:'first',status:'succeeded',at:'2026-09-24T09:00:00Z',output:movie}]};
 const retry={...draft,draft_id:'retry-draft',attempts:[{attempt_id:'retry',status:'failed',at:'2026-09-24T10:00:00Z',message:'新尝试未完成'}]};
 const shown={...data,materials:[...data.materials,movie],drafts:[first,retry]};
 const work=ui.currentProductionWork(shown,'video','demo');
 assert.equal(work.draft.draft_id,'retry-draft');assert.equal(work.attempt.attempt_id,'retry');
 assert.equal(work.displayAttempt.attempt_id,'first');assert.equal(work.phase,'failed');
 const html=ui.draftHtml(retry,shown,'demo');
 assert.match(html,/上一份已生成结果 · 来自脚本第 1 版/);assert.match(html,/data-feedback-attempt="first"/);
 assert.match(html,/<video controls preload="metadata"/);
});
test('an older video cannot replace the pending reference image approval target',()=>{
 const movie={name:'movie.mp4',sha256:hash,kind:'video',url:'/api/runs/demo/materials/movie.mp4'};
 const prior={...draft,draft_id:'prior',attempts:[{attempt_id:'first',status:'succeeded',output:movie}]};
 const awaiting={...draft,draft_id:'awaiting',reference_approved:false,attempts:[]};
 const shown={...data,materials:[...data.materials,movie],drafts:[prior,awaiting]};
 const work=ui.currentProductionWork(shown,'video','demo');
 assert.equal(work.phase,'image_pending');assert.equal(work.outputName,'movie.mp4');
 const html=ui.draftHtml(awaiting,shown,'demo');
 assert.match(html,/data-image="a.png">查看并确认这张图/);
 assert.match(html,/alt="本次待确认的视频参考图"/);
 assert.match(html,/<video controls preload="metadata"/);
});
test('unknown retry still prioritizes original-task recovery while retaining an earlier result',()=>{
 const movie={name:'movie.mp4',sha256:hash,kind:'video',url:'/api/runs/demo/materials/movie.mp4'};
 const first={attempt_id:'first',status:'succeeded',output:movie};
 const pending={attempt_id:'pending',status:'unknown',task_id:'provider-task'};
 const changed={...draft,attempts:[first,pending]};
 const shown={...data,materials:[...data.materials,movie],drafts:[changed]};
 const work=ui.currentProductionWork(shown,'video','demo');
 assert.equal(work.phase,'unknown');assert.equal(work.primaryAction,'refresh');
 assert.equal(work.displayAttempt.attempt_id,'first');
 const html=ui.draftHtml(changed,shown,'demo');
 assert.match(html,/data-refresh="pending"/);assert.match(html,/上一份已生成结果/);
 assert.match(html,/data-feedback-attempt="first"/);
 assert.match(html,/<video controls preload="metadata"/);
 assert.doesNotMatch(html,/data-generate/);
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
test('a later success on another draft cannot change the current image approval target',()=>{
 const older={name:'older.png',sha256:hash,kind:'image',url:'/api/runs/demo/materials/older.png',approved:true};
 const pending={name:'pending.png',sha256:hash,kind:'image',url:'/api/runs/demo/materials/pending.png',approved:false};
 const prior={...draft,kind:'image',draft_id:'prior',attempts:[{attempt_id:'older-job',status:'succeeded',at:'2026-09-24T12:00:00Z',output:older}]};
 const current={...draft,kind:'image',draft_id:'current',attempts:[{attempt_id:'pending-job',status:'succeeded',at:'2026-09-24T11:00:00Z',output:pending}]};
 const shown={...data,materials:[...data.materials,older,pending],drafts:[prior,current]};
 const work=ui.currentProductionWork(shown,'image','demo');
 assert.equal(work.draft.draft_id,'current');assert.equal(work.phase,'image_pending');
 assert.equal(work.outputName,'pending.png');assert.equal(work.displayAttempt.attempt_id,'pending-job');
 const html=ui.draftHtml(current,shown,'demo');
 assert.match(html,/data-image="pending.png">查看并确认这张图/);
 assert.match(html,/src="\/api\/runs\/demo\/materials\/pending.png"/);
 assert.doesNotMatch(html,/data-image="older.png">查看并确认这张图/);
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
 const prepare=ui.currentProductionWork({...data,drafts:[],latest_approved_image:data.materials[0]},'video','demo');
 assert.equal(prepare.phase,'prepare');
 const html=ui.emptyWorkHtml(prepare);
 assert.match(html,/准备这张图的视频输入/);assert.match(html,/已确认参考图：a.png/);
 assert.match(html,/src="\/api\/runs\/demo\/materials\/a.png"/);
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

test('batch progress follows script shot identity after revisions change insertion order',()=>{
 const revised={...draft,kind:'image',draft_id:'shot-02-v2',attempts:[]};
 const unresolved={...draft,kind:'image',draft_id:'shot-08-v1',attempts:[{status:'unknown'}]};
 const old={...draft,kind:'image',draft_id:'shot-02-v1',is_current:false,attempts:[{status:'unknown'}]};
 const current={...data,drafts:[old,unresolved,revised],shots:{image:{shots:[
   {ordinal:2,shot_id:'shot-02',draft_id:revised.draft_id},
   {ordinal:8,shot_id:'shot-08',draft_id:unresolved.draft_id}
 ]}}};
 const blocker=ui.batchBlocker(current,'demo');
 assert.equal(blocker.draft.draft_id,unresolved.draft_id);
 assert.equal(blocker.ordinal,8);
 assert.equal(ui.batchGenerationDrafts(current,'demo').length,0);
});

test('an original image task outranks missing design and a batch of fresh inputs',()=>{
 const original={...draft,kind:'image',draft_id:'old-shot-08',shot_id:'shot-08',source_ready:false,
  design_issue:'This historical input needs design.',is_current:true,
  attempts:[{attempt_id:'original-08',status:'unknown'}]};
 const fresh=[1,2].map(index=>({...draft,kind:'image',draft_id:`fresh-${index}`,shot_id:`shot-0${index}`,
  references:[],attempts:[],can_generate:true}));
 const shown={...data,capabilities:{image:{available:true}},drafts:[original,...fresh],
  unresolved_attempts:[{kind:'image',shot_id:'shot-08',ordinal:8,draft_id:original.draft_id,
    attempt_id:'original-08',status:'unknown'}]};
 const blocker=ui.batchBlocker(shown,'demo');
 assert.equal(blocker.draft.draft_id,original.draft_id);
 assert.equal(blocker.attempt.attempt_id,'original-08');assert.equal(blocker.ordinal,8);
 assert.equal(ui.currentProductionWork(shown,'image','demo').phase,'unknown');
 assert.deepEqual(ui.batchGenerationDrafts(shown,'demo'),[]);
 assert.equal(ui.batchConfirmationHtml({kind:'generate',drafts:fresh.map(row=>({
   draft_id:row.draft_id,expected_sha256:row.draft_sha256}))},shown,'demo'),'');
 const html=ui.draftHtml(original,shown,'demo');
 assert.match(html,/data-recover="original-08"/);assert.doesNotMatch(html,/data-generate|data-edit-input/);
});

test('coverage joins the original attempt instead of its current replacement draft',()=>{
 const original={...draft,kind:'image',draft_id:'old-input',shot_id:'shot-08',source_ready:false,
  design_issue:'Missing design binding',is_current:false,
  attempts:[{attempt_id:'original-08',status:'unknown',task_id:'provider-08'},
    {attempt_id:'later-failed',status:'failed'}]};
 const replacement={...draft,kind:'image',draft_id:'new-input',shot_id:'shot-08',attempts:[]};
 const shown={...data,capabilities:{image:{available:true}},drafts:[original,replacement],
  shots:{image:{shots:[{draft_id:replacement.draft_id,shot_id:'shot-08',ordinal:8,
    attempt_id:'original-08',attempt_status:'unknown',task_id:'provider-08',
    source_ready:true,stage:'unverified',blocker:'attempt_unresolved'}]}}};
 const work=ui.currentProductionWork(shown,'image','demo');
 assert.equal(work.phase,'unknown');assert.equal(work.primaryAction,'refresh');
 assert.equal(work.draft.draft_id,'old-input');assert.equal(work.attempt.attempt_id,'original-08');
 assert.equal(ui.batchBlocker(shown,'demo').ordinal,8);
 assert.deepEqual(ui.batchGenerationDrafts(shown,'demo'),[]);
 assert.notEqual(ui.gate(replacement,shown),'');
 const html=ui.draftHtml(original,shown,'demo');
 assert.match(html,/data-refresh="original-08">查询这次结果/);
 assert.doesNotMatch(html,/data-generate/);
});

test('shared original tasks remain visible outside the current targets and across media batches',()=>{
 const video={...draft,draft_id:'historical-video',shot_id:'old-shot',source_ready:false,is_current:false,
  attempts:[{attempt_id:'old-video-task',status:'submission_unknown'}]};
 const image={...draft,kind:'image',draft_id:'fresh-image',shot_id:'new-shot',references:[],attempts:[]};
 const shown={...data,capabilities:{image:{available:true},video:{available:true}},drafts:[video,image],
  shots:{image:{shots:[{draft_id:image.draft_id,shot_id:image.shot_id,ordinal:1}]}},
  unresolved_attempts:[{kind:'video',shot_id:'old-shot',draft_id:video.draft_id,
   attempt_id:'old-video-task',status:'submission_unknown'}]};
 assert.equal(ui.batchBlocker(shown,'demo').work.kind,'video');
 assert.equal(ui.currentProductionWork(shown,'video','demo').draft.draft_id,video.draft_id);
 assert.deepEqual(ui.batchGenerationDrafts(shown,'demo'),[]);
 assert.deepEqual(ui.videoBatchGenerationDrafts(shown,'demo'),[]);
 // A batch boundary is global; a separate manual input is not silently widened.
 assert.equal(ui.gate(image,shown),'');
});

test('settling an original task restores generation without treating unknown accounting as a task',()=>{
 const original={...draft,kind:'image',draft_id:'historical-image',shot_id:'shot-08',source_ready:false,
  is_current:false,attempts:[{attempt_id:'original-08',status:'retry_authorized',usage_status:'unknown'}]};
 const replacement={...draft,kind:'image',draft_id:'new-input',shot_id:'shot-08',references:[],attempts:[]};
 const success={...draft,kind:'image',draft_id:'saved-image',shot_id:'shot-01',attempts:[{
  attempt_id:'saved-result',status:'succeeded',usage_status:'unknown',output:data.materials[0]}]};
 const shown={...data,capabilities:{image:{available:true}},drafts:[original,success,replacement],
  unresolved_attempts:[]};
 assert.equal(ui.batchBlocker(shown,'demo'),null);
 assert.deepEqual(ui.batchGenerationDrafts(shown,'demo').map(row=>row.draft_id),['new-input']);
 const work=ui.currentProductionWork(shown,'image','demo');
 assert.equal(work.phase,'ready');assert.equal(work.draft.draft_id,replacement.draft_id);
 assert.match(ui.draftHtml(replacement,shown,'demo'),/data-generate="new-input"/);
 assert.equal(shown.materials[0].approved,true);
});

test('a shared unresolved identity with missing local detail blocks a new batch safely',()=>{
 const image={...draft,kind:'image',draft_id:'fresh-image',references:[],attempts:[]};
 const shown={...data,capabilities:{image:{available:true}},drafts:[image],
  unresolved_attempts:[{kind:'image',shot_id:'old-shot',draft_id:'not-in-payload',
   attempt_id:'original-task',status:'unknown'}]};
 const blocker=ui.batchBlocker(shown,'demo');
 assert.equal(blocker.draft,null);assert.equal(blocker.attempt.attempt_id,'original-task');
 assert.equal(blocker.work.primaryAction,'none');
 assert.deepEqual(ui.batchGenerationDrafts(shown,'demo'),[]);
 assert.match(ui.emptyWorkHtml(blocker.work),/原任务记录需要重新读取/);
 assert.doesNotMatch(ui.emptyWorkHtml(blocker.work),/data-generate|data-recover/);
});
