"""Local US creative case curation and retrieval. No network, models or task state."""
import argparse
import copy
from datetime import date
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / 'tracks/us/creative/learning/library.json'
PURPOSES = ('message', 'storyboard', 'performance')
FEEDBACK_SCOPES = ('direction', 'persona_dialogue', 'pace', 'product_fact',
                   'input_drift', 'generation_execution', 'tool_failure', 'user_preference')
FEEDBACK_ACTIONS = ('diagnose', 'local_change', 'stop', 'select', 'continue')


def feedback_key(event):
    identity = [event.get(k) for k in ('run', 'artifact_version', 'source_event')]
    return 'feedback-' + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode('utf-8')).hexdigest()[:24]


def feedback_errors(event):
    required = {'run', 'artifact_version', 'source_event', 'source', 'recorded_at',
                'quote', 'scope', 'action', 'observation', 'hypothesis'}
    allowed = required | {'evidence_refs', 'counterexample_refs', 'improvement_effect'}
    errors = []
    if not isinstance(event, dict):
        return ['feedback must be an object']
    if not required <= event.keys() or event.keys() - allowed:
        errors.append('feedback fields missing or unsupported')
    for field in required - {'hypothesis'}:
        if not isinstance(event.get(field), str) or not event[field].strip():
            errors.append(f'feedback {field} must be nonempty text')
    if event.get('scope') not in FEEDBACK_SCOPES or event.get('action') not in FEEDBACK_ACTIONS:
        errors.append('feedback scope/action is unsupported')
    if event.get('hypothesis') is not None and (not isinstance(event['hypothesis'], str) or not event['hypothesis'].strip()):
        errors.append('feedback hypothesis must be text or null')
    try:
        date.fromisoformat(event.get('recorded_at', ''))
    except (TypeError, ValueError):
        errors.append('feedback recorded_at must be an ISO date')
    for field in ('evidence_refs', 'counterexample_refs'):
        refs = event.get(field, [])
        if not isinstance(refs, list) or any(not isinstance(ref, str) or '/' not in ref for ref in refs):
            errors.append(f'feedback {field} must contain case/evidence references')
    if event.get('improvement_effect') is not None:
        errors.append('feedback cannot claim an unobserved improvement; improvement_effect must be null')
    return errors


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def source_key(url=None, media=None):
    if media:
        return 'sha256:' + hashlib.sha256(Path(media).read_bytes()).hexdigest()
    parsed = urlsplit(url or '')
    match = re.fullmatch(r'/@[^/]+/video/(\d+)/?', parsed.path)
    if parsed.scheme != 'https' or parsed.hostname not in ('www.tiktok.com', 'tiktok.com') or not match or parsed.username:
        raise ValueError('Use a resolved public TikTok /@user/video/ID URL or a local media file; no redirects are fetched.')
    return 'tiktok:' + match[1]


