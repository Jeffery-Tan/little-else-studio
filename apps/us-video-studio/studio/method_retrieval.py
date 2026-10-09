"""Deterministic relevance only; never infer intent, eligibility or quality."""
from __future__ import annotations

import json
import re


ROLE_TERMS = {
    'director': 'audience strategy hook purchase direction 受众 购买 方向 开头',
    'material': 'evidence visual scene character continuity 素材 证据 人物 场景 连续',
    'writer': 'dialogue speech pacing caption copy script 台词 口播 文案 节奏',
}


def terms(value):
    text = value.casefold()
    result = set(re.findall(r'[a-z0-9][a-z0-9_-]{2,}', text))
    for run in re.findall(r'[\u3400-\u9fff]+', text):
        result.update(run[index:index + 2] for index in range(len(run) - 1))
    return result


def relevance(method, cases, *, query, tags, role):
    descriptive = {key: method.get(key) for key in
        ('id', 'retrieval_summary', 'decision', 'when', 'use_when', 'required_inputs')}
    source_tags = [tag for ref in method.get('support_refs', method.get('supports', []))
                   for tag in cases.get(ref.partition('/')[0], {}).get('tags', [])]
    candidates = terms(json.dumps(descriptive, ensure_ascii=False)) | terms(' '.join(source_tags))
    query_terms = terms(query) | terms(' '.join(tags))
    content_matches = candidates & query_terms
    # Role hints only break ties among content matches. They cannot cause an
    # unrelated method to be loaded in full when the real task had no match.
    return 10 * len(content_matches) + (len(candidates & terms(ROLE_TERMS.get(role, '')))
                                       if content_matches else 0)
