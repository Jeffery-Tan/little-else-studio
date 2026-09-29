"""Bounded live check of the storyboard delivery contract on a real HTTP server.

Different from the pytest suite: this starts an actual uvicorn process against a
throwaway data root outside Git, seeds a project with a three-segment script,
drives the real HTTP API, and prints what the page and the controller would each
see. No model is called and no media is generated.

    python scripts/check_storyboard_live.py
"""
from __future__ import annotations

import json
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

from studio.pipeline import Pipeline  # noqa: E402
from studio.production import agent_production_state  # noqa: E402
from studio.store import Store  # noqa: E402

RUN = 'live-storyboard'
PORT = 4764
SCRIPT = {
    'segments': [
        {'tc': '0-2s', 'picture': '包出现在桌面', 'line': 'One.', 'sound': 'ambient', 'duration': 2},
        {'tc': '2-4s', 'picture': '拉链打开', 'line': 'Two.', 'sound': 'zip', 'duration': 2},
        {'tc': '4-6s', 'picture': '午餐装进包', 'line': 'Three.', 'sound': 'ambient', 'duration': 2},
    ],
    'total_duration': 6,
}


def call(method, path, body=None, base=f'http://127.0.0.1:{PORT}'):
    data = None if body is None else json.dumps(body).encode('utf-8')
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode('utf-8')


def seed(root):
    store = Store(root)
    store.create_run(RUN, 'Live storyboard check', 'Synthetic only; no generation')
    pipeline = Pipeline(store)
    for stage in ['intake', 'direction', 'material_plan', 'writing']:
        attempt = pipeline.start_attempt(RUN, stage)
        structured = SCRIPT if stage == 'writing' else (
            {'options': [{'key': 'A', 'claim': 'Synthetic', 'satisfies': 'goal.primary'}]}
            if stage == 'direction' else {})
        pipeline.submit(RUN, stage, attempt.token, content='Synthetic ' + stage, summary=stage,
                        evidence=['Fixture only'], limitations=['Not market evidence'],
                        structured=structured)
        if stage in {'direction', 'writing'}:
            pipeline.record_approval(RUN, stage, nonce=pipeline.issue_nonce(RUN, stage),
                                     decision='approved',
                                     selected_option_id='A' if stage == 'direction' else None)
    return store, pipeline


DECISION_RUN = 'live-decision'


def seed_decision_gate(root):
    """A second project sitting on the direction gate, where a decision waits.

    Separate from the storyboard run because that one has already adopted its
    direction — a decided version offers nothing further, which is the point
    worth checking, but it leaves nothing to decide.
    """
    store = Store(root)
    store.create_run(DECISION_RUN, 'Live decision check', 'Synthetic only')
    pipeline = Pipeline(store)
    for stage in ['intake', 'direction']:
        attempt = pipeline.start_attempt(DECISION_RUN, stage)
        structured = ({'options': [{'key': key, 'claim': f'claim {key}',
                                    'satisfies': 'goal.primary'} for key in 'ABC']}
                      if stage == 'direction' else {})
        pipeline.submit(DECISION_RUN, stage, attempt.token, content='Synthetic ' + stage,
                        summary=stage, structured=structured)
    return store, pipeline


