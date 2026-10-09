"""Real disposable FastAPI save receipts consumed by the shipped JS dialog.

Only the browser DOM/HTTP adapter is synthetic. Drafts and Pydantic response
defaults come from actual temporary Store/Pipeline/FastAPI routes, with no
model/media calls, real credentials, or real project record changes.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from test_production import BASE, RUN, adopt_shot_count, scene, workspace


APP = Path(__file__).resolve().parents[1]
TEXT = 'Synthetic manual composition revision for an exact receipt.'
REQUEST = 'frontend-receipt-contract'


def node_json(script, data):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the shipped frontend contract')
    result = subprocess.run([node, '--input-type=module', '-e', script],
                            input=json.dumps(data), text=True, capture_output=True,
                            cwd=APP, timeout=30, check=False, encoding='utf-8')
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


COMPOSE = r"""
import fs from 'node:fs';
import {createRequire} from 'node:module';
const U=createRequire(import.meta.url)('./studio/web/production.js');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const values=U.editValues(input.prior);values.prompt=input.text;
process.stdout.write(JSON.stringify(U.submission(values,input.before,input.request_id)));
"""


DIALOG = r"""
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {El,loadApp,makeDom,webSource} from './tests/js/dom-fixture.mjs';
const input=JSON.parse(fs.readFileSync(0,'utf8'));
Object.defineProperty(El.prototype,'elements',{get(){return {namedItem:name=>this.querySelector(`[name="${name}"]`)};}});
class LocalFormData {
  constructor(form){this.rows=form.querySelectorAll('[name]').map(node=>[node.getAttribute('name'),node.value]);}
  [Symbol.iterator](){return this.rows[Symbol.iterator]();}
}
const dom=makeDom({body:webSource('index.html').match(/<body[^>]*>([\s\S]*?)<\/body>/)[1]});
for(const node of dom.document.querySelectorAll('[id]'))dom.document.__ids.set(node.getAttribute('id'),node);
const create=dom.document.createElement;
dom.document.createElement=tag=>{const node=create(tag);if(tag==='dialog'){
  node.showModal=()=>node.open=true;node.close=()=>node.open=false;}return node;};
let saved=false,postReads=0;
const app=loadApp({dom,routes:{'/production/drafts':request=>{
  assert.deepEqual(request.body,input.body,'The real form must send exactly the body already saved by FastAPI');
  saved=true;return input.receipt;
},'/production':()=>{
  if(!saved)return input.before;
  if(input.failed_read&&++postReads===1)throw new Error('Failed to fetch');
  return input.after;
},'/api/learning':{cases:[]}},globals:{
  FormData:LocalFormData,crypto:{randomUUID:()=>input.request_id},
  initStudioNavigation(){},setStudioView(){},renderProjects(){},loadInspiration(){},
  setTimeout:()=>0,clearTimeout(){},requestAnimationFrame:fn=>fn(),scrollDown(){},
  __submitJob:()=>{throw new Error('Free draft saving cannot start a model Job');},
}});
vm.runInContext(webSource('production.js'),app.ctx,{filename:'production.js'});
// This contract exercises the production dialog and its actual FastAPI save
// receipt. Whole-page timeline rendering is a separate boundary; acknowledge
// it explicitly rather than letting an absent fixture route simulate failure.
let canonicalRefreshes=0;
app.ctx.refreshTimeline=async()=>{canonicalRefreshes++;};
const flush=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));};
await flush();app.ctx.contractRun=input.run_id;
vm.runInContext('state.runId=contractRun;state.currentJob=null;state.rail=[];',app.ctx);
await app.ctx.openProduction({kind:'image',focusDraft:input.prior.draft_id});
const dialog=app.document.querySelector('.production-dialog'),body=dialog.querySelector('[data-body]');
assert.equal(body.querySelector('.production-record-shot h3').textContent,'首帧 02');
body.listeners.click({target:body.querySelector('[data-edit-input]'),preventDefault(){}});
const form=body.querySelector('form');form.querySelector('textarea').value=input.text;
form.oninput({target:form.querySelector('textarea')});form.onsubmit({preventDefault(){}});await flush();
if(input.failed_read){
  const accepted=JSON.parse(app.storage.getItem(`studio-production:${input.run_id}`));
  assert.equal(accepted.pending,null);assert.equal(accepted.values.supersedes,input.receipt.draft_id);
  assert.equal(accepted.values.prompt,input.text);
  assert.match(dialog.querySelector('[data-error]').textContent,/制作方案已保存，作品记录暂未读取/);
  assert.doesNotMatch(dialog.textContent,/Failed to fetch|上次请求结果待确认|核实上次请求/);
  assert.equal(body.querySelector('[data-save]'),null);assert.equal(body.querySelector('[data-generate]'),null);
  assert.equal(app.observed.filter(row=>row.method==='POST').length,1);
  assert.equal(canonicalRefreshes,0,'The workspace must not refresh before the saved draft has been verified');
  body.querySelector('[data-load]').onclick();await flush();
}
assert.equal(dialog.querySelector('[data-error]').textContent,'');
assert.equal(body.querySelector('[data-draft-id]').dataset.draftId,input.receipt.draft_id);
assert.equal(body.querySelector('.production-record-shot h3').textContent,'首帧 02');
assert.equal(body.querySelector('textarea').value,input.text);
assert.equal(body.querySelector('[data-save]').disabled,false);
const storage=JSON.parse(app.storage.getItem(`studio-production:${input.run_id}`));
assert.equal(storage.pending,null);assert.equal(storage.values.supersedes,input.receipt.draft_id);
assert.equal(storage.values.shot_id,input.prior.shot_id);
assert.equal(app.observed.filter(row=>row.method==='POST').length,1);
assert.ok(app.observed.every(row=>!row.path.includes('/generate')&&!row.path.includes('/jobs')));
assert.equal(canonicalRefreshes,1,'The verified saved draft must refresh the workspace once');
process.stdout.write(JSON.stringify({accepted:true,focused_id:input.receipt.draft_id,
  next_supersedes:storage.values.supersedes,title:body.querySelector('.production-record-shot h3').textContent}));
