import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {creatorProjectView}=require('../../studio/web/creator-view.js');

const writing={stage:'writing',has_doc:true,version:2,approval_decision:'approved',
  blocks:[{type:'timeline',segments:[]},{type:'text',title:'发布文案',text:'Lunch, sorted.'}]};
const direction={stage:'direction',has_doc:true,version:1,approval_decision:'approved',selected_option_id:'B'};
const run={run_id:'demo',title:'午餐包',brief:'做一条短视频',content_format:'video'};
const base={run,timeline:{rail:[direction,writing],brief:{phase:'ready',execution:{active:false}}},
  imageWork:{kind:'image',phase:'needs_plan',draft:null},videoWork:{kind:'video',phase:'needs_plan',draft:null}};
const withVideo=(phase,other={})=>({kind:'video',phase,draft:{draft_id:'v1'},...other});

test('the live timeline format takes precedence over a cached project format',()=>{
  const photo=creatorProjectView({...base,timeline:{...base.timeline,run:{content_format:'photo'}},
    videoWork:withVideo('completed')});
  assert.equal(photo.format,'photo');
  assert.equal(photo.goal,'带货图片与文案');
  assert.equal(photo.action.tab,'storyboard');
  const video=creatorProjectView({...base,run:{...run,content_format:'photo'},
    timeline:{...base.timeline,run:{content_format:'video'}}});
  assert.equal(video.format,'video');
  const legacy=creatorProjectView({...base,timeline:{...base.timeline,run:{title:'Older server'}}});
  assert.equal(legacy.format,'video');
});

test('photo preparation and saved images never select historical video work',()=>{
  const photo={...base,run:{...run,content_format:'photo'}};
  const preparing=creatorProjectView({...photo,
    imageWork:{kind:'image',phase:'prepare',draft:null},
    videoWork:{kind:'video',phase:'prepare',draft:null}});
  assert.equal(preparing.current,'脚本已确认，下一步准备画面');
  assert.equal(preparing.action.label,'准备画面输入');
  assert.equal(preparing.action.tab,'storyboard');
  for(const phase of ['ready','completed','unknown','failed','prepare']){
    const saved=creatorProjectView({...photo,
      imageWork:{kind:'image',phase:'completed',draft:{draft_id:'image-v1'}},videoWork:withVideo(phase)});
    assert.equal(saved.current,'图片已取回，核对实际内容');
    assert.equal(saved.action.tab,'storyboard');
    assert.doesNotMatch(saved.action.label,/视频/);
  }
});

test('direction choice and adopted direction keep a creative destination',()=>{
  const pending=creatorProjectView({...base,timeline:{rail:[direction],pending:{stage:'direction'},brief:{execution:{active:false}}}});
  assert.equal(pending.current,'正在选择创意方向；采用后会继续准备脚本');
  assert.equal(pending.action.label,'选择创意方向');
  const adopted=creatorProjectView({...base,timeline:{rail:[direction],brief:{execution:{active:false}}}});
  assert.equal(adopted.stage,'direction');
  assert.equal(adopted.current,'已采用方向 B，下一份脚本尚未准备好');
  assert.equal(adopted.action.kind,'chat');
});

test('a direction question does not hide the option selection action',()=>{
  const view=creatorProjectView({...base,timeline:{rail:[direction],pending:{stage:'direction'},
    brief:{execution:{active:false},questions:[{stage:'direction',question:'有实物吗？'}]}}});
  assert.equal(view.action.label,'选择创意方向');
});

test('a stale direction uses the server stage command as its next action',()=>{
  const action={kind:'resume_direction',stage:'direction',label:'重做创意方向',
    expected_context_sha256:'a'.repeat(64)};
  const view=creatorProjectView({...base,timeline:{rail:[{...direction,stale:true}],
    next_action:action,brief:{phase:'interrupted',execution:{active:false}}}});
  assert.equal(view.action,action);
  assert.equal(view.stage,'direction');
});

test('script is the current work until media work exists',()=>{
  const pending=creatorProjectView({...base,timeline:{...base.timeline,pending:{stage:'writing'}}});
  assert.equal(pending.action.label,'阅读并决定脚本');
  const returned=creatorProjectView({...base,timeline:{...base.timeline,
    pending:{stage:'writing',passed:false}}});
  assert.equal(returned.current,'脚本新版已保存，但审核提出修改意见');
  assert.equal(returned.action.label,'查看脚本与审核');
  assert.equal(returned.stage,'writing');
  const ready=creatorProjectView(base);
  assert.equal(ready.goal,'商品视频与发布文案');
  assert.equal(ready.current,'脚本已确认，下一步准备实际画面');
  assert.equal(ready.tab,'plan');
  assert.equal(ready.action.tab,'storyboard');
});

test('direction review return does not appear ready for adoption',()=>{
  const returned=creatorProjectView({...base,timeline:{rail:[direction],
    pending:{stage:'direction',passed:false},brief:{execution:{active:false}}}});
  assert.equal(returned.current,'创意方向已保存，但审核提出修改意见');
  assert.equal(returned.action.label,'查看方向与审核');
});

