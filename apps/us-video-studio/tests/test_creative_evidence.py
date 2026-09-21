"""Fixed business scenarios evaluated at the actual director input boundary."""
import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from fakes import ScriptedModel, tool_turn
from studio.config import Settings
from studio.creative_evidence import creative_evidence
from studio.ledger import Ledger
from studio.materials import accept_upload
from studio.pipeline import Pipeline
from studio.store import Store
from studio.tools.dispatch import Dispatcher

CASES = json.loads((Path(__file__).parent / 'fixtures/creative-evidence-cases.json').read_text('utf-8'))['cases']
RUN = 'creative-evidence-fixture'


@pytest.mark.parametrize('case', CASES, ids=lambda c: c['id'])
def test_fixed_business_case_reaches_director_before_first_draft(tmp_path, case):
    store = Store(tmp_path)
    store.create_run(RUN, 'Synthetic creative evaluation', 'Current product evidence only')
    image = io.BytesIO()
    Image.new('RGB', (8, 8), 'red').save(image, format='PNG')
    name = 'fixture.mp4' if case['kind'] == 'video' else 'fixture.png'
    raw = b'synthetic placeholder; no decoding in these evidence tests' if case['kind'] == 'video' else image.getvalue()
    accept_upload(store, RUN, name, raw)
    sha = hashlib.sha256(raw).hexdigest()
    if case.get('generated'):
        store.append_event(RUN, 'production_output', output={'name': name, 'sha256': sha})
    if case['level']:
        record = dict(name=name, sha256='0' * 64 if case.get('stale') else sha,
            purpose=case['purpose'], level=case['level'], observer='human:fixture',
            method='synthetic test record', coverage='explicit fixture scope', observed_at='2026-09-21T00:00:00Z',
            findings=[case['finding']], limitations=['Not a factual business claim'])
        path = store.run_dir(RUN) / 'evidence/observations.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'schema_version': 1, 'observations': [record]}), encoding='utf-8')
    context = creative_evidence(store, RUN)
    for bucket in ('product_observations', 'reference_observations', 'unclassified_observations'):
        assert len(context[bucket]) == int(bucket == case['expected_bucket'])
    if case['gap']:
        assert any(case['gap'] in g['reason'] for g in context['gaps'])
    pipeline = Pipeline(store)
    attempt = pipeline.start_attempt(RUN, 'intake')
    pipeline.submit(RUN, 'intake', attempt.token, content='Only fixture product facts', summary='facts')
    settings = Settings(tmp_path, 'https://example.invalid')
    model = ScriptedModel([tool_turn('save', 'submit_stage', {'content': 'Fixture direction with limitations', 'summary': 'fixture'})])
    result = Dispatcher(store, pipeline, settings, model, Ledger(store, settings), RUN).dispatch('direction', 'Compare creative choices')
    # Broken evidence is allowed as a visible gap, not silently upgraded or
    # converted into a universal prohibition on discussing hypothetical ideas.
    assert result.submitted == 'direction-v1'
    seed = model.calls[0]['messages'][0]['content']
    assert '创作前证据整理' in seed and '参考方法是待验证假设' in seed
    if case['expected_bucket']:
        assert case['finding'] in seed
        assert 'observation:' + sha in seed
    elif case['finding']:
        assert case['finding'] not in seed
    assert '不能移植的因素' in model.calls[0]['system']
