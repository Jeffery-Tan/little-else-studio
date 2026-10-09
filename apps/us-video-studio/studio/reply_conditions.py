"""Source-bound constraints on a root reply, with no creative authority."""
from __future__ import annotations

import hashlib
import re

from .models import PipelineError


REPLY_GUIDANCE = (
    'task.quote 与 constraints[].quote 须是本条原话中的一个连续片段，可直接引用完整原话；'
    '不能拼接不相邻片段、转述或添加省略号。task.format 的 video/photo/none 不表示回复排版。'
    '仅约束本轮主制片回复的条件使用 scope={lifetime:"turn",stages:[],paths:[],quote:原话}，'
    '不伪造 intake 或授权创作。key=reply.format 时 value 只能为 markdown_table 或 text，不填格式说明；'
    '数据行数用 reply.table_rows=规范正整数文本；全文字符上限用 reply.max_chars=规范正整数文本。'
    '该条件自己的 quote 必须同时引用对应数字与行数或字符上限，如“两行表”或“不超过400字”；'
    '只引“不含表头”不支持 value="2"，不从日期或其他数字推测。'
    '表头和分隔线不计数据行，全文长度包含 Markdown 标记、空白与文件名。'
    '表头和数据行以 | 开头，使用当前可呈现的单个 Markdown 表格。'
    '不需行数限制时只填格式；其他回复内容要求仍可用一般 key/value。'
    '这些条件仅适用于本条来源的主回复，不传给创作角色或视觉观察报告。'
)
TYPED_KEYS = frozenset({'reply.format', 'reply.table_rows', 'reply.max_chars'})


def is_reply_scope(scope):
    return scope.get('stages') == []


def _checked_condition(row):
    scope = row.get('scope', {})
    return (is_reply_scope(scope) and scope.get('lifetime') == 'turn' and scope.get('paths') == []
            and row.get('intent') == 'instruction'
            and (row.get('status') == 'needs_clarification' or
                 row.get('key') in TYPED_KEYS and row.get('status') == 'active'
                 and row.get('polarity') == 'require'))


def expected_check_ids(interpretation):
    """Shared selection only; consumers must still verify source and row bytes."""
    return [row['id'] for row in interpretation.get('constraints', []) if _checked_condition(row)]


_CHINESE_DIGITS = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
                   '六': 6, '七': 7, '八': 8, '九': 9}
_ENGLISH_NUMBERS = dict(zip(
    'one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split(),
    range(1, 21)))
_NUMBER = r'(?:[0-9]+|[零一二两三四五六七八九十百千]+|' + '|'.join(_ENGLISH_NUMBERS) + ')'


def _number(token):
    if token.isascii() and token.isdigit():
        return int(token) if len(token) <= 6 else None
    if token.lower() in _ENGLISH_NUMBERS:
        return _ENGLISH_NUMBERS[token.lower()]
    if len(token) == 1:
        return _CHINESE_DIGITS.get(token, 10 if token == '十' else None)
    if len(token) > 8 or not any(char in '十百千' for char in token):
        return None
    total, current, prior_unit = 0, None, 10000
    for char in token:
        if char in _CHINESE_DIGITS:
            if current is not None and current != 0:
                return None
            current = _CHINESE_DIGITS[char]
        else:
            unit = {'十': 10, '百': 100, '千': 1000}[char]
            if unit >= prior_unit:
                return None
            total += (current or 1) * unit
            current, prior_unit = None, unit
    return total + (current or 0)


def _numeric_witness(quote, key, value):
    # This proves the declared number/unit only. It never selects task intent,
    # infers authorization or substitutes for the model's semantic proposal.
    units = r'(?:行|(?:data\s+)?rows?\b)' if key == 'reply.table_rows' else r'(?:字符|字|characters?\b|chars?\b)'
    pattern = r'(?<![A-Za-z0-9_零一二两三四五六七八九十百千])(' + _NUMBER + r')\s*[-－]?\s*' + units
    for match in re.finditer(pattern, quote, re.I):
        if _number(match.group(1)) != value:
            continue
        if key == 'reply.table_rows':
            return True
        before = quote[max(0, match.start() - 24):match.start()]
        after = quote[match.end():match.end() + 12]
        if (re.search(r'(?:不超过|最多|至多|上限|限制|限于|不多于|<=|≤|at most|no more than|up to|maximum|max\.?|within)\s*$', before, re.I)
                or re.match(r'\s*(?:以内|内|以下|为上限|上限|limit\b|maximum\b|or fewer\b)', after, re.I)):
            return True
    return False


def typed_condition_issue(row):
    """A fixed field issue for an already shape-checked condition, without edits."""
    key, value = row['key'], row['value']
    if key not in TYPED_KEYS:
        return
    if not is_reply_scope(row['scope']):
        return {'code': 'reply_scope_invalid', 'field': 'scope',
                'message': '回复格式条件只能使用本轮空阶段、空字段的回复范围。'}
    if row['polarity'] == 'forbid':
        return {'code': 'reply_polarity_invalid', 'field': 'polarity',
                'message': '结构化回复限制须表达所要求的格式、数据行数或字符上限，不能反转其含义。'}
    if key == 'reply.format':
        if value not in {'markdown_table', 'text'}:
            return {'code': 'reply_format_invalid', 'field': 'value',
                    'message': 'reply.format 只能为 markdown_table 或 text。'}
        return
    ceiling = 1000 if key == 'reply.table_rows' else 100000
    if not re.fullmatch(r'[1-9][0-9]{0,5}', value) or int(value) > ceiling:
        return {'code': 'reply_numeric_value_invalid', 'field': 'value',
                'message': '回复行数和字符上限必须为有效范围内的规范正整数文本。'}
    if (row['intent'] == 'instruction' and row['operation'] not in {'release', 'clarify'}
            and not _numeric_witness(row['quote'], key, int(value))):
        return {'code': 'reply_numeric_witness_missing', 'field': 'quote',
                'message': '回复限制的数字必须引用对应的数据行数或字符上限原话，不能引用日期或其他数量。'}


