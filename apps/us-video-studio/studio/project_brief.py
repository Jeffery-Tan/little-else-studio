"""One read model for creators, derived from the existing pipeline and journal.

Execution, deliverable readiness and user decisions are independent. No mutation,
approval, inferred answers or second task database belongs in this projection.
"""
from __future__ import annotations

import hashlib

from .progress import project_progress
from .vocabulary import stage_label

ACTIVE = {"queued", "running", "cancelling"}


def content_format(events: list[dict]) -> str | None:
    """The project's last explicit format, independent of one turn's intent.

    A status/pause turn may carry format=none without changing the work being
    made. This read model grants no execution scope: callers still need a new
    user action and the exact source/image approvals before preparing anything.
    """
    for event in reversed(events):
        if event.get('kind') == 'creative_context_selected':
            value = event.get('selection', {}).get('format')
            if value in {'video', 'photo'}:
                return value
    return None


def latest_execution(store, run_id: str, rows=None) -> dict | None:
    """Read the same execution journal without the billing/recovery projection."""
    if rows is None:
        rows = store.read_events(run_id)
    created = next((r for r in reversed(rows) if r.get("kind") == "job_created"), None)
    if not created:
        return None
    job = {**created, "status": "queued", "events": []}
    for row in rows:
        if row.get("job_id") != job["job_id"]:
            continue
        if row.get("kind") == "job_status":
            job.update(status=row["status"], reason=row.get("reason", ""))
        elif row.get("kind") == "job_deliverable":
            job["deliverable"] = row["deliverable"]
        elif row.get("kind") == "job_progress":
            job["events"].append({**row["event"], "at": row["at"]})
    repair = next((row for row in reversed(rows) if row.get('kind') == 'artifact_versions_withdrawn'), None)
    last_job_seq = max(row.get('seq', 0) for row in rows if row.get('job_id') == job['job_id'])
    if repair and repair.get('seq', 0) > last_job_seq:
        job['project_repair'] = repair['receipt']
    return job


def _matching_reply_check(delivery, receipt, interpretation):
    """Consume the saved typed check; do not recount or repair model text."""
    from .reply_conditions import expected_check_ids

    check = delivery.get('reply_check') if delivery else None
    fields = {'status', 'source_message_id', 'source_sha256', 'interpretation_id',
              'constraint_ids', 'char_count', 'table_data_rows', 'missing'}
    source_hash = hashlib.sha256(receipt['text'].encode('utf-8')).hexdigest()
    expected_ids = set(expected_check_ids(interpretation))
    ids = {row['id'] for row in interpretation.get('constraints', [])
           if row.get('id') in expected_ids and row.get('source_message_id') == receipt['message_id']
           and row.get('source_sha256') == source_hash
           and row.get('origin') == 'grounded_model_interpretation'
           and row.get('quote') and row['quote'] in receipt['text']
           and row.get('scope') == {'lifetime': 'turn', 'stages': [], 'paths': [],
                                    'quote': row.get('scope', {}).get('quote')}}
    def valid_ids(value, *, nonempty=False):
        return (isinstance(value, list) and (bool(value) or not nonempty)
                and all(isinstance(identity, str) and identity in ids for identity in value)
                and len(set(value)) == len(value))
    if (not isinstance(check, dict) or set(check) != fields
            or not isinstance(check['status'], str) or check['status'] not in {'satisfied', 'incomplete'}
            or check['source_message_id'] != receipt['message_id']
            or check['source_sha256'] != source_hash or check['interpretation_id'] != interpretation['id']
            or not valid_ids(check['constraint_ids'])
            or set(check['constraint_ids']) != expected_ids or ids != expected_ids
            or type(check['char_count']) is not int or check['char_count'] < 0
            or check['table_data_rows'] is not None and
               (type(check['table_data_rows']) is not int or check['table_data_rows'] < 0)
            or not isinstance(check['missing'], list)):
        return None
    for row in check['missing']:
        if (not isinstance(row, dict) or set(row) != {'category', 'constraint_ids', 'expected', 'actual'}
                or not isinstance(row['category'], str)
                or not valid_ids(row['constraint_ids'], nonempty=True)
                or not set(row['constraint_ids']) <= set(check['constraint_ids'])):
            return None
        if row['category'] in {'max_chars', 'table_rows'}:
            if (type(row['expected']) is not int or row['expected'] < 1
                    or not (type(row['actual']) is int and row['actual'] >= 0
                            or row['category'] == 'table_rows' and row['actual'] is None)):
                return None
        elif row['category'] == 'table_format':
            if (not isinstance(row['expected'], str) or not isinstance(row['actual'], str)
                    or row['expected'] not in {'markdown_table', 'text'} or row['actual'] not in {
                    'markdown_table', 'text', 'invalid_or_multiple'}):
                return None
        elif row['category'] == 'unresolved_condition':
            if row['expected'] != 'clarified' or row['actual'] != 'needs_clarification':
                return None
        else:
            return None
    if bool(check['missing']) != (check['status'] == 'incomplete'):
        return None
    return check


