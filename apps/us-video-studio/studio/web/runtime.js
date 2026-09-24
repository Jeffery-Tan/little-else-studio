"use strict";

function detachProjectJob() {
  jobWatch++;
  clearTimeout(jobTimer);
  closeJobSource();
  currentJob=null;
  currentRecovery=null;
}

const JOB_ACTIVE = new Set(["queued", "running", "cancelling"]);
const JOB_LABELS = {
  queued: "等待执行", running: "正在执行", cancelling: "正在停止，等待当前调用返回",
  succeeded: "本轮完成", blocked: "本轮需要处理后继续", failed: "本轮执行失败",
  cancelled: "已停止", interrupted: "服务中断，本轮未自动重试",
};
const JOB_ROLE_NAMES = { orchestrator: "主制片", director: "创意导演", material: "视觉策划", writer: "编剧", reviewer: "独立审片" };
function jobGuidance(job) {
  const reason = String(job.reason || "");
  if (job.deliverable?.status === 'incomplete') return `本轮交付还缺：${job.deliverable.missing.join('、')}。已完成的内容已保留，请查看准确版本后继续。`;
  // Missing accounting data is diagnostic, never a creative workflow warning.
  if (/soft cap|预算.*上限|预算不足/i.test(reason)) return state.timeline?.budget?.limit_mode === 'none'
    ? "上一轮被旧额度设置中断。已保存内容仍可阅读；当前不会自动重试，请明确发送下一步要求。"
    : "这轮调用被当时的预算设置挡住。已保存内容仍可阅读；请核对当前设置后再决定下一步。";
  if (/Codex CLI is not installed or not on PATH/i.test(reason)) return "服务没有找到 Codex CLI。请确认本机已安装 Codex，重启工作台服务后再发送消息；已保存的内容不受影响。";
  if (/连接准备未通过|ConnectionUnavailable|CodexUnavailable/i.test(reason)) return "模型连接尚未准备好。请检查制作团队中的对应服务设置，再发送继续指令。";
  if (job.open_attempts?.length) return "有尚未结束的制作阶段。请先按下方恢复步骤处理遗留任务，再继续创作。";
  if (job.status === "failed") return "本轮未完成，已有内容已保留。请查看下方原因与恢复步骤，处理后再继续。";
  if (job.status === "blocked") return "本轮已暂停。先查看保存的内容和下方提示，再说明下一步要求。";
  if (job.status === "interrupted") return "服务曾中断，已有内容已保留。确认进度后，再发送新的继续指令。";
  return "";
}
function jobPresentation(job, recovery = null) {
  if (!job) return { active: false, label: "", detail: "" };
  if (job.status === 'blocked' && recovery?.can_continue
      && /unknown, partial or pending usage|reconcile provider evidence/i.test(String(job.reason || ''))) {
    return {active: false, label: '可以继续', detail: '上一轮未完成，已保存的内容可以继续使用。',
      guidance: '在对话中说明下一步，即可开始新一轮。'};
  }
  const events = job.events || [];
  const team = teamProgress(job);
  const active = JOB_ACTIVE.has(job.status);
  if (job.job_kind === 'reference_video') return {active,label:JOB_LABELS[job.status] || '状态待确认',
    detail:active ? '正在获取并分析参考视频；复刻任务会接续策划。' : job.status==='succeeded' ? '参考任务结果已保存，请查看分析报告和创作方案。' : '已完成的步骤会保留，可在参考视频入口继续或上传原视频。',guidance:jobGuidance(job)};
  if ((job.job_kind || '').startsWith('production_')) return {active, label:JOB_LABELS[job.status] || '状态待确认',
    detail:active?'正在处理制作任务；媒体生成进度与结果保存在制作页。':'本次制作状态已保存，请在制作页查看结果与用量。',guidance:jobGuidance(job)};
  if (job.job_kind === 'artifact_revision') return {active,label:JOB_LABELS[job.status] || '状态待确认',
    detail:active ? '正在将局部修改同步到新版本。' : job.status==='succeeded' ? '新版本已保存，请比较改动后重新审核。' : '',guidance:jobGuidance(job)};
  if (['image_analysis','video_analysis'].includes(job.job_kind)) return {active, label:JOB_LABELS[job.status] || '状态待确认',
    detail:active ? '视觉策划正在分析素材。可以离开页面，任务会继续；不会自动重试。'
      :job.status==='succeeded' ? '素材分析已保存，请核对并选择要采用的观察。' : '',guidance:jobGuidance(job)};
  if (job.brief) return {active, label:job.brief.title,
    detail:active ? team.current : job.brief.detail,
    guidance:active || job.status === 'succeeded' ? '' : jobGuidance(job)};
  const detail = active ? (job.status === "queued" ? "制作团队即将开始。" : job.status === "cancelling"
    ? "正在收尾当前步骤；结束后不再启动新的调用。" : team.current)
    : job.status === "succeeded" ? team.summary
      : job.status === "cancelled" ? "本轮已停止，已有内容与记录已保留。" : "";
  return { active: JOB_ACTIVE.has(job.status), label: JOB_LABELS[job.status] || "状态待确认",
    detail, guidance: jobGuidance(job) };
}

