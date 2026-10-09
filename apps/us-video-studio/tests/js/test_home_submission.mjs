/* Home → implicit project → uploads → real runtime submission. Only HTTP and
 * unrelated rendering are replaced; identities, draft mutation, project
 * selection, receipt recovery and admission run through shipped controllers. */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {File} from 'node:buffer';
import {webcrypto} from 'node:crypto';
import {loadApp, makeDom, webSource} from './dom-fixture.mjs';

const response=(body,status=200)=>({ok:status>=200&&status<300,status,statusText:'Offline fixture',json:async()=>body});
const file=(name='reference.mp4',content='reference')=>new File([content],name,{type:name.endsWith('.mp4')?'video/mp4':'image/png'});
const server=()=>({runs:[],calls:[],created:new Map(),materials:new Map(),accepted:new Map(),latest:new Map()});

async function page({data=server(),storage,fault}={}){
  const dom=makeDom({body:webSource('index.html').match(/<body[^>]*>([\s\S]*?)<\/body>/)[1]});
  const app=loadApp({dom,runtime:'real',...(storage?{storage}:{}),globals:{
    crypto:webcrypto,File,FormData,setTimeout:()=>0,clearTimeout:()=>{},
    setInterval:()=>0,clearInterval:()=>{},renderProjects:()=>{},
    projectOptionLabel:run=>run.title,setStudioView:()=>{},setProjectFocus:()=>{},
    setConversationOpen:()=>{},toggleMaterials:()=>{},
  }});
  for(let i=0;i<4;i++)await new Promise(resolve=>setImmediate(resolve));
  const run=code=>vm.runInContext(code,app.ctx);
  run(webSource('conversation-input.js'));
  run(`refreshTimeline=async()=>{};loadDraft=()=>{};initJourney();`);
  app.ctx.fetch=async(url,options={})=>{
    const method=options.method||'GET';
    const body=options.body instanceof FormData?{name:options.body.get('file').name}
      :options.body?JSON.parse(options.body):null;
    const call={url:String(url),method,body};data.calls.push(call);
    const intercepted=await fault?.(call,data);
    if(intercepted)return intercepted;
    if(url==='/api/runs')return response(data.runs);
    if(url==='/api/conversations'){
      let project=data.created.get(body.request_id);
      if(!project){
        project={run_id:`created-${data.created.size+1}`,title:'Offline project',brief:body.text,kind:'creative'};
        data.created.set(body.request_id,project);data.runs.push(project);
      }
      assert.equal(project.brief,body.text,'creation retry must retain its original text');
      return response(project);
    }
    const material=String(url).match(/^\/api\/runs\/([^/]+)\/materials\?retry_sha256=([a-f0-9]{64})$/);
    if(material){
      const key=`${material[1]}:${body.name}`;
      assert.ok(!data.materials.has(key)||data.materials.get(key)===material[2],'same-name bytes cannot be replaced');
      data.materials.set(key,material[2]);return response({name:body.name});
    }
    const job=String(url).match(/^\/api\/runs\/([^/]+)\/jobs\?enqueue=true$/);
    if(job&&method==='POST'){
      const accepted={job_id:`job-${body.request_id}`,request_id:body.request_id,
        job_kind:'conversation',status:'succeeded',events:[]};
      data.accepted.set(body.request_id,accepted);data.latest.set(job[1],accepted);return response(accepted);
    }
    const latest=String(url).match(/^\/api\/runs\/([^/]+)\/jobs\/latest$/);
    if(latest)return response(data.latest.get(latest[1])||null);
    if(String(url).endsWith('/recovery'))return response({revision:'offline',can_continue:true,open_attempts:[]});
    const receipt=String(url).match(/\/request-receipts\/([^?]+)\?kind=job$/);
    if(receipt)return response({kind:'job',request_id:receipt[1],received:data.accepted.has(receipt[1]),job:data.accepted.get(receipt[1])||null});
    throw Error(`Unexpected boundary: ${method} ${url}`);
  };
  const select=(id,files)=>{const picker=app.document.getElementById(id);picker.files=files;picker.onchange();};
  const text=value=>{const input=app.document.getElementById('start-prompt');input.value=value;input.oninput();};
  const submit=()=>app.document.getElementById('start-conversation').onsubmit({preventDefault(){}});
  return {...app,run,data,select,text,submit,status:()=>app.document.getElementById('start-status').textContent};
}
const posts=(data,suffix)=>data.calls.filter(call=>call.method==='POST'&&call.url.split('?')[0].endsWith(suffix));