def main():
    root = Path(tempfile.mkdtemp(prefix='studio-live-'))
    store, pipeline = seed(root)
    print(f'data root: {root}')
    process = subprocess.Popen(
        [sys.executable, '-m', 'studio.server', '--port', str(PORT)],
        cwd=str(APP), env={**__import__('os').environ, 'STUDIO_DATA_ROOT': str(root)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                status, runtime = call('GET', '/api/runtime')
                if status == 200:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        else:
            raise SystemExit('live server did not come up')
        print(f"runtime fingerprint: {runtime.get('code_fingerprint') or runtime.get('backend_sha256') or 'n/a'}")

        # ---- F5: the conversation can carry a decision, and only a decision ----
        from studio.decision import (
            candidates, commit, continuation_intent, continuation_text, resolve,
        )
        dstore, dpipeline = seed_decision_gate(root)
        waiting = candidates(dstore, dpipeline, DECISION_RUN)
        print('\n[F5-1] what the run is waiting on, as the agent reads it')
        print('    question:', waiting[0]['question'] if waiting else '(nothing)')
        print('    answers :', [a['action'] for a in waiting[0]['answers']] if waiting else [])

        verdict = resolve(waiting, {'action': 'select_direction',
                                    'target': {'stage': 'direction', 'option_id': 'B'},
                                    'option_id': 'B'})
        print('    a stated decision resolves to:', verdict['status'])

        ambiguous = resolve(waiting, {'action': 'select_direction'})
        print('    naming no option asks instead:', ambiguous['status'], '·', ambiguous['reason'])

        result = commit(dstore, dpipeline, DECISION_RUN, verdict['action'],
                        source='conversation',
                        user_message='B 吧，开头生活化一点，然后继续出分镜。',
                        note='开头生活化一点')
        approval = dstore.list_approvals(DECISION_RUN, 'direction')[-1]
        print('\n[F5-2] recorded without a click')
        print('    decision:', approval.decision, '· option:', approval.selected_option_id,
              '· source:', approval.source)
        print('    adopted version:', approval.artifact_version, approval.artifact_sha256[:12])
        print('    instruction for next round:', approval.continuation_note,
              '· changes recorded against the adopted version:', repr(approval.note))
        intent = continuation_intent(dstore, dpipeline, DECISION_RUN)
        print('    follow-up work opened:', intent is not None,
              '· request_id:', (intent['request_id'][:24] + '…') if intent else '')
        print('    the instruction reaches the follow-up:',
              '生活化一点' in continuation_text(approval))
        print('    nothing is left to decide on that version:',
              candidates(dstore, dpipeline, DECISION_RUN) == [])
        print('    a decision naming an option that does not exist is refused:',
              resolve([{'question': 'q', 'stage': 'direction', 'version': 1,
                        'artifact_sha256': 'x' * 64, 'label': '创意方向',
                        'target': {'stage': 'direction', 'version': 1,
                                   'artifact_sha256': 'x' * 64},
                        'answers': [{'action': 'select_direction', 'class': 'creative',
                                     'options': [{'id': 'A', 'label': 'A'}]}]}],
                      {'action': 'select_direction', 'option_id': 'Z'})['reason'])

        status, page = call('GET', f'/api/runs/{RUN}/production')
        assert status == 200, page
        print('\n[1] target shot list from the page payload')
        print('   ', [s['shot_id'] for s in page['targets']['image']['shots']])
        print('    coverage:', {k: page['shots']['image'][k]
                                for k in ('total', 'ready', 'delivered', 'unready_shots')})

        source = page['sources'][-1]
        for index in (1, 2, 3):
            status, saved = call('POST', f'/api/runs/{RUN}/production/drafts', {
                'request_id': f'frame-{index}', 'kind': 'image', 'prompt': f'Frame {index}',
                'source_version': source['version'], 'source_sha256': source['artifact_sha256'],
                'model': 'codex-image', 'shot_id': f'shot-{index:02d}'})
            assert status == 200, saved

        status, page = call('GET', f'/api/runs/{RUN}/production')
        print('\n[2] after all three inputs are prepared')
        print('    coverage:', {k: page['shots']['image'][k]
                                for k in ('total', 'ready', 'delivered', 'unready_shots')})
        print('    stages:', [row['stage_label'] for row in page['shots']['image']['shots']])

        print('\n[3] revise shot 2 by replacing its input')
        status, revised = call('POST', f'/api/runs/{RUN}/production/drafts', {
            'request_id': 'frame-2-v2', 'kind': 'image', 'prompt': 'Frame 2, natural',
            'source_version': source['version'], 'source_sha256': source['artifact_sha256'],
            'model': 'codex-image', 'supersedes': saved and 'frame-2'})
        assert status == 200, revised
        status, page = call('GET', f'/api/runs/{RUN}/production')
        coverage = page['shots']['image']
        print('    total shots still:', coverage['total'])
        print('    shot 2 now points at:', coverage['shots'][1]['draft_id'],
              '· superseded:', coverage['shots'][1]['superseded_draft_ids'])
        replaced = next(d for d in page['drafts'] if d['draft_id'] == 'frame-2')
        print('    replaced input is_current/can_generate:',
              replaced['is_current'], replaced['can_generate'])

        print('\n[4] what the controller reads (agent projection, redacted)')
        agent = agent_production_state(store, pipeline, RUN)
        print('    targets:', [s['shot_id'] for s in agent['targets']['image']['shots']])
        print('    shot stages:', [(row['ordinal'], row['stage_label'])
                                   for row in agent['shots']['image']['shots']])
        blob = json.dumps(agent, ensure_ascii=False)
        for secret in ('nonce', 'materials/', 'endpoint'):
            print(f'    contains {secret!r}:', secret in blob)

        print('\n[5] the page and the controller agree on per-shot stage:',
              [row['stage'] for row in page['shots']['image']['shots']]
              == [row['stage'] for row in agent['shots']['image']['shots']])

        # Scenario F: a confirmation prepared against one snapshot must not be
        # usable after a shot changes. This is the whole point of binding it.
        from studio.decision import prepare_generation_candidate

        before = prepare_generation_candidate(store, pipeline, RUN, 'image')
        print('\n[F] the confirmation a conversation could open')
        print('    covers', before['count'] if before else 0, 'shots ·',
              'cost known:', before['estimate']['known'] if before else None)
        stale = [dict(row) for row in before['target']['drafts']] if before else []
        status, stale_refused = call('POST', f'/api/runs/{RUN}/production/batch-generate', {
            'request_id': 'stale-confirm',
            'drafts': [{'draft_id': 'frame-1', 'expected_sha256': 'f' * 64}],
            'consent': True, 'nonce': page['nonce']})
        print('    an authorisation whose inputs moved is refused:', status)
        print('    (the frozen set it was prepared for was', len(stale), 'drafts)')
        attempts = [e for e in store.read_events(RUN) if e['kind'] == 'production_attempt']
        print('    nothing was submitted by preparing or by the stale attempt:', attempts == [])

        status, refused = call('POST', f'/api/runs/{RUN}/production/drafts', {
            'request_id': 'bad-replace', 'kind': 'image', 'prompt': 'x',
            'source_version': source['version'], 'source_sha256': source['artifact_sha256'],
            'model': 'codex-image', 'shot_id': 'shot-03', 'supersedes': 'frame-2-v2'})
        print('\n[6] naming one shot while replacing another is refused:', status)
        print('    reason:', refused if isinstance(refused, str) else refused)

        # The acceptance table asks that an unadopted or changed script be named
        # for what it is, rather than met with a generic "check the parameters".
        attempt = pipeline.start_attempt(RUN, 'writing')
        pipeline.submit(RUN, 'writing', attempt.token, content='Revised script', summary='v2',
                        structured=SCRIPT)
        revised_script = store.latest_artifact(RUN, 'writing')
        status, unadopted = call('POST', f'/api/runs/{RUN}/production/drafts', {
            'request_id': 'after-change', 'kind': 'image', 'prompt': 'x',
            'source_version': revised_script.version, 'source_sha256': revised_script.sha256,
            'model': 'codex-image', 'shot_id': 'shot-01'})
        print('\n[7] script saved but not adopted yet:', status)
        detail = unadopted.get('detail') if isinstance(unadopted, dict) else str(unadopted)
        print('    reason:', detail)
        print('    names the real cause (not "检查参数"):',
              '核对当前已批准脚本、参考图和参数' not in detail)
        print('\nlive check complete; no model call, no media generation.')
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == '__main__':
    main()
