import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
globalThis.escapeHtml=require('../../studio/web/md.js').escapeHtml;
// Shared gate labels live in stage.js (red line 12); browser loads stage first.
const stageMod=require('../../studio/web/stage.js');
globalThis.gateApproveLabel=stageMod.gateApproveLabel;
globalThis.gateRejectLabel=stageMod.gateRejectLabel;
const {questionCardHtml,journeyGateHtml,journeyStatus}=require('../../studio/web/journey.js');

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
test('formal decision retains the displayed fingerprint and states continuation',()=>{
  const html=journeyGateHtml({stage:'writing',version:3,artifact_sha256:'a'.repeat(64),passed:true},{});
  assert.ok(html.includes('data-version="3"'));
  assert.ok(html.includes('data-sha="'+'a'.repeat(64)+'"'));
  assert.ok(html.includes('确认方案，准备分镜'));
  assert.ok(!html.includes('第 3 版'));
});
test('execution completion cannot replace an unresolved user decision',()=>{
  assert.equal(journeyStatus({execution:{active:false}},{stage:'direction'},null),'选一个你想继续的方向');
  assert.equal(journeyStatus({execution:{active:true}},null,null),'团队正在处理');
});
test('journey and stage gates never share one radio group (P0-4)',()=>{
  const options=[{key:'A',recommended:false,label:'方向',return_note:''},{key:'B',recommended:true,label:'方向',return_note:''}];
  const journey=journeyGateHtml({stage:'direction',version:1,artifact_sha256:'b'.repeat(64),passed:true,options},{});
  assert.ok(journey.includes('name="direction-choice-journey"'));
  assert.ok(!/name="direction-choice"/.test(journey));
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
  assert.ok(!stage.includes('name="direction-choice-journey"'));
});
