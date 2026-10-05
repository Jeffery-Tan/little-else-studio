"use strict";

const ROLE_NAMES = { orchestrator: "主制片", director: "创意导演", material: "视觉策划", writer: "编剧", reviewer: "独立审片（已停用）" };
const ACTIVE_CONNECTION_ROLES = ["orchestrator", "director", "material", "writer"];
const ROLE_DESCRIPTIONS = { orchestrator: "理解需求，统筹每一步", director: "找到切入点与表达方向", material: "安排画面、动作与素材", writer: "把创意写成可拍的脚本", reviewer: "保留历史配置；当前工作流不会调用" };
const PROVIDER_NAMES = { codex_cli: "Codex · 本机登录", deepseek: "DeepSeek", openai_compat: "其他服务 · OpenAI 兼容", anthropic_compat: "其他服务 · Anthropic 兼容" };
const DEEPSEEK_MODELS = ["deepseek-flash", "deepseek-v4-pro"];
const DEEPSEEK_URL = "https://api.deepseek.com/v1";

$("connections-open").onclick = async () => {
  const button = $("connections-open");
  if (button.disabled) return;
  button.disabled = true;
  try {
    const [config, capabilities] = await Promise.all([api("/api/connections"), api("/api/capabilities")]);
    let runtime = null;
    try { runtime = await api("/api/runtime"); } catch {}
    openConnectionDialog({...config, capabilities, runtime, autoDiscoverCodex: true});
  }
  catch (err) { toast(err.message, true); }
  finally { button.disabled = false; }
};
function field(label, input) {
  const node = box("label", "connection-field"); node.append(box("span", "", label), input); return node;
}
function textInput(value = "", placeholder = "") {
  const input = document.createElement("input"); input.value = value; input.placeholder = placeholder; return input;
}
function selectInput(options, selected) {
  const input = document.createElement("select");
  for (const [value, label] of Object.entries(options)) {
    const option = document.createElement("option"); option.value = value; option.textContent = label; input.append(option);
  }
  input.value = selected; return input;
}