def _reply_check_detail(check):
    return ' '.join(
        f"回复要求全文不超过 {row['expected']} 字符，实际 {row['actual']} 字符。"
        if row['category'] == 'max_chars' else
        (f"回复要求表格有 {row['expected']} 行数据，实际 {row['actual']} 行。" if row['actual'] is not None else
         f"回复要求表格有 {row['expected']} 行数据，目前数据行数无法核对。")
        if row['category'] == 'table_rows' else
        ('回复要求 Markdown 表格，当前格式未满足。' if row['expected'] == 'markdown_table' else
         '回复要求纯文本，当前格式未满足。') if row['category'] == 'table_format' else
        '回复条件仍需澄清。' for row in check['missing'])


def _image_analysis(store, run_id, stages, chat):
    """Current pure analysis, from original identities and the shared coverage.

    A status receipt can retain the immediately preceding analysis. An unknown
    or creative request cannot search past another job for an old success.
    """
    from .conversation_input import interpretation_for, message
    from .agent_images import analysis_coverage

    rows = store.read_events(run_id)
    created = [row for row in rows if row.get('kind') == 'job_created']
    for index in range(len(created) - 1, -1, -1):
        original = created[index]
        source_id = original.get('user_input_id')
        if not source_id:
            return None
        receipt = message(store, run_id, source_id)
        if (receipt.get('request_id') != original.get('request_id') or not any(
                row.get('kind') == 'user_message_execution_started'
                and row.get('message_id') == source_id for row in rows)):
            return None
        interpretation = interpretation_for(store, run_id, source_id)
        task = (interpretation or {}).get('task') or {}
        if (task.get('mode') == 'status' and task.get('deliverable') == 'follow_up'
                and task.get('format') == 'none' and not task.get('allowed_stages')
                and not interpretation.get('constraints')
                and not any(interpretation.get(key) for key in ('image_use', 'video_use', 'reuse_artifact'))
                and not any(receipt.get(key) for key in
                            ('feedback_target', 'reference_target', 'reference_recovery', 'production_feedback'))):
            continue
        if (task.get('mode') != 'analyze' or task.get('deliverable') != 'analysis'
                or task.get('format') not in {'photo', 'none'} or task.get('allowed_stages')
                or interpretation.get('video_use') or receipt.get('reference_target')
                or receipt.get('reference_recovery')):
            return None
        # Match the unique turn executing this source inside this job's interval.
        end = created[index + 1]['seq'] if index + 1 < len(created) else float('inf')
        scopes = [row for row in rows if row.get('kind') == 'agent_turn_scope_started'
                  and row.get('user_input_id') == source_id and original['seq'] < row['seq'] < end]
        if len(scopes) != 1:
            return None
        turn_id = scopes[0].get('turn_id')
        if not turn_id:
            return None
        job_rows = [row for row in rows if row.get('job_id') == original['job_id']]
        status = next((row['status'] for row in reversed(job_rows)
                       if row.get('kind') == 'job_status'), 'queued')
        delivery = next((row.get('deliverable') for row in reversed(job_rows)
                         if row.get('kind') == 'job_deliverable'), None)
        coverage = analysis_coverage(store, run_id, source_id, turn_id)
        # format=none may be a video analysis. Until actual image use is bound,
        # only an explicit photo source identifies an image task.
        if coverage is None and task.get('format') != 'photo':
            return None
        replies = [row for row in rows if row.get('kind') == 'agent_reply_recorded'
                   and row.get('job_id') == original['job_id']
                   and row.get('source_message_id') == source_id]
        report = ''
        if len(replies) == 1:
            chat_index = replies[0].get('chat_index')
            if type(chat_index) is int and 0 <= chat_index < len(chat):
                reply = chat[chat_index]
                if reply.get('role') == 'assistant' and isinstance(reply.get('text'), str):
                    report = reply['text']
        total, observed = ((coverage['total'], coverage['observed']) if coverage else (None, None))
        has_reply_check = bool(delivery and 'reply_check' in delivery)
        reply_check = _matching_reply_check(delivery, receipt, interpretation) if has_reply_check else None
        observed_complete = bool(coverage and coverage['requested'] and total > 0
                                 and not coverage['missing'] and observed == total)
        complete = bool(status == 'succeeded' and delivery and delivery.get('goal') == 'analysis'
                        and delivery.get('status') == 'complete' and observed_complete and report.strip()
                        and (not has_reply_check or reply_check and reply_check['status'] == 'satisfied'))
        phase = ('complete' if complete else 'working' if status in ACTIVE else
                 'stopped' if status in {'cancelled', 'interrupted'} else 'incomplete')
        title = {'complete': '图片分析已完成', 'working': '正在分析图片',
                 'stopped': '图片分析已停止', 'incomplete': '图片分析未完成'}[phase]
        detail = (f'本轮固定 {total} 张图片，已核对 {observed} 张观察。' if coverage else
                  '本轮图片来源或完整观察尚未核对。')
        if reply_check and reply_check['status'] == 'incomplete':
            if observed_complete and report.strip() and status in {'succeeded', 'blocked'}:
                title = '图片观察已完成，回复格式未完成'
            detail += ' ' + _reply_check_detail(reply_check)
        elif has_reply_check and reply_check is None:
            detail += ' 回复检查尚未与本轮来源核对。'
        detail += (' 阅读原回复中的可见内容与限制；不自动进入创作或制作。' if complete else
                   ' 已保存的观察保留；未完成项不会自动重试。')
        return {'kind': 'image', 'analysis_only': not any(stage.get('latest') for stage in stages),
                'goal': '图片分析', 'status': phase, 'title': title, 'detail': detail,
                'job_id': original['job_id'], 'request_id': original['request_id'],
                'source_message_id': source_id, 'turn_id': turn_id,
                'interpretation_id': interpretation['id'], 'total': total, 'observed': observed,
                'missing': coverage['missing'] if coverage else [], 'report': report,
                **({'reply_check': reply_check} if reply_check else {})}
    return None


