/* Read-only creator view. It does not grant an action or change a production gate. */
"use strict";

function creatorProjectView({run = {}, timeline = {}, imageWork = null, videoWork = null,
  referenceTasks = [], currentJob = null} = {}) {
  const rail = timeline.rail || [];
  const writing = rail.find(row => row.stage === 'writing' && row.has_doc);
  const direction = rail.find(row => row.stage === 'direction' && row.has_doc);
  const hasCreativeWork = rail.some(row => row.has_doc);
  const reference = [...referenceTasks].reverse().find(row => row.intent === 'analyze');
  const analysisOnly = !hasCreativeWork && !imageWork?.draft && !videoWork?.draft &&
    (Boolean(reference) || currentJob?.job_kind === 'reference_video');
  const hasCaption = Boolean((writing?.blocks || []).find(block =>
    block.type === 'text' && block.title === '发布文案' && block.text));
  const requestedCaption = /(?:与|和|及|附|加上|需要|附带|[+＋]).{0,6}(?:英文)?(?:发布文案|caption)/iu.test(run.brief || '');
  const declaredFormat = /^交付形式：带货图片与文案。/u.test(run.brief || '') ? 'photo'
    : /^交付形式：商品视频。/u.test(run.brief || '') ? 'video' : null;
  const format = run.content_format || declaredFormat || (videoWork?.draft ? 'video' : null) ||
    (imageWork?.draft ? 'photo' : null) ||
    ((writing?.blocks || []).some(block => block.type === 'timeline') ? 'video' : null);
  const goal = analysisOnly ? '参考视频拆解报告'
    : format === 'photo' ? '带货图片与文案'
    : format === 'video' ? `商品视频${hasCaption || requestedCaption ? '与发布文案' : ''}`
    : '交付形式待确认';
  const result = (current, action, tab = 'plan', stage = null, detail = '') =>
    ({goal, current, action, tab, stage, detail, analysisOnly, reference, format});
  const nav = (label, tab, current, stage = null, detail = '') =>
    result(current, {kind: 'navigate', label, tab, stage}, tab, stage, detail);
  const pending = timeline.pending;
  const continuation = timeline.pending_continuation;
  const interaction = timeline.interaction;
  const active = timeline.brief?.execution?.active;

  if (analysisOnly) {
    if (reference?.report) return nav('阅读分析结果', 'plan', '参考拆解报告已保存', null,
      '阅读观察与限制；只分析不会自动进入制作。');
    return result(active ? '正在分析这条参考' : '参考分析尚未完成',
      active ? null : {kind: 'reference', label: '查看参考任务'}, 'plan');
  }
  if (active) return result('团队正在处理当前任务', null, 'plan', null,
    '已保存的作品仍可阅读；新结果会在完成后显示。');
  if (continuation) return result('决定已保存，下一份作品尚未开始准备',
    {kind: 'continue', label: '继续准备'}, 'plan', continuation.stage);
  if (interaction) return result(interaction.question || '有一项需要你确认',
    {kind: 'question', label: '回答这个问题'}, 'plan');
  if (pending?.stage === 'direction') return nav('选择创意方向', 'plan',
    '正在选择创意方向；采用后会继续准备脚本', 'direction');
  if (pending?.stage === 'writing') return nav('阅读并决定脚本', 'plan',
    '脚本已准备好，等待你阅读和决定', 'writing');
  if (timeline.brief?.phase === 'review_failed' && timeline.brief.action?.kind === 'review_stage') {
    const review = timeline.brief.action;
    const name = review.stage === 'direction' ? '创意方向' : review.stage === 'writing' ? '脚本' : '当前作品';
    return result(`${name}已保存，内部审核未完成`, review, 'plan', review.stage,
      timeline.brief.detail || '先阅读当前作品与审核记录，再决定是否重试本版审核。');
  }

  // The latest unresolved attempt takes precedence over an older usable result.
  // The production selector validates material bytes and the exact run URL.
  const work = videoWork?.draft ? videoWork
    : imageWork?.phase === 'image_pending' && imageWork?.draft ? imageWork
    : videoWork?.phase === 'prepare' ? videoWork
    : imageWork?.draft ? imageWork : null;
  if (work) {
    const tab = work.kind === 'video' ? 'video' : 'storyboard';
    if (work.phase === 'unknown') return nav(work.primaryAction === 'refresh'
      ? '查询这次结果' : '核实这次提交', tab,
      `${work.kind === 'video' ? '视频' : '图片'}生成结果尚未确认`, null,
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
    if (work.phase === 'failed' || work.phase === 'blocked' || work.phase === 'stale')
      return nav('查看阻塞与下一步', tab, work.label || '本次制作需要处理后继续');
    if (work.phase === 'prepare') return nav('准备当前画面', tab,
      work.kind === 'image' ? '脚本已确认，下一步准备画面' : '参考图已确认，下一步准备视频');
  }
  if (writing) {
    if (writing.stale || writing.integrity_ok === false) return nav('核对稿件变化', 'plan',
      '当前脚本的来源或文件已变化', 'writing');
    if (writing.approval_decision === 'approved' || writing.approval_decision === 'approved_override'
        || timeline.brief?.phase === 'ready') return result('脚本已确认，下一步准备实际画面',
      {kind:'navigate',label:'准备当前画面',tab:'storyboard'}, 'plan', 'writing');
    return nav('阅读并修改脚本', 'plan', '当前脚本可阅读和修改', 'writing');
  }
  if (direction) {
    const approved = ['approved','approved_override'].includes(direction.approval_decision);
    if (approved) return result(`已采用方向${direction.selected_option_id ? ` ${direction.selected_option_id}` : ''}，下一份脚本尚未准备好`,
      {kind:'chat',label:'讨论下一份脚本'}, 'plan', 'direction');
    return nav('阅读创意方向', 'plan', '当前创意方向可阅读', 'direction');
  }
  if (timeline.brief?.action?.kind === 'review_stage') return result(
    '当前作品已保存，团队检查尚未完成',
    {...timeline.brief.action, kind:'review_stage'}, 'plan', timeline.brief.action.stage);
  return result('说出你想完成的作品', {kind: 'chat', label: '继续对话'}, 'plan');
}

if (typeof module !== 'undefined') module.exports = {creatorProjectView};
