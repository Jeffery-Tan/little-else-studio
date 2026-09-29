"""A direction continuation is one pinned stage task, without a producer loop."""
from __future__ import annotations

import threading

from local_http import TestClient
from fakes import ScriptedModel, final_turn, tool_turn

from studio.config import Pricing, Settings
from studio.direction_command import available, require_current
from studio.models import PipelineError
from studio.pipeline import Pipeline
from studio.server import build_app
from studio.store import Store


RUN = 'direction-command-01'


def settings_for(root):
    return Settings(data_root=root, base_url='https://example.invalid/anthropic',
        budget_limit_usd=10.0, max_tool_rounds=4,
        pricing=Pricing(source='test', input_usd_per_mtok=1.0,
                        output_usd_per_mtok=2.0, as_of='2026-09-28'))


def seed(root, *, old_direction=True):
    store = Store(root)
    store.create_run(RUN, '可平铺小包', '用参考表达做一条有场景的商品视频。')
    pipeline = Pipeline(store)
    attempt = pipeline.start_attempt(RUN, 'intake')
    pipeline.submit(RUN, 'intake', attempt.token, content='小包可平铺，有三种背法。',
                    summary='商品与视频目标')
    if old_direction:
        attempt = pipeline.start_attempt(RUN, 'direction')
        pipeline.submit(RUN, 'direction', attempt.token, content='旧稿：拍连续形变。',
                        summary='旧方向')
        attempt = pipeline.start_attempt(RUN, 'intake')
        pipeline.submit(RUN, 'intake', attempt.token,
                        content='不拍完整形变；切到半打开状态。',
                        summary='已按用户反馈更新需求')
    return store, pipeline


def wait_job(http, job_id):
    for _ in range(300):
        job = http.get(f'/api/runs/{RUN}/jobs/{job_id}').json()
        if job['status'] in {'succeeded', 'failed', 'blocked', 'cancelled', 'interrupted'}:
            return job
        threading.Event().wait(.01)
    raise AssertionError('offline direction job did not settle')


def options():
    return {'options': [
        {'key': key, 'title': title, 'claim': claim,
         'audience_moment': '出门前整理随身物品',
         'hook': hook, 'story': story, 'gives_up': '只突出一个卖点',
         'evidence_strength': '商品图可见', 'difficulty': '中',
         'asset_needs': '现有商品图', 'satisfies': 'goal.primary'}
        for key, title, claim, hook, story in [
            ('A', '半开一眼', '不用展示完整形变', '从半开的包开场', '切到半开状态，再背上出门。'),
            ('B', '一天三背', '同一只包换三种背法', '出门前换背法', '城市一天里换背法，收在背影。'),
            ('C', '普通小包', '合上仍像普通小包', '先看闭合外形', '露出打开状态，接着背出门。'),
        ]], 'recommended': 'A', 'recommended_because': '符合当前不拍连续形变的条件。'}


def test_direction_command_is_bound_to_current_upstream_and_old_version(tmp_path):
    store, pipeline = seed(tmp_path)
    command = available(store, pipeline, RUN)
    assert command and command['kind'] == 'resume_direction'
    assert pipeline.stage_state(RUN, 'direction').stale
    require_current(store, pipeline, RUN, command['expected_context_sha256'])

    attempt = pipeline.start_attempt(RUN, 'intake')
    pipeline.submit(RUN, 'intake', attempt.token, content='新增条件：改为桌边开场。',
                    summary='新需求')
    assert available(store, pipeline, RUN)['expected_context_sha256'] != command['expected_context_sha256']
    try:
        require_current(store, pipeline, RUN, command['expected_context_sha256'])
    except PipelineError as exc:
        assert exc.rule == 'direction_conflict'
    else:
        raise AssertionError('stale command was accepted')


def test_direction_resume_dispatches_only_director_and_exposes_three_choices(tmp_path):
    store, pipeline = seed(tmp_path)
    model = ScriptedModel([
        tool_turn('submit', 'submit_stage', {
            'content': '三个方向供 Jeffery 比较。', 'summary': '按半开状态重新构思',
            'structured': options()}),
        final_turn('已提交三个方向。'),
    ])
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        timeline = http.get(f'/api/runs/{RUN}/timeline').json()
        command = timeline['next_action']
        assert command['kind'] == 'resume_direction'
        assert timeline['brief']['action'] == command
        assert timeline['progress']['action'] == 'stage_command'

        bad = http.post(f'/api/runs/{RUN}/actions/resume-direction', json={
            'request_id': 'wrong-baseline', 'expected_context_sha256': '0' * 64})
        assert bad.status_code == 409
        assert model.calls == []

        payload = {'request_id': 'direction-once',
                   'expected_context_sha256': command['expected_context_sha256']}
        response = http.post(f'/api/runs/{RUN}/actions/resume-direction', json=payload)
        assert response.status_code == 200, response.text
        job = wait_job(http, response.json()['job_id'])
        assert job['status'] == 'succeeded', job['reason']
        assert job['job_kind'] == 'direction_resume'
        assert len(model.calls) == 1
        assert model.calls[0]['tool_names'] and 'submit_stage' in model.calls[0]['tool_names']
        assert pipeline.stage_state(RUN, 'direction').latest.version == 2
        assert pipeline.stage_state(RUN, 'direction').stale is False
        pending = http.get(f'/api/runs/{RUN}/timeline').json()['pending']
        assert [row['key'] for row in pending['options']] == ['A', 'B', 'C']
        assert http.get(f'/api/runs/{RUN}/timeline').json()['next_action'] is None

        replay = http.post(f'/api/runs/{RUN}/actions/resume-direction', json=payload)
        assert replay.status_code == 200 and replay.json()['job_id'] == job['job_id']
        assert len(model.calls) == 1
        new_request = http.post(f'/api/runs/{RUN}/actions/resume-direction', json={
            **payload, 'request_id': 'should-not-regenerate'})
        assert new_request.status_code == 409
        assert len(model.calls) == 1


def test_direction_failure_leaves_a_recoverable_stage_without_auto_retry(tmp_path):
    store, pipeline = seed(tmp_path)
    model = ScriptedModel([final_turn('我只想了想，没有保存。')])
    with TestClient(build_app(settings_for(tmp_path), client_factory=lambda: model)) as http:
        command = http.get(f'/api/runs/{RUN}/timeline').json()['next_action']
        response = http.post(f'/api/runs/{RUN}/actions/resume-direction', json={
            'request_id': 'incomplete-once',
            'expected_context_sha256': command['expected_context_sha256']})
        assert response.status_code == 200
        job = wait_job(http, response.json()['job_id'])
        assert job['status'] in {'failed', 'blocked'}
        assert len(model.calls) == 1
        assert pipeline.stage_state(RUN, 'direction').open_attempt is None
        assert pipeline.stage_state(RUN, 'direction').latest.version == 1
        assert http.get(f'/api/runs/{RUN}/timeline').json()['next_action'] == command
