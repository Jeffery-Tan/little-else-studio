import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import LearningSession from '../../studio/web/learning-session.js';

globalThis.crypto ??= webcrypto;

const proposal={id:'proposal-1',status:'pending',title:'补充口播判断',proposal_sha256:'a'.repeat(64),
  base_library_sha256:'b'.repeat(64),summary:{what:'给旧方法补适用条件',why:'用户指出原解释遗漏人物选择',
    use_when:'生活口播',avoid_when:'纯视觉片',unknown:'跨商品效果'},diff:'-旧条件\n+新条件'};
const run={run_id:'learning-one',kind:'learning',title:'参考研究',brief:'研究口播节奏'};

test('one sentence names a learning run without asking for a separate title',()=>{
  assert.equal(LearningSession.titleFromIntent(' 先不复刻，研究这条为什么信息多但不生硬。  '),'先不复刻，研究这条为什么信息多但不生硬。');
  assert.equal(LearningSession.titleFromIntent('\n\n观察这条参考\n另一句'),'观察这条参考');
  assert.equal(LearningSession.titleFromIntent(' '.repeat(4)),'参考研究');
});

test('learning room shows source, coverage, discussion and readable change without production actions',()=>{
  const html=LearningSession.roomHtml({status:'pending',summary:'整理材料',sources:[{name:'原片.mp4',kind:'video',report:'模型只分析 0–8 秒'}],
    observations:[{content:'0–8 秒中人物拿起包',observer_type:'user',source_name:'原片.mp4',media_sha256:'c'.repeat(64),
      level:'human_interpretation',source_ref:'material:原片.mp4',time_range:[0,8],coverage:'画面，未听音频'}],
    discussion:[{role:'user',text:'我觉得节奏有交流感'},{role:'assistant',text:'这是我的观察摘要'}],proposals:[proposal]},run);
  assert.match(html,/原片与观察范围/);
  assert.match(html,/用户原话/);
  assert.match(html,/未听音频/);
  assert.match(html,/原片 SHA-256/);
  assert.match(html,/cccccccccccccccc/);
  assert.match(html,/human_interpretation/);
  assert.match(html,/给旧方法补适用条件/);
  assert.match(html,/拒绝本次变更/);
  assert.match(html,/data-learning-apply/);
  assert.match(html,/这是我的观察摘要/);
  assert.doesNotMatch(html,/<p class="learning-room-observation">整理材料<\/p>/);
  assert.doesNotMatch(html,/图片批准|提交生成|创作四阶段/);
});

test('no-change conclusion records agreement without claiming a library write',()=>{
  const unchanged={...proposal,type:'delta',no_change:true,payload:{outcome:'no_change'},summary:'没有足够新证据'};
  const pending=LearningSession.proposalCard(unchanged);
  assert.match(pending,/确认本次无知识变更/);
  assert.match(pending,/共享库内容与版本不变/);
  assert.doesNotMatch(pending,/确认并保存变更/);
  const saved=LearningSession.roomHtml({status:'saved',proposals:[{...unchanged,status:'applied'}]},run);
  assert.match(saved,/独立学习 · 已确认，无知识变更/);
  assert.match(saved,/已确认，无知识变更/);
});

