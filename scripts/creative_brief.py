"""Compile an evidence-linked US creative brief; verify prompt bytes before submission.

Local preparation only. Structural consistency is not creative approval or spending permission.
"""
import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

from creative_learning import DEFAULT, creator_library, read, validate as validate_library


def digest(data):
    return hashlib.sha256(data).hexdigest()


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label}: non-empty text required')
    return value


def validate(plan, library):
    errors = validate_library(library)
    if errors:
        raise ValueError('; '.join(errors))
    if plan.get('schema_version') != 1 or plan.get('market') != 'US':
        raise ValueError('Expected schema_version=1 and market=US')
    for field in ('id', 'product_family_id', 'purpose', 'audience_situation', 'current_alternative',
                  'buying_hesitation', 'belief_change', 'speaker_relationship', 'form_reason',
                  'opening_reason_to_watch', 'payoff', 'failure_condition'):
        text(plan.get(field), field)
    if plan.get('audience_status') not in ('hypothesis', 'source_supported'):
        raise ValueError('Audience status must distinguish hypothesis from source-supported')
    if plan['audience_status'] == 'source_supported':
        text(plan.get('audience_source'), 'audience_source')
    cases = {case['id']: case for case in library['cases']}
    decisions = plan.get('reference_decisions', [])
    mode = plan.get('reference_mode', 'references')
    if mode not in ('references', 'original_product_facts'):
        raise ValueError('Unknown reference_mode')
    if not isinstance(decisions, list):
        raise ValueError('reference_decisions must be a list')
    if mode == 'original_product_facts':
        text(plan.get('reference_reason'), 'original reference_reason')
        if decisions:
            raise ValueError('Original product-facts mode cannot contain reference decisions')
    elif not decisions:
        raise ValueError('Explicit reference decisions required')
    for decision in decisions:
        cid, _, eid = text(decision.get('evidence'), 'evidence').partition('/')
        case = cases.get(cid)
        if not case or case['partition'] != 'calibration' or not set(case.get('eligible_for', [])) & {'message', 'storyboard'}:
            raise ValueError('Only eligible calibration evidence may inform this brief')
        if eid not in {item['id'] for item in case['evidence']}:
            raise ValueError('Unresolved evidence reference')
        if decision.get('decision') not in ('adapt', 'reject'):
            raise ValueError('Reference decision must be adapt or reject')
        text(decision.get('reason'), 'reference decision reason')
        text(decision.get('transfer_limit'), 'reference transfer limit')
    facts = plan.get('product_facts', [])
    if not isinstance(facts, list) or not facts:
        raise ValueError('Product evidence required')
    fact_ids = set()
    for fact in facts:
        fid = text(fact.get('id'), 'fact id')
        if fid in fact_ids:
            raise ValueError('Duplicate product fact id')
        fact_ids.add(fid)
        text(fact.get('statement'), 'product statement')
        text(fact.get('source'), 'product source')
        text(fact.get('limit'), 'product evidence limit')
    scenes = plan.get('scenes', [])
    if not isinstance(scenes, list) or not scenes:
        raise ValueError('At least one scene required')
    previous, scene_ids = 0, set()
    for scene in scenes:
        sid = text(scene.get('id'), 'scene id')
        if sid in scene_ids:
            raise ValueError('Duplicate scene id')
        scene_ids.add(sid)
        start, end = scene.get('start'), scene.get('end')
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, end)) or start != previous or end <= start:
            raise ValueError('Scenes must have finite, contiguous increasing times starting at zero')
        previous = end
        for field in ('visual', 'new_information', 'viewer_can_infer', 'delivery', 'action_end_state'):
            text(scene.get(field), f'{sid}/{field}')
        if scene.get('speech_mode') not in ('on_camera', 'voiceover', 'none'):
            raise ValueError('Each scene needs an explicit speech mode')
        if scene['speech_mode'] == 'none':
            if scene.get('spoken_text'):
                raise ValueError('A non-speaking scene cannot contain spoken text')
        else:
            text(scene.get('spoken_text'), f'{sid}/spoken_text')
            text(scene.get('speech_function'), f'{sid}/speech_function')
        if scene['speech_mode'] == 'on_camera' and scene.get('face_visible') is not True:
            raise ValueError('On-camera speech requires visible face; choose voiceover for other scenes')
        refs = scene.get('product_fact_ids', [])
        if not isinstance(refs, list) or not set(refs) <= fact_ids:
            raise ValueError('Scene refers to an unknown product fact')
    return plan