let jobWatch = 0;
let jobTimer = null;
let jobSource = null;
let jobSourceJobId = null;
let currentJob = null;
let currentRecovery = null;
let jobRenderKey = "";
let steerPosting = false;
function canSteerJob(job) {
  return Boolean(job && job.job_kind === 'conversation' &&
    (job.status === 'running' || (job.status === 'cancelling' && job.steer_status === 'received')));
}
function pendingJobKey(runId) { return `studio-job-pending:${runId}`; }
function pendingJobUiKey(runId) { return `studio-job-ui:${runId}`; }
function closeJobSource() {
  jobSource?.close();
  jobSource = null;
  jobSourceJobId = null;
}
function followJobEvents(runId, job, generation) {
  if (typeof EventSource === 'undefined' || jobSourceJobId === job.job_id) return;
  closeJobSource();
  const source = new EventSource(`/api/runs/${runId}/jobs/${job.job_id}/events?after_seq=${job.cursor || 0}`);
  jobSource = source;
  jobSourceJobId = job.job_id;
  let loading = false, newer = false;
  source.onmessage = async () => {
    if (generation !== jobWatch || runId !== state.runId || jobSource !== source) return;
    if (loading) { newer = true; return; }
    loading = true;
    try {
      do {
        newer = false;
        const updated = await api(`/api/runs/${runId}/jobs/${job.job_id}`);
        if (generation !== jobWatch || runId !== state.runId || jobSource !== source) return;
        await watchJob(runId, updated, generation);
      } while (newer && jobSource === source);
    } catch (_) { /* The safety poll retries after a transient read failure. */ }
    finally { loading = false; }
  };
  source.onerror = () => {
    if (jobSource !== source) return;
    closeJobSource();
    clearTimeout(jobTimer);
    jobTimer = setTimeout(() => watchJob(runId, job, generation), 1000);
  };
}
function pendingSteerKey(runId) { return `studio-steer-pending:${runId}`; }
function readPendingSteer(runId) {
  try { return JSON.parse(sessionStorage.getItem(pendingSteerKey(runId)) || "null"); }
  catch (_) { return null; }
}
function readPendingJob(runId) {
  try { return JSON.parse(sessionStorage.getItem(pendingJobKey(runId)) || "null"); }
  catch (_) { return null; }
}
function readPendingJobOptions(runId, payload) {
  try {
    const saved = JSON.parse(sessionStorage.getItem(pendingJobUiKey(runId)) || "{}");
    return saved.request_id === payload?.request_id ? {preserveComposerOnError:saved.preserveComposerOnError === true} : {};
  }
  catch (_) { return {}; }
}
function clearPendingJob(runId) {
  sessionStorage.removeItem(pendingJobKey(runId));
  sessionStorage.removeItem(pendingJobUiKey(runId));
}