def durable_questions(store, run_id: str, stages=("intake", "direction", "material_plan", "writing"),
                      chat: list[dict] | None = None, *, responses=None) -> list[dict]:
    """The product questions still waiting on the user, one card per question.

    A ``product_choice`` — which colourway, which size — is a fact about the
    user's own SKU. Adopting a draft does not answer it, and its author has no
    reason to ask again next version: the next version usually marks it
    ``timing=later`` precisely *because* it is not blocking that draft. Reading
    only the latest artifact, and only rows asking for ``now``, made the card
    vanish the instant a version was adopted — observed 2026-09-29, before the
    user could answer.

    Two consequences shape this:

    * ``timing`` is ignored, and the question survives across versions.
    * Every stage and version words it slightly differently ("四色中的哪一个",
      "四种可见配色中的哪一个", "主推哪个实际在售配色"), so de-duplicating on the
      sentence would stack eight cards asking one thing. They are grouped by
      question *kind* — one product question at a time — and the **newest**
      wording wins, because the later drafts state it with the context that makes
      it answerable.
    """
    from .structured import DURABLE_QUESTION_KINDS, artifact_question_id, question_answered, question_kind
    if responses is None:
        from .interactions import artifact_responses
        responses = artifact_responses(store, run_id)

    newest: dict[str, dict] = {}
    for stage in stages:
        for artifact in store.active_artifacts(run_id, stage):
            for row in (artifact.structured or {}).get("open_questions") or []:
                if not isinstance(row, dict) or not row.get("question"):
                    continue
                if row.get("owner") != "user" or not str(row.get("reason") or "").strip():
                    continue
                kind = question_kind(row)
                if kind not in DURABLE_QUESTION_KINDS:
                    continue
                prior = newest.get(kind)
                # Version breaks a timestamp tie: two drafts can share a second,
                # and picking the older wording then would present the question
                # without the context that makes it answerable.
                if prior is None or (artifact.created_at, artifact.version) > \
                        (prior["created_at"], prior["version"]):
                    newest[kind] = {**row, "kind": kind, "stage": stage,
                                    "version": artifact.version,
                                    "artifact_sha256": artifact.sha256,
                                    "question_id": artifact_question_id(stage, artifact.version, artifact.sha256, row['question']),
                                    "created_at": artifact.created_at, "durable": True}
    return [row for row in newest.values()
            if not question_answered(chat, row["question"], row["created_at"], responses=responses,
                                     stage=row['stage'], version=row['version'], artifact_sha256=row['artifact_sha256'])]


