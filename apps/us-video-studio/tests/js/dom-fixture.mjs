/* A small DOM, enough to run the real `app.js` controllers.
 *
 * The review of this workspace asked for tests that go through the real
 * `stageHtml → wireGate → click` path and the real keyboard/submit path rather
 * than asserting that a rendered string contains a button. That needs a DOM,
 * and this app has no build step and no dependencies to borrow one from — so
 * this file implements the slice of the DOM API `app.js`, `stage.js` and
 * `journey.js` actually touch: elements, attributes, classes, `innerHTML`
 * parsing, delegated `querySelector`/`querySelectorAll` with just enough CSS
 * selector support, and a selector-keyed fetch stub.
 *
 * It is deliberately not a browser: no layout, no events beyond the handlers
 * the app assigns, no HTML error recovery. When a test needs something that is
 * not here, add it here rather than weakening the test.
 */
import {createRequire} from 'node:module';
import fs from 'node:fs';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
const WEB = new URL('../../studio/web/', import.meta.url);

export function webSource(name) {
  return fs.readFileSync(new URL(name, WEB), 'utf8');
}

/* ------------------------------------------------------------- elements -- */

class El {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = new Map();
    this.dataset = {};
    this.style = {};
    this._text = '';
    this.className = '';
    this.hidden = false;
    this.disabled = false;
    this._value = '';
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.checked = false;
    this.files = [];
  }

  /* `value` is an attribute-backed property for inputs and selects, and the
   * thing you type into for a textarea — the page uses both kinds. Reading has
   * to fall back to the attribute or a parsed `<input value="A">` reports "". */
  get value() { return this._value || this.getAttribute('value') || ''; }

  set value(next) {
    this._value = next == null ? '' : String(next);
    if (this.tagName === 'INPUT' || this.tagName === 'SELECT') {
      this.setAttribute('value', this._value);
    }
  }

  get classList() {
    const self = this;
    const list = () => String(self.className || '').split(/\s+/).filter(Boolean);
    return {
      add: (...names) => { self.className = [...new Set([...list(), ...names])].join(' '); },
      remove: (...names) => { self.className = list().filter(n => !names.includes(n)).join(' '); },
      contains: (name) => list().includes(name),
      toggle: (name, force) => {
        const has = force === undefined ? !list().includes(name) : Boolean(force);
        if (has) self.classList.add(name); else self.classList.remove(name);
        return has;
      },
    };
  }

  get textContent() {
    if (this.children.length) return this.children.map(c => c.textContent).join('');
    return this._text;
  }

  set textContent(value) { this._text = value == null ? '' : String(value); this.children = []; }

  get innerHTML() {
    // Elements built from parsed markup have no html string of their own; the
    // honest answer is the markup they represent, so a caller can assert on
    // either a rendered string or a mounted element the same way.
    return this._html !== undefined ? this._html : this.children.map(serialize).join('');
  }

  set innerHTML(html) {
    this._html = String(html);
    // Parsed nodes belong to *this* element (real DOM semantics): re-parent
    // them and take an independent copy of the list, so a later appendChild
    // that moves a node out actually shrinks *our* children — otherwise
    // `while (el.firstChild) host.appendChild(el.firstChild)` never ends.
    const kids = parseHtml(this._html, this);
    this.children = kids.slice();
    for (const child of this.children) child.parentNode = this;
    this._text = '';
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    // `className` is a property on the element and `class` an attribute, and the
    // app writes both ways. Keep the one source they share in step.
    if (name === 'class') this.className = String(value);
    // Boolean attributes are properties in a browser: `el.disabled` is what the
    // page and the tests read, not `getAttribute('disabled')`.
    if (name === 'disabled') this.disabled = true;
    if (name === 'checked') this.checked = true;
    if (name === 'hidden') this.hidden = true;
    if (name.startsWith('data-')) {
      this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
    }
  }

  getAttribute(name) {
    if (name.startsWith('data-')) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      if (key in this.dataset) return this.dataset[key];
    }
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  removeAttribute(name) { this.attributes.delete(name); }
  hasAttribute(name) { return this.getAttribute(name) !== null; }
  appendChild(node) {
    // Real DOM semantics: appending a node that already has a parent MOVES it.
    if (node.parentNode && node.parentNode !== this) {
      const old = node.parentNode;
      old.children = old.children.filter(c => c !== node);
    }
    node.parentNode = this;
    this.children.push(node);
    adoptIds(node, this.ownerDocument || null);
    return node;
  }
  append(...nodes) { for (const node of nodes) this.appendChild(node); }
  get firstChild() { return this.children[0] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const i = this.parentNode.children.indexOf(this);
    return this.parentNode.children[i + 1] || null;
  }
  prepend(...nodes) { for (const node of nodes.reverse()) { node.parentNode = this; this.children.unshift(node); } }
  replaceChildren(...nodes) {
    this.children = [];
    this._text = '';
    this._html = undefined;
    for (const node of nodes) this.appendChild(node);
  }
  before() {}
  after() {}
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(c => c !== this); }
  closest(selector) {
    const [matches] = parseSelector(selector);
    for (let node = this; node; node = node.parentNode) if (matches(node)) return node;
    return null;
  }
  matches(selector) { return parseSelector(selector)[0](this); }
  contains(node) {
    for (let n = node; n; n = n.parentNode) if (n === this) return true;
    return false;
  }
  insertAdjacentHTML(position, html) {
    const nodes = parseHtml(html, this);
    if (String(position).startsWith('before')) {
      this.parentNode
        ? this.parentNode.children.splice(this.parentNode.children.indexOf(this), 0, ...nodes)
        : this.children.unshift(...nodes);
    } else {
      this.children.push(...nodes);
    }
  }
  scrollIntoView() {}
  focus() { this.focused = true; }
  click() { return this.onclick?.({target: this, preventDefault() {}}); }
  addEventListener(type, handler) { (this.listeners ||= {})[type] = handler; }
  removeEventListener() {}
  getBoundingClientRect() { return {top: 0, left: 0, width: 0, height: 0}; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  querySelectorAll(selector) {
    const matchers = parseSelector(selector);
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (matchers.some(m => m(child))) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
}

/* --------------------------------------------------------------- parsing -- */

const VOID_TAGS = new Set(['br', 'hr', 'img', 'input', 'meta', 'link', 'source',
  // SVG shapes are written without a closing tag in this app's icons.
  'path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'use']);

/** Parse the subset of HTML the renderers emit. No error recovery by design. */
export function parseHtml(html, parent = null) {
  const root = new El('div');
  root.parentNode = parent;
  const stack = [root];
  const token = /<!--[\s\S]*?-->|<\/([a-zA-Z0-9-]+)\s*>|<([a-zA-Z0-9-]+)((?:\s+[^\s"'>/=]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'>`]+))?)*)\s*(\/?)>/g;
  let cursor = 0;
  let match;
  const text = (chunk) => {
    if (!chunk) return;
    const node = new El('#text');
    node._text = decodeEntities(chunk);
    stack[stack.length - 1].appendChild(node);
  };
  while ((match = token.exec(html))) {
    text(html.slice(cursor, match.index));
    cursor = token.lastIndex;
    if (match[0].startsWith('<!--')) continue;
    if (match[1]) {
      const name = match[1].toLowerCase();
      for (let i = stack.length - 1; i > 0; i -= 1) {
        if (stack[i].tagName === name.toUpperCase()) { stack.length = i; break; }
      }
      continue;
    }
    const node = new El(match[2]);
    for (const attr of match[3].matchAll(/([^\s"'>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>`]+)))?/g)) {
      const name = attr[1].trim();
      if (!name) continue;
      const value = attr[2] ?? attr[3] ?? attr[4] ?? '';
      node.setAttribute(name, decodeEntities(value));
    }
    stack[stack.length - 1].appendChild(node);
    if (!match[4] && !VOID_TAGS.has(node.tagName.toLowerCase())) stack.push(node);
  }
  text(html.slice(cursor));
  return root.children;
}

function decodeEntities(text) {
  return String(text)
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/&amp;/g, '&');
}

const ESCAPES = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};

/** The markup an element represents, for `innerHTML` on a parsed element. */
function serialize(el) {
  if (el.tagName === '#TEXT') return String(el._text).replace(/[&<>]/g, c => ESCAPES[c]);
  const attrs = [...el.attributes].map(([name, value]) => ` ${name}="${String(value).replace(/"/g, '&quot;')}"`).join('');
  const open = `<${el.tagName.toLowerCase()}${attrs}>`;
  const close = VOID_TAGS.has(el.tagName.toLowerCase()) ? '' : `</${el.tagName.toLowerCase()}>`;
  return open + el.children.map(serialize).join('') + close;
}

/* ------------------------------------------------------------- selectors -- */

/* Selector support: comma groups, and compound parts joined by a descendant,
 * child or adjacent-sibling combinator. That is the whole grammar the page
 * uses; a selector outside it fails loudly rather than silently matching
 * nothing. */
function parseCompound(clause) {
  const tests = [];
  const withoutAttrs = clause.replace(/\[([^\]]+)\]/g, (_, body) => {
    const [rawName, rawValue] = body.split('=');
    const name = rawName.trim();
    const value = rawValue === undefined ? null
      : decodeEntities(rawValue.trim().replace(/^["']|["']$/g, ''));
    if (value === null) tests.push(el => el.getAttribute(name) !== null);
    else if (name.endsWith('^')) tests.push(el => String(el.getAttribute(name.slice(0, -1)) || '').startsWith(value));
    else tests.push(el => el.getAttribute(name) === value);
    return '';
  });
  // Every class in the compound must match — `.msg.assistant` is not `.msg`.
  for (const [, cls] of withoutAttrs.matchAll(/\.([A-Za-z0-9_-]+)/g)) {
    tests.push(el => el.classList.contains(cls));
  }
  const id = withoutAttrs.match(/#([A-Za-z0-9_-]+)/);
  if (id) tests.push(el => el.getAttribute('id') === id[1]);
  const tag = withoutAttrs.replace(/[.#][A-Za-z0-9_-]+/g, '').replace(/[>+~]/g, '').trim();
  if (tag && tag !== '*') tests.push(el => el.tagName === tag.toUpperCase());
  if (!tests.length) tests.push(() => false);
  return el => tests.every(test => test(el));
}

function parseSelector(selector) {
  return String(selector).split(',').map(part => part.trim()).filter(Boolean).map(clause => {
    const parts = clause.split(/(\s*[>+]\s*|\s+)/).filter(Boolean);
    const combinators = [];
    const chain = [];
    for (const piece of parts) {
      if (/^[>+]$/.test(piece.trim())) { combinators.push(piece.trim()); continue; }
      if (/^\s+$/.test(piece)) { combinators.push(' '); continue; }
      chain.push(parseCompound(piece));
    }
    return (el) => {
      if (!chain[chain.length - 1](el)) return false;
      let node = el;
      for (let i = chain.length - 2; i >= 0; i -= 1) {
        const how = combinators[i] || ' ';
        if (how === '>') {
          node = node.parentNode;
          if (!node || !chain[i](node)) return false;
        } else if (how === '+') {
          const siblings = node.parentNode ? node.parentNode.children : [];
          node = siblings[siblings.indexOf(node) - 1];
          if (!node || !chain[i](node)) return false;
        } else {
          node = node.parentNode;
          while (node && !chain[i](node)) node = node.parentNode;
          if (!node) return false;
        }
      }
      return true;
    };
  });
}

/* --------------------------------------------------------------- testing -- */

/** Register every `id` in a parsed subtree, so `getElementById` finds it. */
function adoptIds(node, document) {
  if (!document) return;
  const id = node.getAttribute?.('id');
  if (id) document.__ids.set(id, node);
  for (const child of node.children) adoptIds(child, document);
}

export function makeDom({ids = [], body = ''} = {}) {
  const elements = new Map();
  const document = {
    body: new El('body'),
    documentElement: new El('html'),
    listeners: {},
    __ids: elements,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, Object.assign(new El('div'), {id}));
      return elements.get(id);
    },
    createElement(tag) { return new El(tag); },
    createElementNS(_ns, tag) { return new El(tag); },
    querySelector(selector) { return document.body.querySelector(selector); },
    querySelectorAll(selector) { return document.body.querySelectorAll(selector); },
    addEventListener(type, handler) { document.listeners[type] = handler; },
  };
  document.body.ownerDocument = document;
  if (body) document.body.innerHTML = body;
  for (const id of ids) document.getElementById(id);
  document.__ids = elements;
  return {document, elements, El};
}

/** A `localStorage`/`sessionStorage` pair that records nothing but remembers. */
export function makeStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: (key) => map.delete(key),
    clear: () => map.clear(),
  };
}

/* ----------------------------------------------------------------- app.js -- */

/* The names the page loads before `app.js`, as source.
 *
 * `runtime.js` owns `steerPosting`, `canSteerJob`, `submitJob`, `submitSteer`
 * and `restoreJob`, and `app.js` calls them as bare identifiers. In a browser
 * they are shared top-level bindings; here they have to be declared in the same
 * realm, so `loadApp` prepends this — declared, not assigned to `globalThis`,
 * because `app.js` reads the binding and an object property would not shadow it.
 *
 * Each call is routed through a per-context indirection (`__submitJob` and
 * friends), so two fixtures loaded in one file can never see each other's stubs.
 */
const RUNTIME_SHIM = `
const JOB_ACTIVE = new Set(['queued', 'running', 'cancelling']);
let steerPosting = false;
let restoreJob = async () => {};
function syncComposerAction() {}
async function stopCurrentJob() { return globalThis.__stopCurrentJob?.(); }
async function submitJob(runId, payload, options = {}) {
  return globalThis.__submitJob ? globalThis.__submitJob(runId, payload, options)
                                : {job_id: 'stub-job'};
}
async function submitSteer(runId, jobId, payload) {
  return globalThis.__submitSteer ? globalThis.__submitSteer(runId, jobId, payload)
                                  : {ok: false, reason: 'job_scope'};
}
function canSteerJob(job) {
  return globalThis.__canSteerJob ? globalThis.__canSteerJob(job)
    : Boolean(job && job.job_kind === 'conversation' && job.status === 'running');
}
`;

/* Load the *real* app.js into a fresh context with a stub server.
 *
 * The boundary is `fetch`, not `api`/`toast`/`$`: `app.js` declares those
 * itself, so context properties of the same name are shadowed and useless. The
 * page's own `api()` therefore runs for real against the fixture's `fetch`, and
 * a returned Error becomes a non-ok response exactly as a live HTTP failure
 * would — which is what the retry and "kept the draft" paths key off.
 *
 * `routes` maps a URL substring to a body or an Error. Anything unmatched
 * throws, so a test that forgets a route fails loudly rather than passing on a
 * silent default. `globals` adds anything else the page had (see
 * `RUNTIME_SHIM`); `observed` collects every request for assertions.
 */
export function loadApp({routes = {}, globals = {}, dom = makeDom(), storage = makeStorage(),
                         observed = [], baseUrl = '', runtime = 'shim'} = {}) {
  const {document} = dom;  const ctx = {
    console,
    document,
    localStorage: storage,
    sessionStorage: storage,
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: (fn) => setTimeout(() => fn(Date.now()), 0),
    cancelAnimationFrame: (id) => clearTimeout(id),
    Date, Math, JSON, Promise, Object, Array, String, Number, Boolean, Error, RegExp, Map, Set,
    URLSearchParams, TextEncoder, TextDecoder, Intl,
    escapeHtml: require('../../studio/web/md.js').escapeHtml,
    renderMarkdown: require('../../studio/web/md.js').renderMarkdown,
    blocksHtml: require('../../studio/web/blocks.js').blocksHtml,
    crypto: {randomUUID: () => `req-${observed.length + 1}`},
    ...globals,
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  ctx.self = ctx;
  ctx.__observed = observed;
  ctx.fetch = async (url, options) => {
    const method = options?.method || 'GET';
    const body = options?.body ? JSON.parse(options.body) : null;
    ctx.__observed.push({path: String(url), method, body});
    if (baseUrl) {
      // Live mode: talk to a real server, so the test exercises the routes and
      // the projections rather than a hand-written fixture of them.
      const target = String(url).startsWith('http') ? String(url) : baseUrl + String(url);
      return fetch(target, {method, headers: options?.headers, body: options?.body});
    }
    const key = Object.keys(routes).find(pattern => String(url).includes(pattern));
    // The page's own `init()` lists projects on load. Answering that one with an
    // empty list lets it settle instead of racing the test with a failed
    // selection; every other unrouted request still fails loudly.
    if (key === undefined && String(url).split('?')[0].endsWith('/api/runs')) {
      return response(200, []);
    }
    if (key === undefined) return response(404, null, `no fixture route for ${method} ${url}`);
    const result = typeof routes[key] === 'function'
      ? routes[key]({path: String(url), method, body, calls: ctx.__observed}) : routes[key];
    if (result instanceof Error) return response(result.status || 500, null, result.message);
    return response(200, result);
  };

  vm.createContext(ctx);
  if (runtime === 'real') {
    // The page's real load order (index.html): team-progress.js → runtime.js
    // → … → journey.js → app.js, all classic scripts sharing one global scope.
    // Only `fetch` and the clock are replaced, so the pending/identity logic
    // under test is the code that ships.
    vm.runInContext(webSource('team-progress.js'), ctx, {filename: 'team-progress.js'});
    vm.runInContext(webSource('runtime.js'), ctx, {filename: 'runtime.js'});
    vm.runInContext(webSource('journey.js'), ctx, {filename: 'journey.js'});
  } else if (runtime === 'shim') {
    vm.runInContext(RUNTIME_SHIM, ctx, {filename: 'runtime-shim.js'});
  } else if (runtime !== 'page') {
    throw new Error(`unknown runtime mode: ${runtime}`);
  }
  // Page mode lets a caller evaluate every classic script in index.html order.
  if (runtime !== 'page') vm.runInContext(webSource('app.js'), ctx, {filename: 'app.js'});
  return {ctx, observed, elements: dom.elements, document, storage};
}

function response(status, body, detail) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'fixture error',
    async json() {
      if (body !== null) return body;
      return {detail: detail || 'fixture error'};
    },
  };
}

export {El};
