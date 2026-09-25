"""ActiveBrief P0 — compile only; never mutates, never raises on thin data.

The real failure this exists to prevent is intent dissolving into prose. Tests
therefore pin: deterministic fingerprints, negation → must_not, ask_user
replies → decision clauses (and out of pending), and a fixture shaped like
the chat-4953 project (种草 goal + 补不到素材 + 手部出镜 answer).
"""
from __future__ import annotations

import json

import pytest

from studio import active_brief as ab
from studio.store import Store

RUN = "brief-compile-01"


def _store(tmp_path) -> Store:
    return Store(tmp_path)


def _seed_intake(store: Store, *, facts=None, constraints=None, questions=None,
                 body="intake body"):
    structured = {
        "facts": facts or [],
        "constraints": constraints or [],
        "open_questions": questions or [],
    }
    store.write_artifact(
        RUN, "intake", 1, body,
        summary="seeded",
        author_role="orchestrator",
        attempt="intake#1",
        structured=structured,
    )


@pytest.fixture()
def store(tmp_path):
    s = _store(tmp_path)
    s.create_run(RUN, "US 种草短视频", "我准备做一个时尚达人种草向的带货视频。素材见附件。")
    return s


# ------------------------------------------------------------- empty / thin --


def test_run_without_intake_still_compiles_from_brief(store):
    brief = ab.compile_active_brief(store, RUN)
    assert brief["status"] == "active"
    assert brief["goal"]["id"] == "goal.primary"
    assert "种草" in brief["goal"]["text"]
    assert brief["goal"]["source"] == "run.brief"
    assert brief["pending_questions"] == []
    assert brief["open_amendments"] == []


def test_compile_is_read_only(store):
    before = sorted(p.name for p in store.run_dir(RUN).iterdir())
    ab.compile_active_brief(store, RUN)
    after = sorted(p.name for p in store.run_dir(RUN).iterdir())
    assert before == after
    assert not (store.run_dir(RUN) / "briefs").exists()


# -------------------------------------------------------------------- goal --


def test_run_brief_is_the_goal_and_intake_cannot_override_it(store):
    """F3: the user's own request owns the goal; intake is the model's reading.

    ``run.brief`` is written once at project creation. An intake rewrite may add
    facts and constraints, but it must not redefine what is being made — that
    was the path by which a model rewrite narrowed the objective and the system
    went on self-consistently against a goal the user never agreed to.
    """
    _seed_intake(store, facts=[{
        "label": "交付形态",
        "value": "US 线时尚达人种草向带货视频，主语言美式英语",
        "source": "Jeffery 本轮原话",
    }])
    brief = ab.compile_active_brief(store, RUN)
    assert brief["goal"]["source"] == "run.brief"
    assert brief["goal"]["authority"] == "user"
    assert "时尚达人种草" in brief["goal"]["text"]
    # The intake's own reading is not thrown away — it still lands as a clause.
    assert any("美式英语" in c["text"] for c in brief["clauses"])


# ---------------------------------------------------------------- clauses --


def test_constraints_classify_must_vs_must_not(store):
    _seed_intake(store, constraints=[
        "台词用自然的美式英语",
        "不得使用虚构购买经历",
        "防水不进画面与台词",
    ])
    brief = ab.compile_active_brief(store, RUN)
    by_kind = {c["kind"]: c for c in brief["clauses"]}
    # three constraints + goal
    kinds = [c["kind"] for c in brief["clauses"]]
    assert kinds.count("must") == 1
    assert kinds.count("must_not") == 2
    assert by_kind["must"]["check"] in {"presence", "style"}
    assert all(c["check"] == "absence" for c in brief["clauses"] if c["kind"] == "must_not")
    style = next(c for c in brief["clauses"] if "美式英语" in c["text"])
    assert style["check"] == "style"


