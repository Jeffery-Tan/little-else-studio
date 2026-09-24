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
const {questionCardHtml,journeyGateHtml,journeyStatus,bindJourneyTabs,shouldShowQuestionInTurn}=require('../../studio/web/journey.js');

test('clarification supports recommendation and free text without implicit selection',()=>{
  const html=questionCardHtml({id:'abc',question:'<script>?</script>',options:[{label:'A',description:'one'},{label:'B'}],recommended:0});
  assert.ok(html.includes('推荐'));
  assert.ok(html.includes('data-question-choice'));
  assert.ok(!html.includes('<form'));
  assert.ok(html.includes('下方对话'));
  assert.ok(!html.includes(' checked'));
  assert.ok(!html.includes('<script>'));
  assert.ok(!html.includes('data-act="approve"'));
});
test('conversation directs the user to the single version-bound decision on the canvas',()=>{
  const html=journeyGateHtml({stage:'writing',version:3,artifact_sha256:'a'.repeat(64),passed:true},{});
  assert.ok(html.includes('data-view-plan'));
  assert.ok(html.includes('阅读并决定'));
  assert.ok(!html.includes('class="gate'));
  assert.ok(!html.includes('data-act="approve"'));
});
test('execution completion cannot replace an unresolved user decision',()=>{
  assert.equal(journeyStatus({execution:{active:false}},{stage:'direction'},null),'选一个你想继续的方向');
  assert.equal(journeyStatus({execution:{active:true}},null,null),'团队正在处理');
});
test('only the canvas exposes a version-bound choice and submit action',()=>{
  const options=[{key:'A',recommended:false,label:'方向',return_note:''},{key:'B',recommended:true,label:'方向',return_note:''}];
  const journey=journeyGateHtml({stage:'direction',version:1,artifact_sha256:'b'.repeat(64),passed:true,options},{});
  assert.ok(!journey.includes('type="radio"'));
  assert.ok(!journey.includes('data-act="approve"'));
  const { blocksHtml }=require('../../studio/web/blocks.js');
  globalThis.blocksHtml=blocksHtml;
  globalThis.renderMarkdown=require('../../studio/web/md.js').renderMarkdown;
  const { stageHtml }=require('../../studio/web/stage.js');
  const stage=stageHtml(
    {stage:'direction',label:'创意方向',has_doc:true,version:1,blocks:[],review:null,team_issues:[]},
    {pending:{stage:'direction',version:1,artifact_sha256:'b'.repeat(64),passed:true,options,
      stage_label:'创意方向',headline:'等你决定',findings:[]}}
  );
  assert.ok(stage.includes('name="direction-choice"'));
  assert.ok(stage.includes('data-act="approve"'));
});
test('outcome tabs have linked panels and arrow keys move the single tab stop',()=>{
  const html=fs.readFileSync(new URL('../../studio/web/index.html',import.meta.url),'utf8');
  for(const [tab,panel] of [['plan','stage'],['storyboard','journey-storyboard'],['video','journey-video']]){
    assert.match(html,new RegExp(`id="journey-tab-${tab}"[^>]*aria-controls="${panel}"`));
    assert.match(html,new RegExp(`id="${panel}"[^>]*role="tabpanel"[^>]*aria-labelledby="journey-tab-${tab}"`));
  }
  const panels=Object.fromEntries(['stage','journey-storyboard','journey-video'].map(id=>[id,{hidden:id!=='stage'}]));
  let focused='',mounted='';
  const tabs=['plan','storyboard','video'].map(id=>({dataset:{journeyTab:id},attrs:{},tabIndex:id==='plan'?0:-1,
    setAttribute(name,value){this.attrs[name]=value;},focus(){focused=id;}}));
  globalThis.document={querySelectorAll:()=>tabs,querySelector:()=>null};
  globalThis.state={canvasTab:'plan',timeline:null,rail:[]};
  globalThis.$=id=>panels[id];
  globalThis.openProduction=({mount})=>{mounted=Object.entries(panels).find(([,value])=>value===mount)?.[0];};
  globalThis.selectStage=()=>{};
  bindJourneyTabs();
  let prevented=false;
  tabs[0].onkeydown({key:'ArrowRight',preventDefault(){prevented=true;}});
  assert.equal(prevented,true);assert.equal(focused,'storyboard');assert.equal(mounted,'journey-storyboard');
  assert.equal(tabs[1].tabIndex,0);assert.equal(tabs[0].tabIndex,-1);
  assert.equal(panels['journey-storyboard'].hidden,false);assert.equal(panels.stage.hidden,true);
  tabs[1].onkeydown({key:'End',preventDefault(){}});
  assert.equal(focused,'video');assert.equal(panels['journey-video'].hidden,false);
});

test("pending ask_user renders as an interactive producer bubble in the conversation", () => {
  const interaction = {id: "q1", question: "说话方式选哪种？", message: "说话方式选哪种？\n\n1. 手部…",
    options: [{label: "手部"}, {label: "真人"}], recommended: 0};
  assert.equal(shouldShowQuestionInTurn({reply: {text: interaction.message}}, interaction), true);
  assert.equal(shouldShowQuestionInTurn({reply: {text: "普通回复"}}, interaction), false);
  assert.equal(shouldShowQuestionInTurn({reply: null}, interaction), false);
  assert.equal(shouldShowQuestionInTurn({reply: {text: "x"}}, null), false);
});
