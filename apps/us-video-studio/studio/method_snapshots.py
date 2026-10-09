"""Frozen creative documents in the existing run; runtime rules remain current.

These files are input snapshots, never an editable second method library. Legacy
documents can only be recovered from fingerprinted, actually saved contexts.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json

from .store import _atomic_write_text


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def checked_document(value):
    if not isinstance(value, dict) or not isinstance(value.get('id'), str):
        raise ValueError('Method snapshot has no document identity')
    text, expected = value.get('text'), value.get('sha256')
    if not isinstance(text, str) or not isinstance(expected, str):
        raise ValueError('Method snapshot has no exact document text')
    raw = text.encode('utf-8')
    # Historical _text() decoded a BOM but retained the original byte hash.
    candidates = [(False, raw), (True, b'\xef\xbb\xbf' + raw)]
    matches = [bom for bom, data in candidates if hashlib.sha256(data).hexdigest() == expected]
    if len(matches) != 1:
        raise ValueError('Method snapshot document fingerprint mismatch')
    return {key: copy.deepcopy(value[key]) for key in ('id', 'title', 'text', 'sha256', 'version', 'source')
            if key in value} | {'utf8_bom': matches[0]}


def bundle(documents, identifiers, origin):
    documents = {key: checked_document(value) for key, value in documents.items()}
    if any(key != value['id'] for key, value in documents.items()):
        raise ValueError('Method snapshot identity mismatch')
    value = {'schema_version': 1, 'documents': documents,
             'missing': sorted(set(identifiers) - set(documents)), 'origin': origin}
    return {**value, 'sha256': digest(value)}


def read_bundle(path):
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ValueError('Unsupported method snapshot')
    raw = {key: value[key] for key in ('schema_version', 'documents', 'missing', 'origin')}
    if value.get('sha256') != digest(raw):
        raise ValueError('Method bundle fingerprint mismatch')
    for key, document in value['documents'].items():
        if checked_document(document) != document or document['id'] != key:
            raise ValueError('Method bundle document changed')
    return value


def recover(service, run_id, identifiers):
    """Latest actually loaded revision per document, not today's source text."""
    documents, had_history = {}, (service._run_root(run_id) / 'library-pin.json').is_file()
    for event in service.store.read_events(run_id):
        if event['kind'] not in {'learning_context_loaded', 'learning_document_loaded'}:
            continue
        had_history = True
        try:
            if event['kind'] == 'learning_context_loaded':
                saved = service.context_record(run_id, event['snapshot_sha256'])
                rows = [*saved.get('skills', []), *saved.get('method_cards', [])]
            else:
                snapshot_id = event['snapshot_sha256']
                if not isinstance(snapshot_id, str) or len(snapshot_id) != 64 or any(
                        ch not in '0123456789abcdef' for ch in snapshot_id):
                    continue
                path = service._run_root(run_id) / 'documents' / (snapshot_id + '.json')
                saved = json.loads(path.read_text(encoding='utf-8'))
                original = {key: val for key, val in saved.items()
                            if key not in {'snapshot_sha256', 'evidence_ref'}}
                # Historical document snapshots used learning._json, including indent.
                from .learning import _json, _sha
                if _sha(_json(original)) != snapshot_id:
                    continue
                rows = [saved]
            for row in rows:
                if row.get('id') in identifiers:
                    documents[row['id']] = checked_document(row)
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            # An absent legacy body is a visible gap; current text cannot fill it.
            continue
    return bundle(documents, identifiers, 'legacy_contexts') if had_history else None


def pinned(service, run_id, current, *, persist):
    path = service._run_root(run_id) / 'method-pin.json'
    if path.is_file():
        return read_bundle(path)
    historical = recover(service, run_id, set(current['documents']) | set(current['missing']))
    selected = historical or current
    if persist:
        _atomic_write_text(path, json.dumps(selected, ensure_ascii=False, sort_keys=True, indent=2))
        service.store.append_event(run_id, 'learning_methods_pinned', methods_sha256=selected['sha256'],
                                   origin=selected['origin'], missing=selected['missing'])
    return selected


def write_bundle(service, run_id, value):
    _atomic_write_text(service._run_root(run_id) / 'method-pin.json',
                       json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def document_changes(before, after):
    return [{'id': ident, 'title': after['documents'].get(ident, before['documents'].get(ident, {})).get('title', ident),
             'before_sha256': before['documents'].get(ident, {}).get('sha256'),
             'after_sha256': after['documents'].get(ident, {}).get('sha256'),
             'before_available': ident in before['documents'], 'after_available': ident in after['documents'],
             'diff': '\n'.join(difflib.unified_diff(
                 before['documents'].get(ident, {}).get('text', '').splitlines(),
                 after['documents'].get(ident, {}).get('text', '').splitlines(),
                 fromfile=ident + '/pinned', tofile=ident + '/current', lineterm='')),
             'diff_truncated': False}
            for ident in sorted(set(before['documents']) | set(after['documents']))
            if before['documents'].get(ident, {}).get('sha256') != after['documents'].get(ident, {}).get('sha256')]