function showJob(job, notice = "") {
  if (job && currentJob?.job_id === job.job_id && Number(currentJob.cursor || 0) > Number(job.cursor || 0)) return;
  currentJob = job;
  state.currentJob = job;
  if (typeof configureJourneyTabs === 'function') configureJourneyTabs();
  if (job?.brief) state.projectBrief = job.brief;
  const view = jobPresentation(job, currentRecovery);
  state.busy = view.active || Boolean(readPendingJob(state.runId));
  state.recoveryBlocked = Boolean((currentRecovery && !currentRecovery.can_continue)
    || (!currentRecovery && (job?.open_attempts?.length)));
  const adjusting = canSteerJob(job);
  $("send").disabled = (state.busy && !adjusting) || steerPosting || state.decisionBusy || state.recoveryBlocked;
  $("send").textContent = adjusting ? '调整任务 ↑' : '发送 ↑';
  $("send").setAttribute?.('aria-label', adjusting ? '调整当前任务' : '发送消息');
  if (typeof renderProjectProgress === 'function') renderProjectProgress();
  if (typeof renderJourneyDecision === 'function') renderJourneyDecision();
  const host = $("job-status");
  const lastMessage = [...(job?.events || [])].reverse().find((event) => event.say || event.message);
  const renderKey = JSON.stringify([state.runId, job?.job_id, job?.status, job?.events?.length, job?.goal_revision,
    job?.steer_status, job?.successor_job_id, view.detail, view.guidance, job?.reason,
    lastMessage?.say, lastMessage?.message, notice, job?.recovery, job?.unresolved_calls, job?.open_attempts, currentRecovery?.revision]);
  updateTeamDialog();
  if (renderKey === jobRenderKey) return;
  jobRenderKey = renderKey;
  host.replaceChildren();
  host.hidden = !job && !notice && !state.recoveryBlocked;
  if (host.hidden) return;
  host.setAttribute?.('data-active', String(view.active));
  const copy = box("div", "job-copy");
  // Project status is above both views. This strip is only the live execution
  // observer; historical usage cannot turn it back into a second status panel.
  if (job?.brief && !notice && !state.recoveryBlocked && ['conversation','stage_review'].includes(job.job_kind || 'conversation')) {
    if (view.active) copy.append(box('span','job-live',teamProgress(job).current || '团队正在处理当前步骤'));
    if (!view.active && view.guidance) {
      const warning = box('strong','job-warning',view.guidance);
      warning.setAttribute?.('role','alert');
      copy.append(warning);
    }
    if (job.steer_status === 'received' && view.active)
      copy.append(box('span','job-live','调整已收到，等待旧步骤结束后继续'));
    if (job.steer_status === 'blocked') {
      copy.append(box('span','job-warning',job.steer_reason || '调整已保存，暂时不能自动继续'));
      if (job.steer_latest_text) copy.append(box('span','muted',`最近的调整：${job.steer_latest_text.slice(0, 200)}`));
      const resumeAdjustment = box('button','ghost small','继续保存的调整');
      resumeAdjustment.onclick = async () => {
        resumeAdjustment.disabled = true;
        const runId = state.runId, generation = ++jobWatch;
        closeJobSource(); clearTimeout(jobTimer);
        try {
          const next = await api(`/api/runs/${runId}/jobs/${job.job_id}/resume-steer`,
            {method:'POST',body:'{}'});
          if (runId === state.runId && generation === jobWatch) await watchJob(runId, next, generation);
        } catch (err) {
          if (runId === state.runId) { resumeAdjustment.disabled = false; toast(err.message, true); await restoreJob(runId); }
        }
      };
      copy.append(resumeAdjustment);
    }
    const activity = box('button','text-button','团队工作记录 ↗');
    activity.onclick = openTeamProgress;
    copy.append(activity);
    if (view.active && (job.status !== 'cancelling' || job.steer_status === 'received')) {
      const stop = box('button','ghost small',job.steer_status === 'received' ? '停止接续' : '停止本轮');
      stop.onclick = async () => {
        const runId=state.runId, jobId=job.job_id;
        stop.disabled=true;
        try {const updated=await api(`/api/runs/${runId}/jobs/${jobId}/cancel`,{method:'POST',body:'{}'});
          if(state.runId===runId && currentJob?.job_id===jobId) showJob(updated);
        } catch(err) {toast(err.message,true);stop.disabled=false;}
      };
      copy.append(stop);
    }
    host.append(copy);
    return;
  }
  copy.append(box("strong", "", notice || view.label || "先处理待恢复事项"));
  if (view.detail) copy.append(box("span", "muted", view.detail));
  if (view.active && job?.events?.length) {
    const activity = teamProgress(job);
    const latest = activity.history.at(-1);
    if (latest) copy.append(box('span','job-latest',`最近操作：${latest.role} · ${latest.text}`));
    copy.append(box('span','job-updated',`最近进展 ${teamTime(activity.lastAt) || '时间未记录'} · 新进展会自动更新`));
  }
  if (job && !view.active && view.guidance) {
    const warning = box("strong", "job-warning", view.guidance);
    warning.setAttribute?.("role", "alert");
    copy.append(warning, box("span", "muted", "不会自动重试；只有你再次发起创作，才会启动新的模型调用。"));
  }
  if (job) {
    const activity = box('button','text-button job-activity-open','团队工作记录 ↗');
    activity.onclick = openTeamProgress;
    copy.append(activity);
  }
  host.append(copy);
  if (!view.active && (job?.job_kind || '').startsWith('production_')) {
    const production = box('button','primary small','查看制作记录');
    production.onclick = openProduction;
    copy.append(production);
  }
  if (!view.active && job?.job_kind === 'artifact_revision') {
    const review = box('button','primary small','查看修改版本');
    review.onclick = () => {selectStage(job.job_payload.stage);openRevisionWorkspace(state.runId,job.job_payload.stage);};
    copy.append(review);
  }
  if (!view.active && ['image_analysis','video_analysis','reference_video'].includes(job?.job_kind)) {
    const review = box('button','primary small',job?.job_kind === 'reference_video' ? '查看参考任务' : '查看素材分析');
    review.onclick = () => toggleMaterials(true);
    copy.append(review);
  }
  if (!view.active && currentRecovery && state.recoveryBlocked) renderRecoveryPanel(copy, currentRecovery);
  if (!view.active && currentRecovery?.can_continue && job && !['image_analysis','video_analysis','reference_video'].includes(job.job_kind) && ["interrupted", "failed", "cancelled", "blocked"].includes(job.status)) {
    const resume = box("button", "primary small", "准备继续创作");
    resume.onclick = () => {
      const text = "请先检查已保存的内容和当前进度，告诉我下一步建议。保留已确认的版本。";
      if (typeof prepareProjectDraft === "function") prepareProjectDraft(text);
      else { $("chat-input").value = text; $("chat-input").focus?.(); }
    };
    copy.append(resume);
  }
  if (view.active && job.status !== "cancelling") {
    const cancel = box("button", "ghost small", "停止本轮");
    cancel.onclick = async () => {
      const runId = state.runId, id = job.job_id;
      cancel.disabled = true;
      try {
        const updated = await api(`/api/runs/${runId}/jobs/${id}/cancel`, { method: "POST", body: "{}" });
        if (runId === state.runId && currentJob?.job_id === id) showJob(updated);
      } catch (err) { toast(err.message, true); cancel.disabled = false; }
    };
    host.append(cancel);
  }
}