function openConnectionDialog(config) {
  if (document.querySelector?.(".connection-dialog")) return;
  const dialog = document.createElement("dialog"); dialog.className = "connection-dialog";
  const form = document.createElement("form"), rows = [], roleInputs = {};
  let closed = false, saving = false, codexChecking = false, codexModels = null;
  const heading = box("div", "connection-heading");
  const close = box("button", "ghost", "关闭"); close.type = "button";
  close.onclick = () => { if (!saving) dialog.close(); };
  heading.append(box("h2", "", "你的制作团队"), close);
  form.append(heading, box("p", "muted", "先连接模型服务，再为团队成员分配模型。已有角色不会自动更换。"));
  const capabilities = box("section", "connection-capabilities");
  const mediaBoundaries = config.capabilities
    ? `${config.capabilities.image_analysis.ui_summary} ${config.capabilities.video_understanding.ui_summary} ${config.capabilities.production.ui_summary}`
    : "能力说明暂时不可用，请重新打开模型设置。";
  capabilities.append(box("strong", "", "这支团队现在能做什么"),
    box("p", "", "需求梳理、创意方向、画面规划与脚本。主制片负责自检；独立审阅已停用。这里分配的是文字策划模型。"),
    box("p", "muted", mediaBoundaries));
  form.append(capabilities);
  const runtime = box("details", "connection-runtime");
  const loaded = config.runtime;
  runtime.append(box("summary", "", "当前服务版本与环境"),
    box("p", "muted", loaded
      ? `进程启动：${loaded.started_at} · 代码指纹：${loaded.code_fingerprint} · 数据根标识：${loaded.data_root_id} · Python ${loaded.python}。FFmpeg：${loaded.media_tools?.ffmpeg ? '可找到' : '未找到'}。${loaded.meaning}`
      : "运行信息暂不可用；请确认后端已更新并重新打开模型设置。"));
  if (loaded) runtime.append(box("p", "muted", loaded.hot_reload?.enabled
    ? `热重载已开启。${loaded.hot_reload.message || '代码修改后自动更新；执行中等待本轮结束。'}`
    : "热重载未开启。请使用新版启动脚本启动服务。"));
  form.append(runtime);
  const steps = box("ol", "connection-steps");
  for (const step of ["连接服务", "验证密钥与模型", "分配角色", "保存设置"]) steps.append(box("li", "", step));
  form.append(steps, box("p", "connection-privacy", "API Key 仅在本机加密保存，不回显。验证只读取模型列表，不发起内容生成；保存不会调用模型。"));
  const connectionsHost = box("div", "connection-list"), rolesHost = box("div", "role-grid");
  form.append(box("h3", "", "1 · 连接你的模型服务"), connectionsHost);
  const error = box("p", "connection-error"); error.setAttribute("role", "alert");
  const save = box("button", "primary", "保存团队设置"); save.type = "submit";
  const teamSummary = box("p", "connection-team-summary"); teamSummary.setAttribute("role", "status");
  function selectedModel(fields) { return fields.model ? fields.model.value.trim() : ""; }
  function endpointMatches(row) {
    const endpoint = row.provider.value === "codex_cli" ? "" : row.provider.value === "deepseek" ? DEEPSEEK_URL : row.baseUrl.value.trim().replace(/\/+$/, "");
    return row.provider.value === row.original.provider && endpoint === String(row.original.base_url || "").replace(/\/+$/, "");
  }
  function updateRoleSummary() {
    let incomplete = 0, changed = 0;
    for (const fields of Object.values(roleInputs)) {
      const row = rows.find(item => item.id === fields.connection.value), model = selectedModel(fields);
      const original = config.roles[fields.role];
      if (original?.connection !== fields.connection.value || (original?.model || "") !== model
          || (row && !endpointMatches(row))) changed++;
      if (ACTIVE_CONNECTION_ROLES.includes(fields.role) && (!row || (row.provider.value !== "codex_cli" && (!model || !(row.key.value || (row.original.has_key && endpointMatches(row)))
          || (row.provider.value !== "deepseek" && !row.baseUrl.value.trim()))))) incomplete++;
      fields.priceHelp.hidden = !row || row.provider.value === "codex_cli";
      if (!row) { fields.cost.textContent = "请先选择模型服务。"; continue; }
      if (row.provider.value === "codex_cli") {
        fields.cost.textContent = "使用本机 Codex 额度；不按 API 单价估算，登录与可用额度在任务运行时确认。";
        continue;
      }
      if (!model) { fields.cost.textContent = "请明确选择模型，再核对它的价格。"; continue; }
      if (!row.isNew && !endpointMatches(row)) { fields.cost.textContent = "服务已更换：保存时会清空旧价格，请保存后为新服务重新核实价格。"; continue; }
      const price = [...row.priceRows].reverse().find(item => item.modelInput.value.trim() === model);
      const known = value => value !== "" && Number.isFinite(Number(value)) && Number(value) >= 0;
      if (!price || !known(price.input.value) || !known(price.output.value) || !price.source.value.trim()) {
        fields.cost.textContent = "价格待核实 · 此模型尚未填写完整的输入、输出单价及来源；不会按零费用显示。";
      } else {
        fields.cost.textContent = `参考价格：输入 $${Number(price.input.value)} / 输出 $${Number(price.output.value)}，每百万 token。${price.asOf.value ? `核实于 ${price.asOf.value}。` : "核实日期未填写。"}实际账单以提供方为准。`;
      }
    }
    teamSummary.textContent = `${ACTIVE_CONNECTION_ROLES.length - incomplete} / ${ACTIVE_CONNECTION_ROLES.length} 个角色配置齐全${incomplete ? ` · ${incomplete} 个角色还需模型、密钥或地址` : ""}。${changed ? `${changed} 个角色分配已修改，尚未保存。` : "角色分配未改变。"}配置齐全不代表已通过真实创作测试。`;
  }
  function refreshModel(fields, value = selectedModel(fields)) {
    const row = rows.find(item => item.id === fields.connection.value);
    let input;
    if (row?.provider.value === "codex_cli" && codexModels) {
      const currentDefault = codexModels.find(item => item.is_default);
      const options = Object.assign(Object.create(null), { "": currentDefault
        ? `本机默认 · ${currentDefault.display_name}` : "本机默认模型" });
      for (const item of codexModels) options[item.model] = item.display_name === item.model
        ? item.model : `${item.display_name} · ${item.model}`;
      if (value && !codexModels.some(item => item.model === value)) options[value] = `${value} · 保留原配置`;
      input = selectInput(options, value);
      fields.note.textContent = value && !codexModels.some(item => item.model === value)
        ? "原模型不在本机当前列表中，已保留原值；请确认后保留或主动改选。"
        : "选项来自本机 Codex；空值沿用默认模型，列表读取不代表已验证额度或创作质量。";
    } else if (row?.provider.value === "deepseek") {
      const ids = row.verified ? row.verified.model_ids : DEEPSEEK_MODELS;
      const options = Object.assign(Object.create(null), { "": "请选择模型" });
      for (const id of ids) options[id] = id;
      if (value && !ids.includes(value)) options[value] = `${value} · 保留原配置`;
      input = selectInput(options, value);
      fields.note.textContent = value && !ids.includes(value)
        ? "当前模型不在这份模型列表中，已保留原值。请确认后保留，或主动改选。"
        : row.verified ? "选项来自本次验证返回的模型列表；尚未进行创作调用测试。"
          : "暂用预设模型列表；验证后会显示服务实际返回的模型。";
    } else {
      input = textInput(value, row?.provider.value === "codex_cli" ? "留空使用本机默认模型" : "提供方的模型 ID");
      fields.note.textContent = row?.provider.value === "codex_cli" ? "沿用本机 Codex 登录；留空使用其默认模型。" : "按服务提供方填写模型 ID，保留已有模型设置。";
    }
    input.disabled = saving;
    input.required = Boolean(row && row.provider.value !== "codex_cli");
    input.setAttribute("aria-label", `${ROLE_NAMES[fields.role]}模型`);
    input.onchange = () => { fields.models.set(fields.connection.value, input.value.trim()); if (row?.provider.value === "deepseek") refreshModel(fields, input.value.trim()); else updateRoleSummary(); };
    input.oninput = () => { fields.models.set(fields.connection.value, input.value.trim()); updateRoleSummary(); };
    fields.model = input; fields.modelHost.replaceChildren(input);
    updateRoleSummary();
  }
  function syncButtons() {
    close.disabled = saving; save.disabled = saving;
    for (const row of rows) {
      row.codexLogin?.sync();
      row.check.disabled = saving || row.checking;
      row.codexModelCheck.disabled = saving || codexChecking;
      if (row.remove) {
        const bound = Object.values(roleInputs).some(fields => fields.connection.value === row.id);
        row.remove.disabled = saving || row.checking || bound;
        row.remove.title = bound ? "先将使用此服务的角色分配到其他服务" : "取消这条尚未保存的服务";
      }
    }
    updateRoleSummary();
  }
  function refreshChoices() {
    const counts = new Map();
    for (const row of rows) counts.set(row.name.value, (counts.get(row.name.value) || 0) + 1);
    for (const fields of Object.values(roleInputs)) {
      const previous = fields.connection.value || fields.originalConnection;
      fields.connection.replaceChildren();
      for (const row of rows) {
        const option = document.createElement("option"); option.value = row.id;
        const name = row.name.value || "未命名服务";
        option.textContent = counts.get(row.name.value) > 1 ? `${name} · ${row.id}` : name;
        fields.connection.append(option);
      }
      fields.connection.value = previous; refreshModel(fields);
    }
    syncButtons();
  }
  function invalidate(row) {
    row.editVersion++; row.verified = null; row.status.className = "connection-verification";
    row.status.textContent = "尚未验证。可以先保存，验证并不等于实际创作测试。";
    for (const fields of Object.values(roleInputs)) if (fields.connection.value === row.id) refreshModel(fields);
    row.readiness.textContent = row.provider.value === "codex_cli" ? "本机登录 · 任务开始时检查连接"
      : row.key.value ? "密钥已填写 · 尚未保存" : row.original.has_key && endpointMatches(row) ? "密钥已保存 · 不回显" : "等待填写 API Key";
    updateRoleSummary();
  }
  function addConnection(connection, isNew = false) {
    const card = box("section", "connection-card"); card.dataset.connectionId = connection.id;
    const name = textInput(connection.name || "DeepSeek"); name.required = true;
    const provider = selectInput(PROVIDER_NAMES, connection.provider || "deepseek");
    const baseUrl = textInput(connection.base_url || "", "https://api.example.com/v1");
    const tokenLimit = selectInput({ max_completion_tokens: "现代接口（max_completion_tokens）", max_tokens: "旧兼容接口（max_tokens）" }, connection.token_limit_parameter || "max_completion_tokens");
    const key = textInput("", connection.has_key ? "已保存；留空保留" : "输入 API Key");
    key.type = "password"; key.autocomplete = "new-password"; key.spellcheck = false;
    function savedKeyMatches() {
      const endpoint = provider.value === "deepseek" ? DEEPSEEK_URL : baseUrl.value.trim().replace(/\/+$/, "");
      return Boolean(connection.has_key && provider.value === connection.provider
        && endpoint === String(connection.base_url || "").replace(/\/+$/, ""));
    }
    function updateKeyHint() {
      key.placeholder = savedKeyMatches() ? "已保存；留空保留"
        : connection.has_key ? "服务已改变，请重新填写 API Key" : "输入 API Key";
    }
    const status = box("p", "connection-verification", "尚未验证。可以先保存，验证并不等于实际创作测试。"); status.setAttribute("role", "status");
    const check = box("button", "ghost", "验证密钥与模型"); check.type = "button";
    const readiness = box("span", "connection-readiness");
    const row = { id: connection.id, name, provider, baseUrl, key, tokenLimit, card, status, check, readiness,
      checking: false, editVersion: 0, verified: null, priceRows: [], isNew, original: connection };
    rows.push(row);
    const header = box("div", "connection-card-heading"); header.append(box("strong", "", isNew ? "新的模型服务" : "已保存的模型服务"), readiness);
    if (isNew) {
      const remove = box("button", "ghost small", "取消添加"); remove.type = "button"; row.remove = remove;
      remove.onclick = () => {
        if (saving || row.checking || Object.values(roleInputs).some(fields => fields.connection.value === row.id)) return;
        row.editVersion++; row.key.value = ""; row.codexLogin?.dispose(); rows.splice(rows.indexOf(row), 1); card.remove(); refreshChoices();
      };
      header.append(remove);
    }
    const grid = box("div", "connection-grid"); grid.append(field("选择服务", provider), field("服务名称", name));
    const apiFields = box("div", "connection-key"); apiFields.append(field("API Key", key));
    const verification = box("div", "connection-check");
    verification.append(check, status, box("p", "muted", "仅验证认证与模型列表，不代表 Agent 已实际运行，也不会自动保存 Key 或更换角色。"));
    const hint = box("p", "muted"), advanced = box("details", "connection-advanced"), protocolFields = box("div", "connection-grid");
    const codexLoginHost = box("div", "codex-login");
    row.codexLogin = createCodexLogin(codexLoginHost, () => saving || closed);
    const codexModelHost = box("div", "connection-check");
    const codexModelCheck = box("button", "ghost", "读取本机可选模型"); codexModelCheck.type = "button";
    const codexModelStatus = box("p", "connection-verification", "点击读取后可在下方为每个角色选择 Codex 模型；不会调用模型。候选列表以本机登录返回为准。");
    codexModelStatus.setAttribute("role", "status");
    codexModelHost.append(codexModelCheck, codexModelStatus);
    row.codexModelCheck = codexModelCheck;
    const tokenField = field("输出上限参数（按提供方接口选择）", tokenLimit);
    protocolFields.append(field("服务地址（不含具体接口路径）", baseUrl), tokenField);
    const pricingHost = box("div", "pricing-list"), pricingSection = box("details", "");
    row.advanced = advanced; row.pricingSection = pricingSection;
    pricingSection.append(box("summary", "", "可选：模型价格与来源"), box("p", "muted", "未填写时费用保持待核实，不套用其他模型价格。单位为美元 / 百万 token。"), pricingHost);
    function addPrice(model = "", price = {}) {
      const line = box("div", "price-row");
      const modelInput = textInput(model, "模型 ID"), input = textInput(price.input_usd_per_mtok ?? "", "未知");
      const output = textInput(price.output_usd_per_mtok ?? "", "未知"), source = textInput(price.source || "", "官方价格链接或合同来源"), asOf = textInput(price.as_of || ""); asOf.type = "date";
      for (const number of [input, output]) { number.type = "number"; number.min = "0"; number.step = "any"; }
      for (const control of [modelInput, input, output, source, asOf]) control.oninput = () => invalidate(row);
      line.append(field("模型 ID", modelInput), field("输入单价", input), field("输出单价", output), field("价格来源", source), field("核实日期", asOf));
      row.priceRows.push({ modelInput, input, output, source, asOf }); pricingHost.append(line);
    }
    for (const [model, price] of Object.entries(connection.pricing || {})) addPrice(model, price);
    const priceAdd = box("button", "ghost small", "＋ 模型价格"); priceAdd.type = "button";
    priceAdd.onclick = () => { if (!saving) { addPrice(); invalidate(row); } }; pricingSection.append(priceAdd);
    advanced.append(box("summary", "", "高级连接与费用设置"), protocolFields, pricingSection);
    function providerChanged(changed = true) {
      const deepseek = provider.value === "deepseek", codex = provider.value === "codex_cli";
      if (deepseek) { baseUrl.value = DEEPSEEK_URL; tokenLimit.value = "max_tokens"; }
      updateKeyHint();
      apiFields.hidden = codex; verification.hidden = !deepseek; codexModelHost.hidden = !codex;
      protocolFields.hidden = deepseek || codex; advanced.hidden = codex;
      row.codexLogin.setVisible(codex);
      tokenField.hidden = provider.value !== "openai_compat";
      hint.textContent = deepseek ? "已预设 DeepSeek 官方连接，无需填写地址或兼容参数。填入 Key 后可先验证。"
        : codex ? "使用本机 Codex 登录。模型按下方角色设置；主制片不会自动改用其他服务。"
          : "在高级设置中填写服务地址及兼容参数。切换地址或协议后，请重新填写 Key 并核实价格。";
      if (changed) invalidate(row);
      else readiness.textContent = codex ? "本机登录 · 任务开始时检查连接" : connection.has_key ? "密钥已保存 · 不回显" : "等待填写 API Key";
      refreshChoices();
    }
    provider.onchange = () => {
      key.value = "";
      for (const fields of Object.values(roleInputs)) {
        fields.models.delete(row.id);
        if (fields.connection.value === row.id) fields.model.value = "";
      }
      providerChanged();
    }; key.oninput = () => invalidate(row);
    baseUrl.oninput = () => { updateKeyHint(); invalidate(row); };
    tokenLimit.onchange = () => invalidate(row);
    name.oninput = () => { invalidate(row); refreshChoices(); };
    check.onclick = async () => {
      if (closed || saving || row.checking || provider.value !== "deepseek") return;
      invalidate(row);
      if (!key.value && !savedKeyMatches()) {
        status.textContent = "请先填写 DeepSeek API Key。"; status.className = "connection-verification error"; return;
      }
      const version = row.editVersion, payload = { expected_revision: config.revision, connection_id: row.id };
      if (key.value) payload.api_key = key.value;
      row.checking = true; status.textContent = "正在验证密钥与模型…"; syncButtons();
      try {
        const result = await api("/api/connections/deepseek/check", { method: "POST", body: JSON.stringify(payload) });
        if (closed || row.editVersion !== version || provider.value !== "deepseek") return;
        if (result.status !== "verified" || result.scope !== "credentials_and_models" || !Array.isArray(result.model_ids)
            || !result.model_ids.length || result.model_ids.some(id => typeof id !== "string" || !id.trim())) throw new Error("未取得可用模型列表，请稍后重新验证。");
        row.verified = { ...result, model_ids: [...new Set(result.model_ids)] }; status.className = "connection-verification verified";
        status.textContent = `密钥与模型列表已验证 · ${row.verified.model_ids.length} 个模型。尚未进行实际创作测试。`;
        for (const fields of Object.values(roleInputs)) if (fields.connection.value === row.id) refreshModel(fields);
      } catch (err) {
        if (closed || row.editVersion !== version) return;
        row.verified = null; status.className = "connection-verification error";
        status.textContent = String(err.message || "验证未完成，请检查 Key 后重试。");
        if (err.status === 409) status.textContent += " 本次输入仍保留，未保存或覆盖设置。";
      } finally { row.checking = false; if (!closed) syncButtons(); }
    };
    codexModelCheck.onclick = async () => {
      if (closed || saving || codexChecking || provider.value !== "codex_cli") return;
      codexChecking = true; codexModelStatus.className = "connection-verification";
      codexModelStatus.textContent = "正在读取本机 Codex 可选模型…"; syncButtons();
      try {
        const result = await api("/api/connections/codex/models");
        if (closed || provider.value !== "codex_cli") return;
        if (result.source !== "codex_cli" || result.model_invoked !== false || !Array.isArray(result.models)
            || !result.models.length || result.models.some(item => !item || typeof item.model !== "string"
              || !item.model.trim() || typeof item.display_name !== "string")) throw new Error("本机未返回可用模型列表。");
        codexModels = result.models; codexModelStatus.className = "connection-verification verified";
        codexModelStatus.textContent = `已读取 ${codexModels.length} 个本机可选模型；尚未发起创作或验证额度。`;
        for (const fields of Object.values(roleInputs)) {
          const bound = rows.find(item => item.id === fields.connection.value);
          if (bound?.provider.value === "codex_cli") refreshModel(fields);
        }
      } catch (err) {
        if (closed) return;
        codexModelStatus.className = "connection-verification error";
        codexModelStatus.textContent = String(err.message || "读取失败；可保留原模型设置或稍后重试。");
      } finally { codexChecking = false; if (!closed) syncButtons(); }
    };
    card.append(header, grid, apiFields, hint, codexLoginHost, codexModelHost, verification, advanced); connectionsHost.append(card); providerChanged(false);
    return row;
  }
  for (const connection of config.connections) addConnection(connection);
  const add = box("button", "ghost", "＋ 添加模型服务"); add.type = "button";
  add.onclick = () => { if (!saving) addConnection({ id: `connection-${crypto.randomUUID().slice(0, 8)}` }, true); };
  form.append(add, box("h3", "", "2 · 为团队分配模型"), box("p", "muted", "每个角色独立选择服务与模型。原模型会保留，切换服务后请明确选择。价格按所选服务和准确模型分别核对，可在上方服务的高级设置中填写。"), teamSummary, rolesHost);
  for (const [role, label] of Object.entries(ROLE_NAMES)) {
    const binding = config.roles[role] || { connection: config.connections[0]?.id || "", model: "" };
    const line = box("div", "role-row"); line.dataset.role = role;
    const connection = document.createElement("select"), modelHost = box("div", "role-model-input"), note = box("span", "role-model-note"), cost = box("span", "role-cost-note");
    const priceHelp = box("button", "ghost small role-price-link", "填写 / 核对价格"); priceHelp.type = "button";
    priceHelp.onclick = () => {
      const row = rows.find(item => item.id === connection.value);
      if (!row || saving || row.provider.value === "codex_cli") return;
      row.advanced.open = true; row.pricingSection.open = true;
      row.pricingSection.scrollIntoView?.({ block: "center", behavior: "smooth" });
    };
    const fields = { role, connection, modelHost, note, cost, priceHelp, originalConnection: binding.connection, previousConnection: binding.connection,
      model: textInput(binding.model || ""), models: new Map([[binding.connection, binding.model || ""]]) };
    roleInputs[role] = fields;
    connection.onchange = () => {
      fields.models.set(fields.previousConnection, selectedModel(fields)); fields.previousConnection = connection.value;
      refreshModel(fields, fields.models.get(connection.value) || ""); syncButtons();
    };
    const identity = box("div", "role-identity"); identity.append(box("strong", "", label), box("span", "muted", ROLE_DESCRIPTIONS[role]));
    const modelField = box("div", "connection-field"); modelField.append(box("span", "", "模型"), modelHost, note, cost, priceHelp);
    line.append(identity, field("模型服务", connection), modelField); rolesHost.append(line);
  }
  refreshChoices();
  const actions = box("div", "dialog-actions"); actions.append(save);
  form.append(box("p", "connection-save-note", "3 · 保存后从下一次任务生效，不改变正在运行的任务，也不会重写已有稿件。可以保存尚未验证的配置；这不会被标记为已实测。"), error, actions);
  form.onsubmit = async event => {
    event.preventDefault(); if (saving || closed) return; error.textContent = "";
    if (rows.some(row => row.checking)) { error.textContent = "正在验证密钥与模型，请等验证结束后再保存。"; return; }
    if (codexChecking) { error.textContent = "正在读取本机模型列表，请等候完成后再保存。"; return; }
    const connections = rows.map(row => {
      const pricing = {};
      for (const price of row.priceRows) {
        if (!price.modelInput.value.trim()) continue;
        pricing[price.modelInput.value.trim()] = { source: price.source.value.trim(), as_of: price.asOf.value,
          input_usd_per_mtok: price.input.value === "" ? null : Number(price.input.value), output_usd_per_mtok: price.output.value === "" ? null : Number(price.output.value) };
      }
      const result = { id: row.id, name: row.name.value.trim(), provider: row.provider.value, pricing,
        base_url: row.provider.value === "codex_cli" ? "" : row.provider.value === "deepseek" ? DEEPSEEK_URL : row.baseUrl.value.trim(),
        token_limit_parameter: row.provider.value === "deepseek" ? "max_tokens" : row.tokenLimit.value };
      if (row.key.value && row.provider.value !== "codex_cli") result.api_key = row.key.value;
      return result;
    });
    const roles = Object.fromEntries(Object.entries(roleInputs).map(([role, fields]) => [role, { connection: fields.connection.value, model: selectedModel(fields) }]));
    saving = true; for (const control of form.querySelectorAll("input,select,button")) control.disabled = true; syncButtons();
    try {
      const saved = await api("/api/connections", { method: "PUT", body: JSON.stringify({ expected_revision: config.revision, connections, roles }) });
      rows.forEach(row => { row.key.value = ""; }); saving = false; dialog.close();
      toast(saved.warnings?.length ? "设置已保存。服务已改变，旧价格已清空，请核实后重新填写。" : "团队设置已保存，下一轮开始生效；保存不代表已完成创作测试。");
    } catch (err) {
      if (closed) return;
      error.textContent = err.status === 409 ? "配置已在其他窗口更新，未覆盖现有设置。本次输入仍保留；请关闭后重新打开设置，核对最新配置再保存。" : String(err.message || "保存未完成，输入仍保留，请重试。");
    } finally {
      saving = false;
      if (!closed) { for (const control of form.querySelectorAll("input,select,button")) control.disabled = false; syncButtons(); }
    }
  };
  dialog.append(form); document.body.append(dialog);
  dialog.addEventListener("cancel", event => { if (saving) event.preventDefault(); });
  dialog.addEventListener("close", () => { closed = true; rows.forEach(row => { row.editVersion++; row.key.value = ""; row.codexLogin?.dispose(); }); dialog.remove(); });
  dialog.showModal();
  if (config.autoDiscoverCodex) {
    const codex = rows.find(row => row.provider.value === "codex_cli");
    if (codex) void codex.codexModelCheck.onclick();
  }
}
