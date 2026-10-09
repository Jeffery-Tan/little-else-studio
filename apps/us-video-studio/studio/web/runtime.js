"use strict";

function detachProjectJob() {
  jobWatch++;
  clearTimeout(jobTimer);
  closeJobSource();
  currentJob=null;
  state.currentJob=null;
  currentRecovery=null;
  savedArtifactRefreshes.clear();
}

const JOB_ACTIVE = new Set(["queued", "running", "cancelling"]);
const JOB_LABELS = {
  queued: "等待执行", running: "正在执行", cancelling: "正在停止，等待当前调用返回",
  succeeded: "本轮完成", blocked: "本轮需要处理后继续", failed: "本轮执行失败",
  cancelled: "已停止", interrupted: "服务中断，本轮未自动重试",
};
const JOB_ROLE_NAMES = { orchestrator: "主制片", director: "创意导演", material: "视觉策划", writer: "编剧", reviewer: "独立审片" };
function restoredProjectStatus(job) {
  return !job.deliverable?.blocker && !JOB_ACTIVE.has(job.status) && job.brief?.phase === 'ready' &&
    job.brief?.execution?.project_restored === true &&
    ['conversation','stage_review','direction_resume','writing_revision','annotation_revision','design_upgrade'].includes(job.job_kind || 'conversation');
}
function jobCreativeNames(job) {
  const project = state.timeline?.run;
  const format = project?.run_id === state.runId && job?.run_id === state.runId
    ? project.content_format : null;
  return format === 'photo' ? {writing:'图文方案',inputs:'逐页图片'}
    : format === 'video' ? {writing:'脚本',inputs:'分镜'}
      : {writing:'创作方案',inputs:'创作'};
}
function jobGuidance(job) {
  if (job.status === 'cancelled') return '本轮已停止，已保存内容保留；不会自动继续或重试。下一步需你发送新指令。';
  if (job.status === 'interrupted') return '服务曾中断，已有内容已保留。确认进度后，再发送新的继续指令。';
  // A backend prerequisite has a concrete action. Do not wrap its sentence in
  // generic missing-deliverable or retry advice, or hide it behind old status.
  if (job.status === 'blocked' && typeof job.deliverable?.blocker?.note === 'string' && job.deliverable.blocker.note.trim())
    return job.deliverable.blocker.note;
  if (restoredProjectStatus(job)) return `${job.brief.title}。${job.brief.detail}`;
  const reason = String(job.reason || "");
  const names = jobCreativeNames(job);
  // Why the round stopped outranks what it was missing — the failure explains
  // the missing part (and is the thing to fix).
  if (/连续失败/.test(reason)) return "本轮因同一问题反复失败已停止。具体错误见「团队工作记录」，改掉原因后再发新的指令。";
  if (job.job_kind === 'writing_revision' && job.revision_result?.change_status === 'saved')
    return `${names.writing}新版已保存，请查看准确版本并决定采用或退回。`;
  if (job.job_kind === 'design_upgrade' && job.status === 'succeeded')
    return `设计补充已准备好，请打开方案核对后保存；原${names.writing}与图片保留，尚未生成媒体。`;
  const mediaState = job.status === 'failed' ? null : job.deliverable?.status;
  const mediaGuidance = {
    input_ready:`${names.inputs}输入已齐备，实际媒体尚未生成。请查看准确输入并确认生成范围与费用。`,
    awaiting_authorisation:'准确生成确认已准备，等待你核对范围与费用；尚未提交媒体生成。',
    in_progress:'媒体仍在执行，请查询原任务；不要重新提交。',
    failed:'媒体生成失败，请查看原任务的具体错误与下一步；不会自动重试。',
    unknown:'媒体结果尚待核实，请查询原任务；不要重新提交。',
  };
  if (mediaGuidance[mediaState]) return mediaGuidance[mediaState];
  if (job.status !== 'failed' && job.deliverable?.status === 'incomplete') {
    const stageNames = {intake:'创作需求',direction:'创意方向',material_plan:'素材计划',writing:names.writing};
    const missing = (job.deliverable.missing || []).map(item => String(item).replace(
      /^(intake|direction|material_plan|writing)\s+(?=当前)/u, (_, stage) => stageNames[stage]));
    if (missing.some(item => /独立审核|审核通过|reviewer|review pass/i.test(item)))
      return state.timeline?.pending
        ? '历史任务的审核要求已停用。请阅读当前作品，按现在的决定卡操作。'
        : '历史任务的审核要求已停用。请查看当前准确版本与任务状态。';
    return `本轮交付还缺：${missing.join('、')}。已完成的内容已保留，请查看准确版本后继续。`;
  }
  // Missing accounting data is diagnostic, never a creative workflow warning.
  if (/soft cap|预算.*上限|预算不足/i.test(reason)) return state.timeline?.budget?.limit_mode === 'none'
    ? "上一轮被旧额度设置中断。已保存内容仍可阅读；当前不会自动重试，请明确发送下一步要求。"
    : "这轮调用被当时的预算设置挡住。已保存内容仍可阅读；请核对当前设置后再决定下一步。";
  if (/Codex CLI is not installed or not on PATH/i.test(reason)) return "服务没有找到 Codex CLI。请确认本机已安装 Codex，重启工作台服务后再发送消息；已保存的内容不受影响。";
  if (/连接准备未通过|ConnectionUnavailable|CodexUnavailable/i.test(reason)) return "模型连接尚未准备好。请检查制作团队中的对应服务设置，再发送继续指令。";
  if (/模型连接超时或中断/.test(reason))
    return "负责这一步的团队模型连接超时或中断，新版未保存；本次用量无法确认，系统已停止自动重试。请检查连接后从当前阶段继续。";
  if (/模型输出达到单次上限/.test(reason))
    return "负责这一步的团队模型输出达到单次上限，新版没有保存；系统已停止自动重试以避免重复计费。请从当前阶段重新发起。";
  if (/团队模型返回的结果读不出来|角色模型返回了无法读取的结果/.test(reason))
    return "团队模型的回复格式不完整，新版没有保存；系统已停止自动重试以避免重复计费。请从当前阶段重新发起。";
  if (job.job_kind === 'reference_video' && /视频文件获取失败（HTTP 403）/.test(reason))
    return "TikTok 页面已读取，但视频文件地址返回 HTTP 403。可重试原链接或上传原视频继续；不会自动重试或调用声画模型。";
  if (job.job_kind === 'reference_video' && /reference_fetch|无法连接TikTok|视频下载中断/i.test(reason))
    return "TikTok 原片获取失败，未开始声画分析。可在原参考任务中重试链接或上传原视频；不会自动重试。";
  if (job.open_attempts?.length) return "有尚未结束的制作阶段。请先按下方恢复步骤处理遗留任务，再继续创作。";
  if (job.status === "failed") return "本轮未完成，已有内容已保留。打开「团队工作记录」查看原因，处理后再说明下一步。";
  if (job.status === "blocked") return "本轮已暂停。先查看保存的内容和下方提示，再说明下一步要求。";
  return "";
}
function mediaJobProgress(job) {
  if (job?.job_kind !== 'production_batch_generation' || !JOB_ACTIVE.has(job.status)) return '';
  // This is the server's batch status, not a count inferred from files or tools.
  for (const event of [...(job.events || [])].reverse()) {
    if (event.type !== 'status' || typeof event.message !== 'string') continue;
    const match = /^正在生成第 ([1-9]\d?)\/([1-9]\d?)(?: 个(首帧|视频段)|页图片)。$/u.exec(event.message);
    const photo = /^正在生成(?:第 ([0-9]{2,4}) 页图片|图文图片)（本次 ([1-9]\d?)\/([1-9]\d?) 张）。$/u.exec(event.message);
    if (match && Number(match[1]) <= Number(match[2]) && Number(match[2]) <= 12) return event.message;
    if (photo && (photo[1] == null || Number(photo[1]) > 0)
      && Number(photo[2]) <= Number(photo[3]) && Number(photo[3]) <= 12) return event.message;
  }
  return '';
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
  if (job.job_kind === 'reference_video') {
    const learning=(state.runs||[]).find(run=>run.run_id===state.runId)?.kind==='learning';
    return {active,label:JOB_LABELS[job.status] || '状态待确认',
      detail:learning?(active?'正在获取并分析参考视频；观察会保留在学习记录中。':job.status==='succeeded'?'参考声画观察已保存，请在学习记录中核对覆盖范围。':'已完成的步骤会保留；请核对来源或上传原视频。'):
        active ? '正在获取并分析参考视频；复刻任务会接续策划。' : job.status==='succeeded' ? '参考任务结果已保存，请查看分析报告和创作方案。' : '已完成的步骤会保留，可在参考视频入口继续或上传原视频。',guidance:jobGuidance(job)};
  }
  if ((job.job_kind || '').startsWith('production_')) return {active, label:JOB_LABELS[job.status] || '状态待确认',
    detail:active?(mediaJobProgress(job)||'正在处理制作任务；媒体生成进度与结果保存在制作页。'):'本次制作状态已保存，请在制作页查看结果与用量。',guidance:jobGuidance(job)};
  if (job.job_kind === 'artifact_revision') return {active,label:JOB_LABELS[job.status] || '状态待确认',
    detail:active ? '正在将局部修改同步到新版本。' : job.status==='succeeded' ? '新版本已保存，请比较改动后决定是否采用。' : '',guidance:jobGuidance(job)};
  if (job.job_kind === 'writing_revision') return {active,
    label:job.revision_result?.change_status === 'saved' && !active
      ? `${jobCreativeNames(job).writing}新版已保存` : JOB_LABELS[job.status] || '状态待确认',
    detail:active ? '编剧正在修改选中字段，完成后交你决定。'
      : job.revision_result?.change_status === 'saved' || job.status==='succeeded'
      ? `${jobCreativeNames(job).writing}新版已保存，请阅读并决定是否采用。` : '',
    guidance:jobGuidance(job)};
  if (job.job_kind === 'direction_resume') return {active,
    label:active ? '正在重做创意方向' : job.status==='succeeded' ? '新版创意方向已保存' : JOB_LABELS[job.status] || '状态待确认',
    detail:active ? '导演正在按当前有效资料准备 A/B/C。' : job.status==='succeeded'
      ? '请阅读三个方向，在对话区点选或提出修改意见。' : '旧稿已保留；请查看本次停止原因。',
    guidance:jobGuidance(job)};
  if (['image_analysis','video_analysis'].includes(job.job_kind)) return {active, label:JOB_LABELS[job.status] || '状态待确认',
    detail:active ? '视觉策划正在分析素材。可以离开页面，任务会继续；不会自动重试。'
      :job.status==='succeeded' ? '素材分析已保存，请核对并选择要采用的观察。' : '',guidance:jobGuidance(job)};
  if(job.job_kind==='result_inspection')return {active,label:JOB_LABELS[job.status]||'状态待确认',
    detail:active?'正在检查准确结果的实际画面；检查不触发重新生成。':job.status==='succeeded'
      ?'结果检查已保存，请在媒体对象详情阅读发现与限制；仍需人工确认。':'请核对本次检查的停止原因，原结果已保留。',guidance:jobGuidance(job)};
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

function liveJobStatus(job, view) {
  const events = job.events || [];
  const last = events.at(-1);
  const recordedAt = teamTime(last?.at);
  const waiting = modelWaiting(job);
  const withWaiting = detail => waiting ? `${detail}；${waiting.text}` : detail;
  if (job.steer_status === 'delivered') return {
    label: job.steer_class === 'role_stop' ? '此项停止要求已收到' : '角色补充已收到',
    detail: job.steer_reason || (job.steer_class === 'role_stop'
      ? '已要求停止指定角色，其他任务继续。'
      : '原话已送到指定角色，等待其处理；其他任务继续。'), recordedAt};
  if (job.steer_status === 'received') return {
    label: job.steer_class === 'discussion' ? '讨论已收到' : '调整已收到',
    detail: withWaiting(job.steer_class === 'discussion'
      ? '原任务继续执行，结束后单独回答讨论；不会自动改稿。'
      : '正在等待当前模型调用结束，再按你的调整接续。'), recordedAt};
  if (job.status === 'queued') return {
    label: '已排队', detail: `任务已排队${Number.isInteger(job.queue_position)&&job.queue_position>0?`，当前第 ${job.queue_position} 位`:''}，尚未开始处理。`, recordedAt};
  if(job.job_kind==='result_inspection')return {label:view.label,detail:withWaiting(view.detail),recordedAt};
  if (job.status === 'cancelling') return {
    label: '正在停止', detail: withWaiting('等待当前调用结束；不会启动下一步。'), recordedAt};
  if (job.job_kind === 'direction_resume') {
    const director = events.filter(e => e.role === 'director');
    const saved = director.some(e => e.type === 'agent_tool' && e.tool === 'submit_stage' && e.ok === true);
    const submitting = director.some(e => e.type === 'agent_activity' && e.tool === 'submit_stage') && !saved;
    const checked = [
      ['read_artifact', 'direction', '旧方向'],
      ['list_materials', null, '素材清单'],
      ['read_capabilities', null, '制作能力'],
    ].filter(([tool, stage]) => director.some(e => e.type === 'agent_tool' && e.tool === tool
      && e.ok === true && (!stage || (e.args?.stage || e.stage) === stage)))
      .map(([, , label]) => label);
    const prefix = checked.length ? `已核对${checked.join('、')}；` : '';
    return {label: view.label, recordedAt,
      detail: withWaiting(saved ? '新版 A/B/C 已保存，正在收尾。'
        : submitting ? `${prefix}正在保存新版 A/B/C。`
        : `${prefix}导演正在准备 A/B/C，新版尚未保存。`)};
  }
  const latestActivity = [...events].reverse().find(e => e.type === 'agent_activity' && JOB_ROLE_NAMES[e.role]);
  const role = JOB_ROLE_NAMES[latestActivity?.role] || '制片团队';
  const saved = [...events].reverse().find(e => e.type === 'agent_tool' && e.ok === true
    && ['write_stage','submit_stage'].includes(e.tool));
  const stage = TEAM_STAGES[saved?.args?.stage || saved?.stage] || '阶段内容';
  const checkedNames = {read_project_state:'项目进度',read_project_history:'历史资料',
    read_artifact:'当前方案',list_materials:'素材清单',read_capabilities:'制作能力',
    read_production:'制作方案'};
  const checked = [...new Set(events.filter(e => e.type === 'agent_tool' && e.ok === true
    && checkedNames[e.tool]).map(e => e.tool === 'read_artifact'
      ? TEAM_STAGES[e.args?.stage || e.stage] || checkedNames[e.tool] : checkedNames[e.tool]))];
  const milestone = saved ? `已保存${stage}` : checked.length ? `已核对${checked.slice(-3).join('、')}` : '';
  const working = waiting ? `${JOB_ROLE_NAMES[waiting.role] || role}：${waiting.text}`
    : latestActivity?.phase === 'tool' && latestActivity.tool
    ? `${role}正在${teamAction(latestActivity)}` : `正在等待${role}的模型回复`;
  return {label: view.label, recordedAt,
    detail: `${milestone ? `${milestone}；` : ''}${working}；${saved ? '正在继续处理本轮工作。' : '本轮结果尚未提交。'}`};
}

let jobWatch = 0;
let jobTimer = null;
let jobSource = null;
let jobSourceJobId = null;
let currentJob = null;
let currentRecovery = null;
let jobRenderKey = "";
let steerPosting = false;
let cancelPosting = false;
// Successful persistence tools are emitted only after the trusted handler has
// saved the artifact. Their durable event sequence deduplicates SSE and polls;
// narration and in-progress tool arguments are never refresh signals.
const savedArtifactRefreshes = new Map();
async function refreshSavedArtifacts(runId, job, generation) {
  if (generation !== jobWatch || runId !== state.runId || !job?.job_id) return;
  const receipts = (job.events || []).filter(event => event.type === 'agent_tool'
    && event.ok === true && event.skipped !== true
    && ['write_stage','submit_stage','submit_patch'].includes(event.tool)
    && Number.isInteger(event.seq));
  if (!receipts.length) return;
  const key = `${runId}:${job.job_id}`;
  let record = savedArtifactRefreshes.get(key);
  if (!record) {
    record = {saved: new Set(), pending: null};
    savedArtifactRefreshes.set(key, record);
  }
  if (record.pending) await record.pending;
  if (generation !== jobWatch || runId !== state.runId) return;
  const unseen = receipts.filter(event => !record.saved.has(event.seq));
  if (!unseen.length) return;
  const pending = (async () => {
    try {
      const applied = await refreshTimeline();
      if (applied !== false && generation === jobWatch && runId === state.runId)
        unseen.forEach(event => record.saved.add(event.seq));
    } catch (_) {
      // Leave the exact receipt unacknowledged so the next poll can refresh it.
      if (generation === jobWatch && runId === state.runId)
        toast('内容已保存，画布暂时未刷新；正在继续同步。', true);
    }
  })();
  record.pending = pending;
  await pending;
  if (record.pending === pending) record.pending = null;
}
function canSteerJob(job) {
  return Boolean(job && job.lane!=='query' &&
    (job.status === 'running' || (job.status === 'cancelling' && job.steer_status === 'received')));
}
function syncComposerAction(job = currentJob) {
  if (typeof StudioComposer !== 'undefined') StudioComposer.syncControls?.();
  const querying = Boolean(job?.lane==='query'&&JOB_ACTIVE.has(job.status));
  const active = Boolean(job && job.lane!=='query' && JOB_ACTIVE.has(job.status));
  const canInsert = canSteerJob(job);
  const directRevision = state.composerRequestKind === 'writing_revision';
  const roleTarget = state.roleTarget?.run_id === state.runId ? state.roleTarget : null;
  const unresolved = readPendingJob(state.runId) || readPendingSteer(state.runId);
  const button = $('send');
  const stop = $('job-stop');
  const stopping = active && job.status === 'cancelling' && job.steer_status !== 'received';
  const blocked = Boolean(unresolved) || cancelPosting || steerPosting || state.decisionBusy
    || state.recoveryBlocked || Boolean(state.projectReadIntegrity);
  // While a job holds the run, 停止 *is* the primary action and it stays the
  // primary action until the job ends — whatever is in the composer, and whether
  // or not this kind of job accepts a mid-flight adjustment. It used to swap its
  // identity with 发送/插入调整 depending on those two things, so the main button
  // changed meaning under the user's hands: "agent starts working, so send should
  // become stop; but as soon as I type, stop becomes send again."
  //
  // The composer's own action therefore moves to the secondary button, which
  // keeps one position and one meaning per state instead of one button that
  // alternates. `data-mode` drives the click handler, so the two never disagree
  // about what a press does.
  if (active) {
    button.setAttribute?.('data-mode', 'stop');
    button.textContent = stopping ? '正在停止…' : '停止';
    button.setAttribute?.('aria-label', '停止本轮');
    button.disabled = blocked || stopping;
  } else {
    button.setAttribute?.('data-mode', 'send');
    button.textContent = directRevision ? '直接修改 ↑' : '发送 ↑';
    button.setAttribute?.('aria-label', directRevision ? `按意见直接修改${jobCreativeNames({run_id:state.feedbackTarget?.run_id}).writing}` : '发送消息');
    button.disabled = blocked || state.busy;
  }
  const hint = $('composer-key-hint');
  if (hint) hint.textContent = active
    ? directRevision ? '直接修改草稿已保留，等本轮结束后发送 · Shift + Enter 换行'
    : canInsert ? roleTarget ? `原话只送给${roleTarget.label}，其他任务继续 · Shift + Enter 换行`
      : '中途补充会先保存，当前调用结束后由主制片处理 · Shift + Enter 换行'
    : job.status === 'queued' ? '任务正在排队，草稿可继续编辑；开始执行后可插入调整'
    : '任务正在停止，草稿可继续编辑；结束后可发送'
    : querying ? '原任务仍在查询；可以发送新的文字任务 · Shift + Enter 换行'
    : directRevision ? 'Enter 按意见直接修改 · Shift + Enter 换行' : 'Enter 发送 · Shift + Enter 换行';
  if (stop) {
    if(querying){stop.hidden=false;stop.disabled=blocked||job.status==='cancelling';stop.textContent='停止本地查询';stop.setAttribute?.('aria-label','停止当前原任务的本地查询');stop.setAttribute?.('data-mode','stop-query');return;}
    // Only advertise an adjustment when the current job can actually accept it.
    stop.hidden = !active;
    stop.disabled = Boolean(blocked || !canInsert || directRevision);
    stop.textContent = directRevision ? '等待直接修改' : canInsert ? roleTarget ? '发送给此项 ↑' : '插入调整 ↑'
      : job?.status === 'queued' ? '正在排队…' : '正在停止…';
    stop.setAttribute?.('aria-label', stop.textContent);
    stop.setAttribute?.('data-mode', canInsert && !directRevision ? 'insert' : 'blocked');
  }
}
async function stopCurrentJob() {
  const job = currentJob;
  const runId = state.runId;
  if (!job || !JOB_ACTIVE.has(job.status) || (job.status === 'cancelling' && job.steer_status !== 'received') || cancelPosting
      || readPendingJob(runId) || readPendingSteer(runId)) return;
  cancelPosting = true;
  syncComposerAction(job);
  try {
    const updated = await api(`/api/runs/${runId}/jobs/${job.job_id}/cancel`, {method:'POST', body:'{}'});
    if (state.runId === runId && currentJob?.job_id === job.job_id) showJob(updated);
  } catch (err) {
    if (state.runId === runId && currentJob?.job_id === job.job_id) toast(err.message, true);
  } finally {
    cancelPosting = false;
    if (state.runId === runId) syncComposerAction();
  }
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
    // SSE often closes as the job settles. Read the authoritative snapshot
    // before reconnecting; replaying the old running snapshot can keep
    // cancelling the safety poll forever when each new stream closes quickly.
    jobTimer = setTimeout(async () => {
      if (generation !== jobWatch || runId !== state.runId) return;
      try {
        const updated = await api(`/api/runs/${runId}/jobs/${job.job_id}`);
        await watchJob(runId, updated, generation);
      } catch (_) {
        if (generation !== jobWatch || runId !== state.runId) return;
        if ((job.job_kind||'').startsWith('production_')) {
          const known=currentJob?.job_id===job.job_id?currentJob:job;
          showJob(known,'进度读取暂时中断；仍在跟踪原制作任务。');
          jobTimer=setTimeout(()=>watchJob(runId,known,generation),2500);
        } else jobTimer = setTimeout(() => restoreJob(runId), 2500);
      }
    }, 1000);
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
function stablePayload(value) {
  if (Array.isArray(value)) return value.map(stablePayload);
  if (value && typeof value === 'object') return Object.fromEntries(
    Object.keys(value).sort().map(key => [key, stablePayload(value[key])]));
  return value;
}
function samePendingPayload(saved, incoming) {
  return JSON.stringify(stablePayload(saved)) === JSON.stringify(stablePayload(incoming));
}
function jobWirePayload(runId, payload) {
  if (payload.request_kind || payload.reference_consent !== undefined) return payload;
  return {...payload, reference_consent:(state.runs||[]).find(run=>run.run_id===runId)?.kind!=='learning'};
}
function readPendingJobOptions(runId, payload) {
  try {
    const saved = JSON.parse(sessionStorage.getItem(pendingJobUiKey(runId)) || "{}");
    return saved.request_id === payload?.request_id ? {preserveComposerOnError:saved.preserveComposerOnError === true} : {};
  }
  catch (_) { return {}; }
}
/* Clearing is by identity, never by key alone (U2): a late answer for one
 * request must not delete the marker standing for a different unresolved one. */
function clearPendingJob(runId, requestId, accepted = true) {
  const stored = readPendingJob(runId);
  if (stored && stored.request_id !== requestId) return;
  sessionStorage.removeItem(pendingJobKey(runId));
  sessionStorage.removeItem(pendingJobUiKey(runId));
  if (typeof settleComposerRequest === 'function') settleComposerRequest(runId, requestId, accepted);
  if (typeof StudioComposer !== 'undefined') StudioComposer.settle(runId, requestId, accepted);
}
function clearPendingSteer(runId, requestId, accepted = true) {
  const stored = readPendingSteer(runId);
  if (stored && stored.request_id !== requestId) return;
  sessionStorage.removeItem(pendingSteerKey(runId));
  if (typeof settleComposerRequest === 'function') settleComposerRequest(runId, requestId, accepted);
}

function appendTaskEvidence(host,job){
  if(!job?.job_id||typeof StudioEvidence==='undefined')return;
  const runId=state.runId,details=box('details','task-evidence-entry'),action=box('button','ghost small','读取本任务用量');
  action.onclick=()=>StudioEvidence.openUsage({runId,jobId:job.job_id});
  details.append(box('summary','','任务详情'),action);host.append(details);
}
function showJob(job, notice = "") {
  if (job && currentJob?.job_id === job.job_id && Number(currentJob.cursor || 0) > Number(job.cursor || 0)) return;
  currentJob = job;
  state.currentJob = job;
  if (typeof configureJourneyTabs === 'function') configureJourneyTabs();
  if (job?.brief) state.projectBrief = job.brief;
  const view = jobPresentation(job, currentRecovery);
  const unresolved = readPendingJob(state.runId) || readPendingSteer(state.runId);
  state.busy = (view.active&&job?.lane!=='query') || Boolean(unresolved);
  // An open stage attempt belongs to a running role. It becomes a recovery
  // concern only after that job has ended without closing the attempt.
  state.recoveryBlocked = !view.active && Boolean((currentRecovery && !currentRecovery.can_continue)
    || (!currentRecovery && (job?.open_attempts?.length)));
  syncComposerAction(job);
  if (typeof updateProductionJob === 'function') updateProductionJob(job);
  if ((job?.job_kind||'').startsWith('production_')&&typeof renderProductionAction==='function') renderProductionAction();
  if (typeof renderProjectProgress === 'function') renderProjectProgress();
  if (typeof renderJourneyDecision === 'function') renderJourneyDecision();
  const host = $("job-status");
  if (typeof renderJobProgress === 'function') renderJobProgress(job);
  const waiting = modelWaiting(job);
  const live = view.active && job ? liveJobStatus(job, view) : null;
  const lastMessage = [...(job?.events || [])].reverse().find((event) => event.say || event.message);
  const renderKey = JSON.stringify([state.runId, job?.job_id, job?.status, job?.events?.length, job?.cursor, job?.goal_revision,
    job?.steer_status, job?.steer_reason, job?.role_sessions, job?.successor_job_id, view.detail, view.guidance, job?.reason,
    live?.detail, waiting?.text,
    lastMessage?.say, lastMessage?.message, notice, job?.recovery, job?.unresolved_calls, job?.open_attempts, currentRecovery?.revision]);
  updateTeamDialog();
  if (renderKey === jobRenderKey) return;
  jobRenderKey = renderKey;
  host.replaceChildren();
  host.hidden = !job && !notice && !state.recoveryBlocked;
  if (host.hidden) return;
  host.setAttribute?.('data-active', String(view.active));
  host.setAttribute?.('data-chat-job', String(Boolean(job && ['conversation','stage_review','direction_resume','annotation_revision','design_upgrade'].includes(job.job_kind))));
  const copy = box("div", "job-copy");
  // Project status is above both views. This strip is only the live execution
  // observer; historical usage cannot turn it back into a second status panel.
  if (job && !notice && !state.recoveryBlocked && ['conversation','stage_review','direction_resume','annotation_revision','design_upgrade'].includes(job.job_kind)) {
    if (view.active) {
      const summary = box('div','job-live-summary');
      summary.append(box('strong','job-live-title',live.label),
        box('span','job-live-detail',live.detail));
      if (live.recordedAt) summary.append(box('span','job-live-time',`最近记录 ${live.recordedAt}`));
      copy.append(summary);
    }
    if (!view.active && view.guidance) {
      const restored = restoredProjectStatus(job);
      const warning = box('strong',restored ? 'job-restored' : 'job-warning',view.guidance);
      warning.setAttribute?.('role',restored ? 'status' : 'alert');
      copy.append(warning);
    }
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
    if (job.steer_status === 'unanswered') {
      copy.append(box('span','job-warning',job.steer_reason || '上次询问还没有保存答复，请重新查询当前状态。'));
    }
    const activity = box('button','text-button job-activity-open','团队工作记录 ↗');
    activity.onclick = openTeamProgress;
    copy.append(activity);
    appendTaskEvidence(copy,job);
    host.append(copy);
    return;
  }
  copy.append(box("strong", "", notice || view.label || "先处理待恢复事项"));
  const detail = waiting && !view.detail?.includes(waiting.text)
    ? `${view.detail ? `${view.detail}；` : ''}${waiting.text}` : view.detail;
  if (detail) copy.append(box("span", "muted", detail));
  if (view.active && job?.events?.length) {
    const activity = teamProgress(job);
    const latest = activity.history.at(-1);
    if (latest) copy.append(box('span','job-latest',`最近操作：${latest.role} · ${latest.text}`));
    copy.append(box('span','job-updated',`最近进展 ${teamTime(activity.lastAt) || '时间未记录'} · 新进展会自动更新`));
  }
  if (job && !view.active && view.guidance) {
    const restored = restoredProjectStatus(job);
    const warning = box("strong", restored ? "job-restored" : "job-warning", view.guidance);
    warning.setAttribute?.("role", restored ? "status" : "alert");
    copy.append(warning, box("span", "muted", "不会自动重试；只有你再次发起创作，才会启动新的模型调用。"));
  }
  if (job) {
    const activity = box('button','text-button job-activity-open','团队工作记录 ↗');
    activity.onclick = openTeamProgress;
    copy.append(activity);
    appendTaskEvidence(copy,job);
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
  if (!view.active && job?.job_kind === 'writing_revision'
      && (job.status === 'succeeded' || job.revision_result?.change_status === 'saved')) {
    const writing = box('button', 'primary small', `查看${jobCreativeNames(job).writing}并决定`);
    writing.onclick = () => { showJourneyTab('plan'); selectStage('writing'); setProjectFocus('canvas'); };
    copy.append(writing);
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

async function refreshRecovery(runId, job, generation) {
  const current = () => generation === jobWatch && runId === state.runId && currentJob?.job_id === job?.job_id;
  const options = {key:'recovery',current,refresh:async () => {
    if (await refreshRecovery(runId,job,generation) && current()) await refreshTimeline();
  }};
  const serial = state.refreshSerial;
  const loaded = await boundedProjectRead(() => api(`/api/runs/${runId}/recovery`),current,
    (error,waiting)=>projectReadStatus(runId,serial,error,waiting,options));
  if (!loaded || !current()) return false;
  currentRecovery = loaded.value?.revision ? loaded.value : null;
  projectReadStatus(runId,serial,null,false,options);
  showJob(job);
  return true;
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
    try { await refreshRecovery(runId,job,generation); }
    catch (_) { /* The bounded read retains its visible recovery error and entry. */ }
    if (generation !== jobWatch || runId !== state.runId) return;
    try { await refreshTimeline(); if(state.materialsOpen) await loadMaterialWorkspace(); }
    catch (_) { toast("执行状态已保存，项目内容暂时未刷新。", true); }
    return;
  }
  await refreshSavedArtifacts(runId, job, generation);
  if (generation !== jobWatch || runId !== state.runId) return;
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

function trackAcceptedJob(runId, job) {
  if (runId !== state.runId || !job || typeof job.job_id !== 'string' || !job.job_id
      || typeof job.status !== 'string' || !Object.hasOwn(JOB_LABELS,job.status)
      || (job.run_id && job.run_id !== runId)) return false;
  job={...job,run_id:runId};
  // A repeated durable receipt must not open another stream or discard known
  // history. Only a newer durable cursor can update this already watched Job.
  if (currentJob?.job_id === job.job_id) {
    if ((!JOB_ACTIVE.has(currentJob.status)&&JOB_ACTIVE.has(job.status))
        || !Number(job.cursor) || Number(job.cursor)<=Number(currentJob.cursor||0)) return true;
    const updated={...currentJob,...job,events:job.events?.length?job.events:currentJob.events};
    showJob(updated);
    if (!JOB_ACTIVE.has(updated.status)) {
      clearTimeout(jobTimer);
      const generation=jobWatch;
      void watchJob(runId,updated,generation).catch(()=>{
        if(generation===jobWatch&&runId===state.runId)
          showJob(updated,'任务已结束，保存内容暂时未刷新。');
      });
    }
    return true;
  }
  const generation = ++jobWatch;
  clearTimeout(jobTimer);
  closeJobSource();
  currentRecovery = null;
  // watchJob installs the exact receipt synchronously before any awaited read.
  void watchJob(runId, job, generation).catch(() => {
    if (generation === jobWatch && runId === state.runId)
      showJob(currentJob || job, '任务已接收，进度读取暂时中断；可停止原任务。');
  });
  return true;
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
    if (pending) {
      const receipt = await api(`/api/runs/${runId}/request-receipts/${encodeURIComponent(pending.request_id)}?kind=job`);
      if (runId !== state.runId || generation !== jobWatch) return;
      if (receipt.received) clearPendingJob(runId, pending.request_id);
      else { showUncertainSubmission(runId, pending, readPendingJobOptions(runId, pending)); return; }
    }
    const steer = readPendingSteer(runId);
    if (steer) {
      const receipt = await api(`/api/runs/${runId}/request-receipts/${encodeURIComponent(steer.request_id)}?kind=steer&job_id=${encodeURIComponent(steer.job_id)}`);
      if (runId !== state.runId || generation !== jobWatch) return;
      if (receipt.rejected === true) {
        if (receipt.kind !== 'steer' || receipt.request_id !== steer.request_id || receipt.job_id !== steer.job_id)
          throw new Error('补充回执身份无法核对。');
        clearPendingSteer(runId, steer.request_id, false);
        toast(receipt.reason || '这条补充未能投递，原话已保留。请重新选择当前任务后发送。', true);
      } else if (receipt.received) clearPendingSteer(runId, steer.request_id);
      else {
        showJob(job, '调整是否收到尚未确认。请确认提交状态。');
        const retry = box('button','ghost small','确认调整状态');
        retry.onclick = () => submitSteer(runId, steer.job_id, steer);
        $('job-status').append(retry);
        return;
      }
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
  // A busy input has the same observation connection admission as an idle
  // input. It belongs to this receipt, never to the original running job.
  // Legacy pending receipts predate this field; retry their frozen payload
  // unchanged rather than granting an admission that was never submitted.
  const frozenSteer = readPendingSteer(runId);
  if (!frozenSteer || frozenSteer.request_id !== payload.request_id)
    payload = jobWirePayload(runId, payload);
  if (runId !== state.runId || steerPosting) return {ok:false, reason:'busy'};
  // An adjustment whose answer is still unknown owns this run's marker. A
  // second identity must neither go out nor overwrite that record — the
  // original request_id and payload are what the confirm entry retries (U2).
  const unresolved = readPendingSteer(runId);
  if (unresolved && !samePendingPayload(unresolved, {...payload, job_id:jobId})) {
    try { await restoreJob(runId); } catch (_) { /* best-effort original receipt */ }
    return {ok:false, reason:'pending_unknown', pending_kind:'steer',
            error:new Error('上一次调整的结果还没有确认；请先在任务状态里确认提交。')};
  }
  if (!unresolved) {
    // Cross entry (H2): a *job* submission is what this run is still
    // verifying — a direct adjuster (nothing here is behind `send()`) must
    // not open a second request family while that is unknown. Refuse, name
    // which original needs verification, and surface that original's own
    // entry; the two marker formats stay separate.
    const jobPending = readPendingJob(runId);
    if (jobPending) {
      try { await restoreJob(runId); } catch (_) { /* best-effort entry */ }
      return {ok:false, reason:'pending_unknown', pending_kind:'job',
              error:new Error('上一次发送的结果还没有确认；请先在任务状态里确认提交。')};
    }
  }
  steerPosting = true;
  sessionStorage.setItem(pendingSteerKey(runId), JSON.stringify({...payload, job_id:jobId}));
  if (!unresolved && typeof freezeComposerRequest === 'function') freezeComposerRequest(runId, payload.request_id);
  if (currentJob?.job_id === jobId) showJob(currentJob, '正在提交调整…');
  try {
    const updated = await api(`/api/runs/${runId}/jobs/${jobId}/steer`,
      {method:'POST', body:JSON.stringify(payload)});
    clearPendingSteer(runId, payload.request_id);
    if (runId === state.runId && currentJob?.job_id === jobId) showJob(updated);
    return {ok:true, job:updated};
  } catch (err) {
    if (err.status >= 400 && err.status < 500) {
      clearPendingSteer(runId, payload.request_id, false);
      const notActive = err.status === 409 &&
        /原任务已经结束|任务不在此进程运行/u.test(String(err.message || ''));
      if (runId === state.runId && notActive) {
        // The old job already ended server-side; refresh so send() can open a new one.
        try { await restoreJob(runId); } catch (_) { /* best-effort refresh */ }
        return {ok:false, reason:'job_not_active', error:err};
      }
      if (runId === state.runId) toast(err.message, true);
      // The rejected text is *returned*, not written into the composer. Only the
      // layer that owns the request — the one that took the draft snapshot — can
      // decide whether those words may still go back, and writing them here
      // overwrote whatever the user typed while this was in flight (T4).
      return {ok:false, reason:'client', error:err, rejected_text:payload.text};
    } else if (runId === state.runId) {
      showJob(currentJob, '调整结果尚未确认。刷新后可以用相同请求标识核对。');
    }
    return {ok:false, reason:'unknown', error:err};
  } finally {
    steerPosting = false;
    if (runId === state.runId && currentJob) showJob(currentJob);
  }
}

async function submitJob(runId, payload, options = {}) {
  if (runId !== state.runId) return {accepted:false,error:new Error('项目已切换，请重新打开当前项目。')};
  const unresolved = readPendingJob(runId);
  // Bind new conversation messages to the exact view already rendered. Saved
  // retries retain their original receipt rather than silently rebasing.
  if (!unresolved && !payload.request_kind
      && !Object.hasOwn(payload, 'display_context_id') && state.timeline?.display_context_id)
    payload = {...payload, display_context_id:state.timeline.display_context_id};
  // A saved receipt predates any defaults added since it was sent. Retry that
  // exact payload; only a new input receives current connection admission.
  if (!unresolved || unresolved.request_id !== payload.request_id)
    payload = jobWirePayload(runId, payload);
  // One unresolved submission identity per project (U2). A new request_id must
  // not displace — or silently replace — the record whose outcome is still
  // unknown; the confirm entry for that original identity stays on screen.
  if (unresolved && !samePendingPayload(unresolved, payload)) {
    showUncertainSubmission(runId, unresolved, readPendingJobOptions(runId, unresolved));
    return {accepted:false, uncertain:true, pending_kind:'job',
            error:new Error('上一次发送的结果还没有确认；请先在任务状态里确认提交。'),
            request_id:unresolved.request_id, rejected_text:payload.text};
  }
  if (!unresolved) {
    // Cross entry (H2): an *adjustment* is what this run is still verifying.
    // The page posts jobs directly — the journey review button, production
    // actions — without passing `send()`, so the transport itself refuses the
    // new identity, reports which original needs verification, and reopens
    // that original's own confirmation entry instead.
    const steerPending = readPendingSteer(runId);
    if (steerPending) {
      try { await restoreJob(runId); } catch (_) { /* best-effort entry */ }
      return {accepted:false, uncertain:true, pending_kind:'steer',
              error:new Error('上一次调整的结果还没有确认；请先在任务状态里确认提交。'),
              request_id:steerPending.request_id, rejected_text:payload.text};
    }
  }
  // Both conversation entry points display the same on-demand analysis notice.
  // This does not enable media generation; the main agent still needs an
  // explicit reference request, a supplied source and the bounded tool.
  const generation = ++jobWatch;
  clearTimeout(jobTimer);
  closeJobSource();
  currentRecovery = null;
  state.busy = true;
  $("send").disabled = true;
  // Persist only the user's request, never connection credentials. A retry uses
  // the same id even when the browser never received the acceptance response.
  sessionStorage.setItem(pendingJobKey(runId), JSON.stringify(payload));
  if (!unresolved && typeof freezeComposerRequest === 'function') freezeComposerRequest(runId, payload.request_id);
  if (options.preserveComposerOnError)
    sessionStorage.setItem(pendingJobUiKey(runId), JSON.stringify({request_id:payload.request_id,preserveComposerOnError:true}));
  else sessionStorage.removeItem(pendingJobUiKey(runId));
  showJob(null, "正在提交…");
  let job;
  try {
    const route = payload.request_kind === 'role_resume' ? `role-sessions/${encodeURIComponent(payload.session_id)}/resume` : payload.request_kind === 'reference_video' ? 'reference-videos' : payload.request_kind === 'stage_review' ? 'actions/review' : payload.request_kind === 'writing_revision' ? 'actions/revise-writing' : payload.request_kind === 'direction_resume' ? 'actions/resume-direction' : payload.request_kind === 'video_analysis' ? 'video-analysis' : payload.request_kind === 'image_analysis' ? 'image-analysis' : payload.request_kind === 'artifact_revision'
      ? `edits/${encodeURIComponent(payload.stage)}/${encodeURIComponent(payload.draft_id)}/generate` : payload.request_kind === 'design_upgrade' ? 'production/design-upgrade/propose' : 'jobs';
    const wire = {...payload};
    if (payload.request_kind === 'design_upgrade') delete wire.request_kind;
    const queueable=['jobs','actions/revise-writing','actions/resume-direction'].includes(route);
    job = await api(`/api/runs/${runId}/${route}${queueable?'?enqueue=true':''}`, { method: "POST", body: JSON.stringify(wire) });
  } catch (err) {
    const rejected = err.status >= 400 && err.status < 500;
    // Receipt ownership follows the project that sent the request, even after
    // navigation. Only visible UI updates depend on the current project.
    if (rejected) clearPendingJob(runId, payload.request_id, false);
    if (runId !== state.runId || generation !== jobWatch)
      return {accepted:false,error:err,uncertain:!rejected,request_id:payload.request_id,rejected_text:payload.text};
    if (rejected) {
      // A definite rejection: the words go back only if the composer is still
      // empty. `preserveComposerOnError` means the caller owns the draft (it has
      // a newer copy than this closure does), so this must not touch it.
      if (!payload.request_kind && !options.preserveComposerOnError && !$("chat-input").value.trim())
        $("chat-input").value = payload.text;
      showJob(null, err.message);
      await restoreJob(runId);
      toast(err.message, true);
    } else showUncertainSubmission(runId, payload, options);
    return {accepted:false, error:err, uncertain:!(err.status >= 400 && err.status < 500),
            request_id:payload.request_id, rejected_text:payload.text};
  }
  clearPendingJob(runId, payload.request_id);
  if (runId !== state.runId || generation !== jobWatch) return {accepted:true};
  if (payload.feedback_target && JSON.stringify(state.feedbackTarget) === JSON.stringify(payload.feedback_target))
    clearFeedbackTarget();
  // The request has been accepted and its marker settled. A failure from the
  // renderer after this point must not escape as a thrown error: `send()` would
  // read that as a definite pre-send failure and hand the words back for a
  // resend that could run the prompt twice. The job itself is already durable.
  try { await watchJob(runId, job, generation); }
  catch (_) { try { showJob(job); } catch (_) { /* the status panel is best-effort */ } }
  try { await refreshTimeline(); }
  catch (_) { toast("任务已接受，正在运行；对话暂时未刷新。", true); }
  return {accepted:true};
}

if (typeof module !== "undefined") module.exports = { jobPresentation };
