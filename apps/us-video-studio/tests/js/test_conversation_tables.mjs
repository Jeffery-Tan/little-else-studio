/* Synthetic four-column replies through the shipped Markdown + chat renderer.
 * This fixture checks DOM, safety and style assembly; it has no layout engine.
 * The real narrow-pane reading result is verified separately in the browser.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {createRequire} from 'node:module';
import {loadApp, makeDom, webSource} from './dom-fixture.mjs';

const require = createRequire(import.meta.url);
const {renderMarkdown} = require('../../studio/web/md.js');
const SOURCE = [
  '已查看这两张图片。', '',
  '| 图片 | 可见形态 | 图中文字 | 不能确认 |',
  '| --- | --- | --- | --- |',
  '| way to carry.png | 黑色绑带与分离肩带图示 | Carry it your way | 弹性与可拆卸功能不能由静态图确认 |',
  '| texture.png | 灰色光泽及黑色钩扣形状 | Oxford outside / PEVA inside | 实际材质和性能未核实 |',
  '', '以上只陈述可见内容。',
].join('\n');

function mounted(source = SOURCE, options = {scrollTables:true}) {
  const dom = makeDom({ids:['reading'], body:''});
  const host = dom.document.getElementById('reading');
  host.innerHTML = renderMarkdown(source, options);
  return host;
}

function page() {
  const ids=['chat-input','send','toast','conversation-body','conversation-count',
    'conversation','conversation-toggle','conversation-caret','draft','job-status',
    'rail','stage','journey-decision','run-select'];
  const p=loadApp({runtime:'real',dom:makeDom({ids,body: webSource('index.html')
    .split(/<body[^>]*>/i)[1].split('</body>')[0].replace(/<script[\s\S]*?<\/script>/gi,'')}),
    globals:{setTimeout:()=>0,clearTimeout:()=>{},setInterval:()=>0,clearInterval:()=>{}}});
  p.read=(expression)=>vm.runInContext(expression,p.ctx);
  p.el=(id)=>p.document.getElementById(id);
  return p;
}

function timeline(text) {
  return {turns:[{user:{text:'只看这两张图片'},reply:{text},items:[],detail:null}],
    rail:[],questions:[],progress:{},pending:null,interaction:null};
}

function tableContents(host) {
  const table=host.querySelector('table');
  return {headers:table.querySelectorAll('th').map(node=>node.textContent),
    values:table.querySelectorAll('td').map(node=>node.textContent)};
}

test('chat tables preserve every row, column, filename and native column header relationship',()=>{
  const host=mounted();
  const region=host.querySelector('.markdown-table-scroll');
  assert.equal(region.getAttribute('role'),'region');
  assert.equal(region.getAttribute('tabindex'),'0');
  assert.match(region.getAttribute('aria-label'),/表格.*左右方向键/);
  assert.match(host.querySelector('.markdown-table-hint').textContent,/左右滚动/);
  assert.equal(region.querySelector('table').parentNode,region);
  assert.equal(region.querySelectorAll('thead').length,1);
  assert.equal(region.querySelectorAll('tbody').length,1);
  assert.equal(region.querySelectorAll('th').length,4);
  for(const cell of region.querySelectorAll('th')) assert.equal(cell.getAttribute('scope'),'col');
  assert.deepEqual(tableContents(host),{
    headers:['图片','可见形态','图中文字','不能确认'],
    values:['way to carry.png','黑色绑带与分离肩带图示','Carry it your way','弹性与可拆卸功能不能由静态图确认',
      'texture.png','灰色光泽及黑色钩扣形状','Oxford outside / PEVA inside','实际材质和性能未核实'],
  });
  assert.match(host.textContent,/已查看这两张图片/);
  assert.match(host.textContent,/以上只陈述可见内容/);
});

test('ordinary artifact Markdown is unchanged when the chat option is absent',()=>{
  const legacy=renderMarkdown(SOURCE);
  assert.match(legacy,/<table><thead><tr><th>图片<\/th>/);
  assert.doesNotMatch(legacy,/markdown-table|tabindex|role="region"/);
  const original=mounted(SOURCE,{}),chat=mounted();
  assert.deepEqual(tableContents(chat),tableContents(original));
});

test('table cells and headers keep raw HTML and unsafe links as escaped text',()=>{
  const raw='| <img src=x onerror=boom> | [bad](javascript:boom) |\n| --- | --- |\n'+
    '| <script>alert(1)</script> | **"><img src=x>** |';
  const host=mounted(raw),html=host.innerHTML;
  assert.equal(host.querySelector('script'),null);
  assert.equal(host.querySelector('img'),null);
  assert.equal(host.querySelector('a'),null);
  assert.match(html,/&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html,/&lt;img src=x onerror=boom&gt;/);
  assert.match(html,/scope="col"/);
  assert.equal(host.querySelectorAll('th').length,2);
  assert.equal(host.querySelectorAll('td').length,2);
});

test('multiple chat tables each own their complete keyboard reading region',()=>{
  const host=mounted(SOURCE+'\n\n后续信息\n\n| 新项目 | 完整保留 |\n| --- | --- |\n| A | 最后一列 |');
  assert.equal(host.querySelectorAll('.markdown-table-scroll').length,2);
  assert.equal(host.querySelectorAll('table').length,2);
  assert.match(host.querySelectorAll('.markdown-table-scroll')[1].textContent,/最后一列/);
});

test('the real saved-chat renderer uses the same table boundary for old turns and drops previous project content',()=>{
  const p=page(),old=timeline(SOURCE),before=JSON.stringify(old);
  p.ctx.tl=old;
  p.read('state.runId="old-run";state.busy=false;state.timeline=tl;renderConversation(tl)');
  const host=p.el('conversation-body');
  assert.ok(host.querySelector('.msg.assistant').querySelector('.markdown-table-scroll'));
  assert.equal(host.querySelectorAll('th').length,4);
  assert.match(host.textContent,/way to carry\.png/);
  assert.equal(JSON.stringify(old),before,'only rendering changes; saved model text remains exact');
  p.ctx.tl=timeline('这是另一个项目，没有图片表格。');
  p.read('state.runId="new-run";state.timeline=tl;renderConversation(tl)');
  assert.equal(host.querySelector('table'),null);
  assert.doesNotMatch(host.textContent,/way to carry/);
  assert.match(host.textContent,/这是另一个项目/);
  assert.equal(p.observed.filter(row=>row.method!=='GET').length,0);
});

test('shipped styles override the old block table only inside explicit chat regions and keep full columns',()=>{
  const css=webSource('conversation.css'),index=webSource('index.html');
  assert.ok(index.indexOf('/static/conversation.css')>index.indexOf('/static/cinema.css'));
  assert.match(css,/\.msg\.assistant \.body \.markdown-table-scroll \{[^}]*max-width:100%[^}]*overflow-x:auto/);
  assert.match(css,/\.markdown-table-scroll:focus-visible \{[^}]*outline:2px solid/);
  assert.match(css,/\.msg\.assistant \.body \.markdown-table-scroll>table \{[^}]*display:table;[^}]*max-width:none/);
  assert.match(css,/\.markdown-table-scroll th,\.msg\.assistant \.body \.markdown-table-scroll td \{[^}]*min-width:11rem;[^}]*white-space:normal;word-break:normal;overflow-wrap:anywhere/);
  assert.doesNotMatch(css.split('/* Tables keep readable columns')[1],/display:none|line-clamp|text-overflow|table-layout:fixed/);
});
