import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../studio/web/connections.js', import.meta.url), 'utf8');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.value = ''; this.textContent = ''; this.dataset = {}; this.handlers = new Map(); }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(type, listener) { this.handlers.set(type, listener); }
  showModal() { this.shown = true; }
  close() { this.closed = true; this.handlers.get('close')?.(); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(child => child !== this); }
  querySelectorAll(selector) {
    const matches = node => selector.split(',').some(part => {
      const token = part.trim();
      return token.startsWith('.') ? (node.className || '').split(' ').includes(token.slice(1)) : node.tag === token;
    });
    return nodes(this).slice(1).filter(matches);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
function nodes(root) { return [root, ...root.children.flatMap(nodes)]; }
function visible(root) {
  if (root.hidden) return '';
  if (root.tag === 'details' && !root.open) return visible(root.children[0]);
  return [root.textContent, ...root.children.map(visible)].join(' ');
}
function find(root, predicate) { const value = nodes(root).find(predicate); assert.ok(value, 'Expected UI node'); return value; }
function button(root, text) { return find(root, node => node.tag === 'button' && node.textContent === text); }
function labeled(root, text) { return find(root, node => node.tag === 'label' && node.children[0]?.textContent === text).children[1]; }
function role(root, id) {
  const row = find(root, node => node.dataset.role === id);
  return { row, connection: labeled(row, '模型服务'), model: () => row.querySelector('.role-model-input').children[0] };
}
const config = () => ({ revision: 7, roles: Object.fromEntries(['orchestrator','director','material','writer','reviewer'].map(name => [name,{connection:'codex',model:''}])), connections: [
  { id: 'codex', name: '本机 Codex', provider: 'codex_cli' },
  { id: 'ds', name: 'DeepSeek', provider: 'deepseek', has_key: false, base_url: 'https://api.deepseek.com/v1' },
] });
const verified = (model_ids = ['deepseek-flash', 'deepseek-v4-pro']) => ({status:'verified', model_ids, checked_at:'2026-09-19T00:00:00Z', scope:'credentials_and_models'});
function harness(initial = config(), handler = async () => verified()) {
  const body = new Element('body'), calls = [], toasts = [];
  let id = 0;
  const context = vm.createContext({
    document: { createElement: tag => new Element(tag), body, querySelector: selector => body.querySelector(selector) },
    $: () => new Element('button'),
    box: (tag, className, text = '') => Object.assign(new Element(tag), { className, textContent: text }),
    crypto: { randomUUID: () => `new-id-${++id}-fixture` },
    toast: message => toasts.push(message),
    // Login controller has its own lifecycle and transport coverage in test_codex_login.mjs.
    createCodexLogin: () => ({setVisible(){},sync(){},dispose(){}}),
    api: async (url, options) => { const call = {url, method:options?.method, body:options?.body ? JSON.parse(options.body) : undefined}; calls.push(call); return handler(call); },
  });
  vm.runInContext(source, context);
  context.openConnectionDialog(initial);
  const dialog = body.children[0], form = dialog.children[0];
  const card = id => find(body, node => node.dataset.connectionId === id);
  return {context,body,dialog,form,card,calls,toasts,submit:()=>form.onsubmit({preventDefault(){}})};
}

// Every credential in these tests is synthetic; APIs are in-memory mocks.
test('service and validation precede role assignment; generic advanced fields remain collapsed', () => {
  const initial = config();
  initial.runtime = {started_at:'2026-09-23T10:00:00Z',code_fingerprint:'abc123',data_root_id:'root-12',python:'3.12.6',media_tools:{ffmpeg:false},meaning:'仅代表当前进程。'};
  initial.connections.push({id:'generic',name:'创意服务',provider:'openai_compat',base_url:'https://fixture.invalid/v1',has_key:true});
  const h = harness(initial);
  const text = visible(h.body);
  assert.ok(text.indexOf('1 · 连接') < text.indexOf('2 · 为团队'));
  assert.match(text, /验证密钥与模型/);
  assert.match(text, /主制片/); assert.match(text, /编剧/); assert.match(text, /API Key/);
  assert.doesNotMatch(text, /max_completion_tokens|输出上限参数|输入单价|服务地址（/);
  const keys = nodes(h.body).filter(node => node.type === 'password');
  assert.ok(keys.every(node => node.value === '' && node.autocomplete === 'new-password'));
  assert.equal(h.calls.length, 0);
  for (const detail of nodes(h.body).filter(node => node.tag === 'details')) detail.open = true;
  assert.match(visible(h.body), /abc123.*root-12.*FFmpeg：未找到/);
  assert.match(visible(h.card('generic')), /max_completion_tokens/);
  assert.doesNotMatch(visible(h.card('ds')), /max_completion_tokens|输出上限参数|服务地址（/);
});

test('DeepSeek saves fixed parameters without changing the existing Codex orchestrator', async () => {
  const h = harness(config(), async () => ({revision:8}));
  const ds = h.card('ds'); labeled(ds,'API Key').value = 'synthetic-key';
  const writer = role(h.body,'writer'); writer.connection.value='ds'; writer.connection.onchange();
  assert.deepEqual(writer.model().children.map(option=>option.value), ['', 'deepseek-flash', 'deepseek-v4-pro']);
  writer.model().value='deepseek-flash'; writer.model().onchange();
  await h.submit();
  assert.equal(h.calls.length,1);
  assert.equal(h.calls[0].url,'/api/connections');
  const saved = h.calls[0].body;
  assert.equal(saved.expected_revision,7);
  assert.deepEqual(saved.roles.orchestrator,{connection:'codex',model:''});
  assert.deepEqual(saved.roles.writer,{connection:'ds',model:'deepseek-flash'});
  assert.equal(saved.connections[1].provider,'deepseek');
  assert.equal(saved.connections[1].base_url,'https://api.deepseek.com/v1');
  assert.equal(saved.connections[1].token_limit_parameter,'max_tokens');
  assert.equal(saved.connections[1].api_key,'synthetic-key');
  assert.equal(labeled(ds,'API Key').value,'');
  assert.equal(h.dialog.closed,true);
  assert.match(h.toasts[0],/不代表.*创作测试/);
});

test('verification reads the returned models without saving or silently replacing an existing model', async () => {
  const initial=config(); initial.roles.writer={connection:'ds',model:'legacy-model'};
  const h=harness(initial,async()=>verified(['live-model','deepseek-v4-pro']));
  const ds=h.card('ds'), key=labeled(ds,'API Key'); key.value='synthetic-key'; key.oninput();
  await button(ds,'验证密钥与模型').onclick();
  assert.deepEqual(h.calls.map(call=>call.url),['/api/connections/deepseek/check']);
  assert.deepEqual(h.calls[0].body,{expected_revision:7,connection_id:'ds',api_key:'synthetic-key'});
  const writer=role(h.body,'writer');
  assert.equal(writer.model().value,'legacy-model');
  assert.ok(writer.model().children.some(option=>option.value==='live-model'));
  assert.ok(writer.model().children.some(option=>option.value==='legacy-model'));
  assert.match(visible(writer.row),/已保留原值/);
  assert.match(visible(ds),/尚未进行实际创作测试/);
  assert.equal(role(h.body,'orchestrator').connection.value,'codex');
  assert.equal(key.value,'synthetic-key');
});

test('a saved DeepSeek credential can be verified without returning or resending the key', async () => {
  const initial=config(); initial.connections[1].has_key=true;
  const h=harness(initial);
  await button(h.card('ds'),'验证密钥与模型').onclick();
  assert.deepEqual(h.calls[0].body,{expected_revision:7,connection_id:'ds'});
  assert.equal(labeled(h.card('ds'),'API Key').value,'');
});

test('duplicate verification is blocked and changing a key discards an old response', async () => {
  let finish;
  const h=harness(config(),()=>new Promise(resolve=>{finish=resolve;}));
  const ds=h.card('ds'), key=labeled(ds,'API Key'), check=button(ds,'验证密钥与模型');
  key.value='synthetic-first'; key.oninput();
  const pending=check.onclick(); await check.onclick();
  assert.equal(h.calls.length,1); assert.equal(check.disabled,true);
  key.value='synthetic-second'; key.oninput();
  finish(verified(['old-response-model'])); await pending;
  assert.match(visible(ds),/尚未验证/);
  assert.doesNotMatch(visible(ds),/模型列表已验证/);
  assert.equal(check.disabled,false); assert.equal(key.value,'synthetic-second');
});

test('provider change revokes verification and does not reuse a typed key with another service', async () => {
  const h=harness(), ds=h.card('ds'), key=labeled(ds,'API Key');
  key.value='synthetic-key'; key.oninput(); await button(ds,'验证密钥与模型').onclick();
  const provider=labeled(ds,'选择服务'); provider.value='openai_compat';provider.onchange();
  assert.equal(key.value,'');
  provider.value='deepseek';provider.onchange();
  assert.match(visible(ds),/尚未验证/); assert.doesNotMatch(visible(ds),/模型列表已验证/);
});

test('closing while checking clears credentials and ignores the late result', async () => {
  let finish;
  const h=harness(config(),()=>new Promise(resolve=>{finish=resolve;}));
  const ds=h.card('ds'), key=labeled(ds,'API Key'); key.value='synthetic-key';
  const pending=button(ds,'验证密钥与模型').onclick();
  button(h.body,'关闭').onclick();
  assert.equal(key.value,''); assert.equal(h.body.children.length,0);
  finish(verified()); await pending;
  assert.equal(h.body.children.length,0); assert.equal(h.toasts.length,0);
});

test('failed validation and save conflicts preserve edits without claiming success or overwriting revision', async () => {
  const h=harness(config(),async()=>{const error=new Error('请检查密钥');error.status=409;throw error;});
  const ds=h.card('ds'),key=labeled(ds,'API Key');key.value='synthetic-key';
  await button(ds,'验证密钥与模型').onclick();
  assert.match(visible(ds),/请检查密钥/);assert.equal(key.value,'synthetic-key');
  await h.submit();
  assert.match(visible(h.body),/未覆盖现有设置/);
  assert.equal(key.value,'synthetic-key');assert.equal(h.dialog.closed,undefined);
  assert.equal(h.calls[1].body.expected_revision,7);
  assert.equal(button(h.body,'保存团队设置').disabled,false);
});

test('saving has a duplicate guard, blocks close and keeps input on failure', async () => {
  let fail;
  const h=harness(config(),()=>new Promise((_resolve,reject)=>{fail=reject;}));
  const key=labeled(h.card('ds'),'API Key');key.value='synthetic-key';
  const saving=h.submit();await h.submit();assert.equal(h.calls.length,1);
  button(h.body,'关闭').onclick();assert.equal(h.dialog.closed,undefined);
  let prevented=false;h.dialog.handlers.get('cancel')({preventDefault(){prevented=true;}});assert.equal(prevented,true);
  fail(new Error('网络暂时不可用'));await saving;
  assert.equal(key.value,'synthetic-key');assert.equal(key.disabled,false);
  assert.equal(button(h.body,'保存团队设置').disabled,false);
});

test('same-name services keep ID bindings and only unbound new rows can be removed', async () => {
  const initial=config();initial.connections[0].name='同名';initial.connections[1].name='同名';
  const h=harness(initial,async()=>({revision:8}));
  const producer=role(h.body,'orchestrator');
  assert.equal(producer.connection.value,'codex');
  assert.deepEqual(producer.connection.children.map(option=>option.textContent),['同名 · codex','同名 · ds']);
  assert.equal(nodes(h.card('ds')).some(node=>node.textContent==='取消添加'),false);
  button(h.body,'＋ 添加模型服务').onclick();
  const fresh=find(h.body,node=>node.dataset.connectionId?.startsWith('connection-'));
  const writer=role(h.body,'writer');writer.connection.value=fresh.dataset.connectionId;writer.connection.onchange();
  const remove=button(fresh,'取消添加');assert.equal(remove.disabled,true);remove.onclick();
  assert.ok(nodes(h.body).includes(fresh));
  writer.connection.value='codex';writer.connection.onchange();assert.equal(remove.disabled,false);remove.onclick();
  assert.ok(!nodes(h.body).includes(fresh));
  await h.submit();assert.equal(h.calls[0].body.connections.length,2);
});

test('saved credentials from a generic provider cannot be reused for DeepSeek verification', async () => {
  const initial=config();initial.connections[1]={id:'generic',name:'Other',provider:'openai_compat',has_key:true,base_url:'https://fixture.invalid/v1'};
  const h=harness(initial), card=h.card('generic'), provider=labeled(card,'选择服务');
  provider.value='deepseek';provider.onchange();
  await button(card,'验证密钥与模型').onclick();
  assert.equal(h.calls.length,0);
  assert.match(visible(card),/请先填写 DeepSeek API Key/);
});

test('a provider change during verification invalidates the late success', async () => {
  let finish;
  const h=harness(config(),()=>new Promise(resolve=>{finish=resolve;}));
  const card=h.card('ds');labeled(card,'API Key').value='synthetic-key';
  const pending=button(card,'验证密钥与模型').onclick();
  const provider=labeled(card,'选择服务');provider.value='codex_cli';provider.onchange();
  finish(verified());await pending;
  provider.value='deepseek';provider.onchange();
  assert.match(visible(card),/尚未验证/);
  assert.doesNotMatch(visible(card),/模型列表已验证/);
});

test('saving an unverified service retains existing generic role/model bindings', async () => {
  const initial=config();initial.connections.push({id:'generic',name:'Custom',provider:'anthropic_compat',has_key:true,base_url:'https://fixture.invalid/anthropic'});
  initial.roles.director={connection:'generic',model:'custom-existing-model'};
  const h=harness(initial,async()=>({revision:8}));
  await h.submit();
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].method,'PUT');
  assert.deepEqual(h.calls[0].body.roles.director,{connection:'generic',model:'custom-existing-model'});
  assert.equal(h.calls[0].body.connections[2].base_url,'https://fixture.invalid/anthropic');
  assert.equal(h.calls[0].body.connections[2].api_key,undefined);
  assert.doesNotMatch(JSON.stringify(h.calls[0].body),/checked_at|verified/);
});