const RECOVERY_STAGE_NAMES = { intake: "创作需求", direction: "创意方向", material_plan: "素材方案", writing: "脚本分镜" };
function recoveryField(parent, labelText, type = "text") {
  const label = box("label", "recovery-field");
  const field = box(type === "textarea" ? "textarea" : "input", "");
  if (type !== "textarea") field.type = type;
  label.append(box("span", "", labelText), field);
  parent.append(label);
  return field;
}

function renderRecoveryPanel(host, snapshot) {
  const panel = box("section", "recovery-panel");
  panel.append(box("h3", "", "找回创作进度"));
  panel.append(box("p", "muted", `已保留 ${snapshot.saved_artifacts?.length || 0} 个内容版本。恢复操作不会生成新内容，也不会重发模型请求。`));
  if (!snapshot.can_recover) panel.append(box("p", "", "制作团队仍在执行，请等本轮结束后再处理。"));
  for (const attempt of snapshot.open_attempts || []) {
    const card = box("details", "recovery-item");
    card.append(box("summary", "", `${RECOVERY_STAGE_NAMES[attempt.stage] || "制作步骤"}留下了未完成的尝试`));
    card.append(box("p", "", "关闭这次未完成的尝试，保留旧版本与全部费用记录。关闭不代表内容已完成或获得批准；未知用量仍需单独核对。"));
    const form = box("form", "recovery-form");
    const reason = recoveryField(form, "关闭原因", "textarea");
    reason.required = true; reason.maxLength = 2000;
    const confirm = recoveryField(form, "确认关闭当前未完成尝试，保留历史记录", "checkbox");
    confirm.required = true;
    const save = box("button", "ghost small", "关闭这次未完成尝试");
    save.type = "submit"; save.disabled = !snapshot.can_recover;
    form.append(save);
    form.onsubmit = async (event) => {
      event.preventDefault();
      if (!confirm.checked || !reason.value.trim()) return;
      await saveRecovery(snapshot, "abandon", { stage: attempt.stage, reason: reason.value.trim() }, save);
    };
    card.append(form); panel.append(card);
  }
  const refresh = box("button", "ghost small", "刷新恢复状态");
  refresh.onclick = () => restoreJob(snapshot.run_id);
  panel.append(refresh); host.append(panel);
}