def test_asset_bound_becomes_asset_clause(store):
    _seed_intake(store, facts=[{
        "label": "素材边界",
        "value": "肩带与防水补不到可拍实物素材，不进画面与台词",
        "source": "Jeffery 本轮原话",
    }])
    brief = ab.compile_active_brief(store, RUN)
    assets = [c for c in brief["clauses"] if c["kind"] == "asset"]
    assert len(assets) == 1
    assert "补不到" in assets[0]["text"]
    assert assets[0]["scope"] == "global"


# ------------------------------------------------------ answers / pending --


def test_ask_user_reply_becomes_decision_and_leaves_pending(store):
    question = "方向 A 的说话者，你要哪一种？手部出镜还是露脸对镜口播？"
    _seed_intake(store, questions=[{
        "question": question,
        "blocks": "A 的镜头结构",
        "owner": "user",
        "timing": "now",
        "reason": "商业选择",
    }])
    store.append_chat(RUN, "user", f"关于「{question}」：手部出镜＋画外第一人称（推荐）")
    store.append_chat(RUN, "assistant", "已记录。")

    brief = ab.compile_active_brief(store, RUN)
    assert brief["pending_questions"] == []
    decisions = [c for c in brief["clauses"] if c["kind"] == "decision"]
    assert len(decisions) == 1
    assert "手部出镜" in decisions[0]["answer"]
    assert decisions[0]["check"] == "choice"
    assert decisions[0]["status"] == "confirmed"


def test_unanswered_user_question_stays_pending(store):
    _seed_intake(store, questions=[{
        "question": "三条配色里，哪一条是本次主推（橙 / 灰白 / 咖啡）？",
        "blocks": "首帧选择",
        "owner": "user",
        "timing": "now",
        "reason": "商业选择",
    }])
    brief = ab.compile_active_brief(store, RUN)
    assert len(brief["pending_questions"]) == 1
    assert brief["pending_questions"][0]["owner"] == "user"
    assert not [c for c in brief["clauses"] if c["kind"] == "decision"]


def test_team_questions_are_not_pending_user_work(store):
    _seed_intake(store, questions=[{
        "question": "俯拍首帧能否接上手部动作",
        "blocks": "开场",
        "owner": "team",
        "timing": "now",
        "reason": "团队核验",
    }])
    brief = ab.compile_active_brief(store, RUN)
    assert brief["pending_questions"] == []


# ------------------------------------------------------------ fingerprint --


def test_fingerprint_is_stable_and_content_addressed(store):
    _seed_intake(store, constraints=["台词用自然的美式英语"])
    a = ab.compile_active_brief(store, RUN)
    b = ab.compile_active_brief(store, RUN)
    assert ab.fingerprint(a) == ab.fingerprint(b)
    assert ab.content_sha(a) == ab.content_sha(b)
    assert ab.fingerprint(a).startswith("brief-v1-")


def test_fingerprint_excludes_compiled_at(store):
    brief = ab.compile_active_brief(store, RUN)
    sha_before = ab.content_sha(brief)
    brief["source"]["compiled_at"] = "2099-01-01T00:00:00Z"
    assert ab.content_sha(brief) == sha_before


def test_fingerprint_changes_when_constraints_change(store):
    _seed_intake(store, constraints=["台词用自然的美式英语"])
    first = ab.fingerprint(ab.compile_active_brief(store, RUN))
    store.write_artifact(
        RUN, "intake", 2, "intake body v2",
        summary="rewritten",
        author_role="orchestrator",
        attempt="intake#2",
        structured={"facts": [], "constraints": ["台词用自然的美式英语且不出现中文"],
                    "open_questions": []},
    )
    second = ab.fingerprint(ab.compile_active_brief(store, RUN))
    assert first != second


def test_clause_index_keys_by_id(store):
    _seed_intake(store, constraints=["不得虚构购买经历"])
    brief = ab.compile_active_brief(store, RUN)
    index = ab.clause_index(brief)
    assert set(index) == {c["id"] for c in brief["clauses"]}
    assert all(c["id"] for c in brief["clauses"])


def test_clause_ids_survive_recompile(store):
    _seed_intake(store, constraints=["不得虚构购买经历"])
    ids1 = [c["id"] for c in ab.compile_active_brief(store, RUN)["clauses"]]
    ids2 = [c["id"] for c in ab.compile_active_brief(store, RUN)["clauses"]]
    assert ids1 == ids2