test('media states map to one current work and safe main action',()=>{
  const image=creatorProjectView({...base,imageWork:{kind:'image',phase:'image_pending',draft:{draft_id:'i1'}}});
  assert.equal(image.current,'图片已取回，等待你确认这张画面');
  assert.equal(image.tab,'storyboard');
  const ready=creatorProjectView({...base,videoWork:withVideo('ready')});
  assert.equal(ready.action.label,'核对并生成');
  const done=creatorProjectView({...base,videoWork:withVideo('completed')});
  assert.equal(done.current,'视频已取回，核对实际内容');
  assert.equal(done.action.label,'播放并核对视频');
  const unknown=creatorProjectView({...base,videoWork:withVideo('unknown',{primaryAction:'refresh'})});
  assert.equal(unknown.current,'视频生成结果尚未确认');
  assert.equal(unknown.action.label,'查询这次结果');
  assert.equal(unknown.detail,'先处理原任务；当前不会再次提交生成。');
  const settings=creatorProjectView({...base,videoWork:withVideo('blocked',
    {primaryAction:'settings',label:'本次制作还不能提交',detail:'视频连接尚未配置'})});
  assert.equal(settings.action.kind,'settings');
  assert.equal(settings.action.label,'打开模型设置');
  const occupied=creatorProjectView({...base,videoWork:withVideo('blocked',
    {primaryAction:'none',label:'本次制作还不能提交',detail:'制作团队正在执行'})});
  assert.equal(occupied.action,null);
  const preparing=creatorProjectView({...base,videoWork:{kind:'video',phase:'prepare',draft:null}});
  assert.equal(preparing.action.kind,'prepare');
  assert.equal(preparing.action.label,'准备这张图的视频输入');
});

test('old project and only-analysis report remain readable without a new approval',()=>{
  const old=creatorProjectView({...base,run:{title:'旧项目',brief:'做一条短视频'},timeline:{rail:[direction]}});
  assert.equal(old.tab,'plan');
  assert.equal(old.stage,'direction');
  assert.equal(old.action.kind,'chat');
  const oldScript=creatorProjectView({...base,run:{title:'旧项目',brief:'目标是一条视频与英文发布文案',content_format:'video'},
    timeline:{rail:[{...writing,blocks:[]}],brief:{phase:'ready'}}});
  assert.equal(oldScript.goal,'商品视频与发布文案');
  assert.equal(oldScript.tab,'plan');
  const analysis=creatorProjectView({run:{title:'参考观察',brief:'只分析参考'},
    timeline:{rail:[]},referenceTasks:[{intent:'analyze',status:'completed',report:'观察结果'}]});
  assert.equal(analysis.goal,'参考视频拆解报告');
  assert.equal(analysis.analysisOnly,true);
  assert.equal(analysis.action.label,'阅读分析结果');
  assert.equal(analysis.tab,'plan');
});

test('a newer analysis task in a creative project is current without changing its final deliverable',()=>{
  const review={intent:'analyze',status:'completed',started_at:'2026-09-24T12:00:00Z',report:'只拆解新参考'};
  const earlier={...writing,created_at:'2026-09-24T10:00:00Z'};
  const current=creatorProjectView({...base,timeline:{rail:[direction,earlier],brief:{phase:'ready'}},
    referenceTasks:[review]});
  assert.equal(current.goal,'商品视频与发布文案');
  assert.equal(current.current,'本轮只分析新参考：拆解报告已保存');
  assert.equal(current.referenceCurrent,true);
  assert.equal(current.action,null);
  const tied=creatorProjectView({...base,timeline:{rail:[direction,{...earlier,created_at:review.started_at}],brief:{phase:'ready'}},
    referenceTasks:[{...review,after_creative_work:true}]});
  assert.equal(tied.referenceCurrent,true);
  const overtaken=creatorProjectView({...base,timeline:{rail:[direction,{...earlier,created_at:review.started_at}],brief:{phase:'ready'}},
    referenceTasks:[{...review,after_creative_work:false}]});
  assert.equal(overtaken.referenceCurrent,false);
  const later=creatorProjectView({...base,timeline:{rail:[direction,{...earlier,created_at:'2026-09-24T13:00:00Z'}],brief:{phase:'ready'}},
    referenceTasks:[review]});
  assert.equal(later.referenceCurrent,false);
  assert.equal(later.current,'脚本已确认，下一步准备实际画面');
  const recreated=creatorProjectView({...base,timeline:{rail:[direction,earlier],brief:{phase:'ready'}},
    referenceTasks:[review,{intent:'recreate',started_at:'2026-09-24T13:00:00Z'}]});
  assert.equal(recreated.referenceCurrent,false);
  const creativeJob=creatorProjectView({...base,timeline:{rail:[direction,earlier],brief:{execution:{active:true}}},
    referenceTasks:[review],currentJob:{run_id:'demo',job_kind:'conversation',at:'2026-09-24T13:00:00Z'}});
  assert.equal(creativeJob.referenceCurrent,false);
  assert.equal(creativeJob.current,'团队正在处理当前任务');
  const referenceJob=creatorProjectView({...base,timeline:{rail:[direction,earlier],brief:{execution:{active:true}}},
    referenceTasks:[review],currentJob:{run_id:'demo',job_kind:'reference_video',at:'2026-09-24T12:00:00Z'}});
  assert.equal(referenceJob.referenceCurrent,true);
  assert.match(referenceJob.current,/本轮只分析新参考/);
});