def validate(doc):
    errors = []
    if doc.get('schema_version') != 1 or doc.get('market') != 'US':
        errors.append('Expected schema_version=1 and market=US.')
    if type(doc.get('library_version', 1)) is not int or doc.get('library_version', 1) < 1:
        errors.append('library_version must be a positive integer')
    cases = doc.get('cases', [])
    if not isinstance(cases, list) or not isinstance(doc.get('methods', []), list):
        return errors + ['cases and methods must be lists.']
    ids, keys, hashes, canonical_urls = set(), set(), set(), set()
    groups = {}
    evidence = {}
    feedback_ids = set()
    for c in cases:
        cid = c.get('id', '')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]+', cid) or cid in ids:
            errors.append(f'Invalid/duplicate case id: {cid}')
        ids.add(cid)
        key = c.get('source_key')
        if not key or key in keys:
            errors.append(f'{cid}: missing/duplicate source_key')
        keys.add(key)
        if c.get('source_url'):
            try:
                canonical = source_key(url=c['source_url'])
                if canonical != key or canonical in canonical_urls:
                    errors.append(f'{cid}: source_url identity mismatch or duplicate canonical video')
                canonical_urls.add(canonical)
            except ValueError:
                errors.append(f'{cid}: source_url must be a resolved public TikTok video URL')
        if c.get('market') != 'US':
            errors.append(f'{cid}: cross-market record is not allowed')
        if c.get('partition') not in ('calibration', 'holdout', 'pending'):
            errors.append(f'{cid}: invalid partition')
        group = c.get('comparison_group')
        if not isinstance(group, str) or not group:
            errors.append(f'{cid}: comparison_group required')
        else:
            groups.setdefault(group, set()).add(c.get('partition'))
        if not c.get('title') or not isinstance(c.get('tags'), list):
            errors.append(f'{cid}: title/tags required')
        asset = c.get('media')
        if asset:
            digest = asset.get('sha256', '')
            if not re.fullmatch('[a-f0-9]{64}', digest) or digest in hashes:
                errors.append(f'{cid}: invalid/duplicate media hash; merge observations into the existing case')
            hashes.add(digest)
        observations = c.get('evidence', [])
        eids = set()
        for e in observations:
            eid = e.get('id')
            if not eid or eid in eids or not e.get('source') or not e.get('observation'):
                errors.append(f'{cid}: evidence id/source/observation required and unique')
            eids.add(eid)
            if e.get('kind') not in ('visual_sample', 'asr_semantics', 'user_feedback', 'request_text', 'historical_report', 'full_av_review'):
                errors.append(f'{cid}/{eid}: unknown evidence kind')
            if e.get('kind') == 'full_av_review' and (not e.get('reviewer') or not e.get('reviewed_at') or not e.get('scope')):
                errors.append(f'{cid}/{eid}: audiovisual review needs reviewer, date and scope')
        evidence[cid] = eids
        history = c.get('feedback_history', [])
        if not isinstance(history, list):
            errors.append(f'{cid}: feedback_history must be a list')
            history = []
        for entry in history:
            if not isinstance(entry, dict):
                errors.append(f'{cid}: feedback_history entry must be an object')
                continue
            event = {k: v for k, v in entry.items() if k not in ('id', 'library_version')}
            errors.extend(f'{cid}: {e}' for e in feedback_errors(event))
            fid = entry.get('id')
            if fid != feedback_key(event) or fid in feedback_ids or fid not in eids:
                errors.append(f'{cid}: invalid/duplicate feedback identity or missing evidence')
            feedback_ids.add(fid)
            version = entry.get('library_version')
            if type(version) is not int or not 2 <= version <= doc.get('library_version', 1):
                errors.append(f'{cid}: invalid feedback library_version')
        eligible = c.get('eligible_for', [])
        if not isinstance(eligible, list) or any(p not in PURPOSES for p in eligible):
            errors.append(f'{cid}: invalid eligible_for')
        if eligible and (not observations or not c.get('strategy') or not c.get('limits')):
            errors.append(f'{cid}: learning requires evidence, strategy and limits')
        if 'performance' in eligible and not any(e.get('kind') == 'full_av_review' for e in observations):
            errors.append(f'{cid}: sampled frames/ASR cannot qualify performance')
        feedback = c.get('user_feedback')
        if feedback and (not feedback.get('scope') or not feedback.get('source')):
            errors.append(f'{cid}: user feedback needs exact scope and source')
        if c.get('market_effect') is not None and not c['market_effect'].get('source'):
            errors.append(f'{cid}: market result needs a source')
    for group, partitions in groups.items():
        if {'calibration', 'holdout'} <= partitions:
            errors.append(f'{group}: same-source calibration/holdout leakage')
    mids = set()
    for method in doc.get('methods', []):
        mid = method.get('id')
        if not mid or mid in mids:
            errors.append(f'Invalid/duplicate method id: {mid}')
        mids.add(mid)
        if method.get('status') not in ('hypothesis', 'user_calibrated', 'retired'):
            errors.append(f'{mid}: unsupported method status; no automatic validation claim')
        if not method.get('when') or not method.get('decision') or not method.get('countercondition') or not method.get('supports'):
            errors.append(f'{mid}: condition, decision, countercondition and evidence required')
        refs = []
        for field in ('supports', 'counterexamples'):
            values = method.get(field, [])
            if not isinstance(values, list) or any(not isinstance(ref, str) for ref in values):
                errors.append(f'{mid}: {field} must be a list of evidence references')
            else:
                refs.extend(values)
        for ref in refs:
            cid, _, eid = ref.partition('/')
            if eid not in evidence.get(cid, set()):
                errors.append(f'{mid}: unresolved evidence {ref}')
            elif method.get('status') != 'retired' and next(c for c in cases if c['id'] == cid)['partition'] != 'calibration':
                errors.append(f'{mid}: pending/holdout evidence cannot train an active method')
    for c in cases:
        for entry in c.get('feedback_history', []) if isinstance(c.get('feedback_history', []), list) else []:
            if not isinstance(entry, dict):
                continue
            for field in ('evidence_refs', 'counterexample_refs'):
                refs = entry.get(field, [])
                if not isinstance(refs, list):
                    continue
                for ref in refs:
                    if not isinstance(ref, str):
                        continue
                    cid, _, eid = ref.partition('/')
                    if eid not in evidence.get(cid, set()):
                        errors.append(f"{c['id']}: unresolved feedback evidence {ref}")
    return errors