# ------------------------------------- chat-4953-shaped integration fixture --


def test_project_like_v4_fixture_compiles_the_expected_brief(store):
    """Shapes from the real direction-v4 stuck point, not its prose dump."""
    _seed_intake(store, facts=[
        {
            "label": "交付形态",
            "value": "美国线（US）时尚达人种草向带货视频，主语言为美式英语",
            "source": "Jeffery 本轮原话",
        },
        {
            "label": "Jeffery 报的卖点（尚无画面证据）",
            "value": "小包肩带／挂点与防水表现都补不到可拍实物素材，不进画面、不进台词。",
            "source": "Jeffery 本轮原话",
        },
    ], constraints=[
        "美国消费者看到或听到的台词、字幕与 caption 用自然的美式英语，不做中译英腔。",
        "不得用虚构的购买经历、长期使用、朋友追问或销售成绩建立推荐资格。",
        "防水性能和肩带/斜挎在现有 7 张静图里都没有可见证据，不得当作画面既成事实呈现。",
    ], questions=[
        {
            "question": "方向 A 的说话者，你要哪一种？",
            "blocks": "A 的镜头结构",
            "owner": "user",
            "timing": "now",
            "reason": "商业选择",
        },
        {
            "question": "三条配色里，哪一条是本次主推（橙 / 灰白 / 咖啡）？",
            "blocks": "首帧",
            "owner": "user",
            "timing": "now",
            "reason": "商业选择",
        },
    ])
    store.append_chat(
        RUN, "user",
        "关于「方向 A 的说话者，你要哪一种？」：手部出镜＋画外第一人称（推荐）",
    )

    brief = ab.compile_active_brief(store, RUN)

    # Goal stays the product ask — never the executable downgrade.
    assert "达人种草" in brief["goal"]["text"]
    assert brief["goal"]["kind"] == "product_goal"

    kinds = {c["kind"] for c in brief["clauses"]}
    assert {"must", "must_not", "asset", "decision"} <= kinds

    # 补不到素材 is an asset boundary, not something the director may re-litigate.
    asset = next(c for c in brief["clauses"] if c["kind"] == "asset" and "补不到" in c["text"])
    assert asset["status"] == "confirmed"

    # Answered speaker question is a decision; color choice is still pending.
    dec = next(c for c in brief["clauses"] if c["kind"] == "decision")
    assert "手部出镜" in dec["answer"]
    assert any("配色" in q["question"] for q in brief["pending_questions"])
    assert not any("说话者" in q["question"] for q in brief["pending_questions"])

    # Later P1 gate will fingerprint this token into stage inputs.
    assert ab.fingerprint(brief).startswith("brief-v1-")


def test_json_round_trip_keeps_fingerprint(store):
    brief = ab.compile_active_brief(store, RUN)
    clone = json.loads(json.dumps(brief, ensure_ascii=False))
    # compiled_at lives only under source; excluded from content hash either way.
    assert ab.fingerprint(clone) == ab.fingerprint(brief)


# ------------------------------------------------------------------ P1 gate --


def test_brief_gate_absent_without_options(store):
    gate = ab.brief_gate(store, RUN, {})
    assert gate["status"] == "absent"
    assert gate["blockers"] == []


def test_brief_gate_unmapped_like_legacy_v4(store):
    """v4-shaped options with no satisfies column: visible, not blocked."""
    gate = ab.brief_gate(store, RUN, {
        "options": [
            {"key": "A", "title": "先看里面", "claim": "包中包"},
            {"key": "B", "title": "对比", "claim": "肩带"},
        ],
        "recommended": "A —— 先看里面",
    })
    assert gate["status"] == "unmapped"
    assert gate["recommended"] == "A"
    assert gate["blockers"] == []


