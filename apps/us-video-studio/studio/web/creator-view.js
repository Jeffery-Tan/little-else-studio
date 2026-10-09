/* Read-only creator view. It does not grant an action or change a production gate. */
"use strict";

function creatorProjectView({run = {}, timeline = {}, imageWork = null, videoWork = null,
  referenceTasks = [], currentJob = null} = {}) {
  const rail = timeline.rail || [];
  const writing = rail.find(row => row.stage === 'writing' && row.has_doc);
  const direction = rail.find(row => row.stage === 'direction' && row.has_doc);
  const hasCreativeWork = rail.some(row => row.has_doc);
  const projectedAnalysis = timeline.brief?.analysis;
  const imageAnalysis = projectedAnalysis?.kind === 'image' && projectedAnalysis.analysis_only === true &&
    !hasCreativeWork && !imageWork?.draft && !videoWork?.draft ? projectedAnalysis : null;
  const latestReference = referenceTasks.at(-1);
  const reference = latestReference?.intent === 'analyze' ? latestReference : null;
  const analysisOnly = !hasCreativeWork && !imageWork?.draft && !videoWork?.draft &&
    (Boolean(imageAnalysis) || Boolean(reference) ||
      (currentJob?.job_kind === 'reference_video' && latestReference?.intent === 'analyze'));
  const creativeTimes = [
    ...rail.map(row => row.created_at), imageWork?.draft?.created_at, videoWork?.draft?.created_at,
    imageWork?.attempt?.at, videoWork?.attempt?.at,
  ].map(value => Date.parse(value || '') || 0);
  const referenceStarted = Date.parse(reference?.started_at || '') || 0;
  const referenceAfterCreative = reference?.after_creative_work === true ||
    (reference?.after_creative_work == null && referenceStarted >= Math.max(0, ...creativeTimes));
  const currentProjectJob = currentJob?.run_id === run.run_id ? currentJob : null;
  const newerCreativeJob = currentProjectJob && currentProjectJob.job_kind !== 'reference_video' &&
    (Date.parse(currentProjectJob.at || '') || 0) > referenceStarted;
  const referenceCurrent = !analysisOnly && referenceStarted > 0 && referenceAfterCreative &&
    !newerCreativeJob && !timeline.pending && !timeline.pending_continuation &&
    !timeline.pending_brief_redo &&
    !timeline.interaction && (!timeline.brief?.execution?.active || currentProjectJob?.job_kind === 'reference_video');
  const hasCaption = Boolean((writing?.blocks || []).find(block =>
    block.type === 'text' && block.title === '发布文案' && block.text));
  const requestedCaption = /(?:与|和|及|附|加上|需要|附带|[+＋]).{0,6}(?:英文)?(?:发布文案|caption)/iu.test(run.brief || '');
  const declaredFormat = /^交付形式：带货图片与文案。/u.test(run.brief || '') ? 'photo'
    : /^交付形式：商品视频。/u.test(run.brief || '') ? 'video' : null;
  const projectedFormat = Object.prototype.hasOwnProperty.call(timeline.run || {}, 'content_format')
    ? timeline.run.content_format : run.content_format;
  const format = projectedFormat || declaredFormat || (videoWork?.draft ? 'video' : null) ||
    (imageWork?.draft ? 'photo' : null) ||
    ((writing?.blocks || []).some(block => block.type === 'timeline') ? 'video' : null);
  const writingName = format === 'photo' ? '图文方案' : format === 'video' ? '脚本' : '创作方案';
  const goal = imageAnalysis ? imageAnalysis.goal : analysisOnly ? '参考视频拆解报告'
    : format === 'photo' ? '带货图片与文案'
    : format === 'video' ? `商品视频${hasCaption || requestedCaption ? '与发布文案' : ''}`
    : '交付形式待确认';
  const result = (current, action, tab = 'plan', stage = null, detail = '') =>
    ({goal, current, action, tab, stage, detail, analysisOnly, imageAnalysis, referenceCurrent, reference, format});
  const nav = (label, tab, current, stage = null, detail = '') =>
    result(current, {kind: 'navigate', label, tab, stage}, tab, stage, detail);
  const pending = timeline.pending;
  const continuation = timeline.pending_continuation;
  // An approved brief amendment whose redo job never started. Same recovery
  // shape, different resolution: it is keyed by the amendment, not by a version
  // the amendment itself invalidated.
  const briefRedo = timeline.pending_brief_redo;
  const interaction = timeline.interaction;
  const active = timeline.brief?.execution?.active;
  const tokenCap = currentProjectJob?.status === 'blocked'
    ? /^\[budget\] (\d+) tokens reached the soft cap of (\d+); refusing a new call\.$/u.exec(String(currentProjectJob.reason || ''))
    : null;
  const dollarCap = currentProjectJob?.status === 'blocked'
    ? /^\[budget\] estimated known spend reached the soft cap of \d+(?:\.\d+)? USD; refusing a new call\.$/u.test(String(currentProjectJob.reason || ''))
    : false;

  if ((tokenCap || dollarCap) && timeline.budget?.limit_mode === 'none') return result(
    '上一轮被旧额度设置中断，项目内容已保存',
    {kind:'chat',label:'继续创作'}, 'plan', null,
    '当前已不设置工作台额度。旧任务不会自动重试；请说明接下来要做什么，发送后才会开始新一轮。');
  if (tokenCap) return result(
    `创作已暂停：本机共享 Token 上限已达到（${Number(tokenCap[1]).toLocaleString('zh-CN')} / ${Number(tokenCap[2]).toLocaleString('zh-CN')}）`,
    null, 'plan', null, '已保存的作品和决定仍可阅读。');
  if (dollarCap) return result('创作已暂停：本机共享美元预算上限已达到',
    null, 'plan', null, '已保存的作品和决定仍可阅读。');

  if (imageAnalysis) return nav('阅读分析结果', 'plan', imageAnalysis.title, null, imageAnalysis.detail);
  if (analysisOnly) {
    if (reference?.report) return nav('阅读分析结果', 'plan', '参考拆解报告已保存', null,
      '阅读观察与限制；只分析不会自动进入制作。');
    return result(active ? '正在分析这条参考' : '参考分析尚未完成',
      active ? null : {kind: 'reference', label: '查看参考任务'}, 'plan');
  }
  if (active && !(referenceCurrent && currentProjectJob?.job_kind === 'reference_video'))
    return result('团队正在处理当前任务', null, 'plan', null,
    '已保存的作品仍可阅读；新结果会在完成后显示。');
  if (continuation) return result('决定已保存，下一份作品尚未开始准备',
    {kind: 'continue', label: '继续准备'}, 'plan', continuation.stage);
  if (briefRedo) return result('简报修订已保存，团队重做尚未开始',
    {kind: 'brief-redo', label: '继续重做方向'}, 'plan', 'direction');
  if (interaction) return result(interaction.question || '有一项需要你确认',
    {kind: 'question', label: '回答这个问题'}, 'plan');
  if (pending?.source_execution?.notice) return nav('查看或修改草稿', 'plan',
    pending.headline, pending.stage, pending.source_execution.notice);
  if (pending?.stage === 'direction') return pending.passed === false
    ? nav('查看方向与审核', 'plan', '创意方向已保存，但审核提出修改意见', 'direction')
    : nav('选择创意方向', 'plan', `正在选择创意方向；采用后会继续准备${writingName}`, 'direction');
  if (pending?.stage === 'writing') return pending.passed === false
    ? nav(`查看${writingName}与审核`, 'plan', `${writingName}新版已保存，但审核提出修改意见`, 'writing')
    : nav(`阅读并决定${writingName}`, 'plan', `${writingName}已准备好，等待你阅读和决定`, 'writing');
  if (timeline.brief?.questions?.length) return result('有个细节想和你确认',
    {kind: 'question', label: '回答这个问题'}, 'plan', timeline.brief.questions[0].stage,
    '说出现有条件或还不确定的地方，团队会调整表达与素材安排。');
  if (timeline.brief?.action?.kind === 'review_stage') {
    const review = timeline.brief.action;
    const name = review.stage === 'direction' ? '创意方向' : review.stage === 'writing' ? writingName : '当前作品';
    return result(`${name}已保存，内部审核未完成`, review, 'plan', review.stage,
      timeline.brief.detail || '先阅读当前作品与审核记录，再决定是否重新审核当前方案。');
  }
  if (referenceCurrent) return result(reference?.report
    ? '本轮只分析新参考：拆解报告已保存' : '本轮只分析新参考：报告尚未完成',
    reference?.report ? null : {kind:'reference',label:'查看参考任务'}, 'plan',
    writing?.stage || direction?.stage || null,
    '下方阅读本轮参考；原项目的最终交付仍按作品目标保留。');
  if (timeline.brief?.phase === 'needs_resolution') return result(
    `素材方案有关键缺口，完整${writingName}暂不继续`,
    {kind:'chat',label:'调整素材方案'}, 'plan', 'material_plan', timeline.brief.detail || '');

  // The latest unresolved attempt controls recovery; a verified earlier result
  // can remain the readable work. The selector checks its material bytes and URL.
  const work = format === 'photo'
    ? (imageWork?.draft || imageWork?.phase === 'prepare' ? imageWork : null)
    : videoWork?.draft ? videoWork
    : imageWork?.phase === 'image_pending' && imageWork?.draft ? imageWork
    : videoWork?.phase === 'prepare' ? videoWork
    : imageWork?.draft ? imageWork : null;
  if (work) {
    const tab = work.kind === 'video' ? 'video' : 'storyboard';
    if (work.phase === 'unknown') return nav(work.primaryAction === 'refresh'
      ? '查询这次结果' : '核实这次提交', tab,
      work.resultIsPrevious ? `上一份已生成${work.kind === 'video' ? '视频' : '图片'}仍可查看；这次结果尚未确认`
        : `${work.kind === 'video' ? '视频' : '图片'}生成结果尚未确认`, null,
      '先处理原任务；当前不会再次提交生成。');
    if (work.phase === 'processing') return result(
      `${work.kind === 'video' ? '视频' : '图片'}任务正在处理`, null, tab);
    if (work.phase === 'missing') return nav('查看结果缺口', tab,
      '生成记录已保存，但结果文件暂不可核对');
    if (work.kind === 'image' && work.phase === 'image_pending') return nav(
      '查看并确认图片', tab, '图片已取回，等待你确认这张画面');
    if (work.phase === 'completed') return nav(work.kind === 'video'
      ? '播放并核对视频' : '查看已取回图片', tab,
      `${work.kind === 'video' ? '视频' : '图片'}已取回，核对实际内容`);
    if (work.phase === 'ready') return nav('核对并生成', tab,
      `${work.kind === 'video' ? '视频' : '图片'}输入已准备好，等待你确认本次生成`);
    if (work.phase === 'review') return nav('查看制作准备', tab,
      '制作描述已保存，等待完成文字检查');
    if (work.phase === 'blocked' && work.primaryAction === 'settings')
      return result(work.label, {kind:'settings',label:'打开模型设置'}, tab, null, work.detail);
    if (work.phase === 'blocked' && work.primaryAction === 'none')
      return result(work.label, null, tab, null, work.detail);
    if (work.phase === 'failed' || work.phase === 'blocked' || work.phase === 'stale')
      return nav('说明这次问题', tab,
        // The actual reason, not only the label. Dropping it here was how a
        // failed generation reached the project view as "未完成" with no cause.
        work.resultIsPrevious
          ? `上一份已生成${work.kind === 'video' ? '视频' : '图片'}仍可查看；${work.detail || work.label}`
          : work.detail || work.label || '本次制作需要处理后继续');
    if (work.phase === 'prepare') return result(
      work.kind === 'image' ? `${writingName}已确认，下一步准备画面` : '参考图已确认，下一步准备视频',
      {kind:'prepare',label:work.kind === 'image' ? '准备画面输入' : '准备这张图的视频输入',tab}, tab);
  }
  if (timeline.next_action?.kind === 'resume_direction') return result(
    '当前创意方向需要更新', timeline.next_action, 'plan', 'direction',
    '按当前有效需求和已选参考直接重做，完成后交你点选。');
  if (writing) {
    if (writing.stale || writing.integrity_ok === false) return nav('核对稿件变化', 'plan',
      `当前${writingName}的来源或文件已变化`, 'writing');
    if (writing.approval_decision === 'approved' || writing.approval_decision === 'approved_override'
        || timeline.brief?.phase === 'ready') return result(`${writingName}已确认，下一步准备实际画面`,
      {kind:'navigate',label:'准备当前画面',tab:'storyboard'}, 'plan', 'writing');
    return nav(`阅读并修改${writingName}`, 'plan', `当前${writingName}可阅读和修改`, 'writing');
  }
  if (direction) {
    const approved = ['approved','approved_override'].includes(direction.approval_decision);
    if (approved) return result(`已采用方向${direction.selected_option_id ? ` ${direction.selected_option_id}` : ''}，下一份${writingName}尚未准备好`,
      {kind:'chat',label:`讨论下一份${writingName}`}, 'plan', 'direction');
    return nav('阅读创意方向', 'plan', '当前创意方向可阅读', 'direction');
  }
  if (timeline.brief?.phase === 'interrupted' && String(run.brief || '').trim()) return result(
    '你的需求已保存，本轮制片未完成',
    {kind:'job',label:'查看本轮记录'}, 'plan', null,
    '查看本轮失败原因后，再决定是否继续；不会自动重试。');
  if (String(run.brief || '').trim()) return result(
    '你的需求已保存，等待继续创作', {kind:'chat',label:'继续对话'}, 'plan');
  return result('说出你想完成的作品', {kind: 'chat', label: '继续对话'}, 'plan');
}

if (typeof module !== 'undefined') module.exports = {creatorProjectView};
