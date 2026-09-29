"""One read model for creators, derived from the existing pipeline and journal.

Execution, deliverable readiness and user decisions are independent. No mutation,
approval, inferred answers or second task database belongs in this projection.
"""
from __future__ import annotations

from .progress import project_progress
from .vocabulary import stage_label

ACTIVE = {"queued", "running", "cancelling"}


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
        elif row.get("kind") == "job_progress":
            job["events"].append({**row["event"], "at": row["at"]})
    return job


def durable_questions(store, run_id: str, stages=("intake", "direction", "material_plan", "writing"),
                      chat: list[dict] | None = None) -> list[dict]:
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
    from .structured import DURABLE_QUESTION_KINDS, question_answered, question_kind

    newest: dict[str, dict] = {}
    for stage in stages:
        for artifact in store.list_artifacts(run_id, stage):
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
                                    "created_at": artifact.created_at, "durable": True}
    return [row for row in newest.values()
            if not question_answered(chat, row["question"], row["created_at"])]


def stage_issues(stage: dict, chat: list[dict] | None = None) -> list[dict]:
    from .structured import is_ask_user_question, question_answered, question_kind

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
        answered = question_answered(chat, row["question"], artifact.get("created_at", ""))
        issues.append({**row, "kind": kind, "stage": stage["stage"], "version": artifact.get("version"),
                       "artifact_sha256": artifact.get("sha256"),
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
    issues = [issue for stage in state["stages"] for issue in stage_issues(stage, chat)]
    # A question about the user's own product is not answered by a draft being
    # adopted, so it has to keep being asked even when the version that raised it
    # is decided or superseded. Marking the row we already have — rather than
    # adding a second row for the same question — keeps one card per question and
    # keeps the version that asked it in the row.
    durable_texts = ({row["question"] for row in durable_questions(store, run_id, chat=chat)}
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
    return {"schema_version": 1, "phase": phase, "title": title, "detail": detail,
            "stage": progress["stage"], "completed": progress["completed"], "total": progress["total"],
            "action": action, "questions": questions, "issues": issues,
            "deliverables": [{"stage": s["stage"], "label": stage_label(s["stage"]),
                              "version": s["latest"].get("version"), "sha256": s["latest"].get("sha256"),
                              "status": s["status"], "stale": s.get("stale", False)}
                             for s in state["stages"] if s.get("latest")],
            "execution": {"status": (job or {}).get("status", "idle"), "active": active,
                          "job_id": (job or {}).get("job_id")}}