async function saveRecovery(snapshot, action, payload, button) {
  if (state.runId !== snapshot.run_id) return;
  button.disabled = true;
  try {
    await api(`/api/runs/${snapshot.run_id}/recovery/${action}`, {
      method: "POST", body: JSON.stringify({ ...payload, expected_revision: snapshot.revision }),
    });
    if (state.runId !== snapshot.run_id) return;
    toast(action === "reconcile" ? "核账结果已保存。" : "未完成尝试已关闭，历史内容已保留。");
    await restoreJob(snapshot.run_id);
  } catch (err) { toast(err.message, true); button.disabled = false; }
}

async function watchJob(runId, job, generation) {
  if (generation !== jobWatch || runId !== state.runId) return;
  if (job && currentJob?.job_id === job.job_id && Number(currentJob.cursor || 0) > Number(job.cursor || 0))
    job = currentJob;
  showJob(job);
  if (job?.successor_job_id) {
    closeJobSource();
    let next;
    try { next = await api(`/api/runs/${runId}/jobs/${job.successor_job_id}`); }
    catch (_) {
      if (generation === jobWatch && runId === state.runId)
        jobTimer = setTimeout(() => watchJob(runId, job, generation), 1000);
      return;
    }
    if (generation !== jobWatch || runId !== state.runId) return;
    await watchJob(runId, next, generation);
    await refreshTimeline();
    return;
  }
  if (!jobPresentation(job).active) {
    closeJobSource();
    try {
      const recovery = await api(`/api/runs/${runId}/recovery`);
      if (generation !== jobWatch || runId !== state.runId) return;
      currentRecovery = recovery?.revision ? recovery : null;
      showJob(job);
    } catch (_) { toast("暂时无法读取恢复状态，请刷新后核对。", true); }
    try { await refreshTimeline(); if(state.materialsOpen) await loadMaterialWorkspace(); }
    catch (_) { toast("执行状态已保存，项目内容暂时未刷新。", true); }
    return;
  }
  followJobEvents(runId, job, generation);
  clearTimeout(jobTimer);
  jobTimer = setTimeout(async () => {
    try {
      const updated = await api(`/api/runs/${runId}/jobs/${job.job_id}`);
      await watchJob(runId, updated, generation);
    } catch (_) {
      if (generation !== jobWatch || runId !== state.runId) return;
      showJob(job, "连接暂时断开，正在找回进度…");
      jobTimer = setTimeout(() => watchJob(runId, job, generation), 2500);
    }
  }, jobSource ? 8000 : 1000);
}

function showUncertainSubmission(runId, payload, options = {}) {
  showJob(null, "发送结果尚未确认。请确认提交状态，避免重复执行。");
  const retry = box("button", "ghost small", "确认提交状态");
  retry.onclick = () => submitJob(runId, payload, options);
  $("job-status").append(retry);
}

async function restoreJob(runId) {
  const generation = ++jobWatch;
  clearTimeout(jobTimer);
  closeJobSource();
  currentJob = null;
  currentRecovery = null;
  state.busy = true;
  $("send").disabled = true;
  try {
    const job = await api(`/api/runs/${runId}/jobs/latest`);
    if (runId !== state.runId || generation !== jobWatch) return;
    const pending = readPendingJob(runId);
    if (pending && job?.request_id === pending.request_id) clearPendingJob(runId);
    else if (pending) { showUncertainSubmission(runId, pending, readPendingJobOptions(runId, pending)); return; }
    const steer = readPendingSteer(runId);
    if (steer && (job?.parent_job_id === steer.job_id ||
        (job?.job_id === steer.job_id && Number(job.goal_revision || 0) > steer.expected_goal_revision))) {
      sessionStorage.removeItem(pendingSteerKey(runId));
    } else if (steer) {
      showJob(job, '调整是否收到尚未确认。请确认提交状态。');
      const retry = box('button','ghost small','确认调整状态');
      retry.onclick = () => submitSteer(runId, steer.job_id, steer);
      $('job-status').append(retry);
      return;
    }
    await watchJob(runId, job, generation);
  } catch (_) {
    if (runId !== state.runId || generation !== jobWatch) return;
    showJob(null, "暂时无法读取任务状态，正在重新连接…");
    state.busy = true;
    $("send").disabled = true;
    jobTimer = setTimeout(() => restoreJob(runId), 3000);
  }
}

