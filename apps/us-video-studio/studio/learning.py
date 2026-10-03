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

from .models import PipelineError, utcnow, STAGES
from .store import _atomic_write_text

REPOSITORY = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("studio_creative_learning", REPOSITORY / "scripts/creative_learning.py")
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
SKILLS = ("us-commerce-strategy", "us-creator-video", "us-photo-copy")
LEARNING_SKILL = "us-creative-learning"
METHOD_CARDS = {
    "visual-design": {
        "title": "US 人物、场景与共同视觉设计", "file": "apps/us-video-studio/docs/methods/visual-design.md",
        "source": ".agents/skills/us-commerce-strategy/SKILL.md",
        "skills": ("us-commerce-strategy", "us-creator-video", "us-photo-copy"),
        "roles": ("orchestrator", "director", "material", "writer"),
        "tools": ("read_artifact", "read_method_document"),
        "default_formats": ("video", "photo"),
    },
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
        if not set(spec.get('default_formats', ())) <= {'video', 'photo'}:
            errors.append(f'{ident}: unknown default format')
        for role in spec['roles']:
            if role not in ('orchestrator', 'director', 'material', 'writer', 'reviewer'):
                errors.append(f'{ident}: unknown role {role}')
        for tool in spec['tools']:
            if tool not in REGISTRY or not set(spec['roles']) & set(REGISTRY[tool].roles):
                errors.append(f'{ident}: unavailable tool {tool}')
    for ident in (*SKILLS, LEARNING_SKILL):
        path = root / '.agents/skills' / ident / 'SKILL.md'
        if path.is_file():
            links_exist(path, ident)
        else:
            errors.append(f'{ident}: missing Skill entry')
    return errors


class LearningConflict(ValueError):
    """The reviewed proposal or canonical library no longer matches."""


class LegacyLibraryUnavailable(LearningConflict):
    """A legacy creative Run has no complete, hash-matching library backup."""


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

    def _historical_library(self, wanted_sha256):
        """Find exact bytes retained by an applied, audited learning proposal."""
        for run in self.store.list_runs():
            if run.kind != 'learning':
                continue
            events = self.store.read_events(run.run_id)
            applied = {event.get('proposal_id'): event for event in events
                       if event['kind'] == 'learning_applied'}
            root = self._run_root(run.run_id) / 'proposals'
            for directory in sorted(root.glob('proposal-*')):
                if directory.name not in applied or not (directory / 'applied.json').is_file():
                    continue
                try:
                    proposal = self._read_proposal(directory)
                    receipt = json.loads((directory / 'applied.json').read_text(encoding='utf-8'))
                    event = applied[directory.name]
                    if (event.get('proposal_sha256') and event['proposal_sha256'] != _sha(_json(proposal))
                            or event.get('before_sha256') != proposal.get('base_library_sha256')
                            or event.get('after_sha256') != proposal.get('candidate_sha256')
                            or receipt.get('applied_library_sha256') != proposal.get('candidate_sha256')):
                        continue
                    if proposal.get('type') not in {'migration', 'delta', 'feedback', 'source', 'method', 'observation'}:
                        continue
                    for filename, field in (('before.json', 'base_library_sha256'),
                                            ('after.json', 'candidate_sha256')):
                        if proposal.get(field) != wanted_sha256:
                            continue
                        raw = (directory / filename).read_bytes()
                        if _sha(raw) != wanted_sha256:
                            continue
                        doc = json.loads(raw.decode('utf-8-sig'))
                        if not core.validate(doc):
                            return raw, doc
                except (OSError, ValueError, UnicodeError, KeyError):
                    continue
        return None

    def _read_for_run(self, run_id):
        """Pin shared knowledge for a creative Run, refreshing only on its own apply.

        The pin lives with external Run data. A later learning Run may update the
        canonical library, but it cannot silently change this Run's next role
        context. An explicit same-Run applied proposal remains visible here.
        """
        if run_id is None or self.store.read_run(run_id).kind != "creative":
            return self._read()
        with self.store.run_lock(run_id):
            path = self._run_root(run_id) / "library-pin.json"
            applied = next((event for event in reversed(self.store.read_events(run_id))
                            if event["kind"] == "learning_applied"), None)
            applied_seq = applied["seq"] if applied else 0
            if path.is_file():
                pin = json.loads(path.read_text(encoding="utf-8"))
                if (not isinstance(pin, dict) or not isinstance(pin.get("raw_text"), str)
                        or _sha(pin["raw_text"]) != pin.get("sha256")
                        or type(pin.get("applied_seq")) is not int):
                    raise LearningConflict("Creative learning pin is damaged")
                if pin["applied_seq"] > applied_seq:
                    raise LearningConflict("Creative learning pin is ahead of Run history")
                if pin["applied_seq"] == applied_seq:
                    raw = pin["raw_text"].encode("utf-8")
                    doc = json.loads(raw.decode("utf-8-sig"))
                    errors = core.validate(doc)
                    if errors:
                        raise LearningConflict("Creative learning pin is invalid: " + "; ".join(errors))
                    return raw, doc
            if applied:
                proposal_id = applied.get("proposal_id")
                directory = self._proposal_directory(run_id, proposal_id)
                raw = (directory / "after.json").read_bytes()
                if _sha(raw) != applied.get("after_sha256"):
                    raise LearningConflict("Applied learning proposal bytes changed")
                doc = json.loads(raw.decode("utf-8-sig"))
                errors = core.validate(doc)
                if errors:
                    raise LearningConflict("Applied learning library is invalid: " + "; ".join(errors))
            else:
                raw, doc = self._read()
                if not path.exists():
                    previous_context = next((event for event in reversed(self.store.read_events(run_id))
                                             if event["kind"] == "learning_context_loaded"
                                             and event.get("library_sha256")), None)
                    if previous_context and previous_context["library_sha256"] != _sha(raw):
                        restored = self._historical_library(previous_context['library_sha256'])
                        if restored is None:
                            raise LegacyLibraryUnavailable(
                                "旧项目使用过另一版学习库，但当时未保存完整库快照。"
                                "原库备份也无法按哈希核对；只可回看旧上下文，后续采用新库须明确刷新。")
                        raw, doc = restored
            _atomic_write_text(path, _json({"raw_text": raw.decode("utf-8"),
                                            "sha256": _sha(raw), "applied_seq": applied_seq}))
            self.store.append_event(run_id, "learning_library_pinned",
                                    library_sha256=_sha(raw), library_version=doc.get("library_version", 1),
                                    applied_seq=applied_seq)
            return raw, doc

    def _run_root(self, run_id):
        self.store.read_run(run_id)
        return self.store.run_dir(run_id) / "learning"

    def _legacy_readonly_context(self, run_id, role, intent, format, purpose):
        """Allow status follow-up from old snapshots without inventing library bytes."""
        prior = None
        for event in reversed(self.contexts(run_id)):
            try:
                record = self.context_record(run_id, event['snapshot_sha256'])
            except (ValueError, OSError, KeyError, UnicodeError):
                continue
            if record.get('library_sha256') == event.get('library_sha256'):
                prior = record
                break
        previous_sha = prior['library_sha256'] if prior else next((
            event['library_sha256'] for event in reversed(self.contexts(run_id))
            if event.get('library_sha256')), None)
        note = ('旧学习库完整字节不可核对。本轮只回看已保存项目、产物与历史上下文；'
                '不得写新稿、调用观察、送审或沿用旧批准。要让后续任务使用当前库，须由用户明确刷新。')
        result = {'role': role, 'intent': intent, 'format': format, 'purpose': purpose,
                  'library_sha256': previous_sha, 'library_version': prior.get('library_version') if prior else None,
                  'skills': [], 'seed_case_ids': [], 'case_ids': [], 'library': None,
                  'method_directory': None, 'run_feedback': [], 'selected_references': [],
                  'text': note + ('\n\n此前已保存的上下文，仅供回放：\n' + prior['text'] if prior else ''),
                  'historical_readonly': True}
        digest = _sha(_json(result))
        result.update(snapshot_sha256=digest, evidence_ref=f'learning/contexts/{digest}.json')
        with self.store.run_lock(run_id):
            path = self._run_root(run_id) / 'contexts' / (digest + '.json')
            if not path.exists():
                _atomic_write_text(path, _json(result))
            self.store.append_event(run_id, 'learning_context_replayed',
                                    library_sha256=previous_sha, snapshot_sha256=digest,
                                    evidence_ref=result['evidence_ref'])
        return result

    def pin_status(self, run_id):
        """Read-only comparison for an explicit future-task library refresh."""
        if self.store.read_run(run_id).kind != 'creative':
            raise ValueError('Only a creative Run can refresh its learning library')
        current, _ = self._read()
        current_sha = _sha(current)
        path = self._run_root(run_id) / 'library-pin.json'
        if path.is_file():
            try:
                pin = json.loads(path.read_text(encoding='utf-8'))
                previous_sha = pin.get('sha256')
                if (not isinstance(pin.get('raw_text'), str) or
                        _sha(pin['raw_text']) != previous_sha or
                        type(pin.get('applied_seq')) is not int or
                        core.validate(json.loads(pin['raw_text'].lstrip('\ufeff')))):
                    raise ValueError('invalid pin')
            except (OSError, ValueError, TypeError, UnicodeError) as exc:
                raise LearningConflict('Creative learning pin is damaged') from exc
            source = 'pinned'
        else:
            previous_sha = next((event.get('library_sha256')
                for event in reversed(self.contexts(run_id)) if event.get('library_sha256')), None)
            source = 'historical_unpinned' if previous_sha else 'new'
        return {'source': source, 'run_library_sha256': previous_sha,
                'current_library_sha256': current_sha,
                'refresh_available': bool(previous_sha and previous_sha != current_sha),
                'historical_readonly': bool(source == 'historical_unpinned' and
                    previous_sha != current_sha and self._historical_library(previous_sha) is None)}

    def refresh_run_pin(self, run_id, expected_run_sha256, expected_current_sha256, request_id):
        """User-only choice to use the current library in later creative turns."""
        if (not isinstance(request_id, str) or
                not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', request_id)):
            raise ValueError('Invalid learning refresh request ID')
        with _library_lock(self.library_path), self.store.run_lock(run_id):
            events = self.store.read_events(run_id)
            prior = next((event for event in reversed(events)
                          if event['kind'] == 'learning_library_refreshed' and
                          event.get('request_id') == request_id), None)
            if prior:
                if (prior.get('from_sha256') != expected_run_sha256 or
                        prior.get('to_sha256') != expected_current_sha256):
                    raise LearningConflict('Refresh request ID was used for another library version')
                return self.pin_status(run_id)
            status = self.pin_status(run_id)
            if (status['run_library_sha256'] != expected_run_sha256 or
                    status['current_library_sha256'] != expected_current_sha256 or
                    not status['refresh_available']):
                raise LearningConflict('Learning library changed; review the current version again')
            raw, doc = self._read()
            applied_seq = max((event['seq'] for event in events
                               if event['kind'] == 'learning_applied'), default=0)
            _atomic_write_text(self._run_root(run_id) / 'library-pin.json', _json({
                'raw_text': raw.decode('utf-8'), 'sha256': _sha(raw), 'applied_seq': applied_seq}))
            self.store.append_event(run_id, 'learning_library_refreshed',
                                    from_sha256=expected_run_sha256,
                                    to_sha256=_sha(raw), library_version=doc.get('library_version', 1),
                                    request_id=request_id)
            return self.pin_status(run_id)

    def _text(self, ident, title, path):
        raw = path.read_bytes()
        digest = _sha(raw)
        text = raw.decode("utf-8-sig")
        return {"id": ident, "title": title, "text": text, "content": text,
                "sha256": digest, "version": digest[:12]}

    def _skills(self, include_learning=False):
        names = (*SKILLS, LEARNING_SKILL) if include_learning else SKILLS
        return [self._text(s, s, self.repository_root / ".agents/skills" / s / "SKILL.md") for s in names]

    def skill_catalog(self, role="orchestrator", run_id=None):
        """Model-safe directory. Never exposes the human audit catalog."""
        from .tools.registry import REGISTRY
        if role not in ('orchestrator', 'director', 'material', 'writer', 'reviewer'):
            raise ValueError('Unknown creative role')
        learning_run = run_id is not None and getattr(self.store.read_run(run_id), "kind", "creative") == "learning"
        return {"skills": [{k: s[k] for k in ("id", "title", "sha256", "version")} for s in self._skills(learning_run and role == "orchestrator")],
                "documents": ([{"id": s, "title": s} for s in ((*SKILLS, LEARNING_SKILL) if learning_run and role == "orchestrator" else SKILLS)]
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
                "library": {"sha256": _sha(raw), "version": doc.get("library_version", 1), "schema_version": doc["schema_version"]},
                "skills": self._skills(), "documents": [{"id": k, "title": v[0]} for k, v in DOCUMENTS.items()],
                "cases": [dict(c, case_sha256=fingerprint(c)) for c in doc["cases"]], "methods": doc["methods"],
                "preferences": [dict(p, application_status='stored_only' if p.get('scope') == 'brand'
                                     else 'scope_matched_only') for p in doc.get('preferences', [])],
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
        elif name in (*SKILLS, LEARNING_SKILL, "creative-package-contract"):
            if name == LEARNING_SKILL and (role != "orchestrator" or run_id is None or getattr(self.store.read_run(run_id), "kind", "creative") != "learning"):
                raise ValueError("Learning Skill is limited to the learning orchestrator")
            result = self.document(name) if name == "creative-package-contract" else next(s for s in self._skills(name == LEARNING_SKILL) if s["id"] == name)
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
        try:
            raw, original = self._read_for_run(run_id)
        except LegacyLibraryUnavailable:
            if intent not in ('status', 'pause'):
                raise
            return self._legacy_readonly_context(run_id, role, intent, format, purpose)
        reviewer = role == "reviewer" or purpose == "review"
        doc = self._review_library(original) if reviewer else original
        effective_purpose = "message" if purpose == "review" else purpose
        seeds = core.select(doc, tags, effective_purpose) if case_ids is None else [c for c in doc["cases"] if c["id"] in case_ids]
        if case_ids is not None and set(case_ids) != {c["id"] for c in seeds}:
            raise ValueError("Requested cases are unavailable for this role")
        if any(effective_purpose not in c.get("eligible_for", []) for c in seeds):
            raise ValueError("Requested case does not qualify for this purpose")
        # A reviewer gets the artifact, upstream facts and ActiveBrief from its
        # own task. Producer skills and creative cases can anchor its verdict,
        # so they are available only through an explicit method-card read.
        active = intent in ("new", "continue", "feedback", "edit") and format != "none" and not reviewer
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
        output_skill = "us-creator-video" if format == "video" else "us-photo-copy"
        # The orchestrator owns the complete creative decision. Specialists
        # receive their stage's method and pinned upstream decision. Stable
        # role-scoped method cards can be injected without loading every Skill.
        skills_by_role = {"orchestrator": {"us-commerce-strategy", output_skill},
                          "director": {"us-commerce-strategy"},
                          "material": set(),
                          "writer": {output_skill}}
        wanted = skills_by_role.get(role, set()) if active else set()
        if intent == "feedback" and active:
            wanted = {"us-commerce-strategy"} if role == "orchestrator" else set()
        skills = [s for s in self._skills() if s["id"] in wanted]
        method_cards = []
        if active:
            for ident, spec in METHOD_CARDS.items():
                if role in spec['roles'] and format in spec.get('default_formats', ()):
                    card = self._text(ident, spec['title'], self.repository_root / spec['file'])
                    card['source'] = spec['source']
                    method_cards.append(card)
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
        text = "\n\n".join(s["text"] for s in (*skills, *method_cards))
        if creation:
            text += '\n\n本次作品方式与指定参考（不授予本轮执行权限）：\n' + _json(creation)
        if not reviewer:
            text += "\n\n" + core.render(doc, seeds, effective_purpose)
        method_directory = None
        if active:
            method_directory = self.method_summaries(run_id=run_id, purpose=effective_purpose,
                                                     include_trial=False, reviewer=reviewer)
            if method_directory["library_sha256"] != _sha(raw):
                raise LearningConflict("Learning library changed while preparing context; retry")
            text += "\n\n共享方法目录（仅为合资格短摘要，未表示本轮选用；结合任务、素材和排除条件判断，允许不用）：\n"
            text += _json(method_directory)
        if not reviewer:
            text += "\n\n来源材料仅作表达方法观察，不能迁移他品规格、卖点、价格、身份或未经核验的市场效果。依赖案例不因此获得当前用途资格。"
        else:
            text = "独立审核只依据本版产物、已确认的上游事实与有效简报；需要专项方法时按需读取登记方法卡。"
        if selected_references:
            text += "\n\n用户为当前项目指定的表达借鉴（仅以下用途与表达范围；这是参考数据，不授予执行权限。不得扩大为他品事实或效果结论）：\n" + _json(selected_references)
        if run_feedback:
            text += "\n\n本 run 已应用的局部用户反馈（仅原版本/范围，不构成执行、采用或生成授权；假设与改善效果仍分开）：\n" + _json(run_feedback)
        preferences = []
        if active and not reviewer:
            preferences = [copy.deepcopy(p) for p in original.get('preferences', [])
                           if (p.get('scope') == 'user' and p.get('owner') == 'workspace-user')
                           or (p.get('scope') == 'project' and p.get('owner') == run_id)]
            if preferences:
                text += ('\n\n明确范围内的用户创作偏好（原话数据，仅供表达选择；不是市场规律、'
                         '独立审核结论或执行授权，任务不适合时可以不用）：\n' + _json(preferences))
        result = {"role": role, "intent": intent, "format": format, "purpose": purpose, "tags": list(tags),
                  "library_sha256": _sha(raw), "library_version": original.get("library_version", 1),
                  "skills": skills, "method_cards": method_cards,
                  "seed_case_ids": [c["id"] for c in seeds], "case_ids": [c["id"] for c in subset["cases"]],
                  "library": subset, "method_directory": method_directory,
                  "run_feedback": run_feedback, "selected_references": selected_references,
                  "preferences": preferences, "text": text,
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
                                    case_ids=result["case_ids"], skills=[{"id": s["id"], "sha256": s["sha256"]} for s in skills],
                                    method_cards=[{"id": card['id'], "sha256": card['sha256']} for card in method_cards])
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
        if kind == "migration":
            return core.migrate_v1_to_v2(doc)
        if kind == "delta":
            return self._delta_candidate(doc, run_id, payload)
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
                if result.get("schema_version") == 2:
                    case["evidence"][0].update(source_ref=case["evidence"][0]["source"],
                                               observer_type="system", observation_kind="request_text",
                                               content=case["evidence"][0]["observation"], coverage="text artifact only",
                                               review_state="candidate", media_sha256=None, time_range=None)
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
                method["status"] = "trial" if result.get("schema_version") == 2 else "hypothesis"
            elif action == "retire":
                method["status"] = "retired"
            if result.get("schema_version") == 2 and action in ("revise", "retire"):
                method["parent_revision"] = method["revision"]
                method["revision"] += 1
                method["source_proposal"] = "legacy-method-proposal:" + _sha(_json(payload))
                method["support_refs"] = copy.deepcopy(method["supports"])
                method["counterexample_refs"] = copy.deepcopy(method.get("counterexamples", []))
                if action == "revise":
                    method["market_validation"] = "unknown"
                    method.pop("transfer_assessment", None)
                    method.pop("unverified_use_approved", None)
                    method["trial_scope"] = self._trial_scope(result, method)
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
            observation = {"id": evidence_id, "kind": "historical_report", "source": payload["source"],
                           "observation": payload["observation"], "scope": payload["scope"],
                           "reviewer": "explicit_user_observation", "reviewed_at": payload["recorded_at"]}
            if result.get("schema_version") == 2:
                observation.update(source_ref=payload["source"], observer_type="user",
                                   observation_kind="historical_report", content=payload["observation"],
                                   coverage=payload["scope"], review_state="user_accepted",
                                   media_sha256=None, time_range=None)
            case["evidence"].append(observation)
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

    @staticmethod
    def _user_discussion_sha(store, run_id):
        """Assistant completion after a proposal does not stale it; correction does."""
        from .learning_session import discussion_sha256
        return discussion_sha256(store.read_chat(run_id))

    @staticmethod
    def _trial_scope(doc, method):
        by_id = {case["id"]: case for case in doc["cases"]}
        scopes = []
        for ref in method.get("support_refs", method.get("supports", [])):
            case = by_id.get(ref.partition("/")[0])
            scopes.append(set(case.get("eligible_for", [])) if case and case.get("partition") == "calibration" else set())
        purposes = sorted(set.intersection(*scopes)) if scopes else []
        return {"market": "US", "purposes": purposes, "authorization": "user_applied_delta"}

    def _validate_delta_sources(self, run_id, payload):
        """Only exact model or user observations from this Run may enter a delta."""
        from .image_analysis import candidates
        from .learning_assets import verified_run_material
        from .learning_session import model_finding_observation

        records = {c["analysis_id"]: c for c in candidates(self.store, run_id)}
        human_records = None
        for operation in payload.get("operations", []):
            if isinstance(operation, dict) and operation.get('op') == 'preference.upsert':
                preference = operation.get('preference')
                if not isinstance(preference, dict):
                    raise ValueError('Preference must be an object')
                scope, owner = preference.get('scope'), preference.get('owner')
                if (scope == 'user' and owner != 'workspace-user'):
                    raise ValueError('User preference owner must be this workspace user')
                if scope == 'project':
                    if not isinstance(owner, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', owner):
                        raise ValueError('Preference project owner must be a creative Run ID')
                    try:
                        target = self.store.read_run(owner)
                    except PipelineError as exc:
                        raise ValueError('Preference project owner must exist') from exc
                    if target.kind != 'creative':
                        raise ValueError('Preference project owner must be a creative Run')
                if scope == 'brand' and (not isinstance(owner, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', owner)):
                    raise ValueError('Brand preference owner must be an explicit brand ID')
                ref = preference.get('source_ref')
                match = re.fullmatch(rf'user-chat:{re.escape(run_id)}:(\d+)', ref) if isinstance(ref, str) else None
                chat = self.store.read_chat(run_id)
                index = int(match.group(1)) if match else -1
                if (index < 0 or index >= len(chat) or chat[index].get('role') != 'user' or
                        not isinstance(preference.get('quote'), str) or not preference['quote'].strip() or
                        preference['quote'] not in chat[index].get('text', '')):
                    raise ValueError('Preference quote must match an exact user turn in this learning Run')
                continue
            if not isinstance(operation, dict) or operation.get("op") != "case.upsert":
                continue
            case = operation.get("case")
            if not isinstance(case, dict):
                continue  # Structural validator reports this.
            observations = case.get("evidence", [])
            if not isinstance(observations, list):
                continue
            if not observations:
                raise ValueError("case.upsert needs an exact observed finding; use source intake for an unobserved link")
            media = case.get('media')
            if media is not None:
                if not isinstance(media, dict):
                    raise ValueError('Case media must be a resource object')
                if not str(media.get('resource_id', '')).startswith(f'run-material:{run_id}:'):
                    raise ValueError('Case media must belong to this learning Run')
                verified_run_material(self.store, media.get('resource_id'), media.get('sha256'))
                if any(isinstance(item, dict) and item.get('media_sha256') != media['sha256']
                       for item in observations):
                    raise ValueError('Case media differs from observed source bytes')
            for observation in observations:
                if not isinstance(observation, dict):
                    continue
                source_ref = observation.get("source_ref")
                if isinstance(source_ref, str) and source_ref.startswith("material-observation:"):
                    if human_records is None:
                        from .material_observations import material_workspace
                        from .learning_session import human_finding_ref
                        human_records = {}
                        for material in material_workspace(self.store, run_id)["materials"]:
                            for record in material.get("observations", []):
                                if (record.get("source_kind") != "human_observation" or
                                        record.get("observer") != "human:workspace-user"):
                                    continue
                                for index, finding in enumerate(record.get("findings", [])):
                                    ref = human_finding_ref(material["name"], material["sha256"], record, index)
                                    human_records[ref] = (material, record, finding)
                    source = human_records.get(source_ref)
                    if source is None or observation.get("id") != source_ref:
                        raise ValueError("Human observation source is missing or changed in this Run")
                    material, record, finding = source
                    expected = {"source_name": material["name"], "media_sha256": material["sha256"],
                                "content": finding, "observer_type": "user", "observer": "human:workspace-user",
                                "observation_kind": "human_material_observation", "level": record["level"],
                                "coverage": record["coverage"], "limitations": record["limitations"],
                                "purpose": record["purpose"], "observed_at": record["observed_at"],
                                "review_state": "user_reported"}
                    if any(observation.get(field) != value for field, value in expected.items()):
                        raise ValueError("Human observation must match the exact reported finding and coverage")
                    if observation.get("time_range") is not None or observation.get("kind") == "full_av_review":
                        raise ValueError("Manual observation cannot gain a synthetic time range or full AV qualification")
                    if record["level"] == "transcript_only" and "storyboard" in case.get("eligible_for", []):
                        raise ValueError("Transcript-only observation cannot qualify storyboard use")
                    continue
                if not isinstance(source_ref, str) or not source_ref.startswith("analysis:"):
                    raise ValueError("LearningDelta observation must reference this Run's analysis ID")
                analysis_id = source_ref.partition(":")[2]
                candidate = records.get(analysis_id)
                if candidate is None or candidate.get("status") == "stale":
                    raise ValueError("LearningDelta analysis source is missing or stale")
                prefix = analysis_id + "/"
                ident = observation.get("id")
                if not isinstance(ident, str) or not ident.startswith(prefix):
                    raise ValueError("Observation ID must bind analysis ID and finding index")
                try:
                    index = int(ident[len(prefix):])
                except ValueError:
                    raise ValueError("Observation finding index is invalid") from None
                findings = candidate.get("findings", [])
                if index < 0 or index >= len(findings) or ident != f"{analysis_id}/{index}":
                    raise ValueError("Observation finding index does not exist")
                if (observation.get("media_sha256") != candidate.get("sha256") or
                        observation.get("content") != findings[index] or
                        observation.get("observation", findings[index]) != findings[index]):
                    raise ValueError("Observation must quote the exact finding and media version")
                if (observation.get("observer_type") != "model" or
                        observation.get("review_state") != "model_unreviewed" or
                        observation.get("kind") == "full_av_review"):
                    raise ValueError("Model analysis cannot claim human or full audiovisual review")
                expected = model_finding_observation(candidate, index)
                if (observation.get('observation_kind') != expected['observation_kind'] or
                        observation.get('coverage') != expected['coverage'] or
                        observation.get('limitations') != expected['limitations'] or
                        observation.get('kind', 'model_av_observation' if expected['observation_kind'] == 'model_audiovisual'
                                        else 'visual_sample') !=
                        ('model_av_observation' if expected['observation_kind'] == 'model_audiovisual'
                         else 'visual_sample')):
                    raise ValueError('Model observation kind and coverage must match saved input')
                timeline = next((t for t in candidate.get("timeline", []) if t.get("finding_index") == index), None)
                span = observation.get("time_range")
                if timeline is None:
                    if span is not None:
                        raise ValueError("Unlocalized finding cannot claim a time range")
                elif span not in ([timeline["start_seconds"], timeline["end_seconds"]],
                                  {"start_seconds": timeline["start_seconds"], "end_seconds": timeline["end_seconds"]}):
                    raise ValueError("Observation time range differs from source analysis")

    @staticmethod
    def _delta_candidate(doc, run_id, payload):
        if doc.get("schema_version") != 2:
            raise ValueError("LearningDelta requires a reviewed v2 migration first")
        operations = payload.get("operations")
        if not isinstance(operations, list) or len(operations) > 20:
            raise ValueError("LearningDelta operations must be a list of at most 20")
        declared_no_change = payload.get('no_change') is True or payload.get('outcome') == 'no_change'
        if operations and declared_no_change:
            raise ValueError('no_change cannot include LearningDelta operations')
        if not isinstance(payload.get("rationale"), str) or not payload["rationale"].strip():
            raise ValueError("LearningDelta rationale is required")
        if not operations:
            if payload.get("outcome") != "no_change" and payload.get("no_change") is not True:
                raise ValueError("Empty LearningDelta must explicitly say no_change")
            return copy.deepcopy(doc)
        result = copy.deepcopy(doc)
        touched = set()
        created_or_changed = set()
        for entry in operations:
            if not isinstance(entry, dict) or not isinstance(entry.get("op"), str):
                raise ValueError("LearningDelta operation must have an op")
            op = entry["op"]
            if op == "case.upsert":
                case = copy.deepcopy(entry.get("case"))
                if not isinstance(case, dict):
                    raise ValueError("case.upsert requires a case")
                # Session observations are intentionally model_unreviewed. A
                # confirmed delta may accept them for scoped use without ever
                # claiming that a human watched or listened to the source.
                for observation in case.get("evidence", []) if isinstance(case.get("evidence", []), list) else []:
                    if not isinstance(observation, dict):
                        continue
                    source_ref = str(observation.get("source_ref", ""))
                    if not source_ref.startswith(("analysis:", "material-observation:")):
                        continue
                    observation.setdefault("source", observation["source_ref"])
                    observation.setdefault("observation", observation.get("content"))
                    observation.setdefault("kind", ("historical_report" if source_ref.startswith("material-observation:")
                                                    else "model_av_observation" if observation.get("observation_kind") == "model_audiovisual"
                                                    else "visual_sample"))
                    if observation.get("review_state") == "model_unreviewed":
                        observation["source_review_state"] = "model_unreviewed"
                        observation["review_state"] = ("user_accepted" if case.get("partition") == "calibration"
                                                       else "candidate")
                    elif observation.get("review_state") == "user_reported":
                        observation["source_review_state"] = "user_reported"
                        observation["review_state"] = ("user_accepted" if case.get("partition") == "calibration"
                                                       else "candidate")
                cid = case.get("id")
                previous = next((c for c in result["cases"] if c.get("id") == cid), None)
                if previous is not None:
                    # Same source may gain observations; identity and qualification
                    # cannot be silently replaced by an agent-authored delta.
                    for field in ("source_key", "comparison_group", "partition", "eligible_for", "source_market"):
                        if case.get(field, previous.get(field)) != previous.get(field):
                            raise ValueError(f"case.upsert cannot change {field}")
                    if case.get('media', previous.get('media')) != previous.get('media'):
                        raise ValueError('case.upsert cannot change exact media identity')
                    merged = copy.deepcopy(previous)
                    additions = case.get("evidence", [])
                    if not isinstance(additions, list):
                        raise ValueError("case evidence must be a list")
                    previous_sha = (previous.get('media') or {}).get('sha256')
                    if previous_sha and any(isinstance(item, dict) and
                                            item.get('media_sha256') != previous_sha
                                            for item in additions):
                        raise ValueError('Existing case observation differs from original media')
                    known = {e["id"]: e for e in merged["evidence"]}
                    for observation in additions:
                        if not isinstance(observation, dict) or not observation.get("id"):
                            raise ValueError("case evidence needs an id")
                        if observation["id"] in known and known[observation["id"]] != observation:
                            raise ValueError("Existing observation is immutable; add a revision observation")
                        if observation["id"] not in known:
                            merged["evidence"].append(observation)
                    previous.clear()
                    previous.update(merged)
                else:
                    if not isinstance(case.get('media'), dict) or not case['media'].get('resource_id'):
                        raise ValueError('New observed case requires an exact media resource ID')
                    partition = case.get("partition", "pending")
                    if partition == "pending" and case.get("eligible_for", []) != []:
                        raise ValueError("Pending case cannot carry creative eligibility")
                    if partition == "calibration":
                        evidence = case.get("evidence", [])
                        if (not evidence or not case.get("eligible_for") or
                                any(not isinstance(e, dict) or e.get("review_state") != "user_accepted" for e in evidence)):
                            raise ValueError("Calibrated case requires reviewed observations and explicit scoped eligibility")
                    elif partition != "pending":
                        raise ValueError("New holdout case cannot be introduced by LearningDelta")
                    case.setdefault("partition", "pending")
                    case.setdefault("eligible_for", [])
                    case.setdefault("source_market", "unknown")
                    case.setdefault("source_platform", case.get("platform", "local"))
                    case.setdefault("platform", case["source_platform"])
                    case.setdefault("origin", "user_reference")
                    case.setdefault("source_url", None)
                    case.setdefault("tags", [])
                    case.setdefault("strategy", {})
                    case.setdefault("limits", ["尚未取得创作用途资格；市场效果未知。"])
                    case.setdefault("media", None)
                    case.setdefault("user_feedback", None)
                    case.setdefault("market_effect", None)
                    result["cases"].append(case)
                touched.add((op, cid))
            elif op in ("method.create", "method.revise", "method.add_evidence", "method.retire", "method.activate"):
                supplied_method = entry.get("method")
                mid = entry.get("method_id") or (supplied_method.get("id") if isinstance(supplied_method, dict) else None)
                if not isinstance(mid, str):
                    raise ValueError("Method id required")
                method = next((m for m in result["methods"] if m.get("id") == mid), None)
                if (op, mid) in touched:
                    raise ValueError("Duplicate method operation")
                touched.add((op, mid))
                if op == "method.create":
                    if method is not None:
                        raise ValueError("Method id already exists")
                    method = copy.deepcopy(entry.get("method"))
                    if not isinstance(method, dict) or method.get("id") != mid:
                        raise ValueError("method.create requires an identified method")
                    if method.get("status") not in (None, "trial") or method.get("revision") not in (None, 1):
                        raise ValueError("New method starts at trial revision 1")
                    if any(key in method for key in ("transfer_assessment", "unverified_use_approved", "market_assessment", "migration_pending")):
                        raise ValueError("New method cannot assert approval or evidence maturity")
                    method.update(status="trial", revision=1, source_proposal="delta:" + _sha(_json(payload)),
                                  applicable_markets=["US"], market_validation="unknown")
                    method.setdefault("counterexample_refs", [])
                    method["supports"] = copy.deepcopy(method.get("support_refs", []))
                    method["counterexamples"] = copy.deepcopy(method["counterexample_refs"])
                    if not isinstance(method.get("use_when"), list) or any(not isinstance(v, str) for v in method["use_when"]):
                        raise ValueError("method.create use_when must be a text list")
                    if not isinstance(method.get("avoid_when"), list) or any(not isinstance(v, str) for v in method["avoid_when"]):
                        raise ValueError("method.create avoid_when must be a text list")
                    method["when"] = "; ".join(method.get("use_when", []))
                    method["countercondition"] = "; ".join(method.get("avoid_when", []))
                    method["trial_scope"] = LearningService._trial_scope(result, method)
                    result["methods"].append(method)
                    created_or_changed.add(mid)
                else:
                    if method is None:
                        raise ValueError("Unknown method")
                    if type(entry.get("expected_revision")) is not int or entry["expected_revision"] != method.get("revision"):
                        raise LearningConflict("Method revision changed; prepare a new delta")
                    if op == "method.revise":
                        changes = entry.get("changes")
                        allowed = {"kind", "name", "retrieval_summary", "goal", "use_when", "avoid_when", "required_inputs",
                                   "procedure", "must_preserve", "can_vary", "limitations", "checks", "support_refs",
                                   "counterexample_refs", "decision", "when", "countercondition"}
                        if not isinstance(changes, dict) or not changes or changes.keys() - allowed:
                            raise ValueError("Unsupported method revision fields")
                        for field in ("use_when", "avoid_when"):
                            if field in changes and (not isinstance(changes[field], list) or
                                                     any(not isinstance(v, str) for v in changes[field])):
                                raise ValueError(f"{field} must be a text list")
                        method.update(copy.deepcopy(changes))
                        method["status"] = "trial"
                        method["market_validation"] = "unknown"
                        method.pop("transfer_assessment", None)
                        method.pop("unverified_use_approved", None)
                        if "support_refs" in changes:
                            method["supports"] = copy.deepcopy(changes["support_refs"])
                        if "counterexample_refs" in changes:
                            method["counterexamples"] = copy.deepcopy(changes["counterexample_refs"])
                        if "use_when" in changes and "when" not in changes:
                            method["when"] = "; ".join(changes["use_when"])
                        if "avoid_when" in changes and "countercondition" not in changes:
                            method["countercondition"] = "; ".join(changes["avoid_when"])
                        created_or_changed.add(mid)
                    elif op == "method.add_evidence":
                        additions = entry.get("support_refs", [])
                        negatives = entry.get("counterexample_refs", [])
                        if (not isinstance(additions, list) or not isinstance(negatives, list) or not additions + negatives
                                or any(not isinstance(ref, str) for ref in additions + negatives)):
                            raise ValueError("method.add_evidence requires evidence references")
                        method["support_refs"] = list(dict.fromkeys(method["support_refs"] + additions))
                        method["counterexample_refs"] = list(dict.fromkeys(method["counterexample_refs"] + negatives))
                        method["supports"] = copy.deepcopy(method["support_refs"])
                        method["counterexamples"] = copy.deepcopy(method["counterexample_refs"])
                        created_or_changed.add(mid)
                    elif op == "method.activate":
                        if mid in created_or_changed or method.get("status") != "trial":
                            raise ValueError("Activation requires a previously saved trial revision")
                        reason = entry.get("rationale")
                        if not isinstance(reason, str) or not reason.strip():
                            raise ValueError("Activation needs an explicit scoped reason")
                        if entry.get("unverified_use_approved") is not True:
                            raise ValueError("Activation currently requires explicit unverified-use approval")
                        scope = LearningService._trial_scope(result, method)
                        if not scope["purposes"]:
                            raise ValueError("Trial has no qualified creative purpose")
                        method["status"] = "active"
                        method["unverified_use_approved"] = True
                        method["market_validation"] = "unknown"
                        method["activation"] = {"kind": "unverified_user_permission", "rationale": reason.strip(),
                                                "scope": scope, "source_delta": "delta:" + _sha(_json(payload)),
                                                "effectiveness": "unverified"}
                    else:
                        method["status"] = "retired"
                    method["revision"] += 1
                    method["parent_revision"] = entry["expected_revision"]
                    method["source_proposal"] = "delta:" + _sha(_json(payload))
                    if method["status"] == "trial":
                        method["trial_scope"] = LearningService._trial_scope(result, method)
            elif op == "preference.upsert":
                preference = copy.deepcopy(entry.get("preference"))
                if not isinstance(preference, dict) or not isinstance(preference.get("id"), str):
                    raise ValueError("preference.upsert requires an identified preference")
                existing = next((p for p in result["preferences"] if p.get("id") == preference["id"]), None)
                if existing is not None:
                    if preference.get("scope") != existing.get("scope") or preference.get("owner") != existing.get("owner"):
                        raise ValueError("Preference scope or owner cannot be expanded")
                    existing.update(preference)
                else:
                    result["preferences"].append(preference)
            else:
                raise ValueError("Unsupported LearningDelta operation")
        result["library_version"] += 1
        errors = core.validate(result)
        if errors:
            raise ValueError("; ".join(errors))
        return result

    def method_summaries(self, run_id=None, purpose="message", include_trial=False, max_chars=12000, reviewer=False):
        """Versioned, evidence-qualified method directory for task comparison."""
        if purpose not in core.PURPOSES:
            raise ValueError("Unknown method purpose")
        if type(max_chars) is not int or not 256 <= max_chars <= 50000:
            raise ValueError("max_chars must be 256..50000")
        if include_trial and run_id is None:
            raise ValueError("Trial methods require an explicit project context")
        raw, doc = self._read_for_run(run_id)
        if reviewer:
            doc = self._review_library(doc)
        cases = {c["id"]: c for c in doc["cases"] if c.get("partition") == "calibration"
                 and purpose in c.get("eligible_for", [])}
        included, omitted = [], []
        used = 0
        for method in doc["methods"]:
            status = method.get("status")
            if status == "retired" or status == "draft" or (status == "trial" and not include_trial):
                omitted.append({"id": method.get("id"), "reason": "status"})
                continue
            if status == "trial" and (method.get("trial_scope", {}).get("market") != "US" or
                                       purpose not in method.get("trial_scope", {}).get("purposes", [])):
                omitted.append({"id": method.get("id"), "reason": "trial_scope"})
                continue
            refs = method.get("support_refs", method.get("supports", []))
            if not refs or any(ref.partition("/")[0] not in cases or
                               ref.partition("/")[2] not in {e["id"] for e in cases[ref.partition("/")[0]]["evidence"]}
                               for ref in refs):
                omitted.append({"id": method.get("id"), "reason": "evidence_or_purpose"})
                continue
            row = {"id": method["id"], "revision": method.get("revision", 1), "status": status,
                   "retrieval_summary": method.get("retrieval_summary") or method["decision"],
                   "use_when": method.get("use_when") or [method["when"]],
                   "avoid_when": method.get("avoid_when") or [method["countercondition"]],
                   "support_refs": refs, "market_validation": method.get("market_validation", "unknown"),
                   "trial_requires_validation": status == "trial"}
            if method.get("activation"):
                row["activation"] = method["activation"]
            if status == "trial":
                row["trial_scope"] = method["trial_scope"]
            for field in ("required_inputs", "procedure", "must_preserve", "can_vary", "checks", "limitations"):
                if method.get(field):
                    row[field] = method[field]
            length = len(_json(row))
            if used + length > max_chars:
                omitted.append({"id": method["id"], "reason": "context_budget"})
                continue
            included.append(row)
            used += length
        return {"library_sha256": _sha(raw), "library_version": doc.get("library_version", 1),
                "purpose": purpose, "methods": included, "omitted": omitted, "truncated": any(x["reason"] == "context_budget" for x in omitted)}

    def read_learning_method(self, run_id, method_id, revision, expected_library_sha256,
                             purpose="message", role="orchestrator"):
        """Snapshot an eligible exact method revision and its positive/negative evidence."""
        run = self.store.read_run(run_id)
        if role not in ("orchestrator", "director", "material", "writer", "reviewer"):
            raise ValueError("Unknown creative role")
        if not isinstance(method_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", method_id):
            raise ValueError("Invalid method id")
        if type(revision) is not int or revision < 1:
            raise ValueError("Exact method revision required")
        raw, original = self._read_for_run(run_id)
        if _sha(raw) != expected_library_sha256:
            raise LearningConflict("Learning library changed; read method summaries again")
        reviewer = role == "reviewer"
        allowed = self.method_summaries(run_id=run_id, purpose=purpose,
                                        include_trial=run.kind == "learning", max_chars=50000,
                                        reviewer=reviewer)
        if allowed["library_sha256"] != _sha(raw):
            raise LearningConflict("Learning library changed while reading method")
        if not any(m["id"] == method_id and m["revision"] == revision for m in allowed["methods"]):
            raise ValueError("Method revision is unavailable for this Run and purpose")
        doc = self._review_library(original) if reviewer else original
        method = next((copy.deepcopy(m) for m in doc["methods"]
                       if m.get("id") == method_id and m.get("revision", 1) == revision), None)
        if method is None:
            raise ValueError("Unknown method revision")
        by_case = {c["id"]: c for c in doc["cases"] if c.get("partition") == "calibration"}
        refs = [("support", ref) for ref in method.get("support_refs", method.get("supports", []))]
        refs += [("counterexample", ref) for ref in method.get("counterexample_refs", method.get("counterexamples", []))]
        evidence = []
        for relationship, ref in refs:
            cid, _, eid = ref.partition("/")
            case = by_case.get(cid)
            observation = next((e for e in case["evidence"] if e.get("id") == eid), None) if case else None
            if observation is None:
                raise LearningConflict("Method evidence is no longer available")
            evidence.append({"ref": ref, "relationship": relationship,
                             "case_title": case["title"], "source_platform": case.get("source_platform", case.get("platform")),
                             "source_market": case.get("source_market", "unknown"),
                             "eligible_for": case.get("eligible_for", []), "limits": case.get("limits", []),
                             "observation": copy.deepcopy(observation)})
        content = {"method": method, "evidence": evidence, "purpose": purpose, "role": role,
                   "library_sha256": _sha(raw), "library_version": original.get("library_version", 1),
                   "method_sha256": _sha(_json(method)),
                   "qualification": "Source observations and method scope only; use and effectiveness require separate checks"}
        digest = _sha(_json(content))
        content.update(snapshot_sha256=digest, evidence_ref=f"learning/methods/{digest}.json")
        with self.store.run_lock(run_id):
            path = self._run_root(run_id) / "methods" / (digest + ".json")
            if not path.exists():
                _atomic_write_text(path, _json(content))
            self.store.append_event(run_id, "learning_method_loaded", method_id=method_id,
                                    revision=revision, purpose=purpose, role=role,
                                    library_sha256=content["library_sha256"],
                                    snapshot_sha256=digest, evidence_ref=content["evidence_ref"])
        return content

    def _propose(self, run_id, kind, payload, actor):
        if not isinstance(payload, dict):
            raise ValueError("Proposal payload must be an object")
        if kind == "delta":
            # proposal.json uses sorted keys; build from that same order so
            # apply can reproduce the reviewed bytes exactly.
            payload = json.loads(_json(payload))
        root = self._run_root(run_id)
        raw, before = self._read()
        if kind == "delta":
            self._validate_delta_sources(run_id, payload)
        after = self._candidate(before, run_id, kind, payload)
        no_change = kind == "delta" and (payload.get("outcome") == "no_change" or payload.get("no_change") is True)
        candidate = raw.decode("utf-8") if no_change else _library_text(after, raw)
        # Explicit user submissions are deterministic across transport retries.
        identity_payload = copy.deepcopy(payload)
        if kind == "feedback" and identity_payload["source_identity"]["kind"] == "explicit_user_input":
            identity_payload["source_identity"].pop("at", None)
        if kind == "delta":
            identity_payload["observed_user_discussion_sha256"] = self._user_discussion_sha(self.store, run_id)
        ident = "proposal-" + _sha(_json({"kind": kind, "payload": identity_payload, "base": _sha(raw)}))[:32]
        directory = root / "proposals" / ident
        with self.store.run_lock(run_id):
            if (directory / "proposal.json").exists():
                return self.proposal(run_id, ident)
            diff = "".join(difflib.unified_diff(raw.decode("utf-8-sig").splitlines(True), candidate.lstrip("\ufeff").splitlines(True), fromfile="canonical-before.json", tofile="proposed-after.json"))
            proposal = {"id": ident, "type": kind, "status": "pending", "actor": actor, "run_id": run_id,
                        "created_at": utcnow(), "payload": payload, "base_library_sha256": _sha(raw),
                        "candidate_sha256": _sha(candidate), "diff": diff, "diff_sha256": _sha(diff)}
            if kind == "delta":
                proposal["summary"] = payload.get("summary") or payload["rationale"]
                proposal["discussion_sha256"] = self._user_discussion_sha(self.store, run_id)
                proposal["no_change"] = no_change
            elif kind == "migration":
                proposal["summary"] = "将共享学习库迁移为 v2；旧来源市场保持未知，旧方法保留原资格并标记待补齐。"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "before.json").write_bytes(raw)
            _atomic_write_text(directory / "after.json", candidate)
            _atomic_write_text(directory / "proposal.json", _json(proposal))
            self.store.append_event(run_id, "learning_proposed", proposal_id=ident, proposal_type=kind, actor=actor,
                                    proposal_sha256=_sha(_json(proposal)), base_library_sha256=_sha(raw), candidate_sha256=_sha(candidate),
                                    user_quote=payload.get("quote") if actor == "user" else None)
            return dict(proposal, proposal_sha256=_sha(_json(proposal)))

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

    def migration_preview(self):
        """Read-only migration review; it never writes to the canonical library."""
        raw, before = self._read()
        if before.get("schema_version") != 1:
            raise ValueError("Library is already v2")
        after = core.migrate_v1_to_v2(before)
        candidate = _library_text(after, raw)
        diff = "".join(difflib.unified_diff(raw.decode("utf-8-sig").splitlines(True), candidate.lstrip("\ufeff").splitlines(True),
                                            fromfile="canonical-before.json", tofile="v2-preview.json"))
        return {"base_library_sha256": _sha(raw), "base_library_version": before.get("library_version", 1),
                "candidate_library_version": after["library_version"], "candidate_sha256": _sha(candidate),
                "diff": diff, "unknown_source_markets": [c["id"] for c in after["cases"] if c["source_market"] == "unknown"],
                "methods_pending_review": [m["id"] for m in after["methods"] if m.get("migration_pending")],
                "writes_library": False}

    def propose_migration(self, run_id, actor="user"):
        if actor != "user":
            raise ValueError("Migration requires explicit user review")
        return self._propose(run_id, "migration", {}, actor)

    def propose_delta(self, run_id, payload, actor="agent"):
        if actor not in ("agent", "user"):
            raise ValueError("Unknown proposal actor")
        if getattr(self.store.read_run(run_id), "kind", "creative") != "learning":
            raise ValueError("LearningDelta requires a learning Run")
        if not isinstance(payload, dict):
            raise ValueError("LearningDelta payload must be an object")
        body = copy.deepcopy(payload)
        # The version is measured by the service; a model-supplied hash cannot
        # certify that it saw the actual discussion.
        body.pop("discussion_sha256", None)
        try:
            return self._propose(run_id, "delta", body, actor)
        except (TypeError, AttributeError, KeyError, IndexError) as exc:
            raise ValueError("Malformed LearningDelta payload") from exc

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
        elif (directory / "rejected.json").exists():
            proposal.update(json.loads((directory / "rejected.json").read_text(encoding="utf-8")))
        proposal["proposal_sha256"] = _sha(_json(self._read_proposal(directory)))
        return proposal

    def proposals(self, run_id):
        directory = self._run_root(run_id) / "proposals"
        return [self.proposal(run_id, p.name) for p in sorted(directory.glob("proposal-*")) if (p / "proposal.json").is_file()]

    def reject(self, run_id, proposal_id, expected_proposal_sha256, request_id):
        """Record an explicit user rejection without changing the shared library."""
        directory = self._proposal_directory(run_id, proposal_id)
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_id):
            raise ValueError("Invalid learning reject request_id")
        with self.store.run_lock(run_id):
            proposal = self._read_proposal(directory)
            digest = _sha(_json(proposal))
            if expected_proposal_sha256 != digest:
                raise LearningConflict("Expected proposal hash does not match reviewed content")
            if (directory / "applied.json").exists():
                raise LearningConflict("Applied proposal cannot be rejected")
            if (directory / "rejected.json").exists():
                receipt = json.loads((directory / "rejected.json").read_text(encoding="utf-8"))
                if receipt.get("request_id") != request_id:
                    raise LearningConflict("Proposal was rejected with a different request_id")
                return self.proposal(run_id, proposal_id)
            events = self.store.read_events(run_id)
            if not any(e["kind"] == "learning_proposed" and e.get("proposal_id") == proposal_id
                       and e.get("proposal_sha256") == digest for e in events):
                raise LearningConflict("Proposal integrity mismatch")
            if any(e["kind"] in ("learning_applied", "learning_rejected") and e.get("request_id") == request_id
                   and e.get("proposal_id") != proposal_id for e in events):
                raise LearningConflict("request_id was used for another learning proposal")
            receipt = {"status": "rejected", "rejected_at": utcnow(), "request_id": request_id}
            _atomic_write_text(directory / "rejected.json", _json(receipt))
            self.store.append_event(run_id, "learning_rejected", proposal_id=proposal_id,
                                    proposal_type=proposal["type"], proposal_sha256=digest, request_id=request_id)
            return dict(proposal, proposal_sha256=digest, **receipt)

    def apply(self, run_id, proposal_id, expected_library_sha256, expected_proposal_sha256=None, request_id=None):
        """User-route-only CAS commit; append-only proposal/audit files survive retries."""
        directory = self._proposal_directory(run_id, proposal_id)
        with _library_lock(self.library_path), self.store.run_lock(run_id):
            proposal = self._read_proposal(directory)
            original_hash = _sha(_json(proposal))
            if expected_proposal_sha256 is not None and expected_proposal_sha256 != original_hash:
                raise LearningConflict("Expected proposal hash does not match reviewed content")
            if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_id)):
                raise ValueError("Invalid learning apply request_id")
            events = self.store.read_events(run_id)
            if not any(e["kind"] == "learning_proposed" and e.get("proposal_id") == proposal_id and e.get("proposal_sha256") == original_hash for e in events):
                raise LearningConflict("Proposal integrity mismatch")
            if expected_library_sha256 != proposal["base_library_sha256"]:
                raise LearningConflict("Expected library hash does not match the reviewed proposal")
            if (directory / "rejected.json").exists():
                raise LearningConflict("Rejected proposal cannot be applied")
            if (directory / "applied.json").exists():
                prior = json.loads((directory / "applied.json").read_text(encoding="utf-8"))
                if prior.get("request_id") is not None and request_id is not None and request_id != prior["request_id"]:
                    raise LearningConflict("Proposal was applied with a different request_id")
                return self.proposal(run_id, proposal_id)
            if request_id is not None and any(e["kind"] == "learning_applied" and e.get("request_id") == request_id
                                              and e.get("proposal_id") != proposal_id for e in events):
                raise LearningConflict("request_id was used for another learning proposal")
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
            if proposal["type"] == "delta":
                if getattr(self.store.read_run(run_id), "kind", "creative") != "learning":
                    raise LearningConflict("LearningDelta no longer belongs to a learning Run")
                if proposal.get("discussion_sha256") != self._user_discussion_sha(self.store, run_id):
                    raise LearningConflict("Learning discussion changed; review a new proposal")
                try:
                    self._validate_delta_sources(run_id, proposal["payload"])
                except ValueError as exc:
                    raise LearningConflict(str(exc)) from exc
            rebuilt = self._candidate(json.loads(before.decode("utf-8-sig")), run_id, proposal["type"], proposal["payload"])
            rebuilt_bytes = before if proposal.get("no_change") else _library_text(rebuilt, before).encode("utf-8")
            if _sha(rebuilt_bytes) != proposal["candidate_sha256"]:
                raise LearningConflict("Proposal no longer reproduces its reviewed candidate")
            current = _sha(self.library_path.read_bytes())
            if current not in (expected_library_sha256, proposal["candidate_sha256"]):
                raise LearningConflict("Library changed; prepare a new proposal against its current version")
            # Crash recovery: candidate already installed but receipt not written.
            if proposal["type"] == "migration" and current != proposal["candidate_sha256"]:
                # The proposal already contains exact before.json; this explicit
                # manifest makes the recovery copy and its version easy to audit.
                _atomic_write_text(directory / "migration-backup.json", _json({
                    "sha256": _sha(before), "library_version": json.loads(before.decode("utf-8-sig")).get("library_version", 1),
                    "backup_file": "before.json"}))
            if current != proposal["candidate_sha256"]:
                _atomic_write_text(self.library_path, candidate.decode("utf-8"))
            receipt = {"status": "applied", "applied_at": utcnow(), "applied_library_sha256": proposal["candidate_sha256"],
                       "request_id": request_id}
            _atomic_write_text(directory / "applied.json", _json(receipt))
            self.store.append_event(run_id, "learning_applied", proposal_id=proposal_id, proposal_type=proposal["type"],
                                    before_sha256=proposal["base_library_sha256"], after_sha256=proposal["candidate_sha256"],
                                    request_id=request_id)
            return dict(proposal, proposal_sha256=original_hash, **receipt)
