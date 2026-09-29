"""Product guidance derived from the pipeline; never grants execution or approval."""
from .models import STAGES
from .vocabulary import stage_label


def project_progress(state):
    stages = state['stages']
    def ready(s):
        return (s['latest'] and not s['stale'] and s['integrity_ok']
                and s['latest'].get('digest_algorithm') == 'artifact-v2'
                and s['status'] in {'done', 'approved'})
    completed = sum(1 for s in stages if ready(s))
    # Legacy ungated stages may read as "done" in historical pipeline views,
    # while require_prerequisites refuses to build on their body-only digest.
    target = next((s for s in stages if s['open_attempt']), None)
    target = target or next((s for s in stages if not ready(s)), None)
    next_stage = target['stage'] if target else None
    out = {'completed': completed, 'total': len(STAGES), 'stage': next_stage,
           'title': '策划已就绪', 'detail': '查看采用版本与制作资料，整理本次交付。',
           'action': 'delivery', 'label': '查看交付', 'prompt': ''}
    if target is None:
        return out
    label = stage_label(target['stage'])
    artifact = target['latest']
    out.update(action='draft', label='准备继续', title=f'下一步：{label}',
               detail='先查看制作要求，再发送给制片团队。',
               prompt=f'请基于本项目已保存的资料和当前有效决定，推进「{label}」。先由团队查阅历史、素材和已有回答，解决内部工作；仅确需我提供的事实或选择才请我确认。完成这一阶段后停下来供我阅读。不要代替我批准。')
    if target['open_attempt']:
        out.update(action='recovery', title='有一项制作尚未结束', label='查看运行状态',
                   detail='先确认当前任务是否仍在执行；已保存的内容会保留。')
    elif artifact and not target['integrity_ok']:
        out.update(title=f'请检查「{label}」的保存内容', label='查看这一版', action='stage',
                   detail='文件与保存时的记录不一致，暂不能继续采用。请查看原文与版本记录。')
    elif artifact and artifact.get('digest_algorithm', 'body-v1') != 'artifact-v2':
        out.update(title=f'从历史「{label}」继续', label='准备新版本',
                   detail='旧稿保留作参考，新稿需由你重新确认；历史采用记录不转移。',
                   prompt=f'请以「{label}」历史版本 {artifact["version"]}（指纹 {artifact["sha256"]}）为参考，结合当前资料重新整理这一阶段，保存为新版本供我确认。保留旧稿，不继承历史批准，不继续后续阶段。')
    elif target['stale']:
        out.update(title=f'「{label}」需要更新', label='准备更新',
                   detail='资料或上游决定已变化，旧稿保留，更新后再确认。')
    elif target['status'] == 'needs_resolution':
        out.update(title='素材方案有关键缺口', label='调整素材方案',
                   detail='关键画面还无法落实，先调整画面或补足依据，再写完整脚本。',
                   prompt='请先检查素材方案中标为阻塞的画面和缺口。能替代的由团队调整素材方案；只有我能提供的事实或素材才问我。关键缺口解决后再写完整脚本，不要假设它已经解决。')
    elif target['can_approve']:
        out.update(title=f'「{label}」等你决定', detail='阅读这一版，选择采用或退回修改。',
                   action='review', label='阅读并决定')
    elif (target.get('approval') or {}).get('decision') == 'returned':
        # His own return already stands on this version, and one version takes
        # one decision (red line 8) — so the next thing that can happen is a new
        # version from the team, not another click on this one.
        out.update(title=f'「{label}」已退回，等团队出新版', label='准备继续',
                   detail='你退回了这一版；旧稿保留，团队按你的意见整理新版后再由你确认。',
                   prompt=(f'我退回了「{label}」的当前版本，请按我的意见整理新版本；保留未受影响的内容，'
                           '完成后让我看，不改写其他阶段，也不代替我批准。'))
    elif target['last_rejection']:
        out.update(title=f'「{label}」上次未完成', detail='已保存内容仍可查看。先说明本次如何调整，再发起新一轮。')
    return out
