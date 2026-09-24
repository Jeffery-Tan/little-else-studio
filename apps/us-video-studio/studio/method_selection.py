"""Trace a creator's method decision without asserting creative effectiveness."""
from __future__ import annotations

import re

from .learning import LearningConflict
from .models import STAGES


def record_method_usage(store, learning, run_id: str, payload: dict) -> dict:
    if store.read_run(run_id).kind != 'creative':
        raise ValueError('Method usage belongs to a creative run')
    if not isinstance(payload, dict):
        raise ValueError('Method usage must be an object')
    request_id = payload.get('request_id')
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request_id):
        raise ValueError('A valid method usage request_id is required')
    purpose = payload.get('purpose', 'message')
    summary = learning.method_summaries(run_id=run_id, purpose=purpose,
                                        include_trial=False)
    if payload.get('library_sha256') != summary['library_sha256']:
        raise LearningConflict('Learning library changed; read methods again')
    method = next((row for row in summary['methods'] if row['id'] == payload.get('method_id')),
                  None)
    if method is None or method['revision'] != payload.get('revision'):
        raise ValueError('Method identity or revision is not eligible for this task')
    decision = payload.get('decision')
    if decision not in ('chosen', 'rejected'):
        raise ValueError('Method decision must be chosen or rejected')
    reason = payload.get('reason')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise ValueError('A brief selection reason is required')
    artifact_ref = None
    if decision == 'chosen':
        stage, version, digest = (payload.get(key) for key in
                                  ('stage', 'version', 'artifact_sha256'))
        passage = payload.get('passage')
        if stage not in STAGES or type(version) is not int or version < 1:
            raise ValueError('Chosen method requires an exact creative artifact')
        artifact = store.read_artifact(run_id, stage, version)
        if artifact.sha256 != digest or not store.verify_artifact(run_id, stage, version):
            raise LearningConflict('The creative artifact changed')
        text = store.read_artifact_text(run_id, stage, version)
        if not isinstance(passage, str) or not passage.strip() or passage not in text:
            raise ValueError('The affected passage must occur in the exact artifact')
        artifact_ref = {'stage': stage, 'version': version,
                        'sha256': digest, 'passage': passage}
    canonical = {'request_id': request_id, 'method_id': method['id'],
                 'revision': method['revision'], 'status': method['status'],
                 'trial_scope': method.get('trial_scope'),
                 'library_sha256': summary['library_sha256'],
                 'library_version': summary['library_version'],
                 'purpose': purpose, 'task_profile': store.read_run(run_id).brief,
                 'decision': decision, 'reason': reason,
                 'artifact': artifact_ref,
                 'assessment': 'agent_claim_only; implementation and effectiveness unverified'}
    with store.run_lock(run_id):
        for event in store.read_events(run_id):
            if event['kind'] != 'method_usage_recorded' or event.get('request_id') != request_id:
                continue
            existing = {key: event.get(key) for key in canonical}
            if existing == canonical:
                return event
            raise LearningConflict('The method usage request_id already has different content')
        return store.append_event(run_id, 'method_usage_recorded', **canonical)
