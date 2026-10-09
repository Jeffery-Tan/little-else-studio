import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../studio/web/codex-login.js', import.meta.url), 'utf8');
function node(tag, className, textContent='') {
  return {tag,className,textContent,children:[],append(...items){this.children.push(...items);},setAttribute(key,value){this[key]=value;}};
}
const idle = {status:'not_logged_in',login_active:false,message:'请登录 ChatGPT。'};
const pending = {status:'pending',login_active:true,attempt_id:'attempt-1',message:'请在浏览器完成授权。'};
const logged = {status:'logged_in',login_active:false,message:'已登录 ChatGPT。'};
const flush = () => new Promise(resolve => setImmediate(resolve));
function harness(handler=async()=>idle) {
  const calls=[], timers=new Map(), host=node('div'); let count=0, disabled=false;
  const context=vm.createContext({box:node, api:async(url,options)=>{
    const call={url,method:options?.method || 'GET',body:options?.body && JSON.parse(options.body)};
    calls.push(call); return handler(call);
  },setTimeout:fn=>{timers.set(++count,fn);return count;},clearTimeout:id=>timers.delete(id)});
  vm.runInContext(source,context);
  const controller=context.createCodexLogin(host,()=>disabled);
  const [status,actions,help]=host.children, [login,refresh,cancel]=actions.children;
  return {host,status,help,login,refresh,cancel,controller,calls,timers,
    disable(value){disabled=value;controller.sync();},
    async tick(){const [id,fn]=timers.entries().next().value;timers.delete(id);await fn();}};
}

test('opening reads status only; logged in does not launch login or alter settings',async()=>{
  const h=harness(async()=>logged);h.controller.setVisible(true);await flush();
  assert.deepEqual(h.calls.map(c=>[c.method,c.url]),[['GET','/api/connections/codex/status']]);
  assert.equal(h.login.textContent,'已登录 ChatGPT');assert.equal(h.login.disabled,true);
  await h.login.onclick();assert.equal(h.calls.length,1);assert.equal(h.timers.size,0);
});

test('explicit login opens one attempt and polls status until success',async()=>{
  let requests=0;
  const h=harness(async c=>c.method==='POST'?pending:++requests===1?idle:logged);
  h.controller.setVisible(true);await flush();await h.login.onclick();
  assert.deepEqual(h.calls[1],{url:'/api/connections/codex/login',method:'POST',body:{consent:true}});
  assert.equal(h.cancel.hidden,false);assert.equal(h.login.disabled,true);
  await h.login.onclick();assert.equal(h.calls.length,2);
  await h.tick();assert.equal(h.login.textContent,'已登录 ChatGPT');assert.equal(h.timers.size,0);
});

test('duplicate clicks during request cannot create another login',async()=>{
  let finish;
  const h=harness(c=>c.method==='POST'?new Promise(resolve=>finish=resolve):Promise.resolve(idle));
  h.controller.setVisible(true);await flush();const request=h.login.onclick();await h.login.onclick();
  assert.equal(h.calls.length,2);finish(pending);await request;assert.equal(h.timers.size,1);
});

test('cancellation binds the displayed attempt and does not log out',async()=>{
  const h=harness(async c=>c.method==='GET'?pending:idle);h.controller.setVisible(true);await flush();
  await h.cancel.onclick();assert.deepEqual(h.calls[1],{url:'/api/connections/codex/login/cancel',method:'POST',body:{attempt_id:'attempt-1'}});
  assert.equal(h.cancel.hidden,true);assert.equal(h.timers.size,0);
});

test('closing or switching service stops polling without cancelling backend login',async()=>{
  const h=harness(async()=>pending);h.controller.setVisible(true);await flush();
  h.controller.setVisible(false);assert.equal(h.timers.size,0);assert.equal(h.calls.length,1);
  h.controller.setVisible(true);await flush();assert.equal(h.calls.length,2);
  h.controller.dispose();assert.equal(h.timers.size,0);assert.ok(h.calls.every(c=>c.method==='GET'));
});

test('late reply after closing cannot update controls or restart polling',async()=>{
  let finish;const h=harness(()=>new Promise(resolve=>finish=resolve));h.controller.setVisible(true);
  h.controller.dispose();finish(pending);await flush();assert.equal(h.timers.size,0);
  assert.doesNotMatch(h.status.textContent,/请在浏览器/);
});

test('uncertain POST does not retry automatically; status read discovers original attempt',async()=>{
  let count=0;
  const h=harness(async c=>{if(c.method==='POST')throw new Error('connection lost');return ++count===1?idle:pending;});
  h.controller.setVisible(true);await flush();await h.login.onclick();
  assert.equal(h.timers.size,0);assert.equal(h.login.disabled,true);assert.match(h.status.textContent,/刷新状态/);
  await h.refresh.onclick();assert.equal(h.cancel.hidden,false);assert.equal(h.calls.filter(c=>c.method==='POST').length,1);
});

test('missing CLI and API-key login do not offer a destructive account replacement',async()=>{
  for(const result of [{status:'unavailable',message:'没有找到 CLI。'}, {status:'failed',auth_method:'api_key',message:'已有 API Key 登录，请在终端管理。'}]){
    const h=harness(async()=>result);h.controller.setVisible(true);await flush();
    assert.equal(h.login.disabled,true);assert.equal(h.refresh.disabled,false);assert.equal(h.timers.size,0);
  }
});

test('saving team settings prevents parallel login actions',async()=>{
  const h=harness();h.controller.setVisible(true);await flush();h.disable(true);
  await h.login.onclick();await h.refresh.onclick();assert.equal(h.calls.length,1);
  h.disable(false);assert.equal(h.login.disabled,false);
});

test('index loads login controller before settings UI',()=>{
  const html=fs.readFileSync(new URL('../../studio/web/index.html',import.meta.url),'utf8');
  const login=html.indexOf('/static/codex-login.js'),connections=html.indexOf('/static/connections.js');
  assert.ok(login>=0);assert.ok(connections>login);
});

test('old backend prompts service restart instead of repeated authorization',async()=>{
  const h=harness(async()=>{throw Object.assign(new Error('Not Found'),{status:404});});
  h.controller.setVisible(true);await flush();
  assert.match(h.status.textContent,/重启工作台服务/);
  assert.equal(h.login.disabled,true);assert.equal(h.refresh.disabled,false);assert.equal(h.timers.size,0);
});