"""


@pytest.mark.parametrize('failed_read', [False, True])
def test_actual_fastapi_optional_reference_defaults_reach_exact_saved_dialog(scene, failed_read):
    http, store, _, reviewer, images, videos, _ = scene
    adopt_shot_count(scene, 4)
    data = workspace(scene)
    source = next(row for row in data['sources'] if row['ready'])
    material = next(row for row in data['materials'] if row['name'] == 'reference.png')
    reference = {'name':material['name'], 'sha256':material['sha256'], 'purpose':'product'}
    originals = []
    for index, shot in enumerate(data['targets']['image']['shots'], 1):
        response = http.post(BASE + '/drafts', json={
            'request_id':f'actual-original-{index}', 'kind':'image', 'prompt':f'Synthetic shot {index}.',
            'model':'codex-image', 'source_version':source['version'], 'source_sha256':source['artifact_sha256'],
            'shot_id':shot['shot_id'], 'references':[reference]})
        assert response.status_code == 200, response.text
        originals.append(response.json())
    second = originals[1]
    response = http.post(BASE + '/drafts', json={
        'request_id':'actual-replacement-at-position-5', 'kind':'image', 'prompt':'Synthetic fifth input for shot 2.',
        'model':'codex-image', 'source_version':source['version'], 'source_sha256':source['artifact_sha256'],
        'shot_id':second['shot_id'], 'supersedes':second['draft_id'], 'references':[reference]})
    assert response.status_code == 200, response.text
    before = workspace(scene)
    prior = next(row for row in before['drafts'] if row['draft_id'] == response.json()['draft_id'])
    assert before['drafts'].index(prior) == 4
    assert len(before['targets']['image']['shots']) == 4
    approvals = store.list_approvals(RUN)

    context = {'run_id':RUN, 'before':before, 'prior':prior, 'text':TEXT, 'request_id':REQUEST}
    body = node_json(COMPOSE, context)
    assert 'design_id' not in body['references'][0]
    response = http.post(BASE + '/drafts', json=body)
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert receipt['request_payload']['references'][0]['design_id'] is None
    assert 'design_id' not in receipt['references'][0]
    assert receipt['draft_id'] == body['request_id']
    assert receipt['source_sha256'] == body['source_sha256']
    assert receipt['shot_id'] == body['shot_id'] and receipt['supersedes'] == prior['draft_id']
    after = workspace(scene)
    projected = next(row for row in after['drafts'] if row['draft_id'] == receipt['draft_id'])
    assert projected['is_current'] and projected['source_ready']
    assert projected['draft_sha256'] == receipt['draft_sha256']

    result = node_json(DIALOG, {**context, 'body':body, 'receipt':receipt, 'after':after,
                               'failed_read':failed_read})
    assert result == {'accepted':True, 'focused_id':receipt['draft_id'],
                      'next_supersedes':receipt['draft_id'], 'title':'首帧 02'}
    replay = http.post(BASE + '/drafts', json=body)
    assert replay.status_code == 200 and replay.json() == receipt
    assert not reviewer.calls and not images.calls and not videos.calls
    assert store.list_approvals(RUN) == approvals
    assert not http.app.state.jobs.ledger.entries(RUN)
    assert sum(row.get('kind') == 'production_draft' and row['draft']['draft_id'] == REQUEST
               for row in store.read_events(RUN)) == 1
