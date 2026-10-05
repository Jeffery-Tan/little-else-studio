"""Explicit creative reference selection derived from the existing run journal."""
import hashlib
import json

from .materials import resolve

MODES = {'autonomous', 'recreate', 'inspiration'}


def digest(candidate):
    return hashlib.sha256(json.dumps(candidate, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def current(store, run_id):
    for event in reversed(store.read_events(run_id)):
        if event['kind'] == 'creation_mode_selected':
            return dict(event['selection'])
    return None


def reference_candidate(store, run_id, reference_id):
    for event in reversed(store.read_events(run_id)):
        c = event.get('candidate', {})
        if event['kind'] == 'video_analysis_candidate' and c.get('reference_task_id') == reference_id:
            path = resolve(store, run_id, c['name'])
            if path is None or hashlib.sha256(path.read_bytes()).hexdigest() != c['sha256']:
                raise ValueError('参考视频已变化，请重新分析后使用。')
            return c
    raise ValueError('这条参考尚未完成分析，请先读取参考任务或观察指定视频。')


def selection_for_reference(store, run_id, mode, reference_id=None, scope=''):
    """Build the exact selection without changing the project mode."""
    if mode not in MODES:
        raise ValueError('未知创作方式。')
    selection = {'mode': mode, 'reference_id': None, 'scope': ''}
    if mode != 'autonomous':
        if not isinstance(scope, str) or not scope.strip() or len(scope) > 2000:
            raise ValueError('请说明这次参考保留哪些方面；局部借鉴不能扩大为全片复刻。')
        c = reference_candidate(store, run_id, reference_id)
        selection.update(reference_id=reference_id, scope=scope.strip(), name=c['name'],
                         sha256=c['sha256'], analysis_id=c['analysis_id'], candidate_sha256=digest(c))
    elif reference_id or scope:
        raise ValueError('自主创作不能同时绑定指定参考。')
    return selection


def select(store, run_id, mode, reference_id=None, scope=''):
    selection = selection_for_reference(store, run_id, mode, reference_id, scope)
    if current(store, run_id) != selection:
        store.append_event(run_id, 'creation_mode_selected', selection=selection)
    return selection


def select_for_turn(store, run_id, *, intent, mode, reference_id=None, scope=''):
    """Keep an established reference scope on an ordinary continuation.

    A fresh model paraphrase of the same scope is not a new source decision.
    An edit or a different mode/reference still goes through ``select`` and
    advances the evidence generation when it actually changes the source.
    """
    previous = current(store, run_id)
    if (intent == 'continue' and previous and previous['mode'] == mode
            and previous.get('reference_id') == (reference_id if mode != 'autonomous' else None)):
        return previous
    return select(store, run_id, mode, reference_id, scope)


def context(store, run_id, *, selection=None):
    selection = selection if selection is not None else current(store, run_id)
    if not selection:
        return None
    result = {**selection, 'notice': '当前作品方式不是本轮执行、采用或生成授权。'}
    if selection['mode'] != 'autonomous':
        c = reference_candidate(store, run_id, selection['reference_id'])
        if (c['analysis_id'] != selection['analysis_id'] or c['sha256'] != selection['sha256']
                or digest(c) != selection['candidate_sha256']):
            raise ValueError('指定参考分析版本已变化，请重新明确采用的参考。')
        result['observation'] = {k: c[k] for k in ('name', 'sha256', 'analysis_id', 'findings', 'limitations',
            'timeline', 'reference_methods', 'audiovisual', 'observed_at') if k in c}
        complete_observation = 'timeline' in c and 'audiovisual' in c
        if not complete_observation:
            result['observation']['limitations'] = [*c.get('limitations', []),
                '历史记录缺少逐段画面或声音数据；只能回看已保存文字，不能宣称已完成完整声画观察。']
        result['observation'].update(level='audiovisual_review' if complete_observation else 'existence_only', purpose='reference',
            source_kind='requested_model_reference', reference_task_id=selection['reference_id'],
            method='本次明确指定的模型参考观察，未经人工核对，不是商品事实',
            observation_ref='reference:' + selection['analysis_id'])
    return result


def prompt(store, run_id):
    value = context(store, run_id)
    return ('当前作品方式与指定参考：\n' + json.dumps(value, ensure_ascii=False) if value else
            '当前尚未明确作品方式；默认自主创作，不因有参考文件而自动复刻。')
