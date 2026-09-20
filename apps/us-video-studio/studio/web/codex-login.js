"use strict";

function createCodexLogin(host, isDisabled = () => false) {
  const status = box("p", "connection-verification", "正在检查本机登录…");
  status.setAttribute("role", "status");
  const login = box("button", "primary small", "登录 / 授权"); login.type = "button";
  const refresh = box("button", "ghost small", "刷新登录状态"); refresh.type = "button";
  const cancel = box("button", "ghost small", "取消本次登录"); cancel.type = "button";
  const actions = box("div", "codex-login-actions"); actions.append(login, refresh, cancel);
  const help = box("p", "muted", "使用你的 ChatGPT 账号在系统浏览器完成官方授权。无需填写 API Key；登录不会开始创作或改变角色分配。登录状态不代表剩余额度或模型调用已验证。");
  host.append(status, actions, help);
  let visible = false, disposed = false, busy = false, sequence = 0, timer = null;
  let current = {status:"checking", login_active:false};
  function stopTimer() { if (timer !== null) clearTimeout(timer); timer = null; }
  function sync() {
    const disabled = busy || isDisabled();
    login.disabled = disabled || current.login_active || current.auth_method === "api_key" || ["checking", "logged_in", "unavailable"].includes(current.status);
    login.textContent = current.status === "logged_in" ? "已登录 ChatGPT" : current.login_active ? "等待授权完成" : "登录 / 授权";
    refresh.disabled = disabled;
    cancel.hidden = !current.login_active;
    cancel.disabled = disabled || !current.attempt_id;
  }
  function draw(value) {
    current = value;
    status.textContent = value.message || "暂时无法确认登录状态，请刷新后重试。";
    status.className = "connection-verification" + (value.status === "logged_in" ? " verified" : ["failed", "unavailable"].includes(value.status) ? " error" : "");
    help.textContent = value.login_active
      ? "请在系统浏览器完成授权；完成后这里会自动更新。关闭设置只停止页面查询，不取消登录。浏览器未打开时，可取消后在终端运行 codex login。"
      : "使用你的 ChatGPT 账号在系统浏览器完成官方授权。无需填写 API Key；登录不会开始创作或改变角色分配。登录状态不代表剩余额度或模型调用已验证。";
    sync();
  }
  async function request(action = "status") {
    if (!visible || disposed || busy || isDisabled()) return;
    stopTimer(); busy = true; let poll = false; const ticket = ++sequence; sync();
    try {
      const url = action === "status" ? "/api/connections/codex/status"
        : action === "login" ? "/api/connections/codex/login" : "/api/connections/codex/login/cancel";
      const body = action === "cancel" ? {attempt_id:current.attempt_id} : {consent:true};
      const result = await api(url, action === "status" ? undefined : {method:"POST", body:JSON.stringify(body)});
      if (disposed || !visible || ticket !== sequence) return;
      draw(result);
      poll = result.login_active || result.status === "checking";
    } catch (error) {
      if (disposed || !visible || ticket !== sequence) return;
      // A transport failure may follow an accepted login; refresh status instead of auto-retrying POST.
      status.textContent = error.status === 404 ? "当前服务尚未加载登录功能，请重启工作台服务，再刷新登录状态。"
        : error.status === 409 ? String(error.message) + " 请刷新登录状态后再操作。"
        : "未能确认登录状态。请刷新状态后再操作；不会自动重复发起授权。";
      status.className = "connection-verification error";
      current = {...current, status:"checking"};
    } finally {
      if (ticket === sequence) {
        busy = false; sync();
        if (visible && !disposed && poll) {
          timer = setTimeout(() => request(), 2500);
        }
      }
    }
  }
  login.onclick = () => { if (!login.disabled) return request("login"); };
  refresh.onclick = () => { if (!refresh.disabled) return request(); };
  cancel.onclick = () => { if (!cancel.disabled) return request("cancel"); };
  sync();
  return {
    setVisible(value) {
      host.hidden = !value;
      if (disposed || value === visible) return;
      visible = value; stopTimer(); ++sequence; busy = false;
      if (value) { current = {status:"checking", login_active:false}; sync(); request(); }
    },
    sync,
    dispose() { disposed = true; visible = false; ++sequence; stopTimer(); },
  };
}