test('source and proposal text are escaped, and incomplete hashes cannot be confirmed',()=>{
  const bad='<img src=x onerror="alert(1)">';
  const html=LearningSession.roomHtml({sources:[{name:'reference.mp4',kind:'video',title:bad}],
    observations:[{content:bad}],proposals:[{...proposal,proposal_sha256:null,sha256:null,summary:{what:bad}}]},run);
  assert.doesNotMatch(html,/<img src=x|onerror="/);
  assert.match(html,/&lt;img/);
  assert.doesNotMatch(html,/data-learning-apply/);
  assert.match(html,/缺少准确提案版本/);
});

test('v1 library shows a reviewed upgrade path and the saved reference analysis',()=>{
  const html=LearningSession.roomHtml({sources:[{kind:'reference_analysis',reference_id:'ref-1',name:'原片.mp4',coverage:'模型采样观察'}]},
    run,[],[{reference_id:'ref-1',name:'原片.mp4',report:'这里只看了前 8 秒'}],{schema_version:1});
  assert.match(html,/共享知识库需要升级到 v2/);
  assert.match(html,/data-learning-migration/);
  assert.match(html,/这里只看了前 8 秒/);
  assert.match(html,/<video controls/);
});

test('failed link acquisition is not presented as a saved video and can resume with upload',()=>{
  const task={reference_id:'ref-failed',source_url:'https://www.tiktok.com/@seller/video/1234567890',
    status:'started',execution_status:'failed'};
  const html=LearningSession.roomHtml({sources:[{reference_id:'ref-failed',source_url:task.source_url}]},run,[],[task]);
  assert.match(html,/TikTok 原片获取失败，尚未进行声画分析/);
  assert.match(html,/重试链接或上传原视频/);
  assert.doesNotMatch(html,/来源已保存；观察覆盖以记录为准/);
  const button={dataset:{learningResumeReference:'ref-failed'},addEventListener:(_type,handler)=>{button.click=handler;}};
  const host={innerHTML:'',querySelector:()=>null,querySelectorAll:selector=>
    selector==='[data-learning-resume-reference]'?[button]:[]};
  let opened=null;
  LearningSession.mount({host,session:{},run,referenceTasks:[task],openResumeReference:value=>{opened=value;}});
  button.click();
  assert.equal(opened,task);
});

test('analysis-only learning may end without inventing a method',()=>{
  const open=LearningSession.roomHtml({status:'discussing',proposals:[]},run);
  assert.match(open,/data-learning-close/);
  assert.match(open,/讨论后没有新方法也是有效结论/);
  const pending=LearningSession.roomHtml({status:'pending_review',proposals:[proposal]},run);
  assert.doesNotMatch(pending,/data-learning-close/);
  const closed=LearningSession.roomHtml({status:'closed',proposals:[]},run);
  assert.doesNotMatch(closed,/data-learning-close/);
});

function mounted(action, selected=proposal){
  const apply={disabled:false}, reject={disabled:false}, status={textContent:''};
  const card={dataset:{learningProposal:'proposal-1'},querySelector:selector=>({
    '[data-learning-apply]':apply,'[data-learning-reject]':reject,'.learning-room-action-status':status})[selector]};
  const host={innerHTML:'',querySelector:()=>null,querySelectorAll:selector=>selector==='[data-learning-proposal]'?[card]:[]};
  const calls=[];let reloads=0;
  LearningSession.mount({host,session:{proposals:[selected]},run,request:async(url,options)=>{
    calls.push({url,body:JSON.parse(options.body)});return {status:action==='reject'?'rejected':'applied'};},
    reload:async()=>{reloads++;},notify(){},openMaterials(){},openChat(){},openAnalysis(){}});
  return {apply,reject,status,calls,reloads:()=>reloads};
}

test('apply sends both displayed hashes and one request identity',async()=>{
  const view=mounted('apply');await view.apply.onclick();
  assert.equal(view.calls.length,1);
  assert.equal(view.calls[0].url,'/api/runs/learning-one/learning/proposals/proposal-1/apply');
  assert.equal(view.calls[0].body.expected_proposal_sha256,proposal.proposal_sha256);
  assert.equal(view.calls[0].body.expected_library_sha256,proposal.base_library_sha256);
  assert.ok(view.calls[0].body.request_id);
  assert.equal(view.reloads(),1);
});

test('reject records an explicit rejection without a library hash',async()=>{
  const view=mounted('reject');await view.reject.onclick();
  assert.equal(view.calls.length,1);
  assert.equal(view.calls[0].url,'/api/runs/learning-one/learning/proposals/proposal-1/reject');
  assert.equal(view.calls[0].body.expected_proposal_sha256,proposal.proposal_sha256);
  assert.equal(view.calls[0].body.expected_library_sha256,undefined);
  assert.equal(view.reloads(),1);
});

test('confirming no change sends exact hashes but reports no library mutation',async()=>{
  const unchanged={...proposal,type:'delta',no_change:true,payload:{outcome:'no_change'}};
  const view=mounted('apply',unchanged);await view.apply.onclick();
  assert.equal(view.calls[0].body.expected_library_sha256,proposal.base_library_sha256);
  assert.match(view.status.textContent,/共享库内容与版本不变/);
});
