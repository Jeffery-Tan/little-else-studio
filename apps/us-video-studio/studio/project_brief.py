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


def project_brief(state: dict, job: dict | None = None, chat: list[dict] | None = None) -> dict:
    defaults = {"latest": None, "stale": False, "integrity_ok": True, "status": "empty",
                "open_attempt": None, "can_approve": False, "can_review": False, "last_rejection": ""}
    state = {**state, "stages": [{**defaults, **s} for s in state["stages"]]}
    progress = project_progress(state)
    target = next((s for s in state["stages"] if s["stage"] == progress["stage"]), None)
    artifact = (target or {}).get("latest") or {}
    issues = [issue for stage in state["stages"] for issue in stage_issues(stage, chat)]
    # Only the currently actionable stage can assign user work. Old upstream
    # unknowns remain available for the team without resurfacing as questionnaires.
    question_stage = progress["stage"]
    if target and not artifact:
        previous = [s for s in state["stages"][:state["stages"].index(target)] if s.get("latest")]
        if previous:
            question_stage = previous[-1]["stage"]
    for issue in issues:
        issue["ask_user"] = issue["ask_user"] and issue["stage"] == question_stage
    questions = [i for i in issues if i["ask_user"]]
    action = {"kind": progress["action"], "label": progress["label"], "stage": progress["stage"],
              "prompt": progress["prompt"], "version": artifact.get("version"), "sha256": artifact.get("sha256")}
    title, detail = progress["title"], progress["detail"]
    phase = "ready" if not target else target["status"]
    review_failure = False
    if target and target.get("can_review") and not target.get("open_attempt"):
        # Failure is scoped to this version, not all historical unknown usage.
        for event in (job or {}).get("events", []):
            args = event.get("args") or {}
            review_step = event.get("tool") == "request_review" and args.get("stage") == target["stage"]
            if review_step and event.get("type") == "agent_tool" and not event.get("ok", True):
                # Newer versions created after this event don't inherit its failure.
                review_failure = event.get("at", "") >= artifact.get("created_at", "")
        payload = (job or {}).get("job_payload") or {}
        review_failure |= bool((job or {}).get("job_kind") == "stage_review"
                               and (job or {}).get("status") in {"failed", "blocked", "interrupted", "cancelled"}
                               and payload.get("expected_artifact_sha256") == artifact.get("sha256"))
        action.update(kind="review_stage", label="重新审核当前方案" if review_failure else "审核当前方案")
        title = f"{stage_label(target['stage'])}" + (" · 审核未完成" if review_failure else "已保存")
        detail = "审核未完成，方案已保留。可先查看方案，或只重试这一版的审核。" if review_failure else "下一步由独立审核检查；审核后再由你选择采用。"
        phase = "review_failed" if review_failure else "awaiting_review"
    # Resolve user-owned facts before another review/model call. The draft stays
    # saved; the answer may lead the team to revise its current way of shooting.
    if questions and target and not target.get("open_attempt"):
        title = f"有 {len(questions)} 项需要你确认"
        detail = "可以说明现有条件、不确定处或可接受的替代拍法；团队会据此调整方案。"
        action.update(kind="questions", label="查看待确认事项")
        action["stage"] = question_stage
        phase = "needs_input"
    if job and job.get("status") in {"failed", "interrupted", "cancelled", "blocked"} and not review_failure and not questions:
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
    return {"schema_version": 1, "phase": phase, "title": title, "detail": detail,
            "stage": progress["stage"], "completed": progress["completed"], "total": progress["total"],
            "action": action, "questions": questions, "issues": issues,
            "deliverables": [{"stage": s["stage"], "label": stage_label(s["stage"]),
                              "version": s["latest"].get("version"), "sha256": s["latest"].get("sha256"),
                              "status": s["status"], "stale": s.get("stale", False)}
                             for s in state["stages"] if s.get("latest")],
            "execution": {"status": (job or {}).get("status", "idle"), "active": active,
                          "job_id": (job or {}).get("job_id")}}