def test_brief_gate_ok_when_recommended_claims_goal(store):
    _seed_intake(store, facts=[{
        "label": "交付形态",
        "value": "US 时尚达人种草向带货视频",
        "source": "Jeffery",
    }])
    goal_id = ab.compile_active_brief(store, RUN)["goal"]["id"]
    gate = ab.brief_gate(store, RUN, {
        "options": [
            {"key": "A", "title": "先看里面", "satisfies": goal_id},
            {"key": "B", "title": "对比", "satisfies": ""},
        ],
        "recommended": "A",
    })
    assert gate["status"] == "ok"
    assert gate["blockers"] == []


def test_brief_gate_blocked_when_recommended_needs_amendment(store):
    _seed_intake(store, facts=[{
        "label": "交付形态",
        "value": "US 时尚达人种草向带货视频",
        "source": "Jeffery",
    }])
    goal_id = ab.compile_active_brief(store, RUN)["goal"]["id"]
    gate = ab.brief_gate(store, RUN, {
        "options": [{
            "key": "A",
            "title": "设计细节演示",
            "satisfies": "",
            "violates": goal_id,
            "amend_clause": goal_id,
            "amend_proposal": "目标收窄为设计细节种草",
        }],
        "recommended": "A",
    })
    assert gate["status"] == "blocked"
    kinds = {b["kind"] for b in gate["blockers"]}
    assert "violates" in kinds and "needs_amendment" in kinds


def test_brief_gate_blockers_on_non_recommended_do_not_block_status(store):
    _seed_intake(store, facts=[{
        "label": "交付形态",
        "value": "US 时尚达人种草向带货视频",
        "source": "Jeffery",
    }])
    goal_id = ab.compile_active_brief(store, RUN)["goal"]["id"]
    gate = ab.brief_gate(store, RUN, {
        "options": [
            {"key": "A", "satisfies": goal_id},
            {"key": "B", "violates": goal_id, "amend_clause": goal_id,
             "amend_proposal": "改目标"},
        ],
        "recommended": "A",
    })
    assert gate["status"] == "ok"
    assert gate["blockers"], "non-recommended violations stay visible"


# ------------------------------------------------- pipeline binding (P1) ---


def test_direction_attempt_stamps_brief_snapshot(tmp_path):
    from studio.pipeline import Pipeline

    s = Store(tmp_path)
    s.create_run(RUN, "t", "时尚达人种草向视频")
    pipe = Pipeline(s)
    a = pipe.start_attempt(RUN, "intake")
    pipe.submit(RUN, "intake", a.token, content="intake body",
                summary="s", structured={
                    "facts": [{"label": "交付形态", "value": "US 种草视频", "source": "u"}],
                    "constraints": ["台词用美式英语"],
                    "open_questions": [],
                })
    attempt = pipe.start_attempt(RUN, "direction")
    assert "brief:snapshot" in attempt.inputs
    assert attempt.inputs["brief:snapshot"].startswith("brief-v1-")
    # intake itself is the brief's source and must not self-bind
    intake_attempts = [e for e in s.read_events(RUN)
                       if e.get("kind") == "attempt_started" and e.get("stage") == "intake"]
    assert all("brief:snapshot" not in (e.get("inputs") or {})
               for e in intake_attempts)


def test_legacy_direction_without_brief_key_is_not_mass_staled(tmp_path):
    from studio.pipeline import Pipeline

    s = Store(tmp_path)
    s.create_run(RUN, "t", "brief")
    pipe = Pipeline(s)
    a = pipe.start_attempt(RUN, "intake")
    # Intake without structured facts still submits
    pipe.submit(RUN, "intake", a.token, content="intake body", summary="s")
    # Simulate a pre-P1 direction artifact: no brief:snapshot in inputs
    from studio.evidence import evidence_fingerprint, evidence_generation
    s.write_artifact(
        RUN, "direction", 1, "direction body",
        summary="legacy", author_role="director", attempt="direction#1",
        inputs={
            "intake": pipe.stage_state(RUN, "intake").latest.sha256,
            "evidence:snapshot": evidence_fingerprint(s, RUN),
            "evidence:generation": str(evidence_generation(s, RUN)),
        },
        structured={"options": [{"key": "A", "claim": "x"}], "recommended": "A"},
    )
    state = pipe.stage_state(RUN, "direction")
    assert not state.stale, state.stale_reason
    assert state.brief_gate is not None
    assert state.brief_gate["status"] == "unmapped"


