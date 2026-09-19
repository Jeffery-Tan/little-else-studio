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
from typing import Any, Callable

from ..config import Settings
from ..ledger import Ledger
from ..models import PipelineError, STAGE_SPEC
from ..pipeline import Pipeline
from ..store import Store
from ..tools.dispatch import Dispatcher
from ..tools.registry import tools_for
from ..tools.workspace import ToolError, Workspace
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
    ):
        self.store = store
        self.pipeline = pipeline
        self.settings = settings
        self.client = client
        self.ledger = ledger
        self.run_id = run_id
        self.on_event = on_event
        self.check_cancel = check_cancel
        if learning_service is None:
            from ..learning import LearningService
            learning_service = LearningService(store)
        self.learning = learning_service
        self.selection = creative_selection(store, run_id)
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
        self._current_user_message = message
        self.selection = creative_selection(self.store, self.run_id)
        self.method_context = self.learning.prepare_context(
            self.run_id, "orchestrator", **self.selection, purpose="message")
        self._context_refs = [self.method_context["evidence_ref"]]

        history = self._build_messages()
        workspace = Workspace(self.store, self.pipeline, self.run_id, "orchestrator")
        budgeted = BudgetedClient(
            self.client, self.ledger, run_id=self.run_id, role="orchestrator", stage="intake"
        )

        def execute(name: str, args: dict[str, Any]) -> str:
            return self._execute(name, args, workspace)

        try:
            system = ORCHESTRATOR.system(self.store.read_run(self.run_id), extra=self._system_extra(),
                                         format=self.selection["format"])
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
                + f"（已知 {tokens}，{spend.calls} 次调用；{spend.unknown_calls} 次用量待核实）。",
                "累计数包含本项目里所有角色的调用，不只是你自己的；"
                "派工和送审产生的调用也算在里面。",
                (f"本次运行估算金额：{run_spend.estimated_usd:.4f} 美元。" if run_spend.estimated_usd is not None else "本次运行金额未知，需先核实用量。"),
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
            self._spend_line(),
            "# 当前项目状态（本次重新读取，历史对话不能覆盖）",
            workspace.render_state(),
            workspace.render_context('writing' if self.store.latest_artifact(self.run_id, 'intake') else 'intake'),
            "# Studio 方法目录与显式路由\n" + json.dumps(self.learning.skill_catalog(), ensure_ascii=False),
            "当前显式路由：" + json.dumps(self.selection, ensure_ascii=False),
            self.method_context["text"] if self.method_context else "",
        ]
        return "\n".join(lines)

    # --------------------------------------------------------------- tools -- #

    def _execute(self, name: str, args: dict[str, Any], workspace: Workspace) -> str:
        if name == 'read_project_history':
            from ..project_history import snapshot, read_document
            data = (read_document(self.store, self.run_id, args['document_id']) if args.get('document_id')
                    else snapshot(self.store, self.run_id))
            return json.dumps(data, ensure_ascii=False)
        if name == "prepare_creative_context":
            selection = {"intent": args["intent"], "format": args["format"],
                         "tags": args.get("tags", []), "case_ids": args.get("case_ids")}
            context = self.learning.prepare_context(self.run_id, "orchestrator", **selection, purpose="message")
            self.store.append_event(self.run_id, "creative_context_selected", selection=selection,
                                    snapshot_sha256=context["snapshot_sha256"])
            self.selection, self.method_context = selection, context
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
        if name in ("write_stage", "dispatch", "request_review", "compile_creative_package", "prepare_production"):
            if not production_allowed(self.selection):
                raise ToolError("当前明确路由只允许读取/反馈；先明确选择 new/continue/edit 和交付格式，才能开始制作。")
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
            return workspace.render_state() + "\n\n" + self._spend_line()
        if name == "read_artifact":
            rendered = workspace.render_artifact(args["stage"], args.get("version"))
            artifact = (self.store.read_artifact(self.run_id, args["stage"], args["version"])
                        if args.get("version") is not None else self.store.latest_artifact(self.run_id, args["stage"]))
            if artifact:
                rendered += f"\n\n准确版本引用：artifact:{artifact.stage}-v{artifact.version}#{artifact.sha256}"
            return rendered
        if name == "list_reviews":
            return workspace.render_reviews(args["stage"])
        if name == "list_materials":
            return workspace.render_materials()
        if name == "write_stage":
            return self._write_stage(args)
        if name == "dispatch":
            return self.dispatcher.dispatch(args["stage"], args.get("brief", "")).render()
        if name == "request_review":
            return self._request_review(args["stage"])
        raise ToolError(f"orchestrator has no tool named {name!r}")

    def _write_stage(self, args: dict[str, Any]) -> str:
        stage = args["stage"]
        if OWNER.get(stage) != "orchestrator":
            raise ToolError(
                f"{stage} is owned by {OWNER.get(stage)}, not by you. Use dispatch for it."
            )
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
        extra = (
            f"这一步让下游失效了，需要重做。" if artifact.version > 1 else ""
        )
        return (
            f"已提交 {stage}-v{artifact.version}。它不可修改；要改就是新版本。{extra}\n"
            f"准确版本引用：artifact:{stage}-v{artifact.version}#{artifact.sha256}"
        )

    def _request_review(self, stage: str) -> str:
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
        if outcome.error and outcome.verdict is None:
            raise PipelineError("review", outcome.error)
        return outcome.render()
