"use strict";

const TEAM_ROLES = {orchestrator:'主制片', director:'创意导演', material:'视觉策划', writer:'编剧', reviewer:'独立审片'};
const TEAM_STAGES = {intake:'创作需求', direction:'创意方向', material_plan:'素材计划', writing:'脚本与分镜'};
const TEAM_WORK = {orchestrator:'整理资料与下一步建议', director:'构思创意方向', material:'整理画面与素材计划', writer:'编写脚本与分镜', reviewer:'检查方案与证据'};
const TEAM_TOOLS = {
  read_project_state:['核对项目进度','已核对项目进度'],
  read_project_history:['查阅项目历史','已查阅历史资料'],
  prepare_creative_context:['整理相关方法与案例','已整理相关方法与案例'],
  read_artifact:['阅读方案','已阅读方案'], list_reviews:['核对审核意见','已核对审核意见'],
  list_materials:['查看素材清单','已查看素材清单'],
  write_stage:['保存创作内容','已保存创作内容'], submit_stage:['提交阶段方案','已提交阶段方案'],
  dispatch:['安排角色工作','角色工作已返回'], request_review:['安排独立审核','审核已返回'],
  start_role:['安排角色工作','角色任务已启动'], wait_role:['等待角色交付','已读取角色回执'],
  message_role:['传递本次补充','补充已送到对应角色'], cancel_role:['停止指定任务','已要求停止指定任务'],
  resume_role:['继续角色任务','角色任务已返回'], propose_task_interpretation:['核对本次要求与范围','已绑定本次要求与范围'],
  record_question_answer:['确认本条回答','回答已记录'],
  finish_review:['记录审核结论','已记录审核结论'],
  read_method_document:['查阅创作方法','已查阅创作方法'],
  read_learning_cases:['阅读参考案例','已阅读参考案例'],
  propose_learning_feedback:['整理反馈建议','已整理反馈建议，待你确认'],
  compile_creative_package:['整理创意交付包','已整理创意交付包'],
  read_production:['查阅制作方案','已查阅制作方案'],
  prepare_production:['准备制作方案','已保存制作方案，待你检查'],
  review_production:['检查制作输入','已检查制作输入'],
};
const TEAM_CHAT_TOOLS = new Set([
  'dispatch','request_review','write_stage','submit_stage','finish_review',
  'prepare_production','review_production','propose_learning_feedback','compile_creative_package',
]);

function teamAction(event, completed = false) {
  const stage = TEAM_STAGES[event.args?.stage || event.stage];
  if (event.tool === 'dispatch') {
    const role = TEAM_ROLES[event.args?.role] || '受托角色';
    return `${role}${completed ? event.ok === false ? '未能交回' : '已交回' : '正在处理'}${stage ? ` · ${stage}` : ''}`;
  }
  if (event.tool === 'request_review')
    return `独立审片${completed ? event.ok === false ? '未能完成检查' : '已完成检查' : '正在检查'}${stage ? ` · ${stage}` : ''}`;
  if (event.tool === 'record_question_answer' && completed && event.ok === false) return '本条回答未能记录';
  let text = (TEAM_TOOLS[event.tool] || ['处理项目操作','项目操作已返回'])[completed ? 1 : 0];
  if (stage) text += ` · ${stage}`;
  if (completed && event.ok === false) return `${text.replace(/^已/, '')} · 未完成`;
  if (completed && event.tool === 'read_project_history') {
    try { const result = JSON.parse(event.result); if (typeof result.title === 'string') text += `「${result.title.slice(0, 100)}」`; } catch (_) { /* Older results may be truncated. */ }
  }
  if (completed && ['write_stage','submit_stage'].includes(event.tool) && typeof event.args?.summary === 'string') {
    text += `：${event.args.summary.slice(0, 180)}`;
  }
  if (completed && event.tool === 'finish_review') {
    const verdict = {pass:'审核通过，仍待你决定', return:'需要修改'}[event.args?.verdict];
    if (verdict) text += ` · ${verdict}`;
  }
  return text;
}

function modelWaitDuration(seconds) {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60), remainder = total % 60;
  return minutes ? `${minutes}分${remainder ? `${remainder}秒` : '钟'}` : `${total}秒`;
}