def test_new_direction_goes_stale_when_brief_content_changes(tmp_path):
    from studio.pipeline import Pipeline

    s = Store(tmp_path)
    s.create_run(RUN, "t", "时尚达人种草向视频")
    pipe = Pipeline(s)
    a = pipe.start_attempt(RUN, "intake")
    pipe.submit(RUN, "intake", a.token, content="intake body", summary="s",
                structured={
                    "facts": [{"label": "交付形态", "value": "US 种草视频", "source": "u"}],
                    "constraints": ["台词用美式英语"],
                    "open_questions": [],
                })
    d = pipe.start_attempt(RUN, "direction")
    pipe.submit(RUN, "direction", d.token, content="direction body", summary="d",
                structured={
                    "options": [{"key": "A", "claim": "x", "satisfies": "goal.primary"}],
                    "recommended": "A",
                })
    before = pipe.stage_state(RUN, "direction")
    assert "brief:snapshot" in before.latest.inputs
    assert not before.stale

    # Intake revision changes compiled brief → direction stale (keyed artifacts only)
    a2 = pipe.start_attempt(RUN, "intake")
    pipe.submit(RUN, "intake", a2.token, content="intake body v2", summary="s2",
                structured={
                    "facts": [{"label": "交付形态", "value": "US 种草视频（改）", "source": "u"}],
                    "constraints": ["台词用美式英语", "不得虚构购买经历"],
                    "open_questions": [],
                })
    after = pipe.stage_state(RUN, "direction")
    assert after.stale
    assert "简报" in after.stale_reason


def test_direction_state_exposes_brief_gate_projection(tmp_path):
    from studio.pipeline import Pipeline

    s = Store(tmp_path)
    s.create_run(RUN, "t", "时尚达人种草向带货视频")
    pipe = Pipeline(s)
    a = pipe.start_attempt(RUN, "intake")
    pipe.submit(RUN, "intake", a.token, content="i", summary="s",
                structured={
                    "facts": [{"label": "交付形态", "value": "US 时尚达人种草", "source": "u"}],
                    "constraints": [], "open_questions": [],
                })
    d = pipe.start_attempt(RUN, "direction")
    pipe.submit(RUN, "direction", d.token, content="d", summary="d",
                structured={
                    "options": [{
                        "key": "A", "claim": "x",
                        "violates": "goal.primary",
                        "amend_clause": "goal.primary",
                        "amend_proposal": "收窄目标",
                    }],
                    "recommended": "A",
                })
    state = pipe.stage_state(RUN, "direction")
    assert state.brief_gate["status"] == "blocked"
    assert state.to_dict()["brief_gate"]["status"] == "blocked"


# ----------------------------------------------------------------- P2 gate --


def _blocked_direction_env(tmp_path):
    from studio.pipeline import Pipeline

    s = Store(tmp_path)
    s.create_run(RUN, "t", "时尚达人种草向带货视频")
    pipe = Pipeline(s)
    a = pipe.start_attempt(RUN, "intake")
    pipe.submit(RUN, "intake", a.token, content="i", summary="s",
                structured={
                    "facts": [{"label": "交付形态", "value": "US 时尚达人种草", "source": "u"}],
                    "constraints": [], "open_questions": [],
                })
    d = pipe.start_attempt(RUN, "direction")
    pipe.submit(RUN, "direction", d.token, content="d", summary="d",
                structured={
                    "options": [{
                        "key": "A", "claim": "x",
                        "violates": "goal.primary",
                        "amend_clause": "goal.primary",
                        "amend_proposal": "目标收窄为设计细节种草，不要求出镜穿搭",
                    }],
                    "recommended": "A",
                })
    return s, pipe


