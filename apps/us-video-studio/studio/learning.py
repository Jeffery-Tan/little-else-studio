"""Studio adapter for the single US learning library; no network or model calls.

Human catalog and creator/reviewer context are deliberately separate APIs.
Only the HTTP user action may call apply; agent tools expose proposals only.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import importlib.util
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from .models import utcnow, STAGES
from .store import _atomic_write_text

REPOSITORY = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("studio_creative_learning", REPOSITORY / "scripts/creative_learning.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
SKILLS = ("us-commerce-strategy", "us-creator-video", "us-photo-copy")
METHOD_CARDS = {
    "video-recovery": {
        "title": "视频缺陷诊断与补救方法", "file": "apps/us-video-studio/docs/methods/video-recovery.md",
        "source": "tracks/us/docs/ai-video-recovery-policy.md", "skills": ("us-creator-video",),
        "roles": ("orchestrator", "director", "material", "writer"),
        "tools": ("read_production", "plan_revision"),
    },
    "video-input": {
        "title": "视频制作输入与检查", "file": "apps/us-video-studio/docs/methods/video-input.md",
        "source": "tracks/us/creative/learning/execution.md", "skills": ("us-creator-video",),
        "roles": ("orchestrator", "material", "writer", "reviewer"),
        "tools": ("compile_creative_package", "prepare_production"),
    },
    "photo-input": {
        "title": "图文审阅包与成图检查", "file": "apps/us-video-studio/docs/methods/photo-input.md",
        "source": "tracks/us/creative/photo-copy/README.md", "skills": ("us-photo-copy",),
        "roles": ("orchestrator", "director", "material", "writer", "reviewer"),
        "tools": ("compile_creative_package",),
    },
}
DOCUMENTS = {
    "creative-package-contract": ("创作包格式与提交契约", "apps/us-video-studio/docs/creative-package-contract.md"),
    "recovery-policy": ("AI 视频补救与停止规则", "tracks/us/docs/ai-video-recovery-policy.md"),
    "learning-readme": ("学习库说明", "tracks/us/creative/learning/README.md"),
    "strategy-guide": ("表达策略", "tracks/us/creative/learning/strategy-guide.md"),
    "execution": ("执行与证据边界", "tracks/us/creative/learning/execution.md"),
    "workflow-skills": ("Skill 接入与运行记录", "tracks/us/creative/learning/workflow-skills.md"),
    "batch-001": ("首批案例", "tracks/us/creative/learning/batch-001.md"),
    "commerce-methods-research": ("商业表达研究", "tracks/us/creative/learning/commerce-methods-research.md"),
    "commerce-methods-sources": ("商业表达研究来源登记", "tracks/us/creative/learning/commerce-methods-sources.json"),
    "photo-copy-methods-research": ("图文方法研究", "tracks/us/creative/learning/photo-copy-methods-research.md"),
    "photo-copy-methods-sources": ("图文方法研究来源登记", "tracks/us/creative/learning/photo-copy-methods-sources.json"),
    "tiktok-field-study": ("TikTok 实地研究", "tracks/us/creative/learning/tiktok-field-study.md"),
    "photo-copy-readme": ("图文工作流", "tracks/us/creative/photo-copy/README.md"),
    "comparison-readme": ("比较实验说明", "tracks/us/creative/comparison/README.md"),
    "baseline-001": ("基线比较报告", "tracks/us/creative/comparison/baseline-001.md"),
}
_LOCK = threading.RLock()


def validate_method_packages(repository_root=REPOSITORY):
    """Validate registered, role-scoped cards without reading human research into a model."""
    from .tools.registry import REGISTRY
    root = Path(repository_root).resolve()
    errors = []

    def links_exist(path, label):
        for target in re.findall(r'\]\(([^)]+)\)', path.read_text(encoding='utf-8-sig')):
            if target.startswith(('http://', 'https://', 'mailto:', '#')):
                continue
            candidate = (path.parent / target.split('#', 1)[0]).resolve()
            if not candidate.is_relative_to(root) or not candidate.is_file():
                errors.append(f'{label}: missing or unsafe link {target}')

    for ident, spec in METHOD_CARDS.items():
        for field in ('file', 'source'):
            path = (root / spec[field]).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                errors.append(f'{ident}: missing or unsafe {field}')
            elif field == 'file':
                links_exist(path, ident)
        if not set(spec['skills']) <= set(SKILLS):
            errors.append(f'{ident}: unknown skill')
        for role in spec['roles']:
            if role not in ('orchestrator', 'director', 'material', 'writer', 'reviewer'):
                errors.append(f'{ident}: unknown role {role}')
        for tool in spec['tools']:
            if tool not in REGISTRY or not set(spec['roles']) & set(REGISTRY[tool].roles):
                errors.append(f'{ident}: unavailable tool {tool}')
    for ident in SKILLS:
        path = root / '.agents/skills' / ident / 'SKILL.md'
        if path.is_file():
            links_exist(path, ident)
        else:
            errors.append(f'{ident}: missing Skill entry')
    return errors


class LearningConflict(ValueError):
    """The reviewed proposal or canonical library no longer matches."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"