def stage_issues(stage: dict, chat: list[dict] | None = None, *, responses=None) -> list[dict]:
    from .structured import artifact_question_id, is_ask_user_question, question_answers, question_kind, question_response

    artifact = stage.get("latest") or {}
    active = (not stage.get("stale") and stage.get("integrity_ok", True)
              and artifact.get("digest_algorithm") == "artifact-v2"
              and stage.get("status") != "approved")
    issues = []
    for row in (artifact.get("structured") or {}).get("open_questions", []):
        if not isinstance(row, dict) or not row.get("question"):
            continue
        explicit = row.get("owner") in {"user", "team"} and row.get("timing") in {"now", "later"}
        kind = question_kind(row)
        answers = question_answers(chat, row["question"], artifact.get("created_at", ""), responses=responses,
            stage=stage['stage'], version=artifact.get('version'), artifact_sha256=artifact.get('sha256'))
        answered = bool(answers)
        response = question_response(responses, row['question'], stage=stage['stage'],
                                     version=artifact.get('version'), artifact_sha256=artifact.get('sha256'))
        answer_detail = ({'answer_quote': response['quote'], 'answer_status': response['answer_status'],
                          'answer_source_message_id': response['source_message_id']} if response else
                         {'answer_quote': answers[-1]['answer'], 'answer_status': 'answered',
                          'answer_source_message_id': answers[-1].get('source_message_id')} if answers else {})
        issues.append({**row, "kind": kind, "stage": stage["stage"], "version": artifact.get("version"),
                       "artifact_sha256": artifact.get("sha256"),
                       "question_id": artifact_question_id(stage['stage'], artifact.get('version'), artifact.get('sha256'), row['question']),
                       **answer_detail,
                       "owner": row.get("owner") if explicit else "team",
                       "timing": row.get("timing") if explicit else "later",
                       "needs_triage": not explicit, "active": active, "answered": answered,
                       # brief_amendment is never an ask_user blank — it means
                       # the product goal has to change via amend_brief.
                       "ask_user": bool(active and not answered and is_ask_user_question(row))})
    return issues


def durable_stage_issues(store, run_id: str, stage: dict, chat: list[dict] | None = None) -> list[dict]:
    """This stage's share of the durable questions, in ``stage_issues`` shape.

    Kept for callers that want the rows directly. ``project_brief`` marks the row
    it already has instead of appending these, so a durable question never
    becomes two cards for one question.
    """
    stage_name = stage["stage"]
    return [{**row, "stage": stage_name, "owner": "user", "timing": "now",
             "needs_triage": False, "active": True, "answered": False, "ask_user": True}
            for row in durable_questions(store, run_id, chat=chat)
            if row["stage"] == stage_name]