function modelWaiting(job) {
  if (!['running','cancelling'].includes(job?.status)) return null;
  // A later reply, tool or new turn ends this transport observation. Never
  // keep an old heartbeat visible while a different step is already running.
  const latest = [...(job.events || [])].reverse().find(event =>
    ['agent_activity','agent_narration','agent_narration_preview','agent_tool','agent_turn'].includes(event.type));
  if (latest?.type !== 'agent_activity' || latest.phase !== 'waiting_model') return null;
  const seconds = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
  const elapsed = seconds(latest.elapsed_seconds), remaining = seconds(latest.remaining_seconds);
  const timeout = seconds(latest.timeout_seconds) ?? seconds(elapsed !== null && remaining !== null ? elapsed + remaining : null);
  const parts = [latest.transport_state === 'receiving' ? '正在接收模型回复' : '等待模型回复'];
  if (elapsed !== null) parts.unshift(`已等待 ${modelWaitDuration(elapsed)}`);
  if (timeout !== null) parts.push(`单次最多等待 ${modelWaitDuration(timeout)}`);
  if (remaining !== null) parts.push(`本次最多还能等待 ${modelWaitDuration(remaining)}`);
  return {role:TEAM_ROLES[latest.role] ? latest.role : null, text:parts.join('；')};
}

function teamProgress(job) {
  const events = job?.events || [];
  const active = ['queued','running','cancelling'].includes(job?.status);
  const last = [...events].reverse().find(e => TEAM_ROLES[e.role]);
  const currentRole = active && job.status === 'running' ? last?.role : null;
  const waiting = modelWaiting(job);
  let current = '';
  if (currentRole) {
    const action = last.type === 'agent_activity' && last.phase === 'tool' ? teamAction(last)
      : last.type === 'agent_activity' && last.phase === 'thinking' ? TEAM_WORK[currentRole]
      : waiting ? waiting.text
      : '继续处理本轮内容（等待下一条进展）';
    current = `${TEAM_ROLES[currentRole]} · ${['agent_narration','agent_narration_preview'].includes(last.type) && typeof last.text === 'string'
      ? last.text.trim().slice(0, 220) : action}`;
  } else if (active) current = job.status === 'queued' ? '等待制作团队开始' : job.status === 'cancelling'
    ? '等待当前调用结束，随后停止' : '正在准备本轮工作，等待角色进展';
  if (waiting && !current.includes(waiting.text)) current += `；${waiting.text}`;
  const history = [];
  for (const event of events) {
    if (event.type === 'agent_narration' && typeof event.text === 'string' && event.text.trim()) {
      history.push({role:TEAM_ROLES[event.role] || '制片团队', text:event.text.trim().slice(0, 500), at:event.at});
      continue;
    }
    if (event.type !== 'agent_tool') continue;
    history.push({role:TEAM_ROLES[event.role] || '制片团队', text:teamAction(event, true), at:event.at,
      failed:event.ok === false, stage:event.ok && ['write_stage','submit_stage'].includes(event.tool)
        ? event.args?.stage || event.stage : null});
  }
  const participants = Object.entries(TEAM_ROLES).filter(([role]) => role !== 'reviewer' ||
    events.some(event => event.role === 'reviewer') ||
    (job.role_sessions || []).some(session => session.role === 'reviewer' && session.parent_job_id === job.job_id))
    .map(([role,name]) => {
    const sessions = (job.role_sessions || []).filter(row => row.role === role && row.parent_job_id === job.job_id);
    const working = active && sessions.some(row => ['queued','running'].includes(row.status) && !row.stop_requested);
    return {role,name,status:role === currentRole || working ? '处理中'
      : sessions.length || events.some(e=>e.role === role) ? '本轮已参与' : '本轮未参与'};
  });
  const outputs = history.filter(row => row.stage);
  const summary = outputs.length ? `已保存 ${outputs.length} 次阶段内容，见团队工作记录。`
    : events.some(e=>e.type === 'done' && e.text) ? '本轮回复已保存到下方对话。'
    : '本轮状态已保存，可在团队工作记录中核对进展。';
  return {current, currentRole, history, participants, outputs,
    statusTitle: job?.brief?.title || '', summary: job?.brief?.detail || summary, lastAt:events.at(-1)?.at};
}

function teamTime(at) {
  const date = new Date(at);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'});
}