def _sha(value):
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _library_text(value, original):
    """Preserve field ordering, BOM and newline style for reviewable small diffs."""
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if b"\r\n" in original:
        text = text.replace("\n", "\r\n")
    if original.startswith(b"\xef\xbb\xbf"):
        text = "\ufeff" + text
    return text


@contextmanager
def _library_lock(path):
    """Same canonical path shares a cross-process lock, even across data roots."""
    directory = Path(tempfile.gettempdir()) / "little-else-studio-learning-locks"
    directory.mkdir(exist_ok=True)
    key = _sha(os.path.normcase(str(path.resolve())))
    with _LOCK, (directory / (key + ".lock")).open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class LearningService:
    def __init__(self, store, repository_root=None):
        self.store = store
        self.repository_root = Path(repository_root or REPOSITORY).resolve()
        self.library_path = self.repository_root / "tracks/us/creative/learning/library.json"
        # Pipelines sharing this Store validate selected source contents too.
        self.store.reference_learning = self

    def _read(self):
        raw = self.library_path.read_bytes()
        doc = json.loads(raw.decode("utf-8-sig"))
        errors = core.validate(doc)
        if errors:
            raise ValueError("Invalid learning library: " + "; ".join(errors))
        return raw, doc

    def _run_root(self, run_id):
        self.store.read_run(run_id)
        return self.store.run_dir(run_id) / "learning"

    def _text(self, ident, title, path):
        raw = path.read_bytes()
        digest = _sha(raw)
        text = raw.decode("utf-8-sig")
        return {"id": ident, "title": title, "text": text, "content": text,
                "sha256": digest, "version": digest[:12]}

    def _skills(self):
        return [self._text(s, s, self.repository_root / ".agents/skills" / s / "SKILL.md") for s in SKILLS]

    def skill_catalog(self, role="orchestrator"):
        """Model-safe directory. Never exposes the human audit catalog."""
        from .tools.registry import REGISTRY
        if role not in ('orchestrator', 'director', 'material', 'writer', 'reviewer'):
            raise ValueError('Unknown creative role')
        return {"skills": [{k: s[k] for k in ("id", "title", "sha256", "version")} for s in self._skills()],
                "documents": ([{"id": s, "title": s} for s in SKILLS]
                              + [{"id": "creative-package-contract", "title": "创作包格式与提交契约"}]
                              + [{"id": ident, "title": spec['title'], "skills": list(spec['skills']),
                                  "source": spec['source'], "tools": [t for t in spec['tools']
                                                              if role in REGISTRY[t].roles]}
                                 for ident, spec in METHOD_CARDS.items() if role in spec['roles']])}

    def catalog(self):
        """Human audit view, including pending/holdout; MUST NOT enter model context."""
        from .references import fingerprint
        raw, doc = self._read()
        return {"library_sha256": _sha(raw), "schema_version": doc["schema_version"], "market": "US",
                "library_version": doc.get("library_version", 1),
                "library": {"sha256": _sha(raw), "version": doc.get("library_version", 1), "schema_version": 1},
                "skills": self._skills(), "documents": [{"id": k, "title": v[0]} for k, v in DOCUMENTS.items()],
                "cases": [dict(c, case_sha256=fingerprint(c)) for c in doc["cases"]], "methods": doc["methods"],
                "feedback_scopes": list(core.FEEDBACK_SCOPES), "feedback_actions": list(core.FEEDBACK_ACTIONS),
                "boundary": "审计展示不等于创作者召回资格；待核验/holdout 不进入创作上下文。"}

    def document(self, ident):
        if not isinstance(ident, str) or ident not in DOCUMENTS:
            raise ValueError("Unknown learning document")
        title, relative = DOCUMENTS[ident]
        path = self.repository_root / relative
        if ident == "creative-package-contract" and not path.exists():
            path = REPOSITORY / relative
        return self._text(ident, title, path)

    def read_method_document(self, name, role=None, run_id=None):
        # Raw research/comparison reports contain prior judgments and test answers.
        # Their complete text remains available in the human-only document API.
        role = role or 'orchestrator'
        if role not in ('orchestrator', 'director', 'material', 'writer', 'reviewer'):
            raise ValueError('Unknown creative role')
        if name in METHOD_CARDS:
            spec = METHOD_CARDS[name]
            if role not in spec['roles']:
                raise ValueError('Method document is unavailable to this role')
            result = self._text(name, spec['title'], self.repository_root / spec['file'])
            result['source'] = spec['source']
        elif name in (*SKILLS, "creative-package-contract"):
            result = self.document(name) if name == "creative-package-contract" else next(s for s in self._skills() if s["id"] == name)
        else:
            raise ValueError("Model document access is limited to registered Skills and method cards; research is human audit only")
        if run_id is not None:
            root = self._run_root(run_id)
            digest = _sha(_json(result))
            result.update(snapshot_sha256=digest, evidence_ref=f"learning/documents/{digest}.json")
            with self.store.run_lock(run_id):
                path = root / "documents" / (digest + ".json")
                if not path.exists():
                    _atomic_write_text(path, _json(result))
                self.store.append_event(run_id, "learning_document_loaded", role=role, document_id=name,
                                        snapshot_sha256=digest, evidence_ref=result["evidence_ref"], sha256=result["sha256"])
        return result

    @staticmethod
    def _review_library(doc):
        doc = copy.deepcopy(doc)
        cases = []
        for case in doc["cases"]:
            if case.get("origin") in ("own_output", "own_result") or case.get("user_feedback") or case.get("feedback_history"):
                continue
            if any(e.get("kind") == "user_feedback" for e in case.get("evidence", [])):
                continue
            cases.append(case)
        doc["cases"] = cases
        refs = {f"{c['id']}/{e['id']}" for c in cases for e in c["evidence"]}
        doc["methods"] = [m for m in doc["methods"] if set(m.get("supports", []) + m.get("counterexamples", [])) <= refs]
        return doc

    def context(self, run_id, role, intent="new", format="video", tags=(), case_ids=None, purpose="message"):
        if not isinstance(tags, (list, tuple)) or any(not isinstance(t, str) for t in tags):
            raise ValueError("Tags must be a text list")
        if case_ids is not None and (not isinstance(case_ids, (list, tuple, set)) or any(not isinstance(cid, str) for cid in case_ids)):
            raise ValueError("Case IDs must be a text list")
        if role not in ("orchestrator", "director", "material", "writer", "reviewer"):
            raise ValueError("Unknown creative role")
        if intent not in ("new", "continue", "feedback", "pause", "edit", "status") or format not in ("video", "photo", "none"):
            raise ValueError("Unsupported creative intent/format")
        if purpose not in (*core.PURPOSES, "review"):
            raise ValueError("Unknown learning purpose")
        root = self._run_root(run_id)
        raw, original = self._read()
        reviewer = role == "reviewer" or purpose == "review"
        doc = self._review_library(original) if reviewer else original
        effective_purpose = "message" if purpose == "review" else purpose
        seeds = core.select(doc, tags, effective_purpose) if case_ids is None else [c for c in doc["cases"] if c["id"] in case_ids]
        if case_ids is not None and set(case_ids) != {c["id"] for c in seeds}:
            raise ValueError("Requested cases are unavailable for this role")
        if any(effective_purpose not in c.get("eligible_for", []) for c in seeds):
            raise ValueError("Requested case does not qualify for this purpose")
        active = intent in ("new", "continue", "feedback", "edit") and format != "none"
        selected_references = []
        if active and not reviewer:
            from .references import reference_state, fingerprint
            selected = reference_state(self, run_id)['references']
            selected_ids = {s['case_id'] for s in selected}
            doc = copy.deepcopy(doc)
            by_id = {c['id']: c for c in doc['cases']}
            for selection in selected:
                # A selected source may also appear as a method dependency.
                # Freeze that content and narrow its qualification there too.
                case = by_id.get(selection['case_id'])
                if (selection['status'] != 'ready' or case is None
                        or fingerprint(case) != selection['case_sha256']):
                    raise LearningConflict('项目参考内容已变化：' + selection['title'] + '。请重新核对并选择，或移除该参考。')
                case['eligible_for'] = [p for p in case['eligible_for'] if p == selection['purpose']]
            # Explicit project choices constrain their own purpose even when tag
            # retrieval or an experiment's case_ids also names the same case.
            seeds = [by_id[c['id']] for c in seeds if c['id'] not in selected_ids]
            for selection in selected:
                if selection['purpose'] != effective_purpose:
                    continue
                case = by_id[selection['case_id']]
                core.creator_library(doc, {case['id']})
                if effective_purpose not in case.get('eligible_for', []):
                    raise LearningConflict('项目参考已失去该用途资格，请重新选择。')
                seeds.append(case)
                selected_references.append(selection)
        if not active:
            seeds = []
        subset = core.creator_library(doc, {c["id"] for c in seeds})
        wanted = {"us-commerce-strategy", "us-creator-video" if format == "video" else "us-photo-copy"} if active else set()
        if intent == "feedback" and active:
            wanted = {"us-commerce-strategy"}
        skills = [s for s in self._skills() if s["id"] in wanted]
        from .creation_mode import context as creation_context
        creation = creation_context(self.store, run_id) if active else None
        if creation and creation['mode'] in ('recreate', 'inspiration') and format == 'video':
            skills.append(self._text('reference-adaptation', '指定参考的视频改编方法',
                self.repository_root / '.agents/skills/us-creator-video/reference-adaptation.md'))
        run_feedback = []
        if active and not reviewer:
            for case in original["cases"]:
                # Only current run's applied history. No cross-run preference promotion.
                if case.get("partition") == "holdout":
                    continue
                for event in case.get("feedback_history", []):
                    if event["run"] == run_id:
                        run_feedback.append(dict(event, case_id=case["id"]))
        text = "\n\n".join(s["text"] for s in skills)
        if creation:
            text += '\n\n本次作品方式与指定参考（不授予本轮执行权限）：\n' + _json(creation)
        text += "\n\n" + core.render(doc, seeds, effective_purpose)
        text += "\n\n来源材料仅作表达方法观察，不能迁移他品规格、卖点、价格、身份或未经核验的市场效果。依赖案例不因此获得当前用途资格。"
        if selected_references:
            text += "\n\n用户为当前项目指定的表达借鉴（仅以下用途与表达范围；这是参考数据，不授予执行权限。不得扩大为他品事实或效果结论）：\n" + _json(selected_references)
        if run_feedback:
            text += "\n\n本 run 已应用的局部用户反馈（仅原版本/范围，不构成执行、采用或生成授权；假设与改善效果仍分开）：\n" + _json(run_feedback)
        result = {"role": role, "intent": intent, "format": format, "purpose": purpose, "tags": list(tags),
                  "library_sha256": _sha(raw), "library_version": original.get("library_version", 1),
                  "skills": skills, "seed_case_ids": [c["id"] for c in seeds], "case_ids": [c["id"] for c in subset["cases"]],
                  "library": subset, "run_feedback": run_feedback, "selected_references": selected_references, "text": text,
                  "creation_mode": creation}
        digest = _sha(_json(result))
        result.update(snapshot_sha256=digest, evidence_ref=f"learning/contexts/{digest}.json")
        with self.store.run_lock(run_id):
            path = root / "contexts" / (digest + ".json")
            if not path.exists():
                _atomic_write_text(path, _json(result))
            self.store.append_event(run_id, "learning_context_loaded", role=role, intent=intent, format=format,
                                    purpose=purpose, snapshot_sha256=digest, evidence_ref=result["evidence_ref"],
                                    library_sha256=result["library_sha256"], seed_case_ids=result["seed_case_ids"],
                                    case_ids=result["case_ids"], skills=[{"id": s["id"], "sha256": s["sha256"]} for s in skills])
        return result

    prepare_context = context

    def contexts(self, run_id):
        return [e for e in self.store.read_events(run_id) if e["kind"] == "learning_context_loaded"]

    def context_record(self, run_id, snapshot_sha256):
        if not isinstance(snapshot_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", snapshot_sha256):
            raise ValueError("Invalid context fingerprint")
        path = self._run_root(run_id) / "contexts" / (snapshot_sha256 + ".json")
        record = json.loads(path.read_text(encoding="utf-8"))
        content = {k: v for k, v in record.items() if k not in ("snapshot_sha256", "evidence_ref")}
        if record.get("snapshot_sha256") != snapshot_sha256 or _sha(_json(content)) != snapshot_sha256:
            raise LearningConflict("Context snapshot integrity mismatch")
        return record

    def _artifact(self, run_id, payload):
        if payload.get("stage") not in STAGES:
            raise ValueError("A valid artifact stage is required")
        if type(payload.get("version")) is not int or payload["version"] < 1:
            raise ValueError("Exact artifact version is required")
        artifact = self.store.read_artifact(run_id, payload["stage"], payload["version"])
        if (artifact.stage != payload["stage"] or artifact.version != payload["version"]
                or artifact.filename != f"{payload['stage']}-v{payload['version']}.md"):
            raise LearningConflict("Artifact identity does not match the requested run version")
        if artifact.sha256 != payload.get("artifact_sha256") or not self.store.verify_artifact(run_id, artifact.stage, artifact.version):
            raise LearningConflict("Artifact fingerprint mismatch")
        return artifact

    def _feedback_payload(self, run_id, payload, actor, current_user_message):
        if not isinstance(payload, dict):
            raise ValueError("Feedback must be an object")
        if payload.get("improvement_effect") is not None:
            raise ValueError("Feedback cannot assert an unverified improvement")
        payload = copy.deepcopy(payload)
        artifact = self._artifact(run_id, payload)
        quote = payload.get("quote", "")
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError("Exact user quote is required")
        if actor == "agent":
            if current_user_message is not None and (not isinstance(current_user_message, str) or quote not in current_user_message):
                raise ValueError("Quote is absent from the current user message")
            chat = self.store.read_chat(run_id)
            index = payload.get("source_chat")
            if index is None:
                matches = [i for i, c in enumerate(chat) if c["role"] == "user" and quote in c["text"]]
                if not matches:
                    raise ValueError("Quote has no real user chat source")
                index = matches[-1]
            if type(index) is not int or not 0 <= index < len(chat) or chat[index]["role"] != "user" or quote not in chat[index]["text"]:
                raise ValueError("Quote does not match the specified user chat")
            source = {"kind": "user_chat", "index": index, "sha256": _sha(_json(chat[index])), "at": chat[index]["at"]}
        elif actor == "user":
            source = {"kind": "explicit_user_input", "sha256": _sha(quote), "at": utcnow()}
        else:
            raise ValueError("Unknown proposal actor")
        payload.update(quote=quote, scope=payload.get("scope", "user_preference"), action=payload.get("action", "diagnose"),
                       observation=payload.get("observation") or quote, hypothesis=payload.get("hypothesis"),
                       source_identity=source, artifact=artifact.to_dict())
        return payload

    def _candidate(self, doc, run_id, kind, payload):
        result = copy.deepcopy(doc)
        if kind == "feedback":
            artifact = payload["artifact"]
            cid = payload.get("case_id") or "studio-" + _sha(f"{run_id}:{artifact['stage']}:{artifact['version']}:{artifact['sha256']}")[:24]
            case = next((c for c in result["cases"] if c["id"] == cid), None)
            if case is None:
                if payload.get("case_id"):
                    raise ValueError("Unknown feedback case")
                key = "studio-artifact:" + _sha(f"{run_id}:{artifact['stage']}:{artifact['version']}:{artifact['sha256']}")
                case = {"id": cid, "market": "US", "title": f"Studio {run_id} · {artifact['stage']} v{artifact['version']}",
                        "source_key": key, "source_url": None, "origin": "own_output", "platform": "studio",
                        "comparison_group": "studio-artifact:" + artifact["sha256"], "partition": "pending", "tags": ["studio", artifact["stage"]],
                        "evidence": [{"id": "artifact", "kind": "request_text", "source": f"studio:{run_id}/{artifact['filename']}#{artifact['sha256']}",
                                      "observation": "准确版本的本方策划产物；不是已验证视频表现或市场效果。"}],
                        "eligible_for": [], "strategy": {}, "limits": ["仅本 run 的用户反馈可回流；未校准，不进入跨 run 案例召回。"],
                        "user_feedback": None, "market_effect": None,
                        "studio_artifact": {"run": run_id, "stage": artifact["stage"], "version": artifact["version"], "sha256": artifact["sha256"]}}
                result = core.register(result, case)
            source = payload["source_identity"]
            event = {"run": run_id, "artifact_version": f"{artifact['stage']}-v{artifact['version']}@{artifact['sha256']}",
                     "source_event": f"{source['kind']}:{source.get('index', '')}:{source['sha256']}",
                     "source": f"studio:{run_id}/{source['kind']}:{source.get('index', '')}#{source['sha256']}",
                     "recorded_at": source["at"][:10], "quote": payload["quote"], "scope": payload["scope"],
                     "action": payload["action"], "observation": payload["observation"], "hypothesis": payload["hypothesis"],
                     "improvement_effect": None}
            return core.record_feedback(result, cid, event)
        if kind == "source":
            if not isinstance(payload.get("url"), str) or not isinstance(payload.get("title"), str) or not payload["title"].strip():
                raise ValueError("Source title and resolved public video URL are required")
            key = core.source_key(url=payload["url"])
            if not isinstance(payload.get("tags", []), list) or any(not isinstance(t, str) for t in payload.get("tags", [])):
                raise ValueError("Source tags must be a text list")
            cid = payload.get("id") or "reference-" + _sha(key)[:24]
            if not isinstance(cid, str):
                raise ValueError("Source id must be text")
            item = core.candidate(cid, payload["title"], payload.get("tags", []), url=payload["url"])
            result = core.register(result, item)
        elif kind == "method":
            action = payload.get("action")
            if action not in ("keep", "revise", "retire") or not isinstance(payload.get("rationale"), str) or not payload["rationale"].strip():
                raise ValueError("Method action and rationale are required")
            method = next((m for m in result["methods"] if m["id"] == payload.get("method_id")), None)
            if method is None:
                raise ValueError("Unknown method")
            changes = payload.get("changes", {})
            if not isinstance(changes, dict) or changes.keys() - {"when", "decision", "countercondition", "supports", "counterexamples"}:
                raise ValueError("Unsupported method changes; status cannot be promoted")
            if action != "revise" and changes:
                raise ValueError("Only revise accepts method changes")
            for name, value in changes.items():
                if name in ("supports", "counterexamples"):
                    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                        raise ValueError("Method evidence references must be text lists")
                elif not isinstance(value, str) or not value.strip():
                    raise ValueError("Method descriptions must be nonempty text")
            if action == "revise":
                method.update(changes)
                method["status"] = "hypothesis"
            elif action == "retire":
                method["status"] = "retired"
        elif kind == "observation":
            case = next((c for c in result["cases"] if c["id"] == payload.get("case_id")), None)
            if case is None or case["partition"] not in ("pending", "calibration"):
                raise ValueError("Only pending/calibration cases accept learning observations")
            purpose = payload.get("purpose", "message")
            if purpose not in ("message", "storyboard"):
                raise ValueError("Observation cannot qualify performance")
            for field in ("observation", "source", "scope"):
                if not isinstance(payload.get(field), str) or not payload[field].strip():
                    raise ValueError("Observation, source and exact scope are required")
            if not isinstance(payload.get("strategy"), dict) or not payload["strategy"] or any(not isinstance(v, str) or not v.strip() for v in payload["strategy"].values()):
                raise ValueError("An explicit expression strategy is required")
            if not isinstance(payload.get("limits"), list) or not payload["limits"] or any(not isinstance(s, str) or not s.strip() for s in payload["limits"]):
                raise ValueError("Evidence limitations are required")
            if case.get("origin") == "own_output" and (purpose != "message" or not case.get("feedback_history")):
                raise ValueError("Own output requires real user feedback and can qualify message only")
            evidence_id = "observation-" + _sha(_json(payload))[:24]
            if any(e["id"] == evidence_id for e in case["evidence"]):
                return result
            case["evidence"].append({"id": evidence_id, "kind": "historical_report", "source": payload["source"],
                                     "observation": payload["observation"], "scope": payload["scope"],
                                     "reviewer": "explicit_user_observation", "reviewed_at": payload["recorded_at"]})
            case["partition"] = "calibration"
            case["eligible_for"] = list(dict.fromkeys(case.get("eligible_for", []) + [purpose]))
            case["strategy"] = copy.deepcopy(payload["strategy"])
            case["limits"] = list(dict.fromkeys(case.get("limits", []) + payload["limits"] + ["用户登记的观察，不代表助手实际看听或已验证市场效果。"] ))
            if "tags" in payload:
                if not isinstance(payload["tags"], list) or any(not isinstance(t, str) for t in payload["tags"]):
                    raise ValueError("Tags must be text list")
                case["tags"] = payload["tags"]
        else:
            raise ValueError("Unknown proposal type")
        result["library_version"] = result.get("library_version", 1) + 1
        errors = core.validate(result)
        if errors:
            raise ValueError("; ".join(errors))
        return result

    def _propose(self, run_id, kind, payload, actor):
        if not isinstance(payload, dict):
            raise ValueError("Proposal payload must be an object")
        root = self._run_root(run_id)
        raw, before = self._read()
        after = self._candidate(before, run_id, kind, payload)
        candidate = _library_text(after, raw)
        # Explicit user submissions are deterministic across transport retries.
        identity_payload = copy.deepcopy(payload)
        if kind == "feedback" and identity_payload["source_identity"]["kind"] == "explicit_user_input":
            identity_payload["source_identity"].pop("at", None)
        ident = "proposal-" + _sha(_json({"kind": kind, "payload": identity_payload, "base": _sha(raw)}))[:32]
        directory = root / "proposals" / ident
        with self.store.run_lock(run_id):
            if (directory / "proposal.json").exists():
                return self.proposal(run_id, ident)
            diff = "".join(difflib.unified_diff(raw.decode("utf-8-sig").splitlines(True), candidate.lstrip("\ufeff").splitlines(True), fromfile="canonical-before.json", tofile="proposed-after.json"))
            proposal = {"id": ident, "type": kind, "status": "pending", "actor": actor, "run_id": run_id,
                        "created_at": utcnow(), "payload": payload, "base_library_sha256": _sha(raw),
                        "candidate_sha256": _sha(candidate), "diff": diff, "diff_sha256": _sha(diff)}
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "before.json").write_bytes(raw)
            _atomic_write_text(directory / "after.json", candidate)
            _atomic_write_text(directory / "proposal.json", _json(proposal))
            self.store.append_event(run_id, "learning_proposed", proposal_id=ident, proposal_type=kind, actor=actor,
                                    proposal_sha256=_sha(_json(proposal)), base_library_sha256=_sha(raw), candidate_sha256=_sha(candidate),
                                    user_quote=payload.get("quote") if actor == "user" else None)
            return proposal

    def propose_feedback(self, run_id, payload, current_user_message=None, actor="agent"):
        return self._propose(run_id, "feedback", self._feedback_payload(run_id, payload, actor, current_user_message), actor)

    propose = propose_feedback

    def propose_source(self, run_id, payload, actor="user"):
        if actor not in ("user", "agent"):
            raise ValueError("Unknown proposal actor")
        return self._propose(run_id, "source", copy.deepcopy(payload), actor)

    def propose_method(self, run_id, payload, actor="user"):
        if actor not in ("user", "agent"):
            raise ValueError("Unknown proposal actor")
        return self._propose(run_id, "method", copy.deepcopy(payload), actor)

    def propose_observation(self, run_id, payload, actor="user"):
        if actor != "user":
            raise ValueError("Learning eligibility requires an explicit user observation")
        if not isinstance(payload, dict):
            raise ValueError("Observation must be an object")
        payload = dict(copy.deepcopy(payload), recorded_at=utcnow()[:10])
        return self._propose(run_id, "observation", payload, actor)

    def _proposal_directory(self, run_id, proposal_id):
        if not isinstance(proposal_id, str) or not re.fullmatch(r"proposal-[a-f0-9]{32}", proposal_id):
            raise ValueError("Invalid proposal id")
        return self._run_root(run_id) / "proposals" / proposal_id

    @staticmethod
    def _read_proposal(directory):
        if not (directory / "proposal.json").is_file():
            raise ValueError("Unknown learning proposal")
        return json.loads((directory / "proposal.json").read_text(encoding="utf-8"))

    def proposal(self, run_id, proposal_id):
        directory = self._proposal_directory(run_id, proposal_id)
        proposal = self._read_proposal(directory)
        if (directory / "applied.json").exists():
            proposal.update(json.loads((directory / "applied.json").read_text(encoding="utf-8")))
        return proposal

    def proposals(self, run_id):
        directory = self._run_root(run_id) / "proposals"
        return [self.proposal(run_id, p.name) for p in sorted(directory.glob("proposal-*")) if (p / "proposal.json").is_file()]

    def apply(self, run_id, proposal_id, expected_library_sha256):
        """User-route-only CAS commit; append-only proposal/audit files survive retries."""
        directory = self._proposal_directory(run_id, proposal_id)
        with _library_lock(self.library_path), self.store.run_lock(run_id):
            proposal = self._read_proposal(directory)
            original_hash = _sha(_json(proposal))
            events = self.store.read_events(run_id)
            if not any(e["kind"] == "learning_proposed" and e.get("proposal_id") == proposal_id and e.get("proposal_sha256") == original_hash for e in events):
                raise LearningConflict("Proposal integrity mismatch")
            if expected_library_sha256 != proposal["base_library_sha256"]:
                raise LearningConflict("Expected library hash does not match the reviewed proposal")
            if (directory / "applied.json").exists():
                return self.proposal(run_id, proposal_id)
            before = (directory / "before.json").read_bytes()
            candidate = (directory / "after.json").read_bytes()
            if _sha(before) != proposal["base_library_sha256"] or _sha(candidate) != proposal["candidate_sha256"] or _sha(proposal["diff"]) != proposal["diff_sha256"]:
                raise LearningConflict("Reviewed candidate integrity mismatch")
            if proposal["type"] == "feedback":
                self._artifact(run_id, proposal["payload"])
                source = proposal["payload"]["source_identity"]
                if source["kind"] == "user_chat":
                    chat = self.store.read_chat(run_id)
                    index = source["index"]
                    if index >= len(chat) or _sha(_json(chat[index])) != source["sha256"]:
                        raise LearningConflict("Original user feedback source changed")
            rebuilt = self._candidate(json.loads(before.decode("utf-8-sig")), run_id, proposal["type"], proposal["payload"])
            if _sha(_library_text(rebuilt, before)) != proposal["candidate_sha256"]:
                raise LearningConflict("Proposal no longer reproduces its reviewed candidate")
            current = _sha(self.library_path.read_bytes())
            if current not in (expected_library_sha256, proposal["candidate_sha256"]):
                raise LearningConflict("Library changed; prepare a new proposal against its current version")
            # Crash recovery: candidate already installed but receipt not written.
            if current != proposal["candidate_sha256"]:
                _atomic_write_text(self.library_path, candidate.decode("utf-8"))
            receipt = {"status": "applied", "applied_at": utcnow(), "applied_library_sha256": proposal["candidate_sha256"]}
            _atomic_write_text(directory / "applied.json", _json(receipt))
            self.store.append_event(run_id, "learning_applied", proposal_id=proposal_id, proposal_type=proposal["type"],
                                    before_sha256=proposal["base_library_sha256"], after_sha256=proposal["candidate_sha256"])
            return dict(proposal, **receipt)
