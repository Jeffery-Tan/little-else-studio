"""Live check of the F5 confirmation card: the set is exact and nothing is submitted.

Runs a real server against a throwaway data root, prepares a real storyboard, and
then asks the two questions scenario E turns on:

* does the whole-set request produce a confirmation bound to the exact shots; and
* does merely preparing that confirmation submit anything to a provider?

No model is called and no media is generated.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
sys.stdout.reconfigure(encoding='utf-8')

from studio.decision import prepare_generation_candidate  # noqa: E402
from studio.pipeline import Pipeline  # noqa: E402
from studio.production import DraftIn, draft_records, prepare_draft  # noqa: E402
from studio.store import Store  # noqa: E402

RUN = 'live-confirm'
PORT = 4765
SCRIPT = {
    'segments': [
        {'tc': '0-2s', 'picture': '包出现在桌面', 'line': 'One.', 'sound': 'ambient', 'duration': 2},
        {'tc': '2-4s', 'picture': '拉链打开', 'line': 'Two.', 'sound': 'zip', 'duration': 2},
        {'tc': '4-6s', 'picture': '午餐装进包', 'line': 'Three.', 'sound': 'ambient', 'duration': 2},
    ],
    'total_duration': 6,
}


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode('utf-8')
    request = urllib.request.Request(f'http://127.0.0.1:{PORT}' + path, data=data,
                                     method=method, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode('utf-8')


def seed(root):
    store = Store(root)
    store.create_run(RUN, 'Live confirmation check', 'Synthetic only; no generation')
    pipeline = Pipeline(store)
    for stage in ['intake', 'direction', 'material_plan', 'writing']:
        attempt = pipeline.start_attempt(RUN, stage)
        structured = SCRIPT if stage == 'writing' else (
            {'options': [{'key': 'A', 'claim': 'Synthetic', 'satisfies': 'goal.primary'}]}
            if stage == 'direction' else {})
        pipeline.submit(RUN, stage, attempt.token, content='Synthetic ' + stage, summary=stage,
                        structured=structured)
        if stage in {'direction', 'writing'}:
            pipeline.record_approval(RUN, stage, nonce=pipeline.issue_nonce(RUN, stage),
                                     decision='approved',
                                     selected_option_id='A' if stage == 'direction' else None)
    source = store.latest_artifact(RUN, 'writing')
    for index in (1, 2, 3):
        prepare_draft(store, pipeline, RUN, DraftIn(
            request_id=f'frame-{index}', kind='image', prompt=f'Frame {index} description.',
            source_version=source.version, source_sha256=source.sha256,
            model='codex-image', shot_id=f'shot-{index:02d}'), actor='agent')
    return store, pipeline


def main():
    root = Path(tempfile.mkdtemp(prefix='studio-confirm-'))
    store, pipeline = seed(root)
    print(f'data root: {root}')
    process = subprocess.Popen(
        [sys.executable, '-m', 'studio.server', '--port', str(PORT)],
        cwd=str(APP), env={**os.environ, 'STUDIO_DATA_ROOT': str(root)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                if call('GET', '/api/runtime')[0] == 200:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        else:
            raise SystemExit('live server did not come up')

        status, page = call('GET', f'/api/runs/{RUN}/production')
        assert status == 200, page
        drafts = [d for d in page['drafts'] if d['kind'] == 'image' and d.get('is_current')]
        print('\n[E-1] the set a whole-set request would confirm')
        print('    shots with input:', [d['shot_id'] for d in drafts])
        print('    per-item cost known:',
              [isinstance(d.get('estimate', {}).get('amount'), (int, float)) for d in drafts])

        candidate = prepare_generation_candidate(store, pipeline, RUN, 'image')
        print('    server-side frozen set:', [row['draft_id'] for row in candidate['target']['drafts']])
        print('    total estimate known:', candidate['estimate']['known'],
              '· amount:', candidate['estimate']['amount'])
        print('    class:', candidate['class'], '(never submitted from conversation)')

        # The page payload is what the conversation card renders from, so the two
        # must describe the same set.
        page_ids = sorted(d['draft_id'] for d in drafts)
        frozen_ids = sorted(row['draft_id'] for row in candidate['target']['drafts'])
        print('    card and page describe the same set:', page_ids == frozen_ids)

        before = [e for e in store.read_events(RUN) if e['kind'] == 'production_attempt']
        print('\n[E-2] preparing submits nothing')
        print('    provider attempts before:', len(before))
        print('    (no batch-generate call was made; only the read above)')

        status, refused = call('POST', f'/api/runs/{RUN}/production/batch-generate', {
            'request_id': 'no-nonce', 'drafts': [
                {'draft_id': d['draft_id'], 'expected_sha256': d['draft_sha256']} for d in drafts],
            'consent': True, 'nonce': 'wrong'})
        print('\n[E-3] a confirmation without the browser nonce is refused:', status)
        after = [e for e in store.read_events(RUN) if e['kind'] == 'production_attempt']
        print('    provider attempts after:', len(after))
        print('\nconfirm check complete; no model call, no media generation.')
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == '__main__':
    main()