def test_record_approval_refuses_blocked_option_even_with_override(tmp_path):
    from studio.models import PipelineError
    from studio.pipeline import Pipeline

    s, pipe = _blocked_direction_env(tmp_path)
    pipe.record_review(RUN, "direction", verdict="return",
                       findings=[{"kind": "content", "stage": "direction", "issue": "不够种草"}])
    nonce = pipe.issue_nonce(RUN, "direction")
    with pytest.raises(PipelineError) as err:
        pipe.record_approval(
            RUN, "direction", nonce=nonce, decision="approved",
            override_reason="我就是要硬过", selected_option_id="A",
        )
    assert err.value.rule == "brief"
    assert "简报" in str(err.value)


def test_record_approval_still_allows_return_when_blocked(tmp_path):
    from studio.pipeline import Pipeline

    s, pipe = _blocked_direction_env(tmp_path)
    pipe.record_review(RUN, "direction", verdict="return",
                       findings=[{"kind": "content", "stage": "direction", "issue": "按原简报重做"}])
    nonce = pipe.issue_nonce(RUN, "direction")
    approval = pipe.record_approval(
        RUN, "direction", nonce=nonce, decision="returned",
        note="按原简报重做方向，不要降级目标",
    )
    assert approval.decision == "returned"


def test_propose_amend_approve_bumps_version_and_stales_direction(tmp_path):
    from studio.active_brief import compile_active_brief, fingerprint, propose_amendment
    from studio.pipeline import Pipeline

    s, pipe = _blocked_direction_env(tmp_path)
    before = compile_active_brief(s, RUN)
    assert before["version"] == 1
    assert not pipe.stage_state(RUN, "direction").stale

    fp0 = fingerprint(before)
    amd = propose_amendment(
        s, RUN,
        changes=[{
            "clause_id": "goal.primary",
            "op": "replace",
            "text": "US 线设计细节种草短片（不要求真人出镜穿搭）",
        }],
        note="门禁批准",
        approve=True,
    )
    assert amd["status"] == "approved"

    after = compile_active_brief(s, RUN)
    assert after["version"] == 2
    assert "设计细节种草" in after["goal"]["text"]
    assert fingerprint(after) != fp0

    state = pipe.stage_state(RUN, "direction")
    assert state.stale
    assert "简报" in state.stale_reason


def test_rejected_amendment_does_not_change_fingerprint(tmp_path):
    from studio.active_brief import compile_active_brief, decide_amendment, fingerprint, propose_amendment

    s, pipe = _blocked_direction_env(tmp_path)
    fp0 = fingerprint(compile_active_brief(s, RUN))
    amd = propose_amendment(
        s, RUN,
        changes=[{"clause_id": "goal.primary", "op": "replace", "text": "不该生效的目标"}],
        approve=False,
    )
    assert amd["status"] == "pending"
    assert fingerprint(compile_active_brief(s, RUN)) == fp0

    decide_amendment(s, RUN, amd["id"], decision="rejected", note="不改简报")
    after = compile_active_brief(s, RUN)
    assert after["version"] == 1
    assert fingerprint(after) == fp0
    assert after["open_amendments"] == []


def test_cannot_retire_goal_primary(tmp_path):
    from studio.active_brief import propose_amendment

    s, _ = _blocked_direction_env(tmp_path)
    with pytest.raises(ValueError):
        propose_amendment(
            s, RUN,
            changes=[{"clause_id": "goal.primary", "op": "retire"}],
            approve=True,
        )


def test_timeline_pending_is_brief_amendment_when_blocked(tmp_path):
    from studio.timeline import _pending_action

    s, pipe = _blocked_direction_env(tmp_path)
    pipe.record_review(RUN, "direction", verdict="return", findings=[
        {"kind": "content", "stage": "direction", "issue": "未承接种草"},
    ])
    pending = _pending_action(pipe.state(RUN).to_dict(), None, s)
    assert pending is not None
    assert pending["kind"] == "brief_amendment"
    assert pending["options"] == []
    assert pending["amend"]["clause_id"] == "goal.primary"
    assert "设计细节种草" in pending["amend"]["text"]
    assert pending["amend"]["valid"] is True
    assert "时尚达人种草" in pending["amend"]["current_text"]
    assert pending["blockers"]


