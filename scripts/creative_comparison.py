"""Offline comparison of supplied creative inputs and outputs; no generation or network."""
import argparse
import difflib
import hashlib
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Covers small ASR end-time rounding only; never normalizes or clips source timing.
ASR_END_TOLERANCE_SECONDS = 0.05


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def artifact(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError('Artifact must be a local file.')
    return {'path': path.as_posix(), 'sha256': digest(path), 'bytes': path.stat().st_size}


def verify(item, label):
    if not isinstance(item, dict) or not isinstance(item.get('path'), str):
        raise ValueError(f'{label}: expected a local path and sha256')
    if not Path(item['path']).is_absolute():
        raise ValueError(f'{label}: absolute local path required')
    actual = artifact(item['path'])
    if actual['sha256'] != item.get('sha256'):
        raise ValueError(f'{label}: sha256 mismatch; re-check the intended version')
    if 'bytes' in item and actual['bytes'] != item['bytes']:
        raise ValueError(f'{label}: size mismatch')
    return actual


def template(case_id):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]+', case_id):
        raise ValueError('case_id must be lowercase letters, digits and hyphens')
    return {
        'schema_version': 1, 'market': 'US', 'case_id': case_id,
        'reference_video': None, 'reference_asr': None,
        'runs': [{
            'id': name, 'reference_video': None, 'reference_images': None,
            'reference_images_status': 'unknown', 'user_prompt': None,
            'backend_prompt': None, 'output_video': None, 'output_asr': None,
            'input_binding_evidence': None,
            'model_parameters': {'status': 'unknown', 'values': {}, 'source': None},
        } for name in ('oumomo', 'local')],
        'observations': [],
        'limits': ['No architecture, downloader, hidden model or causal performance inference.'],
    }


def probe(path):
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)],
        capture_output=True, text=True, encoding='utf-8', check=True, timeout=60)
    data = json.loads(result.stdout)
    video = next((s for s in data['streams'] if s['codec_type'] == 'video'), None)
    if not video:
        raise ValueError('Expected a video stream')
    return {'duration_seconds': float(data['format']['duration']),
            'width': video['width'], 'height': video['height'],
            'fps': video.get('avg_frame_rate'),
            'has_audio': any(s['codec_type'] == 'audio' for s in data['streams']),
            'scope': 'container/stream metadata only; not decode, listening or content approval'}


def finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def inspect_asr(item, media, duration_seconds):
    if item is None:
        return None
    if not finite_number(duration_seconds) or duration_seconds <= 0:
        raise ValueError('ASR validation needs a positive finite media duration')
    bound = verify(item, 'ASR')
    data = read(bound['path'])
    if data.get('sha256') != media['sha256']:
        raise ValueError('ASR media hash mismatch')
    segments = data.get('segments')
    if not isinstance(segments, list):
        raise ValueError('ASR segments must be a list')
    for s in segments:
        if not isinstance(s, dict) or not isinstance(s.get('text'), str):
            raise ValueError('Invalid ASR segment')
        start, end = s.get('start'), s.get('end')
        if not finite_number(start) or not finite_number(end):
            raise ValueError('ASR timestamps must be finite numbers, not strings or booleans')
        if not (0 <= start <= end <= duration_seconds + ASR_END_TOLERANCE_SECONDS):
            raise ValueError('ASR segment outside media duration (50ms end-rounding tolerance)')
    return {'artifact': bound, 'method': data.get('method', 'unknown'),
            'segments': [{'start': s['start'], 'end': s['end'], 'text': s['text'].strip()}
                         for s in segments],
            'timing_validation': {'source_duration_seconds': duration_seconds,
                                  'end_rounding_tolerance_seconds': ASR_END_TOLERANCE_SECONDS,
                                  'timestamps_modified': False},
            'scope': 'existing ASR semantics only; not verified pronunciation, affect or exact timing'}


