"""Bind media feedback to its immutable production input, without authorizing it."""
from __future__ import annotations

import json

from .models import PipelineError


PRODUCTION_TARGETS = frozenset({'production_draft', 'media'})
PRODUCTION_DELIVERABLES = frozenset({'production_input', 'production_media'})


def bind_target(store, run_id, target, *, replacement_request_id=None):
    """Use the production projection for identity and current-source checks.

    Old records without a verifiable input may still be discussed. They cannot
    be used to prepare a replacement; this never manufactures a shot identity
    or relaxes the production module's visual or generation checks.
    """
    if not target or target.get('kind') not in PRODUCTION_TARGETS:
        return None
    if target.get('run_id') != run_id:
        raise PipelineError('production_feedback_changed', '反馈所属项目已变化，请重新选择当前作品。')
    from .production import attempt_records, draft_records, source_snapshot
    with store.run_lock(run_id):
        drafts = draft_records(store, run_id)
        attempt = None
        if target['kind'] == 'media':
            attempt = attempt_records(store, run_id).get(target.get('attempt_id'))
            output = (attempt or {}).get('output') or {}
            if (not attempt or attempt.get('status') not in {'succeeded', 'completed'}
                    or output.get('sha256') != target.get('output_sha256')):
                raise PipelineError('production_feedback_changed', '媒体结果已变化，请重新打开当前作品。')
            from .evidence import evidence_snapshot
            if not any(row['name'] == output.get('name') and row['sha256'] == target['output_sha256']
                       for row in evidence_snapshot(store, run_id)['materials']):
                raise PipelineError('production_feedback_changed', '媒体文件已变化，请重新打开当前作品。')
            if target.get('at_seconds') is not None and attempt.get('media_kind') != 'video':
                raise PipelineError('production_feedback_changed', '只有视频可以标记播放时间。')
            draft_id, expected = attempt.get('draft_id'), attempt.get('draft_sha256')
        else:
            draft_id, expected = target.get('draft_id'), target.get('draft_sha256')
            if target.get('attempt_id'):
                attempt = attempt_records(store, run_id).get(target['attempt_id'])
                if (not attempt or attempt.get('draft_id') != draft_id
                        or attempt.get('draft_sha256') != expected):
                    raise PipelineError('production_feedback_changed', '制作尝试已变化，请重新选择准确输入。')
        draft = drafts.get(draft_id)
        if target['kind'] == 'production_draft' and (not draft or draft['draft_sha256'] != expected):
            raise PipelineError('production_feedback_changed', '制作输入已变化，请重新打开准确版本。')
        if draft and expected and draft['draft_sha256'] != expected:
            raise PipelineError('production_feedback_changed', '媒体所属制作输入无法核对，请重新选择当前作品。')
        if not draft:
            return {'binding_issue': '历史媒体缺少准确制作输入，请先选择当前镜头；本轮只能讨论。'}
        if draft.get('superseded_by'):
            replacement = drafts.get(replacement_request_id)
            if (draft['superseded_by'] != replacement_request_id or not replacement
                    or replacement.get('supersedes') != draft_id):
                raise PipelineError('production_feedback_changed', '所选输入已被新版替换，请重新选择当前镜头。')
        baseline = draft.get('source_snapshot')
        if baseline and baseline != source_snapshot(store, run_id):
            raise PipelineError('production_feedback_changed', '所选输入的源依据已过期，请核对当前镜头后重新反馈。')
        if baseline and draft.get('source_version') and draft.get('source_sha256'):
            from .pipeline import Pipeline
            source_state = Pipeline(store).stage_state(run_id, 'writing')
            source = source_state.latest
            # Source bytes may return to a historical hash. The pipeline's
            # event generations still keep that adopted version stale.
            if (not source or (source.version, source.sha256) != (
                    draft['source_version'], draft['source_sha256']) or source_state.stale
                    or not source_state.integrity_ok or source_state.open_attempt):
                raise PipelineError('production_feedback_changed', '所选输入的源版本已过期或正在修改，请重新核对当前镜头。')
        binding = {key: draft.get(key) for key in (
            'draft_id', 'draft_sha256', 'shot_id', 'kind', 'source_version', 'source_sha256', 'source_snapshot')}
        if not expected or not all(binding.values()):
            binding['binding_issue'] = '历史输入缺少准确镜头或源版本，本轮只能讨论，不能自动补造制作身份。'
        from .project_brief import content_format
        source = store.latest_artifact(run_id, 'writing')
        binding['format'] = content_format(store.read_events(run_id)) or (
            'photo' if source and (source.structured or {}).get('photo_type') else 'video')
        return binding


def validate_replacement(store, run_id, target, binding, args):
    """A local correction replaces exactly the input selected by the user."""
    with store.run_lock(run_id):
        current = bind_target(store, run_id, target, replacement_request_id=args.get('request_id'))
        if not current or current.get('binding_issue'):
            raise PipelineError('production_feedback_scope', (current or {}).get('binding_issue')
                                or '本轮没有可核对的制作反馈目标。')
        if current != binding:
            raise PipelineError('production_feedback_changed', '所选制作输入已变化，请重新选择当前镜头。')
        if any(args.get(key) != binding[key] for key in (
                'shot_id', 'kind', 'source_version', 'source_sha256')) or args.get('supersedes') != binding['draft_id']:
            raise PipelineError('production_feedback_scope',
                                '本轮只能替换用户选中的准确镜头输入；保留 shot_id、源版本，并用 supersedes 指向该输入。')


def deliverable_status(store, pipeline, run_id, goal, binding, replacement_id):
    """Check this selected-shot correction through the shared shot projection."""
    status = 'incomplete'
    missing = [binding.get('binding_issue') or '用户选中镜头的新替换输入（绑定原输入与镜头）']
    note = '原输入存在不等于已完成本轮反馈修改。'
    if replacement_id:
        from .production import _read_state_without_service
        state = _read_state_without_service(store, pipeline, run_id)
        shot = next((row for row in state['shots'][binding['kind']]['shots']
                     if row['shot_id'] == binding['shot_id'] and row['draft_id'] == replacement_id), None)
        if not shot or not shot['source_ready']:
            missing, note = ['绑定当前源依据的所选镜头替换输入'], '目标输入已改变或源依据已失效。'
        elif goal == 'production_input' or shot['stage'] == 'completed':
            status, missing, note = 'complete', [], '所选镜头的修改已保存，其他镜头保留。'
        else:
            status = {'input_ready': 'input_ready', 'awaiting_authorisation': 'awaiting_authorisation',
                      'submitted': 'in_progress', 'generating': 'in_progress', 'retrieving': 'in_progress',
                      'unverified': 'unknown', 'failed': 'failed'}.get(shot['stage'], 'incomplete')
            missing = ['所选镜头修改后的可阅读媒体结果']
            note = shot.get('reason') or '修改后的输入已保存，媒体提交仍需用户核对准确版本和费用。'
    return {'goal': goal, 'status': status, 'missing': missing, 'note': note, 'target_shots': 1,
            'repeat_key': json.dumps([status, missing], ensure_ascii=False)}