def compile_prompt(plan):
    """Emit scene-specific speech instructions, never a hidden global lip-sync ban."""
    parts = [f"Create a US product video for {plan['product_family_id']}.",
             f"Communication purpose: {plan['purpose']}",
             f"Audience situation ({plan['audience_status']}): {plan['audience_situation']}",
             f"Current alternative: {plan['current_alternative']}",
             f"Buying hesitation: {plan['buying_hesitation']}",
             f"Intended belief change: {plan['belief_change']}",
             f"Speaker relationship: {plan['speaker_relationship']}",
             f"Speaking-form rationale: {plan['form_reason']}",
             f"Opening reason to watch: {plan['opening_reason_to_watch']}",
             f"Payoff: {plan['payoff']}",
             'Product evidence boundaries (do not invent additional claims):']
    if plan.get('reference_mode') == 'original_product_facts':
        parts.insert(-1, 'Original concept based on product facts; no usable learning reference. '
                     f"Reason: {plan['reference_reason']}")
    for fact in plan['product_facts']:
        parts.append(f"- {fact['statement']} Limit: {fact['limit']}")
    for scene in plan['scenes']:
        mode = {
            'on_camera': 'The visible speaker says these words on camera with synchronized mouth movement.',
            'voiceover': 'These words are off-camera voiceover for this scene.',
            'none': 'No spoken words in this scene; use the specified visual and sound delivery.'
        }[scene['speech_mode']]
        parts += ['', f"Scene {scene['id']}, {scene['start']}–{scene['end']} seconds.",
                  scene['visual'], f"Action end state: {scene['action_end_state']}", mode,
                  f"Spoken text: {scene.get('spoken_text', '')}",
                  f"Purpose of these words: {scene.get('speech_function', 'No spoken words')}",
                  f"Delivery: {scene['delivery']}",
                  f"New information: {scene['new_information']}",
                  f"What the visible/audible evidence should let the viewer infer: {scene['viewer_can_infer']}"]
    return '\n'.join(parts) + '\n'