def compare(doc, probe_fn=probe):
    if doc.get('schema_version') != 1 or doc.get('market') != 'US':
        raise ValueError('Expected schema_version=1 and market=US')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]+', doc.get('case_id', '')):
        raise ValueError('Invalid case_id')
    runs = doc.get('runs')
    if not isinstance(runs, list) or not runs:
        raise ValueError('At least one run is required')
    missing, results, ids, parameter_limits = [], [], set(), []
    reference = verify(doc['reference_video'], 'reference_video') if doc.get('reference_video') else None
    if not reference:
        missing.append('reference_video')
        if doc.get('reference_asr'):
            raise ValueError('Reference ASR without reference video')
    ref_result = None
    if reference:
        reference_technical = probe_fn(reference['path'])
        ref_result = {'artifact': reference, 'technical': reference_technical,
                      'asr': inspect_asr(doc.get('reference_asr'), reference, reference_technical['duration_seconds'])}
    for run in runs:
        rid = run.get('id', '')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]+', rid) or rid in ids:
            raise ValueError('Run ids must be valid and unique')
        ids.add(rid)
        checked = {}
        for key in ('reference_video', 'user_prompt', 'backend_prompt', 'output_video', 'input_binding_evidence'):
            if run.get(key):
                checked[key] = verify(run[key], f'{rid}.{key}')
            else:
                missing.append(f'{rid}.{key}')
                checked[key] = None
        images = run.get('reference_images')
        image_status = run.get('reference_images_status')
        if image_status not in ('unknown', 'supplied', 'confirmed_none'):
            raise ValueError(f'{rid}: invalid reference_images_status')
        if image_status == 'unknown':
            if images not in (None, []):
                raise ValueError(f'{rid}: unknown images cannot have an asserted complete set')
            missing.append(f'{rid}.reference_images')
        elif not isinstance(images, list) or (image_status == 'supplied' and not images) or (image_status == 'confirmed_none' and images):
            raise ValueError(f'{rid}: inconsistent image set')
        checked['reference_images'] = [verify(a, f'{rid}.reference_images') for a in (images or [])]
        checked['reference_images_status'] = image_status
        params = run.get('model_parameters', {})
        if params.get('status') not in ('unknown', 'partial', 'documented') or not isinstance(params.get('values'), dict):
            raise ValueError(f'{rid}: invalid model_parameters')
        if params['status'] != 'unknown' and (not params['values'] or not params.get('source')):
            raise ValueError(f'{rid}: documented parameter values need source evidence')
        if params['status'] == 'unknown' and params['values']:
            raise ValueError(f'{rid}: unknown parameters cannot contain asserted values')
        checked['model_parameters'] = dict(params)
        if params.get('source'):
            checked['model_parameters']['source'] = verify(params['source'], f'{rid}.model_parameters.source')
        if params['status'] != 'documented':
            parameter_limits.append(f'{rid}: parameters {params["status"]}; hidden settings stay unknown, no controlled causal attribution')
        video = checked['output_video']
        checked['technical'] = probe_fn(video['path']) if video else None
        if run.get('output_asr') and not video:
            raise ValueError(f'{rid}: ASR without output')
        checked['asr'] = inspect_asr(run.get('output_asr'), video, checked['technical']['duration_seconds']) if video else None
        checked['prompt_delta'] = None
        if checked['user_prompt'] and checked['backend_prompt']:
            before = Path(checked['user_prompt']['path']).read_text(encoding='utf-8-sig').splitlines()
            after = Path(checked['backend_prompt']['path']).read_text(encoding='utf-8-sig').splitlines()
            checked['prompt_delta'] = '\n'.join(difflib.unified_diff(before, after, fromfile='user_prompt', tofile='backend_prompt', lineterm=''))
        results.append({'id': rid, **checked})
    observations = []
    for observation in doc.get('observations', []):
        if observation.get('kind') not in ('historical_report', 'asr_semantics', 'user_feedback'):
            raise ValueError('Observations must declare their actual evidence scope')
        if not observation.get('claim') or not observation.get('limits'):
            raise ValueError('Observation needs claim and limits')
        observations.append({**observation, 'source': verify(observation['source'], 'observation.source')})
    if len(results) < 2:
        missing.append('second_run')
    input_sets = []
    for run in results:
        if run['reference_video'] and run['user_prompt'] and run['reference_images_status'] != 'unknown':
            input_sets.append((run['reference_video']['sha256'], run['user_prompt']['sha256'],
                               tuple(a['sha256'] for a in run['reference_images'])))
    equal = len(input_sets) == len(results) and len(results) >= 2 and len(set(input_sets)) == 1
    if not equal:
        missing.append('identical_reference_video_images_and_user_prompt_across_runs')
    return {'schema_version': 1, 'market': 'US', 'case_id': doc['case_id'],
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'status': 'ready_for_observable_comparison' if not missing else 'not_ready_for_same_input_comparison',
            'missing': missing, 'reference': ref_result, 'runs': results, 'observations': observations,
            'same_inputs_verified': equal and all(r['input_binding_evidence'] for r in results),
            'parameter_limits': parameter_limits,
            'hidden_architecture': 'unknown', 'creative_acceptance': 'not_assessed',
            'causal_effect': 'not_established', 'limits': doc.get('limits', [])}