def test_timeline_amendment_without_proposal_carries_no_replacement_text(tmp_path):
    """F4: a diagnosis is not a change proposal.

    The recommended option declares no goal and proposes no amendment, so the
    gate's blocker text is an error message about the *draft*. It must never be
    projected as the body that would replace the product goal.
    """
    from studio.pipeline import Pipeline
    from studio.timeline import _pending_action

    s = Store(tmp_path)
    s.create_run(RUN, "t", "时尚达人种草向带货视频")
    pipe = Pipeline(s)
    a = pipe.start_attempt(RUN, "intake")
    pipe.submit(RUN, "intake", a.token, content="i", summary="s",
                structured={"facts": [{"label": "交付形态", "value": "种草视频", "source": "u"}],
                            "constraints": [], "open_questions": []})
    d = pipe.start_attempt(RUN, "direction")
    pipe.submit(RUN, "direction", d.token, content="d", summary="d",
                structured={
                    "options": [{"key": "A", "title": "别的方向", "claim": "没声明满足目标"}],
                    "recommended": "A",
                })
    pipe.record_review(RUN, "direction", verdict="pass", findings=[])

    pending = _pending_action(pipe.state(RUN).to_dict(), None, s)
    assert pending["kind"] == "brief_amendment"
    assert pending["amend"]["valid"] is False
    assert pending["amend"]["text"] == "", "no proposal means no replacement body"
    assert pending["amend"]["clause_id"] == "", "the clause id is not a body either"
    assert pending["amend"]["reason"]
    # The diagnosis stays visible, but as a blocker — never as the new goal.
    assert any("未声明" in str(b.get("detail") or "") for b in pending["blockers"])


def test_timeline_keeps_normal_decision_when_gate_ok(tmp_path):
    from studio.config import Pricing, Settings
    from studio.ledger import Ledger
    from studio.pipeline import Pipeline
    from studio.timeline import _pending_action

    s = Store(tmp_path)
    s.create_run(RUN, "t", "时尚达人种草向带货视频")
    pipe = Pipeline(s)
    settings = Settings(
        data_root=tmp_path,
        base_url="https://example.invalid/anthropic",
        budget_limit_usd=10.0,
        pricing=Pricing(source="test", input_usd_per_mtok=1.0, output_usd_per_mtok=2.0,
                        as_of="2026-09-17"),
    )
    ledger = Ledger(s, settings)
    a = pipe.start_attempt(RUN, "intake")
    pipe.submit(RUN, "intake", a.token, content="i", summary="s",
                structured={
                    "facts": [{"label": "交付形态", "value": "US 时尚达人种草", "source": "u"}],
                    "constraints": [], "open_questions": [],
                })
    d = pipe.start_attempt(RUN, "direction")
    pipe.submit(RUN, "direction", d.token, content="d", summary="d",
                structured={
                    "options": [{"key": "A", "claim": "x", "satisfies": "goal.primary"}],
                    "recommended": "A",
                })
    pipe.record_review(RUN, "direction", verdict="pass", findings=[])
    pending = _pending_action(pipe.state(RUN).to_dict(), ledger, s)
    assert pending["kind"] == "decision_request"
    assert [o["key"] for o in pending["options"]] == ["A"]


# ------------------------------------------------------------------ P3 kind --


def test_question_kind_normalizes_and_ask_user_excludes_brief_amendment():
    from studio.structured import is_ask_user_question, question_kind

    base = {
        "question": "这样算不算种草？",
        "blocks": "目标",
        "owner": "user",
        "timing": "now",
        "reason": "商业选择",
    }
    assert question_kind({**base, "kind": "brief_amendment"}) == "brief_amendment"
    assert not is_ask_user_question({**base, "kind": "brief_amendment"})
    assert is_ask_user_question({**base, "kind": "fact"})
    assert is_ask_user_question(base), "missing kind defaults to fact and is askable"
    assert question_kind({"question": "x", "kind": "nope"}) == "fact"