test('verification cannot save implicitly and saving waits for an in-flight check', async () => {
  let finish;
  const h=harness(config(),()=>new Promise(resolve=>{finish=resolve;}));
  labeled(h.card('ds'),'API Key').value='synthetic-key';
  const pending=button(h.card('ds'),'验证密钥与模型').onclick();
  await h.submit();
  assert.equal(h.calls.length,1);assert.match(visible(h.body),/等验证结束后再保存/);
  finish(verified());await pending;
  assert.equal(h.calls.length,1);assert.equal(h.dialog.closed,undefined);
});

test('saved-key hint follows the original provider and normalized endpoint identity', () => {
  const initial=config();initial.connections[1]={id:'generic',name:'Generic',provider:'openai_compat',has_key:true,base_url:'https://fixture.invalid/v1'};
  const h=harness(initial), card=h.card('generic'), key=labeled(card,'API Key');
  const address=labeled(card,'服务地址（不含具体接口路径）'), provider=labeled(card,'选择服务');
  assert.equal(key.placeholder,'已保存；留空保留');
  address.value='https://changed.invalid/v1';address.oninput();
  assert.match(key.placeholder,/重新填写/);
  address.value='https://fixture.invalid/v1/';address.oninput();
  assert.equal(key.placeholder,'已保存；留空保留');
  provider.value='anthropic_compat';provider.onchange();assert.match(key.placeholder,/重新填写/);
  provider.value='openai_compat';provider.onchange();assert.equal(key.placeholder,'已保存；留空保留');
  provider.value='deepseek';provider.onchange();assert.match(key.placeholder,/重新填写/);
});