def validate_typed_condition(row):
    issue = typed_condition_issue(row)
    if issue:
        raise PipelineError('task_interpretation', issue['message'])


def _cells(line):
    cells, start, escaped = [], 0, False
    for index, char in enumerate(line):
        if char == '|' and not escaped:
            cells.append(line[start:index].strip())
            start = index + 1
        escaped = char == '\\' and not escaped
    cells.append(line[start:].strip())
    if line.lstrip().startswith('|'):
        cells.pop(0)
    if start == len(line) and line.endswith('|'):
        cells.pop()
    return cells


def table_shape(text):
    """Exactly one Markdown table; only leading/trailing blank lines allowed."""
    lines = [line.strip() for line in text.strip().splitlines()]
    if len(lines) < 2 or not any('|' in line for line in lines):
        return 'text', None
    delimiters = [index for index, line in enumerate(lines) if index > 0
                  and '|' in line and len(_cells(line)) >= 1
                  and all(re.fullmatch(r':?-{3,}:?', cell) for cell in _cells(line))]
    if not delimiters:
        malformed = any('|' in line and len(_cells(line)) >= 1 and all(
            re.fullmatch(r':?-+:?', cell) for cell in _cells(line)) for line in lines)
        return ('invalid_or_multiple' if malformed else 'text'), None
    if delimiters != [1] or any(not line for line in lines):
        return 'invalid_or_multiple', None
    # Match the shipped reply renderer's supported subset. In particular,
    # bare rows or escaped pipes must not pass while displaying as prose or
    # with a different number of cells. Do not rewrite the model's output.
    if not lines[0].startswith('|') or any('\\|' in line for line in lines):
        return 'invalid_or_multiple', None
    header, delimiter = _cells(lines[0]), _cells(lines[1])
    if (len(header) < 1 or len(delimiter) != len(header)
            or not all(re.fullmatch(r':?-{3,}:?', cell) for cell in delimiter)):
        return 'invalid_or_multiple', None
    for line in lines[2:]:
        cells = _cells(line)
        if (not line.startswith('|') or len(cells) != len(header)
                or all(re.fullmatch(r':?-{3,}:?', cell) for cell in cells)):
            return 'invalid_or_multiple', None
    return 'markdown_table', len(lines) - 2


def check_reply(text, receipt, interpretation, rows):
    """Check the original final text; return no claim for untyped semantics."""
    if not receipt or not interpretation:
        return None
    source_id = receipt['message_id']
    source_hash = hashlib.sha256(receipt['text'].encode('utf-8')).hexdigest()
    if interpretation.get('source_message_id') != source_id:
        raise PipelineError('reply_source', '本轮回复条件的解释来源无法核对。')
    bound = {row['id']: row for row in interpretation['constraints']}
    relevant = []
    for row in rows:
        if (row.get('source_message_id') != source_id or row.get('source_sha256') != source_hash
                or row.get('origin') != 'grounded_model_interpretation' or row.get('id') not in bound
                or row != bound[row['id']] or not is_reply_scope(row['scope'])
                or row['scope']['lifetime'] != 'turn' or row['scope']['paths']
                or not row['quote'] or row['quote'] not in receipt['text']
                or row['scope']['quote'] and row['scope']['quote'] not in receipt['text']):
            raise PipelineError('reply_source', '本轮回复条件必须来自该条真实输入的准确已保存解释。')
        if _checked_condition(row):
            relevant.append(row)
    if {row['id'] for row in relevant} != set(expected_check_ids(interpretation)):
        raise PipelineError('reply_source', '本轮回复格式核对不能遗漏准确解释中的有效限制。')
    if not relevant:
        return None
    actual_format, data_rows = table_shape(text)
    missing = []
    for row in relevant:
        issue = None
        if row['status'] == 'needs_clarification':
            issue = ('unresolved_condition', 'clarified', 'needs_clarification')
        elif row['key'] == 'reply.max_chars' and len(text) > int(row['value']):
            issue = ('max_chars', int(row['value']), len(text))
        elif row['key'] == 'reply.format' and actual_format != row['value']:
            issue = ('table_format', row['value'], actual_format)
        elif row['key'] == 'reply.table_rows' and data_rows != int(row['value']):
            issue = ('table_rows', int(row['value']), data_rows)
        if issue:
            missing.append({'category': issue[0], 'constraint_ids': [row['id']],
                            'expected': issue[1], 'actual': issue[2]})
    return {'status': 'incomplete' if missing else 'satisfied',
            'source_message_id': source_id, 'source_sha256': source_hash,
            'interpretation_id': interpretation['id'],
            'constraint_ids': [row['id'] for row in relevant], 'char_count': len(text),
            'table_data_rows': data_rows, 'missing': missing}
