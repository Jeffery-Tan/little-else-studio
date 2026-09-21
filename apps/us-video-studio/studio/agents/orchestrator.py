"""The main agent: the one Jeffery talks to.

It is the only role with ``dispatch``, so it is the only role that can spend
money on sub-agents. It has no approval tool, which is the point — the gate is
between it and the next stage, not inside it.

Conversation context is a short window of the chat log plus a live state read.
Tool results are not carried between turns: the orchestrator re-reads state
each turn instead. That keeps the context small (which matters at a $10 budget)
and means it can never act on a stale picture of the run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import uuid
from typing import Any, Callable

from ..config import Settings
from ..ledger import Ledger
from ..models import PipelineError, STAGE_SPEC
from ..pipeline import Pipeline
from ..store import Store
from ..project_brief import project_brief, latest_execution
from ..tools.dispatch import Dispatcher
from ..tools.registry import tools_for
from ..tools.execution import ToolResult
from ..tools.workspace import ToolError, Workspace
from ..revision_scope import work_plan, plan_revision, check_base, patch_payload, record_applied
from .budget import BudgetedClient
from .client import JobCancelled, ModelClient, ModelError, run_agent_loop
from .codex_client import CodexUnavailable
from ..connections import ConnectionUnavailable
from .reviewer import run_review
from .roles import ORCHESTRATOR, OWNER, creative_selection, production_allowed

#: How many past chat turns to carry into a request. The state read supplies
#: everything else, so this only needs to cover the current conversation.
CHAT_WINDOW = 20


@dataclass
class OrchestratorTurn:
    text: str
    stopped_because: str
    rounds: int
    tool_log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def used_tools(self) -> list[str]:
        return [t["tool"] for t in self.tool_log]

    @property
    def failed_tools(self) -> list[dict[str, Any]]:
        return [t for t in self.tool_log if not t["ok"]]


class Orchestrator:
    def __init__(
        self,
        store: Store,
        pipeline: Pipeline,
        settings: Settings,
        client: ModelClient,
        ledger: Ledger,
        run_id: str,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        check_cancel: Callable[[], None] | None = None,
        learning_service=None,
        production_review=None,
    ):
        self.store = store
        self.pipeline = pipeline
        self.settings = settings
        self.client = client
        self.ledger = ledger
        self.run_id = run_id
        self.on_event = on_event
        self.check_cancel = check_cancel
        self.production_review = production_review
        if learning_service is None:
            from ..learning import LearningService
            learning_service = LearningService(store)
        self.learning = learning_service
        self.selection = creative_selection(store, run_id)
        self._scope_selected = False
        self._revision_scope = None
        self._turn_id = None
        self.method_context = None
        self._context_refs: list[str] = []
        self._current_user_message = ""
        self.dispatcher = Dispatcher(
            store, pipeline, settings, client, ledger, run_id, on_event=on_event,
            check_cancel=check_cancel,
            learning_service=self.learning,
        )

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is not None:
            self.on_event(event)

    # -------------------------------------------------------------- public -- #

    def send(self, message: str) -> OrchestratorTurn:
        """One turn of conversation. The user's message is logged first."""
        message = message.strip()
        if not message:
            raise ValueError("empty message")
        if self.check_cancel:
            self.check_cancel()

        # Persist before doing anything else, for two reasons and the second one
        # is a repair:
        #
        # 1. The conversation is ordered by this timestamp. A message stamped
        #    when its own work *finished* renders below the work it caused — the
        #    live run showed the opening question timestamped three minutes after
        #    the intake artifact it produced.
        # 2. If the budget cap aborts the turn, what he wrote must survive. It
        #    used to be dropped, because the raise happened before the append.
        self.store.append_chat(self.run_id, "user", message)
        from ..interactions import answer
        answer(self.store, self.run_id, message)
        self._current_user_message = message
        previous = creative_selection(self.store, self.run_id)
        self._scope_selected = False
        self._revision_scope = None
        self._turn_id = uuid.uuid4().hex
        self.selection = {"intent": "status", "format": previous["format"], "tags": [], "case_ids": None}
        self.store.append_event(self.run_id, "agent_turn_scope_started", turn_id=self._turn_id,
                                previous_selection=previous, scope_selected=False)
        self.method_context = self.learning.prepare_context(
            self.run_id, "orchestrator", **self.selection, purpose="message")
        self._context_refs = [self.method_context["evidence_ref"]]

        history = self._build_messages()
        workspace = Workspace(self.store, self.pipeline, self.run_id, "orchestrator")
        budgeted = BudgetedClient(
            self.client, self.ledger, run_id=self.run_id, role="orchestrator", stage="intake"
        )

        def execute(name: str, args: dict[str, Any]) -> str | ToolResult:
            return self._execute(name, args, workspace)

        try:
            system = ORCHESTRATOR.system(self.store.read_run(self.run_id), extra=self._system_extra(),
                                         format="none")
            self._emit({'type': 'context_ready', 'role': 'orchestrator',
                        'system_sha256': hashlib.sha256(system.encode('utf-8')).hexdigest(),
                        'messages_sha256': hashlib.sha256(json.dumps(history, ensure_ascii=False,
                            sort_keys=True).encode('utf-8')).hexdigest(),
                        'chat_window': CHAT_WINDOW, 'messages_count': len(history)})
            self._emit({'type': 'learning_context', 'role': 'orchestrator',
                        'snapshot_sha256': self.method_context['snapshot_sha256'],
                        'evidence_ref': self.method_context['evidence_ref']})
            result = run_agent_loop(
                budgeted,
                model=self.settings.model_for("orchestrator"),
                system=system,
                messages=history,
                tools=tools_for("orchestrator"),
                execute_tool=execute,
                max_rounds=self.settings.max_tool_rounds,
                max_continuations=self.settings.max_continuations,
                check_cancel=self.check_cancel,
                on_activity=lambda event: self._emit({"role": "orchestrator", "stage": "", **event}),
                on_turn=lambda t: self._emit(
                    {"type": "agent_turn", "role": "orchestrator", "stage": "",
                     "stop_reason": t.stop_reason, "tools": [c.name for c in t.tool_calls]}
                ),
                on_tool=lambda r: self._emit(
                    {"type": "agent_tool", "role": "orchestrator", "stage": "", **r}
                ),
            )
        except Exception as exc:
            # Leave a trace on disk. The SSE error event is not persisted, so
            # without this a reload shows his question followed by silence and
            # no way to find out why. 'system' is filtered out of model requests
            # by _build_messages, so this cannot come back as a bad role.
            detail = str(exc) if isinstance(exc, (JobCancelled, ModelError, PipelineError,
                CodexUnavailable, ConnectionUnavailable)) else '执行失败；请检查连接及已保留的调用状态。'
            self.store.append_chat(self.run_id, "system", f"{type(exc).__name__}: {detail}")
            raise

        if result.text:
            self.store.append_chat(self.run_id, "assistant", result.text)
        return OrchestratorTurn(
            text=result.text,
            stopped_because=result.stopped_because,
            rounds=result.rounds,
            tool_log=result.tool_log,
        )

    # ------------------------------------------------------------- context -- #

    def _build_messages(self) -> list[dict[str, Any]]:
        """The request history. Only user and assistant turns may come out of it.

        The chat log also holds 'system' entries (failures) so the timeline can
        explain a silent turn. Those are for the screen; sending one to the
        Messages API would be a 400. The filter is the invariant that keeps the
        two audiences from colliding.
        """
        prior = self.store.read_chat(self.run_id)[-CHAT_WINDOW:]
        turns: list[tuple[str, str]] = [
            (t["role"], t["text"])
            for t in prior
            if t.get("role") in ("user", "assistant") and t.get("text")
        ]

        # Merge consecutive same-role turns and make sure the list opens with a
        # user turn: the Messages API requires it, and a shim may or may not
        # enforce it. Getting this wrong is a 400, not a subtle bug.
        while turns and turns[0][0] != "user":
            turns.pop(0)

        messages: list[dict[str, Any]] = []
        for role, text in turns:
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += "\n\n" + text
            else:
                messages.append({"role": role, "content": text})

        # send() persisted the user's message before calling this, so the list
        # always ends with their turn. Anything else means the log was emptied
        # under us and continuing would send a request with no question in it.
        if not messages:
            raise PipelineError(
                "empty_history",
                "the chat log has no user turn; refusing to send a request with "
                "nothing to answer",
            )
        return messages

    def _spend_line(self) -> str:
        """The current cost picture, read fresh from the ledger.

        Named as a line but returns a block: it is the answer to "what have we
        spent", and both the system prompt and ``read_project_state`` show it so
        the agent never has to say it cannot see the number.
        """
        spend = self.ledger.totals()
        run_spend = self.ledger.totals(self.run_id)
        limit = self.settings.budget_limit_usd
        tokens = f"{spend.input_tokens} 输入 / {spend.output_tokens} 输出 token"
        return "\n".join(
            [
                "# 成本（实时）",
                "",
                (f"开发期累计估算：{spend.estimated_usd:.4f} 美元" if spend.estimated_usd is not None else "开发期金额未知")
                + f"（已知 {tokens}，{spend.calls} 次调用；{spend.unknown_calls} 次未返回完整用量（不阻断继续））。",
                "累计数包含本项目里所有角色的调用，不只是你自己的；"
                "派工和送审产生的调用也算在里面。",
                (f"本次运行估算金额：{run_spend.estimated_usd:.4f} 美元。" if run_spend.estimated_usd is not None else "本次运行金额未知。无需手工核账，账单以提供方平台为准，不影响继续创作。"),
                f"开发期总额度 {limit:.2f} 美元，跨所有运行共享。",
                "美元按价目表估算，不是账单；用量含未核实部分时只展示已知小计。上限为软阈值，不保证最后一笔不超额。",
                *(
                    [
                        f"价目表未确认（{self.settings.pricing.source}），"
                        "所以美元阈值无法判断；token 软阈值仅阻止累计已达到阈值后的新调用。"
                    ]
                    if not self.settings.usd_cap_is_enforceable
                    else []
                ),
            ]
        )

    def _system_extra(self) -> str:
        workspace = Workspace(self.store, self.pipeline, self.run_id, "orchestrator")
        lines = [
            "# 你能做什么",
            "",
            "读状态和产物、自己写 intake、把阶段派给角色 Agent、送审。",
            "**你不能批准任何东西**，也没有这个工具。门禁由 Jeffery 在界面上点。",
            "",
            "# 当前项目摘要（界面与对话共用）\n" + json.dumps(
                project_brief(self.pipeline.state(self.run_id).to_dict(), latest_execution(self.store, self.run_id)), ensure_ascii=False),
            "# 当前项目状态（本次重新读取，历史对话不能覆盖）",
            workspace.render_state(),
            workspace.render_context('writing' if self.store.latest_artifact(self.run_id, 'intake') else 'intake'),
            "# Studio 方法目录与显式路由\n" + json.dumps(self.learning.skill_catalog(), ensure_ascii=False),
            "当前显式路由：" + json.dumps(self.selection, ensure_ascii=False),
            "本轮工作范围尚未选择。历史路由不授予权限；写稿、派工、审核前必须按当前用户原话调用 prepare_creative_context。",
            self.method_context["text"] if self.method_context else "",
        ]
        return "\n".join(lines)

    # --------------------------------------------------------------- tools -- #

    def _execute(self, name: str, args: dict[str, Any], workspace: Workspace) -> str | ToolResult:
        if name == 'read_work_plan':
            return json.dumps(work_plan(self.pipeline, self.run_id, **args), ensure_ascii=False)
        if name == 'read_capabilities':
            from ..tools.context import capabilities
            return json.dumps(capabilities(self.settings, 'orchestrator'), ensure_ascii=False)
        if name == 'read_conversation_history':
            from ..tools.context import conversation
            return json.dumps(conversation(self.store, self.run_id, **args), ensure_ascii=False)
        if name == 'ask_user':
            from ..interactions import ask
            question = ask(self.store, self.run_id, args)
            return ToolResult(json.dumps(question, ensure_ascii=False), concludes_turn=True)
        if name == 'read_project_history':
            from ..project_history import snapshot, read_document
            data = (read_document(self.store, self.run_id, args['document_id']) if args.get('document_id')
                    else snapshot(self.store, self.run_id))
            return json.dumps(data, ensure_ascii=False)
        if name == "prepare_creative_context":
            selection = {"intent": args["intent"], "format": args["format"],
                         "tags": args.get("tags", []), "case_ids": args.get("case_ids")}
            if self._scope_selected and not production_allowed(self.selection) and production_allowed(selection):
                raise ToolError("本轮已明确为只读/反馈/暂停，不能自行升级制作；等待新的用户指令。")
            context = self.learning.prepare_context(self.run_id, "orchestrator", **selection, purpose="message")
            self.store.append_event(self.run_id, "creative_context_selected", selection=selection,
                                    snapshot_sha256=context["snapshot_sha256"], turn_id=self._turn_id)
            self.selection, self.method_context = selection, context
            self._scope_selected = True
            self._context_refs.append(context["evidence_ref"])
            self._emit({"type": "learning_context", "role": "orchestrator",
                        "snapshot_sha256": context["snapshot_sha256"], "evidence_ref": context["evidence_ref"]})
            return context["text"] + "\n\n当前路由：" + json.dumps(selection, ensure_ascii=False)
        if name == "read_learning_cases":
            selection = dict(self.selection)
            selection.update({k: args[k] for k in ("tags", "case_ids") if k in args})
            context = self.learning.prepare_context(self.run_id, "orchestrator", **selection, purpose="message")
            self._context_refs.append(context["evidence_ref"])
            return context["text"]
        if name == "read_method_document":
            document = self.learning.read_method_document(args["name"], role="orchestrator", run_id=self.run_id)
            if isinstance(document, dict) and document.get("evidence_ref"):
                self._context_refs.append(document["evidence_ref"])
            return document if isinstance(document, str) else json.dumps(document, ensure_ascii=False)
        if name == "propose_learning_feedback":
            proposal = self.learning.propose_feedback(self.run_id, args,
                current_user_message=self._current_user_message, actor="agent")
            return json.dumps(proposal, ensure_ascii=False)
        if name == 'read_production':
            from ..production import draft_records
            from ..evidence import evidence_snapshot
            return json.dumps({'drafts': list(draft_records(self.store, self.run_id).values()),
                               'materials': evidence_snapshot(self.store, self.run_id)['materials'],
                               'notice': '准备方案免费；生成、图审与费用只能由用户在制作界面确认。'}, ensure_ascii=False)
        if name in ("write_stage", "dispatch", "request_review", "compile_creative_package", "prepare_production", "review_production", "plan_revision", "submit_patch"):
            if not self._scope_selected:
                raise ToolError("尚未选择本轮工作范围；先根据本条用户消息调用 prepare_creative_context。历史制作路由不能自动沿用。")
            if not production_allowed(self.selection):
                raise ToolError("当前明确路由只允许读取/反馈；先明确选择 new/continue/edit 和交付格式，才能开始制作。")
        if name == 'plan_revision':
            with self.store.run_lock(self.run_id):
                self._revision_scope = plan_revision(self.store, self.pipeline, self.run_id, args,
                    turn_id=self._turn_id, user_message=self._current_user_message, previous=self._revision_scope)
            return json.dumps(self._revision_scope, ensure_ascii=False)
        if name == 'submit_patch':
            scope = self._revision_for('intake', args.get('plan_id'))
            if not scope or scope['mode'] != 'local':
                raise ToolError('先绑定 intake 的局部修改范围；专业阶段通过 dispatch 交责任角色。')
            if self.check_cancel:
                self.check_cancel()
            with self.store.run_lock(self.run_id):
                payload, changes = patch_payload(self.store, self.pipeline, self.run_id, scope,
                                                {k: v for k, v in args.items() if k != 'plan_id'})
                attempt = self.pipeline.start_attempt(self.run_id, 'intake', actor='orchestrator')
                try:
                    artifact = self.pipeline.submit(self.run_id, 'intake', attempt.token,
                                                    **payload, actor='orchestrator-revision')
                except Exception:
                    self.pipeline.reject(self.run_id, 'intake', attempt.token, '局部修改未保存，原稿保留。')
                    raise
                impact = record_applied(self.store, self.pipeline, self.run_id, scope, artifact, changes)
            return json.dumps({'version': artifact.version, 'sha256': artifact.sha256, 'work_plan': impact}, ensure_ascii=False)
        if name == 'review_production':
            if not self.production_review:
                raise ToolError('制作检查服务暂不可用，保留方案。')
            return json.dumps(self.production_review(args), ensure_ascii=False)
        if name == 'prepare_production':
            from ..production import DraftIn, prepare_draft
            try:
                return json.dumps(prepare_draft(self.store, self.pipeline, self.run_id, DraftIn(**args), actor='agent'), ensure_ascii=False)
            except (ValueError, PipelineError) as exc:
                raise ToolError('制作方案未保存，请核对当前已批准脚本、参考图和参数。') from exc
        if name == "compile_creative_package":
            from ..creative_packages import CreativePackages
            if args["format"] != self.selection["format"]:
                raise ToolError("编译格式必须与本轮明确选择的交付格式一致。")
            return json.dumps(CreativePackages(self.store, repository_root=self.learning.repository_root).compile(
                self.run_id, **args), ensure_ascii=False)
        if name == "read_project_state":
            # Spend belongs here, not only in the system prompt: the system
            # prompt is written once at the start of the turn, and an agent that
            # has just spent money on four dispatch rounds needs the current
            # number, not the number from before it started.
            return "# 当前项目摘要\n" + json.dumps(project_brief(
                self.pipeline.state(self.run_id).to_dict(), latest_execution(self.store, self.run_id)), ensure_ascii=False) + "\n\n" + workspace.render_state()
        if name == "read_artifact":
            rendered = workspace.render_artifact(args["stage"], args.get("version"))
            artifact = (self.store.read_artifact(self.run_id, args["stage"], args["version"])
                        if args.get("version") is not None else self.store.latest_artifact(self.run_id, args["stage"]))
            if artifact:
                rendered += f"\n\n准确版本引用：artifact:{artifact.stage}-v{artifact.version}#{artifact.sha256}"
                rendered += '\n\n用于定位修改字段的原结构（数组下标从 0 开始）：\n' + json.dumps(artifact.structured, ensure_ascii=False)
            return rendered
        if name == "list_reviews":
            return workspace.render_reviews(args["stage"])
        if name == "list_materials":
            return workspace.render_materials()
        if name == "write_stage":
            return self._write_stage(args)
        if name == "dispatch":
            scope = self._revision_for(args['stage'], args.get('plan_id'))
            outcome = self.dispatcher.dispatch(args["stage"], args.get("brief", ""), revision_scope=scope)
            return ToolResult(outcome.render(), is_error=bool(outcome.error or not outcome.submitted))
        if name == "request_review":
            return self._request_review(args["stage"])
        raise ToolError(f"orchestrator has no tool named {name!r}")

    def _revision_for(self, stage, plan_id):
        scope = self._revision_scope
        if scope and scope['mode'] == 'local' and scope['stage'] != stage:
            raise ToolError('本轮已限定局部修改，不能改写其他阶段。')
        if plan_id:
            if not scope or scope['plan_id'] != plan_id or scope['stage'] != stage or scope['turn_id'] != self._turn_id:
                raise ToolError('修订计划不是本轮当前阶段的范围，请重新核对。')
            check_base(self.store, self.pipeline, self.run_id, scope)
            return scope
        if ((scope and scope['mode'] == 'local') or
                (self.selection['intent'] == 'edit' and self.store.latest_artifact(self.run_id, stage))):
            raise ToolError('修改已有内容前先用 plan_revision 绑定准确版本和范围，并传入 plan_id。')
        return None

    def _write_stage(self, args: dict[str, Any]) -> str:
        with self.store.run_lock(self.run_id):
            return self._write_stage_locked(args)

    def _write_stage_locked(self, args: dict[str, Any]) -> str:
        stage = args["stage"]
        if OWNER.get(stage) != "orchestrator":
            raise ToolError(
                f"{stage} is owned by {OWNER.get(stage)}, not by you. Use dispatch for it."
            )
        scope = self._revision_for(stage, args.get('plan_id'))
        if scope and scope['mode'] == 'local':
            raise ToolError('局部修改不能提交全文；使用 submit_patch。')
        state = self.pipeline.stage_state(self.run_id, stage)
        attempt = state.open_attempt or self.pipeline.start_attempt(
            self.run_id, stage, actor="orchestrator"
        )
        artifact = self.pipeline.submit(
            self.run_id,
            stage,
            attempt.token,
            content=args.get("content", ""),
            summary=args.get("summary", ""),
            evidence=list(dict.fromkeys([*(args.get("evidence") or []), *self._context_refs])),
            limitations=args.get("limitations") or [],
            # Passed through unvalidated on purpose: the pipeline normalises it,
            # and a model that got the shape wrong must still be able to submit
            # the document it wrote. See `studio/structured.py`.
            structured=args.get("structured"),
            actor=f"orchestrator:{attempt.key}",
        )
        affected = [s.stage for s in self.pipeline.state(self.run_id).stages.values() if s.latest and s.stale]
        if scope:
            record_applied(self.store, self.pipeline, self.run_id, scope, artifact, [])
        extra = f"已存在且需更新的下游版本：{'、'.join(affected)}。" if affected else ""
        return (
            f"已提交 {stage}-v{artifact.version}。它不可修改；要改就是新版本。{extra}\n"
            f"准确版本引用：artifact:{stage}-v{artifact.version}#{artifact.sha256}"
        )

    def _request_review(self, stage: str) -> ToolResult:
        if stage not in STAGE_SPEC:
            raise ToolError(f"unknown stage: {stage!r}")
        outcome = run_review(
            store=self.store,
            pipeline=self.pipeline,
            settings=self.settings,
            client=self.client,
            ledger=self.ledger,
            run_id=self.run_id,
            stage=stage,
            on_event=self.on_event,
            check_cancel=self.check_cancel,
            learning_service=self.learning,
        )
        return ToolResult(outcome.render(), is_error=bool(outcome.error or outcome.verdict is None))