let teamDialog = null;
let teamDialogKey = '';
function roleInputClosed(session) {
  return session?.input_closed === true || Boolean(session?.artifact_receipt);
}
function roleInputClosedMessage(session) {
  if (session?.pending_input_notice) return session.pending_input_notice;
  const receipt = session?.artifact_receipt;
  const saved = `${TEAM_STAGES[receipt?.stage] || '本项产物'}${Number.isInteger(receipt?.version) ? `第 ${receipt.version} 版` : ''}`;
  return `${saved}已保存，旧任务不再接收补充。请阅读当前稿，在新稿上重新选择具体位置并发送修改意见。`;
}
function openTeamProgress() {
  if (teamDialog) { teamDialog.close(); return; }
  const dialog = box('dialog','team-dialog');
  dialog.setAttribute('aria-label','团队工作记录');
  const header = box('div','team-dialog-heading');
  header.append(box('h2','','团队工作记录'));
  const close = box('button','ghost small','关闭');
  close.onclick = () => dialog.close(); header.append(close);
  dialog.append(header, box('div','team-dialog-body'));
  dialog.addEventListener('close',()=>{
    dialog.remove();
    if (teamDialog === dialog) { teamDialog=null;teamDialogKey=''; }
    if (dialog.roleFeedbackRunId === state.runId) $('chat-input').focus();
  });
  document.body.append(dialog); teamDialog=dialog; updateTeamDialog(); dialog.showModal();
}

