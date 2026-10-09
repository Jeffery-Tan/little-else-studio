"""Assemble one role task's pinned, auditable input without sharing agent chat."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from .tools.workspace import ToolError, Workspace


@dataclass(frozen=True)
class TaskContext:
    seed: str
    system_extra: str
    sources: tuple[dict, ...]
    seed_sha256: str


def assemble_role_task(store, pipeline, learning, run_id, workspace: Workspace,
                       stage: str, brief: str, *, previous=None, revision_scope=None,
                       method_context: dict, user_input_id=None, turn_scope=None,
                       journal_constraints=None) -> TaskContext:
    """Read exact stage versions once; log only identities and hashes elsewhere."""
    role = workspace.role
    local_revision = bool(revision_scope and revision_scope['mode'] == 'local')
    visible = {name: store.latest_artifact(run_id, name) for name in workspace.readable}
    if previous is not None:
        visible[stage] = previous
    versions = {name: artifact.version for name, artifact in visible.items() if artifact}
    sources = tuple({'kind': 'artifact', 'stage': name, 'version': artifact.version,
                     'sha256': artifact.sha256}
                    for name, artifact in visible.items() if artifact)

    parts = ['# 给你的任务', '', brief.strip(), '', f'# 你要交付的阶段：{stage}', '']
    from .conversation_input import applicable_constraints, constraints_for_scope
    constraints = ((turn_scope or {})['project_constraints']
                   if 'project_constraints' in (turn_scope or {}) else applicable_constraints(
                       store, run_id, user_input_id=user_input_id))
    paths = [field['path'] for field in revision_scope['fields']] if local_revision else None
    applicable = constraints_for_scope(constraints, stage=stage, paths=paths)
    conflicts = [row for row in applicable if row['status'] == 'needs_clarification']
    if conflicts:
        raise ToolError('本阶段的用户条件冲突，需要先澄清：' + '；'.join(
            row.get('ambiguity') or row['quote'] for row in conflicts))
    applicable = [row for row in applicable if row['status'] == 'active']
    if applicable:
        # Only the same trusted journal's exact fresh projection can replace
        # this duplicate. Preserve source identities even when its text is sent
        # once per request rather than repeated inside the immutable seed.
        if journal_constraints != applicable:
            parts += ['# 仍有效且适用于本阶段的用户项目条件', json.dumps(applicable, ensure_ascii=False)]
        parts += ['这些条件来自已经执行的用户消息，超过对话窗口仍有效。不得用任务摘要覆盖；'
                  'scope.stages/paths 限定适用范围，exceptions 指定局部例外；不得把局部条件扩成全片。'
                  '冲突时报告冲突，只有明确替换才使旧条件失效。', '']
        sources += tuple({'kind': 'user_constraint', 'message_id': row['source_message_id'],
                          'sha256': hashlib.sha256(row['quote'].encode('utf-8')).hexdigest(),
                          'constraint_id': row['id'], 'scope': row['scope']} for row in applicable)
        sources += tuple({'kind': 'user_constraint_exception', 'message_id': exception['source_message_id'],
                          'sha256': hashlib.sha256(exception['quote'].encode('utf-8')).hexdigest(),
                          'constraint_id': exception['replacement_id'], 'scope': exception['scope']}
                         for row in applicable for exception in row.get('exceptions', []) if exception.get('quote'))
    if user_input_id:
        from .conversation_input import message, interpretation_for, interpreted_task_constraints
        receipt = message(store, run_id, user_input_id)
        constraints = interpreted_task_constraints(receipt, interpretation_for(store, run_id, user_input_id))
        source = {'kind': 'user_message', 'message_id': receipt['message_id'],
                  'sha256': hashlib.sha256(receipt['text'].encode('utf-8')).hexdigest(),
                  'scope': {'stage': stage, 'turn': turn_scope or {},
                            'revision_plan_id': revision_scope['plan_id'] if revision_scope else None}}
        sources += (source,)
        parts += ['# 本轮真实用户要求（由服务端绑定，不是主制片摘要）',
                  json.dumps({'source': source, 'text': receipt['text'],
                              'constraints': constraints}, ensure_ascii=False),
                  '原话里的条件在本阶段范围内同样适用；派工摘要不得删改这些条件。'
                  '只完成本阶段，不重新记录决定，不扩大局部范围，也不授予媒体或费用执行权限。'
                  '原话与本阶段或已采用版本冲突时说明冲突，不自行更换方向或重做其他阶段。', '']
        recovery = (turn_scope or {}).get('reference_recovery') or {}
        recovery_message_id = recovery.get('recovery_message_id')
        if recovery_message_id and recovery_message_id != user_input_id:
            continuation = message(store, run_id, recovery_message_id)
            source = {'kind': 'user_message', 'message_id': recovery_message_id,
                      'sha256': hashlib.sha256(continuation['text'].encode('utf-8')).hexdigest()}
            sources += (source,)
            parts += ['# 用户这次明确接续及补充的原话',
                      json.dumps({'source': source, 'text': continuation['text']}, ensure_ascii=False),
                      '保留原任务最终交付；本次补充不能扩大任务权限或代替用户决定。', '']
    if stage == 'direction':
        from .active_brief import compile_active_brief
        active = compile_active_brief(store, run_id)
        required = [dict(id=c['id'], text=c['text'], source=c.get('source', ''))
                    for c in active.get('clauses', []) if c.get('status') == 'confirmed']
        parts += ['# 当前有效目标与已确认条件',
                  json.dumps({'goal': active.get('goal'), 'clauses': required}, ensure_ascii=False),
                  '每个可供采用的方向都要在 satisfies 中标明 goal.primary 和实际满足的条款；不能把未确认信息写成事实。', '']
    # A producing role receives facts once. Keep alternate layout and unrelated
    # direction options out of the specialist request.
    parts += [workspace.render_context(stage, versions=versions, include_layout=False,
        reference_selection=(turn_scope or {}).get('reference_selection')), '']
    if stage == 'writing' and visible.get('material_plan'):
        # Legacy dual-written artifacts may have a short prose body. The
        # versioned design object remains authoritative and must reach the
        # specialist even when alternate presentation is suppressed.
        design = (visible['material_plan'].structured or {}).get('visual_design')
        if design:
            parts += ['# 当前素材计划中的共同人物与场景设计',
                      json.dumps(design, ensure_ascii=False, sort_keys=True),
                      '分镜通过 character_ids 与 scene_id 引用此设计；无人镜头明确 character_ids=""。', '']
    for up in workspace.readable:
        if up in ('intake', stage):
            continue
        if up == 'direction' and stage in ('material_plan', 'writing'):
            # render_context already supplies the approved option and its
            # relevant dependencies. Other options are not this role's task.
            continue
        parts += [f'## 上游产物 {up}', '', workspace.render_artifact(
            up, versions.get(up), include_layout=False), '']
    if previous is not None:
        if not store.verify_artifact(run_id, stage, previous.version):
            raise ToolError('上一版内容指纹校验失败，不能作为修订基线。')
        if stage == 'direction' and not revision_scope:
            # A stale direction can be long and is not an upstream source. The
            # director needs its alternatives for comparison, not a full second
            # copy of old prose, metadata and discarded dependency tables.
            old = previous.structured or {}
            old_options = old.get('options')
            if not isinstance(old_options, list):
                old_options = []
            options = [{key: str(row.get(key, ''))[:500] for key in
                        ('key', 'title', 'claim', 'hook', 'story', 'gives_up')}
                       for row in old_options[:3] if isinstance(row, dict)]
            parts += ['# 上一版方向仅供对照（已过期时不能当作当前事实）',
                      json.dumps({'version': previous.version, 'sha256': previous.sha256,
                                  'summary': previous.summary[:500], 'options': options,
                                  'legacy_excerpt': store.read_artifact_text(run_id, stage, previous.version)[:600]
                                  if not options else ''},
                                 ensure_ascii=False),
                      '以当前有效目标和本次任务为准；可以保留成立的想法，也可以重新构思，不必沿用旧稿的创意或结构。']
        else:
            parts += [('# 本任务的修订基线（旧稿供修改，不覆盖当前事实和真实选择）'
                       if revision_scope else '# 上一版仅供对照（当前任务可以重新构思）'),
                      workspace.render_artifact(stage, previous.version, include_layout=False),
                      ('按当前任务和问题修改；未受影响的表达应保留，不机械重做全部方案。'
                       if revision_scope else '以当前事实和本次任务为准；不必沿用旧稿的创意或结构。')]
        store.append_event(run_id, 'role_revision_context', role=role, stage=stage,
                           version=previous.version, artifact_sha256=previous.sha256)
    parts += ['# 怎么做', '',
              '先读上游产物，再动笔。完成之后调用 '
              + ('submit_patch' if local_revision else 'submit_stage') + ' 提交。',
              '不要提交半成品：提交之后就是新版本，依赖本版的下游需要复核更新。']
    seed = '\n'.join(parts)
    if revision_scope:
        seed += '\n# 本轮修订范围\n' + json.dumps(revision_scope, ensure_ascii=False)
    if local_revision:
        seed += ('\n本任务只用 submit_patch 提交 changes[{path,value}] 与 summary，不能提交全文。'
                 '未列字段由程序保留。上方已提供核验过的完整修订基线，可直接通读；'
                 '仅在信息缺失或需重新核对时使用 read_artifact，原读取权限不变。'
                 '仍须复查台词可读时长、前后衔接和声画关系；'
                 '范围内无法修好时说明具体冲突，不扩大修改。')

    readable = '、'.join(workspace.readable) or '（无）'
    system_extra = (
        '# 你能看到的东西\n\n'
        f'你可以读取这些阶段的产物：{readable}。其他阶段读不到，也不需要读到。\n'
        '你看不到任何一个同事的对话、推理或自我评价——你手上是产物本身和给你的说明。\n'
        'read_capabilities 可查能力边界；read_method_document 可读登记方法。方法目录：\n'
        + json.dumps(learning.skill_catalog(role), ensure_ascii=False)
        + '\n\n' + method_context['text']
        + ('\n本任务为局部修订，前述 submit_stage 提交要求在本任务改为 submit_patch；'
           '只能修改绑定字段，不重写全文。' if local_revision else '')
    )
    return TaskContext(seed, system_extra, sources,
                       hashlib.sha256(seed.encode('utf-8')).hexdigest())