def compile_files(plan_path, library_path, out):
    plan_bytes, library_bytes = plan_path.read_bytes(), library_path.read_bytes()
    # Parse the same bytes that are frozen/hashed, not a second filesystem read.
    plan = json.loads(plan_bytes.decode('utf-8-sig'))
    library = json.loads(library_bytes.decode('utf-8-sig'))
    validate(plan, library)
    subset = creator_library(library, {d['evidence'].split('/')[0]
                                       for d in plan.get('reference_decisions', [])})
    validate(plan, subset)
    snapshot_bytes = (json.dumps(subset, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    prompt = compile_prompt(plan)
    # Refuse replacement of any earlier frozen brief or user work.
    out.mkdir(parents=True, exist_ok=False)
    (out / 'brief.json').write_bytes(plan_bytes)
    (out / 'library-snapshot.json').write_bytes(snapshot_bytes)
    (out / 'prompt.txt').write_bytes(prompt.encode('utf-8'))
    record = {'schema_version': 2, 'market': 'US', 'brief_id': plan['id'],
              'brief_sha256': digest(plan_bytes), 'library_sha256': digest(library_bytes),
              'library_schema_version': library['schema_version'],
              'library_snapshot_sha256': digest(snapshot_bytes),
              'library_snapshot_scope': 'eligible_calibration_dependency_closure',
              'prompt_sha256': digest(prompt.encode('utf-8')),
              'structural_validation': 'passed', 'creative_review': 'not_performed_by_compiler',
              'generation_authorized': False, 'references_approved': False}
    (out / 'manifest.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    lines = [f"# {plan['id']} · 创意准备包", '',
             '结构校验已过；不是内容通过、图审或费用授权。未生成媒体。', '',
             '## 传播任务', '',
             f"处境：{plan['audience_situation']}（{plan['audience_status']}）",
             f"原有办法：{plan['current_alternative']}；顾虑：{plan['buying_hesitation']}",
             f"希望改变的判断：{plan['belief_change']}",
             f"人物关系：{plan['speaker_relationship']}；形式依据：{plan['form_reason']}",
             f"开场：{plan['opening_reason_to_watch']}；结尾：{plan['payoff']}", '', '## 案例取舍', '']
    if plan.get('reference_mode') == 'original_product_facts':
        lines += [f"基于商品事实原创；没有可用参考。原因：{plan['reference_reason']}"]
    for decision in plan.get('reference_decisions', []):
        lines += [f"- {decision['evidence']} / {decision['decision']}：{decision['reason']}；迁移边界：{decision['transfer_limit']}"]
    lines += ['', '## 片内证据与观众推断', '']
    for scene in plan['scenes']:
        lines += [f"### {scene['id']} / {scene['start']}–{scene['end']} 秒 / {scene['speech_mode']}", '',
                  f"看到：{scene['visual']}", f"听到：{scene.get('spoken_text') or '无口播'}",
                  f"台词作用：{scene.get('speech_function', '无')}；新增信息：{scene['new_information']}",
                  f"凭此能推断：{scene['viewer_can_infer']}", f"表演与声音：{scene['delivery']}", '']
    lines += ['## 助手内容自审仍需完成', '',
              '检查人物理由、可见证据和实际表达，不把填写字段视作内容成立。',
              '以正常速度读完整稿，核实等待、理由、动作窗口；字数和每分钟词数仅供预算。',
              f"否决/停止条件：{plan['failure_condition']}",
              '实际提交前用verify对读最终prompt；图片/预算/完整请求继续走原执行器审核。', '']
    (out / 'review.md').write_text('\n'.join(lines), encoding='utf-8-sig')
    return record


def verify(compiled, actual_prompt):
    record = read(compiled / 'manifest.json')
    version = record.get('schema_version')
    if version not in (1, 2):
        raise ValueError('Unsupported compiled manifest schema_version')
    library_key = 'library_snapshot_sha256' if version == 2 else 'library_sha256'
    for filename, key in [('brief.json', 'brief_sha256'), ('library-snapshot.json', library_key), ('prompt.txt', 'prompt_sha256')]:
        if digest((compiled / filename).read_bytes()) != record[key]:
            raise ValueError(f'{filename} changed since compilation; compile a new version')
    plan, library = read(compiled / 'brief.json'), read(compiled / 'library-snapshot.json')
    validate(plan, library)
    if version == 2 and creator_library(library, {d['evidence'].split('/')[0]
                                                 for d in plan.get('reference_decisions', [])}) != library:
        raise ValueError('Creator snapshot contains material outside its calibration dependency scope')
    expected = compile_prompt(plan).encode('utf-8')
    if (compiled / 'prompt.txt').read_bytes() != expected or actual_prompt.read_bytes() != expected:
        raise ValueError('Actual prompt differs from the reviewed brief. Reconcile the brief and recompile; do not append conflicting instructions.')
    return {'prompt_matches_brief': True, 'creative_approval': False, 'generation_authorization': False}


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    make = sub.add_parser('compile')
    make.add_argument('--brief', type=Path, required=True)
    make.add_argument('--library', type=Path, default=DEFAULT)
    make.add_argument('--output-dir', type=Path, required=True)
    check = sub.add_parser('verify')
    check.add_argument('--compiled', type=Path, required=True)
    check.add_argument('--actual-prompt', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = compile_files(args.brief, args.library, args.output_dir) if args.command == 'compile' else verify(args.compiled, args.actual_prompt)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
