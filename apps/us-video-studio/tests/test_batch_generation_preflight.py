"""One-call batch checks on real temporary files; no model or provider submission."""
import hashlib
import threading

import pytest

from studio.materials import accept_upload
from studio.models import PipelineError
from studio.production import ActionIn, BatchGenerateIn, DraftIn, prepare_draft, draft_records
from studio.store import Store
from test_production import scene, adopt_shot_count, png
from test_server import RUN


@pytest.fixture
def prepared(scene, request):
    count = getattr(request, 'param', 2)
    adopt_shot_count(scene, count)
    service = scene[0].app.state.production
    store = scene[1]
    source = store.latest_artifact(RUN, 'writing')
    path = store.run_dir(RUN) / 'materials/reference.png'
    reference = {'name': 'reference.png', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                 'purpose': 'product'}
    rows = [prepare_draft(store, scene[2], RUN, DraftIn(
        request_id=f'preflight-shot-{index}', kind='image',
        prompt=f'Synthetic product shot {index}; do not actually generate.',
        source_version=source.version, source_sha256=source.sha256,
        references=[reference], model='codex-image', shot_id=segment['shot_id']))
        for index, segment in enumerate(source.structured['segments'], 1)]
    service.image_probe()
    payload = BatchGenerateIn(request_id='isolated-preflight', consent=True, nonce=service.nonce(RUN),
        expected_connection_revision=service.connections.public()['revision'],
        drafts=[{'draft_id': row['draft_id'], 'expected_sha256': row['draft_sha256']} for row in rows])
    return service, payload, rows, scene


def assert_no_submission(prepared):
    service, _, _, scene = prepared
    assert not scene[3].calls and not scene[4].calls and not scene[5].calls
    assert not service.jobs.ledger.entries(RUN)
    assert not any(row['kind'] in {'job_created', 'production_attempt'} for row in scene[1].read_events(RUN))