test('home preflight remains editable and creates no identity or project for invalid attachments',async()=>{
  for(const [files,message] of [
    [[file(),file()],/重复附上/],
    [Array.from({length:25},(_,i)=>file(`image-${i}.png`)),/24/],
    [[{name:'large.mp4',type:'video/mp4',size:64*1024*1024+1}],/64 MB/],
    [[file('CON.mp4')],/文件名/],
  ]){
    const p=await page();p.select('start-files',files);p.text('Start with these files');await p.submit();
    assert.match(p.status(),message);assert.equal(p.data.calls.length,0);
    assert.equal(p.storage.getItem('studio-first-message'),null);
    assert.ok(p.document.getElementById('start-attachments').querySelectorAll('button').every(button=>!button.disabled));
  }
  const p=await page();p.select('start-reference-files',[file(),file()]);p.text('参考拆解');await p.submit();
  p.document.getElementById('start-attachments').querySelectorAll('button')[1].onclick();await p.submit();
  assert.equal(posts(p.data,'/api/conversations').length,1);
  assert.equal(posts(p.data,'/jobs')[0].body.attachments.length,1);
});

test('confirmed first-message rejection allows changes and reload, reuses its project/upload, then a fresh project starts clean',async()=>{
  let refuse=true;
  const p=await page({fault:call=>{
    if(refuse&&call.method==='POST'&&call.url.endsWith('/jobs?enqueue=true')){refuse=false;return response({detail:'Fixture job refused'},409);}
  }});
  p.select('start-reference-files',[file()]);p.text('Original request');await p.submit();
  const rejected=posts(p.data,'/jobs')[0];
  const saved=JSON.parse(p.storage.getItem('studio-first-message'));
  assert.equal(saved.editable,true);assert.equal(saved.run_id,'created-1');
  p.text('Corrected request');
  const use=p.document.getElementById('start-attachments').querySelector('select');use.value='product';use.onchange();
  const fresh=await page({data:p.data,storage:p.storage});
  assert.equal(fresh.document.getElementById('start-prompt').value,'Corrected request');
  assert.equal(fresh.document.getElementById('start-attachments').querySelector('select').value,'product');
  await fresh.submit();
  const corrected=posts(p.data,'/jobs')[1];
  assert.notEqual(corrected.body.request_id,rejected.body.request_id);
  assert.equal(corrected.url,rejected.url);assert.match(corrected.body.text,/^Corrected request/);
  assert.deepEqual(corrected.body.attachments,[{name:'reference.mp4',purpose:'product'}]);
  assert.equal(posts(p.data,'/api/conversations').length,1);
  assert.equal(p.data.calls.filter(call=>call.url.includes('/materials?')).length,1);
  assert.equal(fresh.storage.getItem('studio-first-message'),null);
  fresh.select('start-reference-files',[file()]);fresh.text('Another project');await fresh.submit();
  assert.equal(posts(p.data,'/api/conversations').length,2);
  assert.equal(p.data.calls.filter(call=>call.url.includes('/materials?')).length,2,
    'a previous project upload is not borrowed by a new project');
  assert.match(posts(p.data,'/jobs')[2].url,/created-2\/jobs\?enqueue=true$/);
});

test('a definite partial-upload rejection can remove the bad attachment and send under a new identity without uploading confirmed files',async()=>{
  const p=await page({fault:call=>call.url.includes('/materials?')&&call.body.name==='bad.png'
    ?response({detail:'Fixture upload rejected'},400):null});
  p.select('start-files',[file('product.png'),file('bad.png')]);p.text('Original files');await p.submit();
  const before=JSON.parse(p.storage.getItem('studio-first-message'));
  assert.equal(before.editable,true);assert.equal(posts(p.data,'/jobs').length,0);
  p.document.getElementById('start-attachments').querySelectorAll('button')[1].onclick();
  p.text('Use the confirmed product image');await p.submit();
  assert.notEqual(posts(p.data,'/jobs')[0].body.request_id,before.request_id);
  assert.deepEqual(posts(p.data,'/jobs')[0].body.attachments,[{name:'product.png',purpose:null}]);
  assert.equal(posts(p.data,'/api/conversations').length,1);
  assert.equal(p.data.calls.filter(call=>call.url.includes('/materials?')&&call.body.name==='product.png').length,1);
});

