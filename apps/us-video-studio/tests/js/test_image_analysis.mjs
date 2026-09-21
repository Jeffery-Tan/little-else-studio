import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const ui=require('../../studio/web/image-analysis.js');
const candidate={analysis_id:'a1',name:'bag.png',connection_name:'Fixture',model:'vision',status:'pending',
  findings:['Handle visible','Zipper visible'],limitations:['Size unknown'],estimated_usd:null};

test('a current pending analysis replaces the uploaded label without changing adopted evidence',()=>{
  const data={materials:[{name:'bag.png',sha256:'same',observations:[],status_label:'uploaded'}]};
  const result=ui.enrich(data,{candidates:[{...candidate,sha256:'same'}]});
  assert.equal(result.materials[0].status_label,'分析完成 · 待你确认');
  assert.deepEqual(result.materials[0].observations,[]);
  assert.equal(data.materials[0].status_label,'uploaded');
  assert.equal(ui.enrich(data,{candidates:[{...candidate,sha256:'old'}]}).materials[0].status_label,'uploaded');
});

test('image connection setup is explicit about capabilities, transmission and unknown cost',()=>{
  const html=ui.setup({name:'bag.png'},{connections:[{id:'api',name:'Vision API'}]});
  assert.match(html,/name="consent" type="checkbox" required/);
  assert.match(html,/不会自动进入创作依据/);
  assert.match(html,/不代表所有模型都能看图/);
  assert.doesNotMatch(html,/checked/);
  assert.match(ui.costText({base_url:'https://fixture.invalid',pricing:{}},'vision'),/费用将显示为待核实/);
});
test('unavailable connections lead to setup, not a fake analyze button',()=>{
  const html=ui.setup({name:'bag.png'},{connections:[]});
  assert.match(html,/打开模型设置/);
  assert.match(html,/尚未接入此图片分析入口/);
  assert.doesNotMatch(html,/type="submit"/);
});
test('model output and provider names render as text',()=>{
  const c={...candidate,connection_name:'<script>x</script>',findings:['<img src=x onerror=bad>'],limitations:['<svg onload=bad>']};
  const html=ui.review(c)+ui.candidateCard(c);
  assert.doesNotMatch(html,/<script>|<img src=x|<svg/);
  assert.match(html,/&lt;img/);
});
test('no finding is adopted by default; historical accepted selections remain visible',()=>{
  const html=ui.review(candidate);
  assert.match(html,/type="submit" class="primary" disabled/);
  assert.doesNotMatch(html,/checked/);
  const accepted=ui.review({...candidate,status:'accepted',selected_findings:[1]});
  assert.match(accepted,/value="1" checked/);
  assert.doesNotMatch(accepted,/value="0" checked|type="submit"/);
  assert.doesNotMatch(ui.review({...candidate,status:'stale'}),/type="submit"/);
  const card=ui.candidateCard({...candidate,status:'accepted',selected_findings:[1]});
  assert.match(card,/Zipper visible/);
  assert.doesNotMatch(card,/Handle visible/);
});

test('video setup describes actual transmission and cannot pretend it hears the soundtrack',()=>{
  const html=ui.setup({name:'reference.mp4',kind:'video'},{connections:[{id:'api',name:'Vision'}],video:{available:true}});
  assert.match(html,/最多 12/);
  assert.match(html,/不发送原视频、音频或项目对话/);
  assert.match(html,/开始视频画面分析/);
  assert.doesNotMatch(html,/checked/);
  const unavailable=ui.setup({kind:'video'},{connections:[],video:{available:false}});
  assert.match(unavailable,/视频观察组件尚未就绪/);
  assert.doesNotMatch(unavailable,/<form|type="submit"/);
});

test('video review exposes observation coverage and bounded reference hypotheses as escaped text',()=>{
  const html=ui.review({...candidate,media_kind:'video',sampling:{frames:[{},{}],duration_seconds:4,audio_track_present:true},
    reference_methods:[{supports:[0,1],mechanism:'<script>unsafe</script>',non_transferable:'<img src=x>',test_change:'Compare two scenes'}]});
  assert.match(html,/检测到音轨，但没有发送或听取/);
  assert.match(html,/只有全部依据被采用/);
  assert.match(html,/&lt;script&gt;/);
  assert.doesNotMatch(html,/<script>|<img src=x/);
});