@pytest.mark.parametrize('prepared', [4], indirect=True)
def test_batch_checks_each_exact_input_once_and_default_single_entry_still_reads_fresh(prepared, monkeypatch):
    service, payload, rows, scene = prepared
    calls = []
    original = service.get_draft
    def tracked(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(service, 'get_draft', tracked)
    before = (scene[1].run_dir(RUN) / 'events.jsonl').read_bytes()
    runner, config = service.prepare_batch_generation(RUN, payload)
    assert calls == [(RUN, row['draft_id'], row['draft_sha256'], True) for row in rows]
    assert config['kind'] == 'image_batch' and callable(runner)
    assert not {'draft_reader', 'read_checked', 'checked', '_draft_reader'} & set(runner.__code__.co_freevars)
    assert (scene[1].run_dir(RUN) / 'events.jsonl').read_bytes() == before
    assert_no_submission(prepared)
    calls.clear()
    single = ActionIn(request_id='isolated-single', expected_sha256=rows[0]['draft_sha256'],
        consent=True, nonce=payload.nonce, expected_connection_revision=payload.expected_connection_revision)
    service.prepare_generation(RUN, rows[0]['draft_id'], single)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert_no_submission(prepared)


def test_preflight_returns_deep_copies_and_reader_expires_without_entering_runners(prepared, monkeypatch):
    service, payload, rows, scene = prepared
    captured = []
    runners = []
    original = service._prepare_batch_generation
    original_single = service.prepare_generation
    def inspect(run_id, submitted, reader):
        captured.append(reader)
        row = rows[0]
        first = reader(run_id, row['draft_id'], row['draft_sha256'], True)
        first['references'][0]['purpose'] = 'scene'
        first['design_binding']['scene_id'] = 'fabricated-test-scene'
        again = reader(run_id, row['draft_id'], row['draft_sha256'], True)
        assert again['references'][0]['purpose'] == 'product'
        assert again['design_binding']['scene_id'] != first['design_binding']['scene_id']
        return original(run_id, submitted, reader)
    def single(*args, **kwargs):
        result = original_single(*args, **kwargs)
        runners.append(result[0])
        return result
    monkeypatch.setattr(service, '_prepare_batch_generation', inspect)
    monkeypatch.setattr(service, 'prepare_generation', single)
    before = draft_records(scene[1], RUN)
    service.prepare_batch_generation(RUN, payload)
    assert draft_records(scene[1], RUN) == before
    assert all(not {'reader', '_draft_reader', 'read_checked', 'checked'} & set(r.__code__.co_freevars) for r in runners)
    with pytest.raises(ValueError, match='预检已失效'):
        captured[0](RUN, rows[0]['draft_id'], rows[0]['draft_sha256'], True)
    assert_no_submission(prepared)


def test_exception_discards_checked_rows_and_next_request_rechecks_them(prepared, monkeypatch):
    service, payload, rows, _ = prepared
    captured, calls = [], []
    original = service._prepare_batch_generation
    original_get = service.get_draft
    def tracked(*args, **kwargs):
        calls.append(args)
        return original_get(*args, **kwargs)
    def fail(run_id, submitted, reader):
        captured.append(reader)
        reader(run_id, rows[0]['draft_id'], rows[0]['draft_sha256'], True)
        raise RuntimeError('Synthetic preflight failure')
    monkeypatch.setattr(service, 'get_draft', tracked)
    monkeypatch.setattr(service, '_prepare_batch_generation', fail)
    with pytest.raises(RuntimeError, match='Synthetic preflight failure'):
        service.prepare_batch_generation(RUN, payload)
    with pytest.raises(ValueError, match='预检已失效'):
        captured[0](RUN, rows[0]['draft_id'], rows[0]['draft_sha256'], True)
    monkeypatch.setattr(service, '_prepare_batch_generation', original)
    service.prepare_batch_generation(RUN, payload)
    assert [call[1] for call in calls] == [rows[0]['draft_id'], *[row['draft_id'] for row in rows]]
    assert_no_submission(prepared)


@pytest.mark.parametrize('scope', ['other_run', 'unchecked_source', 'other_thread'])
def test_active_reader_rejects_cross_scope_use(prepared, monkeypatch, scope):
    service, payload, rows, _ = prepared
    original = service._prepare_batch_generation
    errors = []
    def inspect(run_id, submitted, reader):
        row = rows[0]
        args = (run_id, row['draft_id'], row['draft_sha256'], True)
        if scope == 'other_run':
            args = ('other-synthetic-run', *args[1:])
        elif scope == 'unchecked_source':
            args = (*args[:3], False)
        def attempt():
            try:
                reader(*args)
            except ValueError as exc:
                errors.append(str(exc))
        if scope == 'other_thread':
            worker = threading.Thread(target=attempt)
            worker.start(); worker.join(3)
            assert not worker.is_alive()
        else:
            attempt()
        assert len(errors) == 1 and '预检已失效' in errors[0]
        return original(run_id, submitted, reader)
    monkeypatch.setattr(service, '_prepare_batch_generation', inspect)
    service.prepare_batch_generation(RUN, payload)
    assert_no_submission(prepared)


@pytest.mark.parametrize('bad', ['nonce', 'consent', 'fingerprint', 'duplicate'])
def test_exact_confirmation_checks_still_reject_without_submission(prepared, bad):
    service, payload, _, _ = prepared
    data = payload.model_dump()
    if bad == 'nonce': data['nonce'] = 'wrong'
    elif bad == 'consent': data['consent'] = False
    elif bad == 'fingerprint': data['drafts'][1]['expected_sha256'] = '0' * 64
    else: data['drafts'][1] = dict(data['drafts'][0])
    with pytest.raises((ValueError, PipelineError)):
        service.prepare_batch_generation(RUN, BatchGenerateIn(**data))
    assert_no_submission(prepared)


@pytest.mark.parametrize('changed', ['reference', 'script', 'connection'])
def test_next_preflight_and_frozen_runner_recheck_changes_without_media_calls(prepared, changed):
    service, payload, _, scene = prepared
    runner, _ = service.prepare_batch_generation(RUN, payload)
    if changed == 'reference':
        (scene[1].run_dir(RUN) / 'materials/reference.png').write_bytes(png((241, 240)))
    elif changed == 'script':
        source = scene[1].latest_artifact(RUN, 'writing')
        (scene[1].run_dir(RUN) / f'artifacts/writing-v{source.version}.md').write_text('Synthetic tampering', encoding='utf-8')
    else:
        config = service.connections.public()
        service.connections.save({**config, 'expected_revision': config['revision']})
    with pytest.raises((ValueError, PipelineError)):
        service.prepare_batch_generation(RUN, payload)
    with pytest.raises((ValueError, PipelineError)):
        runner('', lambda _: None, lambda: None)
    assert_no_submission(prepared)


def test_other_store_cannot_change_sources_mid_preflight_and_worker_checks_after_release(prepared, monkeypatch):
    service, payload, _, scene = prepared
    entered, release, waiting, changed = (threading.Event() for _ in range(4))
    original = service.get_draft
    results, errors = [], []
    def pause(*args, **kwargs):
        value = original(*args, **kwargs)
        if not entered.is_set():
            entered.set()
            assert release.wait(5), 'Synthetic preflight was not released'
        return value
    monkeypatch.setattr(service, 'get_draft', pause)
    def prepare():
        try: results.append(service.prepare_batch_generation(RUN, payload))
        except Exception as exc: errors.append(exc)
    def change():
        other = Store(scene[1].root)
        waiting.set()
        with other.run_lock(RUN):
            accept_upload(other, RUN, 'new-synthetic-source.png', png((242, 240)))
        changed.set()
    first = threading.Thread(target=prepare)
    second = threading.Thread(target=change)
    first.start()
    try:
        assert entered.wait(5)
        second.start()
        assert waiting.wait(3)
        assert not changed.wait(.05)
    finally:
        release.set()
        first.join(10)
        if second.ident is not None: second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert not errors and len(results) == 1 and changed.is_set()
    with pytest.raises(PipelineError, match='依据'):
        results[0][0]('', lambda _: None, lambda: None)
    assert_no_submission(prepared)
