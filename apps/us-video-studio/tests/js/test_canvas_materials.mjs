import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {loadApp, makeDom, webSource} from './dom-fixture.mjs';

test('the central materials view shows only recorded files and opens the original', () => {
  const app = loadApp({dom: makeDom({ids: [
    'journey-assets', 'journey-tab-assets', 'viewer', 'viewer-name', 'viewer-body',
  ]})});
  app.ctx.__materials = [
    {name: 'bag front.jpg', kind: 'image', bytes: 2048, usable: true},
    {name: 'unavailable.mp4', kind: 'video', bytes: 0, usable: false},
  ];
  vm.runInContext('state.runId="preview"; state.materials=globalThis.__materials; renderCanvasMaterials()', app.ctx);

  const host = app.document.getElementById('journey-assets');
  const tiles = host.querySelectorAll('.tile');
  assert.equal(tiles.length, 2);
  assert.match(host.textContent, /bag front.jpg/);
  assert.match(host.textContent, /unavailable.mp4/);
  assert.equal(app.document.getElementById('journey-tab-assets').textContent, '素材 1');
  const preview = tiles[0].querySelector('.tile-preview');
  assert.equal(preview.tagName, 'BUTTON');
  assert.equal(preview.getAttribute('aria-label'), '查看素材：bag front.jpg');
  assert.equal(tiles[1].getAttribute('role'), null);

  preview.onclick({target:preview});
  assert.equal(app.document.getElementById('viewer').hidden, false);
  assert.match(app.document.getElementById('viewer-body').querySelector('img').src,
    /\/api\/runs\/preview\/materials\/bag%20front\.jpg$/);
});

test('video cards expose analysis beside preview without starting a model call', async () => {
  const app=loadApp({globals:{openMaterialAnalysis:(...args)=>app.opened=args},
    dom:makeDom({ids:['journey-assets','journey-tab-assets']})});
  app.ctx.__materials=[
    {name:'reference.mp4',kind:'video',bytes:2048,usable:true,sha256:'source'},
    {name:'broken.mp4',kind:'video',usable:false},
    {name:'product.png',kind:'image',usable:true},
  ];
  vm.runInContext('state.runId="preview";state.materials=__materials;renderCanvasMaterials()',app.ctx);
  const host=app.document.getElementById('journey-assets');
  const actions=host.querySelectorAll('.tile-analysis');
  assert.equal(actions.length,1);
  assert.equal(actions[0].textContent,'分析视频');
  assert.equal(actions[0].getAttribute('aria-label'),'分析视频：reference.mp4');
  assert.equal(actions[0].parentNode.querySelector('.tile-preview').contains(actions[0]),false);
  assert.equal(app.opened,undefined);
  await actions[0].onclick();
  assert.equal(app.opened[0],'preview');
  assert.equal(app.opened[1].sha256,'source');
});

test('video preview exposes the same analysis entry and image preview hides it', async () => {
  const app=loadApp({globals:{openMaterialAnalysis:(...args)=>app.opened=args},
    dom:makeDom({ids:['viewer','viewer-name','viewer-body','viewer-analysis']})});
  vm.runInContext('state.runId="preview";openViewer({name:"reference.mp4",kind:"video",usable:true,bytes:1})',app.ctx);
  const action=app.document.getElementById('viewer-analysis');
  assert.equal(action.hidden,false);
  await action.onclick();
  assert.equal(app.document.getElementById('viewer').hidden,true);
  assert.equal(app.opened[1].name,'reference.mp4');
  vm.runInContext('openViewer({name:"product.png",kind:"image",usable:true,bytes:1})',app.ctx);
  assert.equal(action.hidden,true);
});

function analysisApp({candidate=null,reference=null,material={name:'reference.mp4',kind:'video',sha256:'source'},routes={}}={}) {
  const app=loadApp({routes:{
    '/material-observations':{materials:[material],expected_manifest_sha256:'manifest'},
    '/image-analysis':{candidates:candidate?[candidate]:[],connections:[]},
    '/reference-videos':{tasks:reference?[reference]:[]},
    ...routes,
  },globals:{openReferenceVideo:(...args)=>app.opened={kind:'reference',args}},
    dom:makeDom({ids:['toast']})});
  vm.runInContext(webSource('image-workspace.js'),app.ctx);
  vm.runInContext('openImageSetup=(...args)=>globalThis.__opened={kind:"setup",args};openImageReview=(...args)=>globalThis.__opened={kind:"review",args};state.runId="preview";',app.ctx);
  app.ctx.__material={name:'reference.mp4',kind:'video',sha256:'source',usable:true};
  app.ctx.__opener={disabled:false};
  app.open=()=>vm.runInContext('openMaterialAnalysis("preview",__material,__opener)',app.ctx);
  return app;
}

test('analysis entry continues the original failed reference instead of creating a second analysis',async()=>{
  const reference={reference_id:'existing',intent:'analyze',name:'reference.mp4',sha256:'source',status:'downloaded'};
  const app=analysisApp({reference});
  await app.open();
  assert.equal(app.opened.kind,'reference');
  assert.equal(app.opened.args[1].reference_id,'existing');
  assert.equal(app.ctx.__opened,undefined);
  assert.ok(app.observed.every(call=>call.method==='GET'));
  assert.equal(app.ctx.__opener.disabled,false);
});

test('valid existing observations open for review rather than repeat analysis',async()=>{
  const candidate={name:'reference.mp4',sha256:'source',status:'pending',analysis_id:'saved'};
  const app=analysisApp({candidate});
  await app.open();
  assert.equal(app.ctx.__opened.kind,'review');
  assert.equal(app.ctx.__opened.args[2].analysis_id,'saved');
  assert.ok(app.observed.every(call=>call.method==='GET'));
});

test('a new video opens existing setup; stale observations and recreation do not authorize a task',async()=>{
  const app=analysisApp({candidate:{name:'reference.mp4',sha256:'source',status:'stale'},
    reference:{intent:'recreate',name:'reference.mp4',sha256:'source',status:'downloaded'}});
  await app.open();
  assert.equal(app.ctx.__opened.kind,'setup');
  assert.equal(app.ctx.__opened.args[1].sha256,'source');
  assert.equal(app.opened,undefined);
  assert.ok(app.observed.every(call=>call.method==='GET'));
});

test('a changed video cannot open analysis against the old bytes',async()=>{
  const app=analysisApp({material:{name:'reference.mp4',kind:'video',sha256:'changed'}});
  await app.open();
  assert.equal(app.ctx.__opened,undefined);
  assert.equal(app.opened,undefined);
  assert.equal(app.ctx.__opener.disabled,false);
});

test('project switches and tasks starting during reads prevent opening the old analysis',async()=>{
  for(const change of ['state.runId="other"','state.busy=true']){
    let app;
    app=analysisApp({routes:{'/reference-videos':()=>{
      vm.runInContext(change,app.ctx);return {tasks:[]};
    }}});
    await app.open();
    assert.equal(app.ctx.__opened,undefined);
    assert.equal(app.opened,undefined);
    assert.equal(app.ctx.__opener.disabled,false);
  }
});