def test_open_questions_card_omits_brief_amendment():
    from studio.structured import open_questions

    rows = [
        {"question": "主推哪组配色", "blocks": "首帧", "owner": "user",
         "timing": "now", "reason": "商业选择", "kind": "product_choice"},
        {"question": "这算不算达人种草", "blocks": "目标", "owner": "user",
         "timing": "now", "reason": "要改简报", "kind": "brief_amendment"},
    ]
    listed = open_questions({"open_questions": rows}, "direction")
    assert [q["question"] for q in listed] == ["主推哪组配色"]


def test_project_brief_ask_user_false_for_brief_amendment(tmp_path):
    from studio.pipeline import Pipeline
    from studio.project_brief import stage_issues

    s = Store(tmp_path)
    s.create_run(RUN, "t", "brief")
    pipe = Pipeline(s)
    a = pipe.start_attempt(RUN, "intake")
    pipe.submit(RUN, "intake", a.token, content="i", summary="s", structured={
        "facts": [{"label": "交付形态", "value": "US 种草", "source": "u"}],
        "constraints": [],
        "open_questions": [
            {"question": "改目标吗", "blocks": "goal", "owner": "user",
             "timing": "now", "reason": "要改简报", "kind": "brief_amendment"},
            {"question": "主推配色", "blocks": "首帧", "owner": "user",
             "timing": "now", "reason": "商业选择", "kind": "product_choice"},
        ],
    })
    issues = stage_issues(pipe.stage_state(RUN, "intake").to_dict())
    by_q = {i["question"]: i for i in issues}
    assert by_q["改目标吗"]["ask_user"] is False
    assert by_q["改目标吗"]["kind"] == "brief_amendment"
    assert by_q["主推配色"]["ask_user"] is True


def test_validate_rejects_unknown_question_kind():
    from studio.structured import validate_canonical_input

    payload = {
        "facts": [{"label": "a", "value": "b", "source": "c"}],
        "constraints": [],
        "open_questions": [{
            "question": "q", "blocks": "b", "owner": "user",
            "timing": "now", "reason": "r", "kind": "banana",
        }],
    }
    with pytest.raises(ValueError, match="kind"):
        validate_canonical_input("intake", payload)


# ----------------------------------------------------------- P3 clause_ids --


def test_record_review_rejects_unknown_clause_id(tmp_path):
    from studio.models import PipelineError
    from studio.pipeline import Pipeline

    s, pipe = _blocked_direction_env(tmp_path)
    with pytest.raises(PipelineError) as err:
        pipe.record_review(RUN, "direction", verdict="return", findings=[{
            "kind": "content", "stage": "direction", "issue": "x",
            "clause_ids": ["not.a-real-clause"],
        }])
    assert err.value.rule == "review"
    assert "clause_id" in str(err.value)


def test_record_review_accepts_known_clause_ids(tmp_path):
    from studio.pipeline import Pipeline

    s, pipe = _blocked_direction_env(tmp_path)
    review = pipe.record_review(RUN, "direction", verdict="return", findings=[{
        "kind": "content", "stage": "direction",
        "issue": "未承接种草目标",
        "clause_ids": ["goal.primary"],
    }])
    assert review.findings[0]["clause_ids"] == ["goal.primary"]


def test_record_review_stamps_gate_clause_ids_when_model_omits_them(tmp_path):
    s, pipe = _blocked_direction_env(tmp_path)
    review = pipe.record_review(RUN, "direction", verdict="return", findings=[{
        "kind": "content", "stage": "direction", "issue": "还不够种草",
    }])
    assert review.findings[0].get("clause_ids") == ["goal.primary"]


def test_finish_review_schema_lists_clause_ids():
    from studio.tools.registry import tools_for

    finish = next(t for t in tools_for("reviewer") if t["name"] == "finish_review")
    finding = finish["input_schema"]["properties"]["findings"]["items"]
    assert "clause_ids" in finding["properties"]