test('active execution and saved decision do not imply a new media authorization',()=>{
  const active=creatorProjectView({...base,timeline:{...base.timeline,brief:{execution:{active:true}}}});
  assert.equal(active.action,null);
  const continuing=creatorProjectView({...base,timeline:{...base.timeline,pending_continuation:{stage:'direction'}}});
  assert.equal(continuing.action.kind,'continue');
});

test('a failed first turn keeps the saved request visible and points to recovery',()=>{
  const fresh={run_id:'fresh',title:'新项目',brief:'先构思一条商品视频'};
  const failed=creatorProjectView({run:fresh,timeline:{rail:[],brief:{phase:'interrupted',
    execution:{status:'failed',active:false}}}});
  assert.equal(failed.current,'你的需求已保存，本轮制片未完成');
  assert.deepEqual(failed.action,{kind:'job',label:'查看本轮记录'});
  const waiting=creatorProjectView({run:fresh,timeline:{rail:[]}});
  assert.equal(waiting.current,'你的需求已保存，等待继续创作');
  const empty=creatorProjectView({run:{run_id:'fresh',brief:''},timeline:{rail:[]}});
  assert.equal(empty.current,'说出你想完成的作品');
});

test('an old token-cap failure becomes an explicit new-turn option when the cap is disabled',()=>{
  const reason='[budget] 2009455 tokens reached the soft cap of 2000000; refusing a new call.';
  const blocked=creatorProjectView({...base,timeline:{rail:[],brief:{execution:{active:false}},
      budget:{limit_tokens:null,limit_usd:null,limit_mode:'none'}},
    currentJob:{run_id:'demo',job_kind:'conversation',status:'blocked',reason}});
  assert.match(blocked.current,/上一轮被旧额度设置中断/);
  assert.match(blocked.detail,/旧任务不会自动重试/);
  assert.deepEqual(blocked.action,{kind:'chat',label:'继续创作'});
  assert.doesNotMatch(blocked.current+blocked.detail,/已达到|查看用量/);

  const stillLimited=creatorProjectView({...base,timeline:{rail:[],budget:{limit_tokens:2000000}},
    currentJob:{run_id:'demo',job_kind:'conversation',status:'blocked',reason}});
  assert.match(stillLimited.current,/本机共享 Token 上限已达到/);
  assert.equal(stillLimited.action,null);

  const otherProject=creatorProjectView({...base,timeline:{rail:[]},
    currentJob:{run_id:'other',job_kind:'conversation',status:'blocked',reason}});
  assert.equal(otherProject.action.kind,'chat');
  const dollarCap=creatorProjectView({...base,timeline:{rail:[]},
    currentJob:{run_id:'demo',job_kind:'conversation',status:'blocked',
      reason:'[budget] estimated known spend reached the soft cap of 10.00 USD; refusing a new call.'}});
  assert.equal(dollarCap.action,null);
  const oldDollarCap=creatorProjectView({...base,timeline:{rail:[],budget:{limit_mode:'none'}},
    currentJob:{run_id:'demo',job_kind:'conversation',status:'blocked',
      reason:'[budget] estimated known spend reached the soft cap of 10.00 USD; refusing a new call.'}});
  assert.equal(oldDollarCap.action.label,'继续创作');
});

test('old reviewed content keeps a failed internal review visible with its existing retry action',()=>{
  const review={kind:'review_stage',label:'重试本版审核',stage:'direction',version:3,sha256:'exact-v3'};
  const view=creatorProjectView({...base,timeline:{rail:[direction],brief:{phase:'review_failed',
    detail:'审核未完成，方案已保留。',action:review,execution:{active:false}}}});
  assert.equal(view.current,'创意方向已保存，内部审核未完成');
  assert.equal(view.action,review);
  assert.equal(view.stage,'direction');
  assert.equal(view.detail,'审核未完成，方案已保留。');
});

test('a saved direction awaiting its first review offers review instead of adoption',()=>{
  const review={kind:'review_stage',label:'审核这一版',stage:'direction',version:1,sha256:'exact-v1'};
  const view=creatorProjectView({...base,timeline:{rail:[direction],brief:{phase:'awaiting_review',
    detail:'审核后再选择采用。',action:review,execution:{active:false}}}});
  assert.equal(view.action,review);
  assert.equal(view.stage,'direction');
});

test('critical material gap asks for a plan adjustment before a full script',()=>{
  const view=creatorProjectView({...base,timeline:{rail:[direction,{stage:'material_plan',has_doc:true}],
    brief:{phase:'needs_resolution',detail:'关键画面无法落实',execution:{active:false}}}});
  assert.match(view.current,/素材方案有关键缺口/);
  assert.equal(view.action.label,'调整素材方案');
  assert.equal(view.stage,'material_plan');
});