def record_feedback(doc, case_id, event):
    """Append scoped evidence to an existing identity; never infer approval or execution authority."""
    errors = validate(doc) + feedback_errors(event)
    if errors:
        raise ValueError('; '.join(errors))
    if not any(c['id'] == case_id for c in doc['cases']):
        raise ValueError('Unknown case_id; register the source before recording feedback')
    event = copy.deepcopy(event)
    for key in ('evidence_refs', 'counterexample_refs'):
        event.setdefault(key, [])
    event.setdefault('improvement_effect', None)
    fid = feedback_key(event)
    for c in doc['cases']:
        for previous in c.get('feedback_history', []):
            if previous['id'] != fid:
                continue
            previous_event = {k: v for k, v in previous.items() if k not in ('id', 'library_version')}
            if c['id'] == case_id and previous_event == event:
                return copy.deepcopy(doc)
            raise ValueError('Conflicting feedback identity; preserve the original and use a new source_event for a correction')
    result = copy.deepcopy(doc)
    result['library_version'] = doc.get('library_version', 1) + 1
    case = next(c for c in result['cases'] if c['id'] == case_id)
    case.setdefault('feedback_history', []).append(dict(event, id=fid, library_version=result['library_version']))
    case['evidence'].append({'id': fid, 'kind': 'user_feedback', 'source': event['source'],
                             'observation': event['observation'], 'scope': event['scope']})
    errors = validate(result)
    if errors:
        raise ValueError('; '.join(errors))
    return result


def creator_library(doc, case_ids=None):
    """Export only eligible calibration seeds and complete relevant method dependencies.

    Dependency cases do not seed unrelated methods. Empty seeds are valid for original work.
    Unknown/ineligible explicit seeds fail closed; inaccessible dependencies omit their method.
    """
    available = {c['id']: copy.deepcopy(c) for c in doc['cases']
                 if c.get('partition') == 'calibration' and c.get('eligible_for')}
    # Remove unavailable feedback dependencies before selecting methods. Propagate
    # removals so a feedback-to-feedback reference cannot smuggle a test answer.
    while True:
        evidence_refs = {f"{cid}/{e['id']}" for cid, case in available.items() for e in case['evidence']}
        changed = False
        for case in available.values():
            history = case.get('feedback_history', [])
            blocked = {event['id'] for event in history
                       if not set(event.get('evidence_refs', []) + event.get('counterexample_refs', [])) <= evidence_refs}
            if blocked:
                case['feedback_history'] = [e for e in history if e['id'] not in blocked]
                case['evidence'] = [e for e in case['evidence'] if e['id'] not in blocked]
                changed = True
        empty = [cid for cid, case in available.items() if not case['evidence']]
        for cid in empty:
            del available[cid]
        if not changed and not empty:
            break
    seeds = set(available) if case_ids is None else set(case_ids)
    if seeds - available.keys():
        raise ValueError('Creator context contains unknown, pending, holdout or ineligible case ids')
    chosen = set(seeds)
    methods = []
    for method in doc['methods']:
        refs = method.get('supports', []) + method.get('counterexamples', [])
        dependencies = {ref.split('/')[0] for ref in refs}
        if method.get('status') == 'retired' or not dependencies & seeds or not dependencies <= available.keys():
            continue
        if any(ref.partition('/')[2] not in {e['id'] for e in available[ref.partition('/')[0]]['evidence']} for ref in refs):
            continue
        methods.append(copy.deepcopy(method))
        chosen.update(dependencies)
    # Valid feedback references are evidence dependencies too.
    while True:
        before = set(chosen)
        for cid in sorted(chosen):
            for event in available[cid].get('feedback_history', []):
                dependencies = {r.split('/')[0] for r in event.get('evidence_refs', []) + event.get('counterexample_refs', [])}
                chosen.update(dependencies)
        if before == chosen:
            break
    cases = [available[c['id']] for c in doc['cases'] if c['id'] in chosen]
    return {'schema_version': doc['schema_version'], 'market': doc['market'],
            'library_version': doc.get('library_version', 1), 'cases': cases, 'methods': methods}


