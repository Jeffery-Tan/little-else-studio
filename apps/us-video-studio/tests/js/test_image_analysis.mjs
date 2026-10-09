import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const ui=require('../../studio/web/image-analysis.js');
const candidate={analysis_id:'a1',name:'bag.png',connection_name:'Fixture',model:'vision',status:'pending',
  findings:['Handle visible','Zipper visible'],limitations:['Size unknown'],estimated_usd:null};

test('native Omni setup transmits video and audio with a fixed model and explicit consent',()=>{
  const c={id:'bailian_omni',name:'百炼北京',suggested_model:'qwen3.8-omni-flash',native_video:true,
    base_url:'https://fixture.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
    pricing:{'qwen3.8-omni-flash':{input_cny_per_mtok:.8,output_cny_per_mtok:2.7}}};
  const html=ui.setup({name:'reference.mp4',kind:'video'},{connections:[],bailian_omni:c,video:{available:true}});
  assert.match(html,/视频及可用音轨/);
  assert.match(html,/readonly/);
  assert.match(html,/开始声画分析/);
  assert.doesNotMatch(html,/checked|不发送原视频|没有发送或听取/);
  assert.match(ui.costText(c,c.suggested_model,'video'),/输入 ¥0.8/);
  assert.match(ui.costText(c,c.suggested_model,'video'),/含思考/);
});

test('the selected MiMo video service takes precedence over the legacy observer',()=>{
  const c={id:'mimo',name:'MiMo <unsafe>',provider:'mimo',has_key:true,
    suggested_model:'mimo-v2.6-flash',native_video:true,base_url:'https://api.xiaomimimo.com/v1',pricing:{}};
  const html=ui.setup({name:'reference.mp4',kind:'video'},{connections:[],video_connection:c,
    bailian_omni:{id:'legacy',name:'百炼北京',native_video:true},video:{available:true}});
  assert.match(html,/name="connection_id"><option value="mimo"/);
  assert.match(html,/readonly value="mimo-v2.6-flash"/);
  assert.match(html,/视频及可用音轨发送至 MiMo &lt;unsafe&gt;/);
  assert.match(html,/默认 2 fps/);
  assert.doesNotMatch(html,/百炼北京|<unsafe>|checked/);
  const cost=ui.costText(c,c.suggested_model,'video');
  assert.match(cost,/费用将显示为待核实/);
  assert.match(cost,/8192 token（含思考）/);
  assert.doesNotMatch(cost,/undefined|¥undefined/);
  assert.match(ui.costText({...c,pricing:{[c.suggested_model]:{input_usd_per_mtok:.14,output_usd_per_mtok:.28}}},c.suggested_model,'video'),/输入 \$0.14/);
});

test('a selected MiMo video service without its key shows the exact gap and cannot fall back',()=>{
  const html=ui.setup({name:'reference.mp4',kind:'video'},{connections:[],
    video_connection:{id:'mimo',name:'MiMo',provider:'mimo',has_key:false,native_video:true},
    bailian_omni:{id:'legacy',name:'百炼北京',native_video:true},video:{available:true}});
  assert.match(html,/为所选 MiMo 服务填写 API Key/);
  assert.match(html,/不会自动改用其他服务/);
  assert.match(html,/data-model-settings/);
  assert.doesNotMatch(html,/百炼北京|<form|type="submit"/);
});

test('a new response with no selected native connection does not substitute its legacy field',()=>{
  const html=ui.setup({name:'reference.mp4',kind:'video'},{connections:[{id:'vision',name:'Vision'}],
    video_connection:null,bailian_omni:{id:'legacy',name:'百炼北京',native_video:true},video:{available:true}});
  assert.match(html,/开始视频画面分析/);
  assert.doesNotMatch(html,/百炼北京|开始声画分析/);
});

test('a MiMo connection in the generic video form only authorizes frames',()=>{
  const html=ui.setup({name:'reference.mp4',kind:'video'},{connections:[{id:'mimo',name:'MiMo',provider:'mimo'}],
    video_connection:null,video:{available:true}});
  assert.match(html,/value="mimo"/);
  assert.match(html,/开始视频画面分析/);
  assert.match(html,/不发送原视频、音频或项目对话/);
  assert.doesNotMatch(html,/开始声画分析|视频及可用音轨/);
});

test('native review presents audio scope and CNY instead of a false silent-frame report',()=>{
  const html=ui.review({...candidate,media_kind:'video',estimated_cny:.014,
    audiovisual:{duration_seconds:30,audio_sent:true,audio_ranges:[[0,30]]}});
  assert.match(html,/可用音轨/);
  assert.match(html,/¥0.014000/);
  assert.doesNotMatch(html,/没有发送或听取|实际分析 undefined|\$0.014/);
});

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