def project_brief(state: dict, job: dict | None = None, chat: list[dict] | None = None,
                  *, direction_action: dict | None = None,
                  store=None, run_id: str | None = None) -> dict:
    defaults = {"latest": None, "stale": False, "integrity_ok": True, "status": "empty",
                "open_attempt": None, "can_approve": False, "can_review": False, "last_rejection": ""}
    state = {**state, "stages": [{**defaults, **s} for s in state["stages"]]}
    progress = project_progress(state)
    target = next((s for s in state["stages"] if s["stage"] == progress["stage"]), None)
    artifact = (target or {}).get("latest") or {}
    from .interactions import artifact_responses
    responses = artifact_responses(store, run_id) if store is not None and run_id else []
    issues = [issue for stage in state["stages"] for issue in stage_issues(stage, chat, responses=responses)]
    # A question about the user's own product is not answered by a draft being
    # adopted, so it has to keep being asked even when the version that raised it
    # is decided or superseded. Marking the row we already have — rather than
    # adding a second row for the same question — keeps one card per question and
    # keeps the version that asked it in the row.
    durable_texts = ({row["question"] for row in durable_questions(store, run_id, chat=chat, responses=responses)}
                     if store is not None and run_id else set())
    for issue in issues:
        if issue.get("question") in durable_texts:
            issue["durable"] = True
    # Only the currently actionable stage can assign user work. Old upstream
    # unknowns remain available for the team without resurfacing as questionnaires.
    question_stage = progress["stage"]
    if target and not artifact:
        previous = [s for s in state["stages"][:state["stages"].index(target)] if s.get("latest")]
        if previous:
            question_stage = previous[-1]["stage"]
    for issue in issues:
        # A durable question is about the user's product, not about the draft
        # that happened to raise it, so the stage currently being worked on does
        # not decide whether it is still being asked.
        issue["ask_user"] = (issue["ask_user"] and issue["stage"] == question_stage) \
            or bool(issue.get("durable"))
    questions = [i for i in issues if i["ask_user"]]
    action = {"kind": progress["action"], "label": progress["label"], "stage": progress["stage"],
              "prompt": progress["prompt"], "version": artifact.get("version"), "sha256": artifact.get("sha256")}
    title, detail = progress["title"], progress["detail"]
    phase = "ready" if not target else target["status"]
    # A complete direction is a real choice. A related question stays visible
    # beside it, but must not replace the option decision in the project status.
    if questions and target and not target.get("open_attempt") and not target.get("can_approve"):
        title = f"有 {len(questions)} 项需要你确认"
        detail = "可以说明现有条件、不确定处或可接受的替代拍法；团队会据此调整方案。"
        action.update(kind="questions", label="查看待确认事项")
        action["stage"] = question_stage
        phase = "needs_input"
    if job and job.get("status") in {"failed", "interrupted", "cancelled", "blocked"} and not questions and not (target and target.get("can_approve")):
        historical_accounting = "reconcile provider evidence" in job.get("reason", "")
        if not historical_accounting and job.get("job_kind", "conversation") == "conversation":
            title = "本轮已停止" if job["status"] == "cancelled" else "本轮未完成，已有内容已保留"
            phase = "interrupted"
            detail = progress["detail"]
    active = bool(job and job.get("status") in ACTIVE)
    project_restored = bool(job and job.get('project_repair') and not questions and not active
                            and all(s.get('status') in ('done', 'approved') and not s.get('stale')
                                    for s in state['stages']))
    if project_restored:
        title, detail = ('原采用方案已恢复',
                         '已保留分镜制作输入与修改意见，可继续核对分镜；生成仍需在制作界面确认。')
        phase = 'ready'
    if active:
        title = "制片团队正在工作" if job["status"] != "cancelling" else "正在停止本轮"
        detail = "新成果会自动显示；当前已保存的方案仍可阅读。"
        action.update(kind="activity", label="查看团队进展")
        phase = "working"
    elif (direction_action and not questions and action['kind'] == 'draft'
          and action['stage'] == 'direction'):
        action = dict(direction_action)
        title = '创意方向等待生成' if not artifact else '创意方向需要重做'
        detail = '按当前有效资料直接派导演；完成后停下来供你点选。'
    result = {"schema_version": 1, "phase": phase, "title": title, "detail": detail,
            "stage": progress["stage"], "completed": progress["completed"], "total": progress["total"],
            "action": action, "questions": questions, "issues": issues,
            "deliverables": [{"stage": s["stage"], "label": stage_label(s["stage"]),
                              "version": s["latest"].get("version"), "sha256": s["latest"].get("sha256"),
                              "status": s["status"], "stale": s.get("stale", False)}
                             for s in state["stages"] if s.get("latest")],
            "execution": {"status": (job or {}).get("status", "idle"), "active": active,
                          "project_restored": project_restored,
                          "job_id": (job or {}).get("job_id")}}
    analysis = (_image_analysis(store, run_id, state['stages'],
                               chat if chat is not None else store.read_chat(run_id))
                if store is not None and run_id else None)
    if analysis:
        result['analysis'] = analysis
        if analysis['analysis_only']:
            result.update(phase='analysis_' + analysis['status'], title=analysis['title'],
                          detail=analysis['detail'], stage=None,
                          completed=analysis['observed'], total=analysis['total'],
                          action={'kind': 'analysis', 'label': '阅读分析结果', 'stage': None})
    return result