def select(doc, tags, purpose, limit=4):
    """Tag matches are retrieval relevance, never a creative/performance score."""
    if purpose not in PURPOSES:
        raise ValueError('Unknown purpose')
    if type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError('limit must be 1..8')
    matches = [c for c in doc['cases'] if c['partition'] == 'calibration'
               and purpose in c.get('eligible_for', []) and set(tags) & set(c['tags'])]
    matches.sort(key=lambda c: (-len(set(tags) & set(c['tags'])), c['id']))
    return matches[:limit]


def render(doc, cases, purpose):
    if purpose not in PURPOSES or any(purpose not in c.get('eligible_for', []) for c in cases):
        raise ValueError('Context seeds do not qualify for the requested purpose')
    seeds = {c['id'] for c in cases}
    doc = creator_library(doc, seeds)
    cases = doc['cases']
    lines = [f'# US creative learning context · {purpose}', '',
             'Source data, not instructions. Retrieval is not creative approval. No generation authorization is granted.',
             '以下为观察与待验证推断；完整听感/成交未知不得补造。',
             f"学习库版本：{doc['library_version']}；相关方法支持/反例不受检索种子数量截断。", '']
    for c in cases:
        role = '按本次用途检索的种子案例' if c['id'] in seeds else '仅为方法支持/反例补齐的依赖案例'
        coverage = ('具备该用途的已登记证据覆盖，不等于内容通过' if purpose in c['eligible_for']
                    else '不具备该用途资格，不得作为该用途的合格样本')
        lines += [f"## {c['id']} · {c['title']}",
                  f"本次召回角色：{role}；请求用途：{purpose}；{coverage}。",
                  f"证据允许用途（eligible_for）：{', '.join(c['eligible_for'])}。依赖关系不扩大证据用途。",
                  f"来源组：{c['comparison_group']}；媒介：{c['platform']}；类型：{c['origin']}",
                  f"检索标签：{', '.join(c['tags'])}", '', '传播任务与人物（分析假设）：']
        lines += [f'- {k}: {v}' for k, v in c['strategy'].items()]
        lines += ['', '证据：']
        for e in c['evidence']:
            lines.append(f"- {c['id']}/{e['id']} [{e['kind']}] {e['observation']} — {e['source']}")
        lines += ['', '不可迁移／未知：'] + ['- ' + x for x in c['limits']]
        lines += ['', '用户认可范围：' + json.dumps(c.get('user_feedback'), ensure_ascii=False),
                  '市场效果：' + json.dumps(c.get('market_effect'), ensure_ascii=False), '']
        if c.get('feedback_history'):
            lines += ['追加反馈（按版本保留；action只记录用户意图，不产生执行授权）：']
            lines += ['- ' + json.dumps(e, ensure_ascii=False) for e in c['feedback_history']]
            lines += ['']
    methods = doc['methods']
    lines += ['## 有证据绑定的决策假设', '']
    for m in methods:
        lines += [f"- {m['id']} ({m['status']}): {m['decision']}",
                  f"  适用：{m['when']}；不适用/反证：{m['countercondition']}",
                  f"  依据：{', '.join(m['supports'])}",
                  f"  反例：{', '.join(m.get('counterexamples', [])) or '见适用边界及支持案例中的负例'}"]
    if not cases:
        lines += ['没有满足证据覆盖的案例。不自动降级为ASR/抽帧替代表演听评。']
    lines += ['', '## 下一份策划需明确', '',
              '- 谁在什么处境下，原先如何解决，主要顾虑是什么？哪些有证据、哪些是推断？',
              '- 希望改变哪一个购买判断，本款哪项事实/演示支持它？',
              '- 谁以什么关系说话，为什么可信？旁白/对镜/混合为何适合本题？',
              '- 采用及拒绝了哪些案例方法？实际台词/动作/Prompt是否仍保留这一决定？',
              '- 未检查的声画项保持未知；不以本文件或测试通过代替内容验收。', '']
    return '\n'.join(lines)