test('unknown project creation keeps the original identity and payload across reload and rejects edits',async()=>{
  let lose=true;
  const p=await page({fault:call=>{
    if(lose&&call.url==='/api/conversations'){lose=false;throw new TypeError('lost creation response');}
  }});
  p.select('start-reference-files',[file()]);p.text('Original unknown request');await p.submit();
  const saved=JSON.parse(p.storage.getItem('studio-first-message'));
  assert.equal(saved.editable,undefined);
  p.text('Changed while unknown');await p.submit();
  assert.equal(posts(p.data,'/api/conversations').length,1);assert.match(p.status(),/未确认/);
  const fresh=await page({data:p.data,storage:p.storage});
  assert.equal(fresh.document.getElementById('start-prompt').value,'Original unknown request');
  assert.equal(fresh.document.getElementById('start-attachments').querySelector('button').disabled,true);
  fresh.select('start-reference-files',[file()]);await fresh.submit();
  assert.deepEqual(posts(p.data,'/api/conversations').map(call=>call.body),[
    {request_id:saved.request_id,text:saved.text},{request_id:saved.request_id,text:saved.text},
  ]);
  assert.equal(posts(p.data,'/jobs')[0].body.request_id,saved.request_id);
});

test('unknown partial upload retains the same frozen request and confirmed files through refresh and reselection',async()=>{
  let lose=true;
  const p=await page({fault:call=>{
    if(lose&&call.url.includes('/materials?')&&call.body.name==='reference.mp4'){lose=false;throw new TypeError('lost upload response');}
  }});
  p.select('start-files',[file('product.png')]);p.select('start-reference-files',[file()]);p.text('Unknown partial upload');await p.submit();
  const saved=JSON.parse(p.storage.getItem('studio-first-message'));
  assert.deepEqual(saved.uploaded,[0]);assert.equal(saved.editable,undefined);
  const fresh=await page({data:p.data,storage:p.storage});
  fresh.select('start-reference-files',[file()]);await fresh.submit();
  assert.equal(posts(p.data,'/jobs')[0].body.request_id,saved.request_id);
  assert.equal(posts(p.data,'/api/conversations').length,1);
  assert.equal(p.data.calls.filter(call=>call.url.includes('/materials?')&&call.body.name==='product.png').length,1);
  const attempts=p.data.calls.filter(call=>call.url.includes('/materials?')&&call.body.name==='reference.mp4');
  assert.equal(attempts.length,2);assert.equal(attempts[0].url,attempts[1].url);
});

test('unknown accepted home job belongs to runtime receipt recovery after refresh, without another create, upload or job',async()=>{
  const p=await page({fault:(call,data)=>{
    if(call.method==='POST'&&call.url.endsWith('/jobs?enqueue=true')){
      const job={job_id:'accepted-unknown',request_id:call.body.request_id,job_kind:'conversation',status:'succeeded',events:[]};
      data.accepted.set(call.body.request_id,job);data.latest.set('created-1',job);
      throw new TypeError('lost accepted job response');
    }
  }});
  p.select('start-reference-files',[file()]);p.text('Accepted with a lost response');await p.submit();
  const original=posts(p.data,'/jobs')[0].body;
  assert.equal(p.storage.getItem('studio-first-message'),null);
  assert.deepEqual(JSON.parse(p.storage.getItem('studio-job-pending:created-1')),original);
  const fresh=await page({data:p.data,storage:p.storage});
  await fresh.run(`selectRun('created-1')`);
  assert.equal(fresh.storage.getItem('studio-job-pending:created-1'),null);
  assert.equal(posts(p.data,'/api/conversations').length,1);
  assert.equal(posts(p.data,'/jobs').length,1);
  assert.equal(p.data.calls.filter(call=>call.url.includes('/materials?')).length,1);
});

test('a definitely rejected project creation can be corrected with a new creation identity',async()=>{
  let reject=true;
  const p=await page({fault:call=>{
    if(reject&&call.url==='/api/conversations'){reject=false;return response({detail:'Fixture creation rejected'},422);}
  }});
  p.text('Rejected initial text');await p.submit();
  const original=posts(p.data,'/api/conversations')[0].body;
  assert.equal(JSON.parse(p.storage.getItem('studio-first-message')).editable,true);
  p.text('Corrected initial text');await p.submit();
  const corrected=posts(p.data,'/api/conversations')[1].body;
  assert.notEqual(corrected.request_id,original.request_id);assert.equal(corrected.text,'Corrected initial text');
  assert.equal(p.data.runs.length,1);
});
