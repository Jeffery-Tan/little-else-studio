import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
globalThis.escapeHtml=require('../../studio/web/md.js').escapeHtml;
// Shared gate labels live in stage.js (red line 12); browser loads stage first.
const stageMod=require('../../studio/web/stage.js');
globalThis.gateApproveLabel=stageMod.gateApproveLabel;
globalThis.gateRejectLabel=stageMod.gateRejectLabel;
const {questionCardHtml,artifactQuestionHtml,journeyStatus,bindJourneyTabs,shouldShowQuestionInTurn,productionBatchCommand}=require('../../studio/web/journey.js');

test('direct batch instructions open the right decision without treating revisions as consent',()=>{
  assert.equal(productionBatchCommand('生成全部首帧'),'generate');
  assert.equal(productionBatchCommand('确认这批生成图'),'approve');
  assert.equal(productionBatchCommand('好，批准全部',{generate:5,approve:0}),'generate');
  assert.equal(productionBatchCommand('批准全部',{generate:2,approve:1}),'choose');
  assert.equal(productionBatchCommand('生成全部视频',{video_generate:5}),'video_generate');
  assert.equal(productionBatchCommand('准备整组视频输入',{video_prepare:1}),'video_prepare');
  assert.equal(productionBatchCommand('拼接成片',{assemble:1}),'assemble');
  assert.equal(productionBatchCommand('这五张图的描述都要调整'),null);
  assert.equal(productionBatchCommand('先不要生成这些图'),null);
});

test('a request phrased about shots still opens the one-time generation confirmation',()=>{
  // F5 scenario E: the whole-set request is spoken about in shots now, and it
  // must reach the same confirmation card rather than being read as a revision.
  assert.equal(productionBatchCommand('按这六镜生成吧',{generate:6}),'generate');
  assert.equal(productionBatchCommand('这六个镜头都生成',{generate:6}),'generate');
  assert.equal(productionBatchCommand('生成这六镜',{generate:6}),'generate');
  assert.equal(productionBatchCommand('这六个镜头先别生成'),null);
  assert.equal(productionBatchCommand('第二镜重做一下'),null);
  assert.equal(productionBatchCommand('开始做分镜生成吧',{generate:5,approve:0}),'generate');
  assert.equal(productionBatchCommand('开始做分镜生成吧',{generate:0,approve:0}),null);
  assert.equal(productionBatchCommand('开始做分镜生成吧',{generate:2,approve:1}),null);
  for(const query of ['这五张图生成了吗','是否生成这五张图','什么时候生成这五张图','能否生成这五张图'])
    assert.equal(productionBatchCommand(query,{generate:5}),null,query);
});