async function submitSteer(runId, jobId, payload) {
  if (runId !== state.runId || steerPosting) return;
  steerPosting = true;
  sessionStorage.setItem(pendingSteerKey(runId), JSON.stringify({...payload, job_id:jobId}));
  if (currentJob?.job_id === jobId) showJob(currentJob, '正在提交调整…');
  try {
    const updated = await api(`/api/runs/${runId}/jobs/${jobId}/steer`,
      {method:'POST', body:JSON.stringify(payload)});
    sessionStorage.removeItem(pendingSteerKey(runId));
    if (runId === state.runId && currentJob?.job_id === jobId) showJob(updated);
  } catch (err) {
    if (err.status >= 400 && err.status < 500) {
      sessionStorage.removeItem(pendingSteerKey(runId));
      if (runId === state.runId) { $('chat-input').value = payload.text; toast(err.message, true); }
    } else if (runId === state.runId) {
      showJob(currentJob, '调整结果尚未确认。刷新后可以用相同请求标识核对。');
    }
  } finally {
    steerPosting = false;
    if (runId === state.runId && currentJob) showJob(currentJob);
  }
}

async function submitJob(runId, payload, options = {}) {
  if (runId !== state.runId) return;
  // Both conversation entry points display the same on-demand analysis notice.
  // This does not enable media generation; the main agent still needs an
  // explicit reference request, a supplied source and the bounded tool.
  if (!payload.request_kind && payload.reference_consent === undefined) payload={...payload,reference_consent:true};
  const generation = ++jobWatch;
  clearTimeout(jobTimer);
  closeJobSource();
  currentRecovery = null;
  state.busy = true;
  $("send").disabled = true;
  // Persist only the user's request, never connection credentials. A retry uses
  // the same id even when the browser never received the acceptance response.
  sessionStorage.setItem(pendingJobKey(runId), JSON.stringify(payload));
  if (options.preserveComposerOnError)
    sessionStorage.setItem(pendingJobUiKey(runId), JSON.stringify({request_id:payload.request_id,preserveComposerOnError:true}));
  else sessionStorage.removeItem(pendingJobUiKey(runId));
  showJob(null, "正在提交…");
  let job;
  try {
    const route = payload.request_kind === 'reference_video' ? 'reference-videos' : payload.request_kind === 'stage_review' ? 'actions/review' : payload.request_kind === 'video_analysis' ? 'video-analysis' : payload.request_kind === 'image_analysis' ? 'image-analysis' : payload.request_kind === 'artifact_revision'
      ? `edits/${encodeURIComponent(payload.stage)}/${encodeURIComponent(payload.draft_id)}/generate` : 'jobs';
    job = await api(`/api/runs/${runId}/${route}`, { method: "POST", body: JSON.stringify(payload) });
  } catch (err) {
    if (runId !== state.runId || generation !== jobWatch) return;
    if (err.status >= 400 && err.status < 500) {
      clearPendingJob(runId);
      if (!payload.request_kind && !options.preserveComposerOnError && !$("chat-input").value.trim())
        $("chat-input").value = payload.text;
      showJob(null, err.message);
      await restoreJob(runId);
      toast(err.message, true);
    } else showUncertainSubmission(runId, payload, options);
    return;
  }
  clearPendingJob(runId);
  if (runId !== state.runId || generation !== jobWatch) return;
  if (payload.feedback_target && JSON.stringify(state.feedbackTarget) === JSON.stringify(payload.feedback_target))
    clearFeedbackTarget();
  await watchJob(runId, job, generation);
  try { await refreshTimeline(); }
  catch (_) { toast("任务已接受，正在运行；对话暂时未刷新。", true); }
}

if (typeof module !== "undefined") module.exports = { jobPresentation };