function updateTeamDialog() {
  if (!teamDialog) return;
  const job = currentJob;
  const runId = state.runId;
  const brief = state.projectBrief || job?.brief;
  const key = JSON.stringify([runId, job?.job_id, job?.status, job?.events?.length, job?.cursor, job?.role_sessions, brief]);
  if (key === teamDialogKey) return;
  teamDialogKey = key;
  const host = teamDialog.querySelector('.team-dialog-body');
  const scroll = host.scrollTop;
  const technicalOpen = host.querySelector('.team-diagnostics')?.open;
  host.replaceChildren();
  if (!job) { host.append(box('p','','这个项目还没有本轮工作记录。')); return; }
  const view = teamProgress({...job, brief});
  host.append(box('p','team-current',view.current || view.statusTitle || '本次执行已结束'));
  if (!JOB_ACTIVE.has(job.status) && job.reason) {
    host.append(box('p','team-reason',jobGuidance(job) || '本轮未完成，请按当前状态处理。'));
    host.append(box('p','muted','已保存的需求和作品不会自动重试；处理原因后，在对话中说明下一步。'));
  } else if (!JOB_ACTIVE.has(job.status)) host.append(box('p','muted',view.summary));
  const roles = box('div','team-members');
  for (const row of view.participants) {
    const card = box('div',`team-member${row.role === view.currentRole ? ' active' : ''}`);
    card.append(box('strong','',row.name),box('span','',row.status));roles.append(card);
  }
  host.append(roles,box('h3','','本轮做了什么'));
  for (const session of job.role_sessions || []) {
    const card = box('div','team-member');
    const labels = {queued:'等待执行',running:'正在执行',waiting:'等待明确继续',completed:'已交回',cancelled:'已停止',failed:'未完成',interrupted:'服务中断，未自动重跑'};
    card.append(box('strong','',TEAM_ROLES[session.role] || '创作角色'),
      box('span','',session.stop_requested && ['queued','running'].includes(session.status)
        ? '正在停止此项' : roleInputClosed(session) && session.status === 'running'
          ? '产物已保存，正在收尾' : labels[session.status] || '状态待核实'));
    if (session.reason) card.append(box('p','muted',session.reason));
    const liveSession = () => state.runId === runId && currentJob?.job_id === job.job_id
      ? currentJob.role_sessions?.find(row => row.session_id === session.session_id) : null;
    if (roleInputClosed(session)) {
      card.append(box('p','muted',roleInputClosedMessage(session)));
      const receipt = session.artifact_receipt;
      if (TEAM_STAGES[receipt?.stage] && Number.isInteger(receipt.version) && receipt.version > 0) {
        const read = box('button','ghost small','查看已保存版本');
        read.onclick = () => {
          if (!liveSession()) return;
          dialogCloseTeam();
          openReader(receipt.stage, receipt.version).catch(err => toast(err.message, true));
        };
        card.append(read);
        const feedback = box('button','ghost small','在当前稿重新选择反馈');
        feedback.onclick = () => {
          if (!liveSession()) return;
          dialogCloseTeam();showJourneyTab('plan',{stage:receipt.stage});setProjectFocus('canvas');
        };
        card.append(feedback);
      }
    }
    if (session.parent_job_id === job.job_id && canSteerJob(job) && !session.stop_requested && ['queued','running'].includes(session.status)) {
      if (!roleInputClosed(session)) {
        const amend = box('button','ghost small','补充要求');
        amend.onclick = () => {
          if (!setRoleFeedbackTarget(runId, liveSession())) return;
          teamDialog.roleFeedbackRunId=runId;dialogCloseTeam();$('chat-input').focus();
        };
        card.append(amend);
      }
      const stop = box('button','ghost small','停止此项');
      stop.onclick = async () => {
        const live = liveSession();
        if (!live || live.stop_requested || !canSteerJob(currentJob) || !['queued','running'].includes(live.status)) return;
        stop.disabled = true;
        try { await submitSteer(runId, job.job_id, {text:'停止', request_id:crypto.randomUUID(),
          expected_goal_revision:currentJob.goal_revision || 0, target_session_id:session.session_id}); }
        catch (err) { if (state.runId === runId) toast(err.message, true); }
        finally { stop.disabled = false; }
      };
      card.append(stop);
    } else if (!JOB_ACTIVE.has(job.status) && !session.stop_requested && !roleInputClosed(session) && ['waiting','interrupted','failed'].includes(session.status)) {
      if (session.resume_command?.resume_action !== 'continue' || !session.resume_command.text) {
        card.append(box('p','muted','继续入口尚未核实，请刷新任务状态后再试。'));
        host.append(card);
        continue;
      }
      const resume = box('button','ghost small','继续此项');
      resume.onclick = async () => {
        const live = liveSession();
        if (!live || live.stop_requested || roleInputClosed(live) || JOB_ACTIVE.has(currentJob.status)
            || !['waiting','interrupted','failed'].includes(live.status)) return;
        if (live.resume_command?.resume_action !== 'continue' || !live.resume_command.text) {
          toast('继续入口已变化，请刷新任务状态后再试。', true);return;
        }
        resume.disabled = true;
        try { await submitJob(runId, {request_kind:'role_resume',session_id:session.session_id,
          expected_task_id:live.task_id,request_id:crypto.randomUUID(),
          resume_action:live.resume_command.resume_action,text:live.resume_command.text}); }
        catch (err) { if (state.runId === runId) toast(err.message, true); }
        finally { resume.disabled = false; }
      };
      card.append(resume);
    }
    host.append(card);
  }
  const history = box('ol','team-history');
  for (const row of view.history) {
    const item = box('li',row.failed?'team-step failed':'team-step');
    item.append(box('span','team-step-meta',`${teamTime(row.at)} · ${row.role}`),box('p','',row.text));
    if (TEAM_STAGES[row.stage]) {
      const read = box('button','ghost small','查看当前阶段');
      read.onclick=()=>{dialogCloseTeam();selectStage(row.stage);setProjectFocus('canvas');};item.append(read);
    }
    history.append(item);
  }
  if (!view.history.length) history.append(box('li','muted','尚无已完成的操作记录；收到进展后会显示在这里。'));
  host.append(history);
  const technical = box('details','team-diagnostics');
  technical.open = Boolean(technicalOpen);
  technical.append(box('summary','','技术诊断（排障时使用）'));
  technical.append(box('p','muted',`执行编号：${job.job_id || '待确认'}`));
  if (job.reason) technical.append(box('pre','',String(job.reason)));
  if (job.recovery) technical.append(box('pre','',String(job.recovery)));
  if (job.open_attempts?.length || job.unresolved_calls?.length) technical.append(box('pre','',JSON.stringify({open_attempts:job.open_attempts,unresolved_calls:job.unresolved_calls},null,2)));
  technical.append(box('pre','',JSON.stringify((job.events || []).map(e=>({type:e.type,role:e.role,tool:e.tool,ok:e.ok,at:e.at})),null,2)));
  host.append(technical);host.scrollTop=scroll;
}
function dialogCloseTeam() { teamDialog?.close(); }

if (typeof module !== 'undefined') module.exports = {teamAction, teamProgress, modelWaiting, roleInputClosed, roleInputClosedMessage};