test('verification-lock 409 preserves the actual wait instruction without asking to discard the dialog', async () => {
  const message='正在验证连接，请等待本次结果。';
  const h=harness(config(),async()=>{const error=new Error(message);error.status=409;throw error;});
  const card=h.card('ds'),key=labeled(card,'API Key');key.value='synthetic-key';
  await button(card,'验证密钥与模型').onclick();
  assert.match(visible(card),/正在验证连接，请等待本次结果/);
  assert.doesNotMatch(visible(card),/其他窗口更新|重新打开|关闭后/);
  assert.equal(key.value,'synthetic-key');
  assert.equal(h.dialog.closed,undefined);
});

test('configuration-conflict 409 retains the backend instruction to reopen settings', async () => {
  const h=harness(config(),async()=>{const error=new Error('模型设置已在其他窗口更新，请重新打开设置后再验证。');error.status=409;throw error;});
  const card=h.card('ds');labeled(card,'API Key').value='synthetic-key';
  await button(card,'验证密钥与模型').onclick();
  assert.match(visible(card),/模型设置已在其他窗口更新，请重新打开设置后再验证/);
  assert.match(visible(card),/本次输入仍保留/);
});

test('role model controls keep an accessible role-specific name across service changes', () => {
  const h=harness(), producer=role(h.body,'orchestrator'), writer=role(h.body,'writer');
  assert.equal(producer.model()['aria-label'],'主制片模型');
  assert.equal(writer.model()['aria-label'],'编剧模型');
  writer.connection.value='ds';writer.connection.onchange();
  assert.equal(writer.model().tag,'select');
  assert.equal(writer.model()['aria-label'],'编剧模型');
  writer.connection.value='codex';writer.connection.onchange();
  assert.equal(writer.model().tag,'input');
  assert.equal(writer.model()['aria-label'],'编剧模型');
});

