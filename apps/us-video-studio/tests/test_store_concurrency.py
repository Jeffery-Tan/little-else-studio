"""Real temporary journals preserve event identities across role/UI threads."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import json
import os
import threading

import pytest

from studio.config import Settings
from studio.jobs import JobManager
from studio.ledger import Ledger
from studio.pipeline import Pipeline
from studio.store import Store
import studio.store as store_module


RUN = 'concurrent-event-fixture'


def stores(tmp_path):
    first = Store(tmp_path)
    first.create_run(RUN, 'Synthetic concurrency test', 'No provider calls')
    return first, Store(tmp_path / 'runs' / '..')


@pytest.mark.parametrize('operation', ['append_event', 'read_events'])
def test_event_io_owns_shared_run_lock_at_journal_boundary(tmp_path, monkeypatch, operation):
    first, second = stores(tmp_path)
    original_read = store_module._read_jsonl
    probes = []

    def journal_read(path):
        if path.name == 'events.jsonl':
            # Probe in another thread: an RLock acquisition in this thread
            # would succeed even when the operation correctly owns the lock.
            def probe():
                lock = second.run_lock(RUN)
                acquired = lock.acquire(blocking=False)
                probes.append(acquired)
                if acquired:
                    lock.release()
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join(3)
            assert not thread.is_alive()
        return original_read(path)

    monkeypatch.setattr(store_module, '_read_jsonl', journal_read)
    if operation == 'append_event':
        first.append_event(RUN, 'fixture_progress')
    else:
        first.read_events(RUN)
    assert probes == [False]


def test_event_operations_are_reentrant_across_store_instances(tmp_path):
    first, second = stores(tmp_path)
    assert first.run_lock(RUN) is second.run_lock(RUN)

    def nested():
        with first.run_lock(RUN):
            second.append_event(RUN, 'fixture_progress')
            return second.read_events(RUN)

    with ThreadPoolExecutor(max_workers=1) as pool:
        rows = pool.submit(nested).result(3)
    assert [row['seq'] for row in rows] == [1]


def test_concurrent_events_have_unique_monotonic_sequences(tmp_path):
    first, second = stores(tmp_path)
    workers, per_worker = 8, 20
    start = threading.Barrier(workers)

    def write(worker):
        store = first if worker % 2 else second
        start.wait(3)
        for index in range(per_worker):
            # Some real callers hold a run lock, others rely on Store itself.
            if worker % 2:
                with store.run_lock(RUN):
                    store.append_event(RUN, 'fixture_progress', worker=worker, index=index)
            else:
                store.append_event(RUN, 'fixture_diagnostic', worker=worker, index=index)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(write, worker) for worker in range(workers)]
        for future in futures:
            future.result(15)
    rows = first.read_events(RUN)
    assert [row['seq'] for row in rows] == list(range(1, workers * per_worker + 1))
    assert len({(row['worker'], row['index']) for row in rows}) == workers * per_worker


def test_reader_waits_until_real_appended_line_is_complete(tmp_path, monkeypatch):
    first, second = stores(tmp_path)
    first.append_event(RUN, 'fixture_existing')
    partial_written, finish_write, reader_started = (threading.Event() for _ in range(3))

    def partial_append(path, record):
        raw = json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n'
        cut = len(raw) // 2
        with path.open('a', encoding='utf-8', newline='\n') as handle:
            handle.write(raw[:cut])
            handle.flush()
            os.fsync(handle.fileno())
            partial_written.set()
            assert finish_write.wait(3)
            handle.write(raw[cut:])
            handle.flush()
            os.fsync(handle.fileno())

    def read():
        reader_started.set()
        return second.read_events(RUN)

    monkeypatch.setattr(store_module, '_append_jsonl_after_check', partial_append)
    with ThreadPoolExecutor(max_workers=2) as pool:
        write_future = pool.submit(first.append_event, RUN, 'fixture_new')
        try:
            assert partial_written.wait(3)
            read_future = pool.submit(read)
            assert reader_started.wait(3)
            with pytest.raises(FutureTimeout):
                read_future.result(.1)
        finally:
            finish_write.set()
        write_future.result(3)
        assert [row['seq'] for row in read_future.result(3)] == [1, 2]


def test_job_event_cursor_retains_all_concurrent_progress(tmp_path):
    first, second = stores(tmp_path)
    settings = Settings(data_root=tmp_path, base_url='http://127.0.0.1:1')
    jobs = JobManager(first, Pipeline(first), Ledger(first, settings), prepare=None)
    created = first.append_event(RUN, 'job_created', job_id='fixture-job',
                                 request_id='fixture-request', text='Synthetic task')
    workers, per_worker = 4, 12
    start = threading.Barrier(workers)

    def progress(worker):
        start.wait(3)
        for index in range(per_worker):
            if worker % 2:
                jobs._event(RUN, 'fixture-job', 'job_progress',
                            event={'type': 'status', 'identity': f'{worker}:{index}'})
            else:
                second.append_event(RUN, 'job_progress', job_id='fixture-job',
                    event={'type': 'status', 'identity': f'{worker}:{index}'})

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(progress, worker) for worker in range(workers)]
        for future in futures:
            future.result(15)
    cursor, received = created['seq'], []
    # A reconnect at each individual seq must retain all following events.
    while True:
        page = jobs.get(RUN, 'fixture-job', after_seq=cursor)['events']
        if not page:
            break
        event = page[0]
        received.append(event['identity'])
        cursor = event['seq']
    assert len(received) == workers * per_worker
    assert len(set(received)) == workers * per_worker
    assert jobs.event_marks(RUN, 'fixture-job', after_seq=created['seq']) == [
        {'seq': seq, 'kind': 'job_progress'} for seq in range(2, cursor + 1)]