def render(report):
    lines = [f"# {report['case_id']}", '', f"Status: {report['status']}", '',
             '本地材料对照；没有上传、下载、生成或付费调用。', '',
             '## 已有材料', '']
    for name, item in [('reference', report['reference'])] + [(r['id'], r) for r in report['runs']]:
        if item and item.get('technical'):
            tech = item['technical']
            lines.append(f"- {name}: {tech['duration_seconds']:.3f}s; {tech['width']}×{tech['height']}; audio={tech['has_audio']}。仅媒体参数。")
        if item and item.get('asr'):
            lines.extend(['', f'### {name} / 既有ASR，非听评', ''])
            for s in item['asr']['segments']:
                lines.append(f"- {s['start']:.2f}–{s['end']:.2f}s: {s['text']}")
        if item and item.get('prompt_delta') is not None:
            lines.extend(['', f'### {name} / 用户与后台提示词文本差异', '', '```diff', item['prompt_delta'] or '(identical)', '```'])
    lines.extend(['', '## 受限观察', ''])
    for o in report['observations']:
        lines.append(f"- [{o['kind']}] {o['claim']} 边界：{o['limits']}")
    lines.extend(['', '## 补齐项', ''] + [f'- {m}' for m in report['missing']])
    lines.extend(['', '## 判断边界', '', '输入齐全只表示可以比较可观察的处理差异，不证明幕后使用什么模型、下载器或系统架构。',
                  '编码、分辨率、时长、提示词相似均不能识别隐藏模型；ASR和技术检查不能证明原速听感或商业效果。'])
    lines.extend('- ' + x for x in report['limits'])
    lines.extend('- ' + x for x in report['parameter_limits'])
    return '\n'.join(lines) + '\n'


def write_new(path, text):
    path = Path(path).resolve()
    if path.is_relative_to(ROOT):
        raise ValueError('Runtime inputs/reports must remain outside the Git workspace')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init'); init.add_argument('case_id'); init.add_argument('--output', required=True)
    bind = sub.add_parser('hash'); bind.add_argument('path')
    run = sub.add_parser('compare'); run.add_argument('manifest'); run.add_argument('--output-dir', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'hash':
            print(json.dumps(artifact(args.path), ensure_ascii=False, indent=2))
        elif args.command == 'init':
            write_new(args.output, json.dumps(template(args.case_id), ensure_ascii=False, indent=2) + '\n')
            print('Created local comparison intake. All absent inputs remain unknown.')
        else:
            report = compare(read(args.manifest))
            report['manifest'] = artifact(args.manifest)
            dest = Path(args.output_dir)
            if (dest / 'comparison.json').exists() or (dest / 'comparison.md').exists():
                raise ValueError('Report already exists; use a new revision directory')
            write_new(dest / 'comparison.json', json.dumps(report, ensure_ascii=False, indent=2) + '\n')
            write_new(dest / 'comparison.md', render(report))
            print(json.dumps({'status': report['status'], 'report': str(dest / 'comparison.md'), 'missing': report['missing']}, ensure_ascii=False))
            return 0 if not report['missing'] else 2
    except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError) as exc:
        print(f'Comparison failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