test('capability guidance uses the declared image, audio and generation boundaries', () => {
  const initial=config();
  initial.capabilities={image_analysis:{ui_summary:'主制片可观察项目图片。'},
    video_understanding:{ui_summary:'素材区百炼北京可做有限声画分析。'},
    production:{ui_summary:'用户在制作工作区确认实际生成。'}};
  const h=harness(initial), text=visible(h.body);
  assert.match(text,/文字策划模型/);
  assert.match(text,/主制片可观察项目图片/);
  assert.match(text,/有限声画分析/);
  assert.match(text,/用户在制作工作区确认实际生成/);
  assert.doesNotMatch(text,/尚未接入视频声画理解/);
  assert.match(text,/不改变正在运行的任务，也不会重写已有稿件/);
  assert.match(text,/5 \/ 5 个角色配置齐全/);
  assert.match(visible(role(h.body,'material').row),/使用本机 Codex 额度/);
  assert.equal(h.calls.length,0);
});

test('role completeness follows explicit model choice and credential identity without claiming connectivity', () => {
  const h=harness(), writer=role(h.body,'writer'), card=h.card('ds');
  writer.connection.value='ds';writer.connection.onchange();
  assert.equal(writer.model().value,'');
  assert.match(visible(h.body),/4 \/ 5 个角色配置齐全/);
  writer.model().value='deepseek-flash';writer.model().onchange();
  assert.match(visible(h.body),/4 \/ 5 个角色配置齐全/);
  const key=labeled(card,'API Key');key.value='synthetic-key';key.oninput();
  assert.match(visible(card),/密钥已填写 · 尚未保存/);
  assert.match(visible(h.body),/5 \/ 5 个角色配置齐全/);
  assert.match(visible(h.body),/1 个角色分配已修改，尚未保存/);
  assert.match(visible(h.body),/配置齐全不代表已通过真实创作测试/);
  assert.equal(h.calls.length,0);
});

