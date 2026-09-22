"""A derived creative brief, not another evidence store or a truth classifier."""
import hashlib
import json

from .evidence import evidence_snapshot


def creative_evidence(store, run_id, *, source=None):
    source = source if source is not None else evidence_snapshot(store, run_id)
    result = {'product_observations': [], 'reference_observations': [], 'unclassified_observations': [], 'gaps': [],
              'manifest_sha256': source['manifest_sha256'], 'errors': source['errors'],
              'notice': '以下是有来源的观察记录，不表示本角色亲自看过。参考方法是待验证假设，不能成为本款事实、真实用户经历或成交证明。',
              'creative_use': ['导演先把购买判断与本款证据对应，再借鉴参考的画面功能和推进顺序；不复制身份、性能或流量结论。',
                              '编剧把可借鉴方法落实到具体动作、对白与观众新增理解；台词只能使用当前商品依据。',
                              '注明引用的 observation_ref、哪些内容采用或不采用、仍需验证什么。未覆盖处保留假设，不凭标题补造原片。']}
    for material in source['materials']:
        for observation in material['observations']:
            digest = hashlib.sha256(json.dumps(observation, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            item = {'name': material['name'], 'material_sha256': material['sha256'],
                    'observation_ref': f"observation:{material['sha256']}:{digest}",
                    'origin': material.get('origin', 'uploaded'), **observation}
            purpose = observation.get('purpose')
            if purpose == 'product' and material.get('origin') != 'generated':
                result['product_observations'].append(item)
            elif purpose == 'reference':
                result['reference_observations'].append(item)
            else:
                result['unclassified_observations'].append(item)
                result['gaps'].append({'name': material['name'], 'reason': '生成素材不能作为商品事实' if material.get('origin') == 'generated'
                    else '观察未明确商品/参考用途，不能自动归类为商品事实', 'owner': 'team'})
        if not material['observations']:
            result['gaps'].append({'name': material['name'], 'reason': '只有文件，尚无当前字节版本的已采用观察',
                'next_action': '使用素材观察入口处理；可先讨论假设，不宣称已经看过', 'owner': 'team'})
        elif material['kind'] == 'video':
            modes = {o['level'] for o in material['observations']}
            if 'audiovisual_review' not in modes:
                result['gaps'].append({'name': material['name'], 'reason': '尚无完整声画观察；抽帧与转写不能合并冒充完整看听',
                    'observed_modes': sorted(modes), 'owner': 'team'})
    from .creation_mode import context
    try:
        selected = context(store, run_id)
        if selected:
            result['creation_mode'] = {k: v for k, v in selected.items() if k != 'observation'}
            result['reference_observations'] = [selected['observation']] if 'observation' in selected else []
    except ValueError as exc:
        result['reference_observations'] = []
        result['errors'] = [*result['errors'], str(exc)]
    return result