test('clarification supports recommendation and free text without implicit selection',()=>{
  const html=questionCardHtml({id:'abc',question:'<script>?</script>',options:[{label:'A',description:'one'},{label:'B'}],recommended:0});
  assert.ok(html.includes('推荐'));
  assert.ok(html.includes('data-question-choice'));
  assert.ok(!html.includes('<form'));
  assert.ok(html.includes('填入对话框'));
  assert.ok(!html.includes(' checked'));
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('data-act="approve"'));
});
test('artifact questions are asked in the conversation with room for an uncertain answer',()=>{
  const html=artifactQuestionHtml({stage:'direction',version:3,question:'能实拍吗？',reason:'需要决定拍法'});
  assert.ok(html.includes('能实拍吗？'));
  assert.ok(html.includes('没有或不确定也可以'));
  assert.ok(html.includes('data-send-artifact-answer'));
  assert.ok(!html.includes('不能成立'));
});
test('execution completion cannot replace an unresolved user decision',()=>{
  assert.equal(journeyStatus({execution:{active:false}},{stage:'direction'},null),'选一个你想继续的方向');
  assert.equal(journeyStatus({execution:{active:false},questions:[{}]},{stage:'direction'},null),'选一个你想继续的方向');
  assert.equal(journeyStatus({execution:{active:true}},null,null),'团队正在处理');
});
test('only the conversation exposes a version-bound choice and submit action',()=>{
  const options=[{key:'A',recommended:false,label:'方向',return_note:''},{key:'B',recommended:true,label:'方向',return_note:''}];
  const conversation=stageMod.gateHtml({stage:'direction',version:1,artifact_sha256:'b'.repeat(64),passed:true,options},{});
  assert.ok(conversation.includes('type="radio"'));
  assert.ok(conversation.includes('data-act="approve"'));
  const { blocksHtml }=require('../../studio/web/blocks.js');
  globalThis.blocksHtml=blocksHtml;
  globalThis.renderMarkdown=require('../../studio/web/md.js').renderMarkdown;
  const { stageHtml }=require('../../studio/web/stage.js');
  const stage=stageHtml(
    {stage:'direction',label:'创意方向',has_doc:true,version:1,blocks:[],review:null,team_issues:[]},
    {readOnly:true,pending:{stage:'direction',version:1,artifact_sha256:'b'.repeat(64),passed:true,options,
      stage_label:'创意方向',headline:'等你决定',findings:[]}}
  );
  assert.ok(!stage.includes('name="direction-choice"'));
  assert.ok(!stage.includes('data-act="approve"'));
  assert.ok(!stage.includes('pane-ask'));
});
test('hidden detail navigation keeps one selected surface as arrow keys move through available artifacts',()=>{
  const html=fs.readFileSync(new URL('../../studio/web/index.html',import.meta.url),'utf8');
  for(const [tab,panel] of [['plan','stage'],['assets','journey-assets'],['reference','journey-reference'],['storyboard','journey-storyboard'],['video','journey-video']]){
    assert.match(html,new RegExp(`id="journey-tab-${tab}"[^>]*aria-controls="${panel}"`));
    assert.match(html,new RegExp(`id="${panel}"[^>]*role="tabpanel"[^>]*aria-labelledby="journey-tab-${tab}"`));
  }
  assert.match(html,/id="journey-tab-script"[^>]*data-plan-stage="writing"[^>]*aria-controls="stage"/);
  const panels=Object.fromEntries(['stage','journey-assets','journey-reference','journey-storyboard','journey-video','project-stages']
    .map(id=>[id,{hidden:id!=='stage',attrs:{},setAttribute(name,value){this.attrs[name]=value;}}]));
  let focused='';
  const tabs=['plan','assets','reference','script','storyboard','video'].map(id=>({id:`journey-tab-${id}`,
    dataset:{journeyTab:id==='script'?'plan':id,...(id==='script'?{planStage:'writing'}:id==='plan'?{planStage:'direction'}:{})},
    attrs:{},hidden:id==='reference',tabIndex:id==='plan'?0:-1,
    setAttribute(name,value){this.attrs[name]=value;},focus(){focused=id;}}));
  globalThis.document={querySelectorAll:()=>tabs,querySelector:()=>null};
  globalThis.state={canvasTab:'plan',timeline:null,rail:[{stage:'direction'},{stage:'writing'}],activeStage:'direction',productionData:{drafts:[]}};
  const nodes={...panels,...Object.fromEntries(tabs.map(tab=>[tab.id,tab]))};
  globalThis.$=id=>nodes[id];
  globalThis.StudioProduction={canvasHtml:()=>'<article>当前成果</article>'};
  globalThis.renderCanvasMaterials=()=>{};
  globalThis.selectStage=stage=>{state.activeStage=stage;};
  let detailRenders=0;
  globalThis.renderPane=()=>{detailRenders++;};
  bindJourneyTabs();
  let prevented=false;
  tabs[0].onkeydown({key:'ArrowRight',preventDefault(){prevented=true;}});
  assert.equal(prevented,true);assert.equal(focused,'assets');
  assert.equal(tabs[1].tabIndex,0);assert.equal(tabs[0].tabIndex,-1);
  assert.equal(panels['journey-assets'].hidden,false);assert.equal(panels.stage.hidden,true);
  tabs[1].onkeydown({key:'ArrowRight',preventDefault(){}});
  assert.equal(focused,'script');assert.equal(panels.stage.hidden,false);
  assert.equal(state.activeStage,'writing');assert.equal(panels.stage.attrs['aria-labelledby'],'journey-tab-script');
  assert.equal(detailRenders,1);
  tabs[3].onkeydown({key:'End',preventDefault(){}});
  assert.equal(focused,'video');assert.equal(panels['journey-video'].hidden,false);
  assert.equal(panels['journey-video'].innerHTML,'<article>当前成果</article>');
});

test("pending ask_user renders as an interactive producer bubble in the conversation", () => {
  const interaction = {id: "q1", question: "说话方式选哪种？", message: "说话方式选哪种？\n\n1. 手部…",
    options: [{label: "手部"}, {label: "真人"}], recommended: 0};
  assert.equal(shouldShowQuestionInTurn({pending_clarification_id: "q1", reply: {text: interaction.message}}, interaction), true);
  assert.equal(shouldShowQuestionInTurn({reply: {text: interaction.message}}, interaction), false);
  assert.equal(shouldShowQuestionInTurn({pending_clarification_id: "q2"}, interaction), false);
  assert.equal(shouldShowQuestionInTurn({pending_clarification_id: "q1"}, null), false);
});