test('price coverage is exact to connection and model and never fills missing rates with zero', () => {
  const initial=config();initial.connections[1].has_key=true;
  initial.connections[1].pricing={'deepseek-flash':{input_usd_per_mtok:0,output_usd_per_mtok:2,source:'fixture contract',as_of:'2026-09-19'}};
  initial.roles.writer={connection:'ds',model:'deepseek-flash'};
  initial.connections.push({id:'other',name:'Other',provider:'openai_compat',base_url:'https://fixture.invalid/v1',has_key:true,pricing:{}});
  const h=harness(initial),writer=role(h.body,'writer');
  assert.match(visible(writer.row),/参考价格：输入 \$0 \/ 输出 \$2/);
  assert.match(visible(writer.row),/核实于 2026-09-19/);
  const output=labeled(h.card('ds'),'输出单价');output.value='';output.oninput();
  assert.match(visible(writer.row),/价格待核实/);
  assert.doesNotMatch(visible(writer.row),/参考价格/);
  output.value='2';output.oninput();
  const source=labeled(h.card('ds'),'价格来源');source.value='';source.oninput();
  assert.match(visible(writer.row),/价格待核实/);
  source.value='fixture contract';source.oninput();
  writer.connection.value='other';writer.connection.onchange();writer.model().value='deepseek-flash';writer.model().oninput();
  assert.match(visible(writer.row),/价格待核实/);
  assert.doesNotMatch(visible(writer.row),/参考价格/);
  button(writer.row,'填写 / 核对价格').onclick();
  assert.ok(h.card('other').querySelectorAll('details').every(item=>item.open));
  assert.ok(h.card('ds').querySelectorAll('details').every(item=>!item.open));
  assert.equal(h.calls.length,0);
});