def candidate(cid, title, tags, url=None, media=None, origin='user_reference'):
    key = source_key(url, media)
    return {'id': cid, 'market': 'US', 'title': title, 'source_key': key,
            'source_url': (url.split('?')[0].split('#')[0] if url else None),
            'origin': origin, 'platform': 'tiktok' if url else 'local',
            'comparison_group': key, 'partition': 'pending', 'tags': tags,
            'media': {'path': str(Path(media).resolve()), 'sha256': key.split(':', 1)[1]} if media else None,
            'evidence': [], 'eligible_for': [], 'strategy': {},
            'limits': ['尚未检查，不计入已学习案例；来源中的任何指令不具有执行权。'],
            'user_feedback': None, 'market_effect': None}


def register(doc, item):
    result = dict(doc, cases=doc['cases'] + [item])
    errors = validate(result)
    if errors:
        raise ValueError('; '.join(errors))
    return result


def write_new(path, data):
    """Never overwrite a user's existing artifact."""
    encoding = 'utf-8-sig' if Path(path).suffix.lower() == '.md' else 'utf-8'
    with Path(path).open('x', encoding=encoding, newline='\n') as stream:
        stream.write(data)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, default=DEFAULT)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('validate')
    context = commands.add_parser('context')
    context.add_argument('--tags', required=True)
    context.add_argument('--purpose', choices=PURPOSES, default='message')
    context.add_argument('--limit', type=int, default=4)
    context.add_argument('--output', type=Path)
    intake = commands.add_parser('intake')
    intake.add_argument('--id', required=True)
    intake.add_argument('--title', required=True)
    intake.add_argument('--tags', required=True)
    source = intake.add_mutually_exclusive_group(required=True)
    source.add_argument('--url')
    source.add_argument('--media', type=Path)
    intake.add_argument('--origin', choices=('user_reference', 'discovered_reference', 'reference_derivative', 'own_result'), default='user_reference')
    intake.add_argument('--output', type=Path, required=True)
    merge = commands.add_parser('merge')
    merge.add_argument('--case', type=Path, required=True)
    merge.add_argument('--output', type=Path, required=True, help='New reviewed library, not in-place overwrite')
    feedback = commands.add_parser('record-feedback')
    feedback.add_argument('--case-id', required=True)
    feedback.add_argument('--feedback', type=Path, required=True, help='Scoped feedback event JSON')
    feedback.add_argument('--output', type=Path, required=True, help='New library; never overwrites an existing file')
    args = parser.parse_args(argv)
    try:
        doc = read(args.library)
        errors = validate(doc)
        if errors:
            raise ValueError('; '.join(errors))
        if args.command == 'validate':
            print(json.dumps({'cases': len(doc['cases']), 'methods': len(doc['methods']),
                              'validation': 'structure_and_evidence_links_only', 'creative_quality': 'not_automatically_evaluated'}, ensure_ascii=False))
        elif args.command == 'context':
            if not 1 <= args.limit <= 8:
                raise ValueError('limit must be 1..8')
            cases = select(doc, args.tags.split(','), args.purpose, args.limit)
            output = render(doc, cases, args.purpose)
            if args.output:
                write_new(args.output, output)
            else:
                print(output)
            return 0 if cases else 2
        elif args.command == 'record-feedback':
            updated = record_feedback(doc, args.case_id, read(args.feedback))
            write_new(args.output, json.dumps(updated, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps({'library_version': updated.get('library_version', 1),
                              'appended': updated != doc, 'execution_authorized': False}))
        else:
            item = read(args.case) if args.command == 'merge' else candidate(
                args.id, args.title, args.tags.split(','), args.url, args.media, args.origin)
            merged = register(doc, item)
            write_new(args.output, json.dumps(merged if args.command == 'merge' else item, ensure_ascii=False, indent=2) + '\n')
            print('Saved locally. Intake is not a learned/approved case; no media was downloaded or uploaded.')
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