test('changing provider clears obsolete role model choices and marks old prices unusable', () => {
  const initial=config();initial.connections[1].has_key=true;
  initial.connections[1].pricing={'deepseek-flash':{input_usd_per_mtok:1,output_usd_per_mtok:2,source:'fixture',as_of:'2026-09-19'}};
  initial.roles.writer={connection:'ds',model:'deepseek-flash'};
  const h=harness(initial),writer=role(h.body,'writer'),card=h.card('ds');
  const provider=labeled(card,'选择服务');provider.value='openai_compat';provider.onchange();
  assert.equal(writer.model().value,'');
  writer.model().value='deepseek-flash';writer.model().oninput();
  assert.match(visible(writer.row),/服务已更换.*清空旧价格/);
  assert.doesNotMatch(visible(writer.row),/参考价格/);
  assert.match(visible(h.body),/4 \/ 5 个角色配置齐全/);
  writer.connection.value='codex';writer.connection.onchange();
  writer.connection.value='ds';writer.connection.onchange();
  assert.equal(writer.model().value,'deepseek-flash'); // Explicit selection is kept for this draft service.
  provider.value='deepseek';provider.onchange();
  assert.equal(writer.model().value,''); // Another provider change requires another explicit choice.
  assert.equal(h.calls.length,0);
});

test('generic role stays incomplete until its service address is present', () => {
  const initial=config();initial.connections[1]={id:'generic',name:'Other',provider:'openai_compat',base_url:'',has_key:false};
  initial.roles.writer={connection:'generic',model:'text-model'};
  const h=harness(initial),card=h.card('generic');
  const key=labeled(card,'API Key');key.value='synthetic-key';key.oninput();
  assert.match(visible(h.body),/4 \/ 5 个角色配置齐全/);
  const address=labeled(card,'服务地址（不含具体接口路径）');address.value='https://fixture.invalid/v1';address.oninput();
  assert.match(visible(h.body),/5 \/ 5 个角色配置齐全/);
  assert.equal(h.calls.length,0);
});

test('changing the service behind unchanged empty Codex bindings marks every affected role unsaved', () => {
  const h=harness(),card=h.card('codex'),provider=labeled(card,'选择服务');
  provider.value='deepseek';provider.onchange();
  assert.match(visible(h.body),/0 \/ 5 个角色配置齐全/);
  assert.match(visible(h.body),/5 个角色分配已修改，尚未保存/);
  assert.doesNotMatch(visible(h.body),/角色分配未改变/);
  provider.value='codex_cli';provider.onchange();
  assert.match(visible(h.body),/角色分配未改变/);
  assert.match(visible(h.body),/5 \/ 5 个角色配置齐全/);
  assert.equal(h.calls.length,0);
});

test('changing a saved API endpoint marks only its bound roles changed and normalized restoration clears the notice', () => {
  const initial=config();initial.connections.push({id:'generic',name:'Other',provider:'openai_compat',base_url:'https://fixture.invalid/v1',has_key:true});
  initial.roles.writer={connection:'generic',model:'text-model'};
  const h=harness(initial),address=labeled(h.card('generic'),'服务地址（不含具体接口路径）');
  address.value='https://other.invalid/v1';address.oninput();
  assert.match(visible(h.body),/1 个角色分配已修改，尚未保存/);
  address.value='https://fixture.invalid/v1/';address.oninput();
  assert.match(visible(h.body),/角色分配未改变/);
  assert.equal(h.calls.length,0);
});
