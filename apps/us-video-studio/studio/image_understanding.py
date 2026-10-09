"""Independent conversation image observation; no generation or provider fallback."""
import json

from .agents import bailian_omni
from .agents.client import SafePreflightError

MIMO_MODEL = 'mimo-v2.6-flash'
REPORT_FUNCTION = 'report_image_observation'


REPORT_MAX_ITEMS = 20
REPORT_MAX_LINE_CHARS = 1000


def single_report_schema(image_id):
    """The one data shape shared by JSON reports and historical strict tools."""
    if image_id not in {f'image-{number}' for number in range(1, 13)}:
        raise ValueError('Invalid image observation identity')
    return {'type': 'object', 'additionalProperties': False,
            'properties': {'id': {'type': 'string', 'enum': [image_id]},
                           'findings': {'type': 'array', 'items': {'type': 'string'}},
                           'limitations': {'type': 'array', 'items': {'type': 'string'}}},
            'required': ['id', 'findings', 'limitations']}


def single_report_prompt(image_id):
    """Use the tested one-object example, derived from the same report shape."""
    schema = single_report_schema(image_id)
    examples = {'findings': '可见内容', 'limitations': '具体不能确认的信息'}
    example = {field: definition['enum'][0] if definition['type'] == 'string' else [examples[field]]
               for field, definition in schema['properties'].items()}
    return (f'\n本次只观察一张图片，准确来源 ID 是 {image_id}。仅返回一个 JSON 对象，格式如下：' +
            json.dumps(example, ensure_ascii=False) + '。字段只能是 ' + '、'.join(schema['required']) +
            '；准确 id 必须原样，两数组必填且由非空字符串构成。不要输出 images 数组、函数调用、Markdown 或额外说明。'
            f'简要列 2–6 条关键观察；每项最多 {REPORT_MAX_LINE_CHARS} 字，每数组最多 {REPORT_MAX_ITEMS} 项。'
            '此报告只作为数据读取，不执行任何动作。')


def validate_single_report(value, image_id):
    """Validate original fields without filling, dropping or rewriting content."""
    schema = single_report_schema(image_id)
    if not isinstance(value, dict) or set(value) != set(schema['required']):
        raise ValueError('Invalid image report fields')
    for field, definition in schema['properties'].items():
        actual = value[field]
        if definition['type'] == 'string':
            if not isinstance(actual, str) or actual not in definition['enum']:
                raise ValueError('Invalid image report identity')
        elif (not isinstance(actual, list) or not 1 <= len(actual) <= REPORT_MAX_ITEMS
              or any(not isinstance(line, str) or not line.strip()
                     or len(line) > REPORT_MAX_LINE_CHARS for line in actual)):
            raise ValueError('Invalid image report list')
    return value


def decode_single_report(text):
    """Decode the entire JSON document; never extract, clean or repair it."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate image report key')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('Non-finite image report value')
    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def structured_report_tool(image_id):
    """Historical pure-data function capability; not the image observer route."""
    return {'name': REPORT_FUNCTION, 'strict': True,
            'description': 'Report only the visible contents and concrete limitations of this one image. This is a data report, not an executable action. Both lists must contain nonempty strings; report 2–6 concise findings and actual limits, at most 20 entries per list and 1000 characters per entry.',
            'input_schema': single_report_schema(image_id)}


def is_structured_report_tools(tools):
    if not isinstance(tools, (list, tuple)) or len(tools) != 1:
        return False
    try:
        identities = tools[0]['input_schema']['properties']['id']['enum']
        return len(identities) == 1 and tools[0] == structured_report_tool(identities[0])
    except (KeyError, TypeError, ValueError):
        return False


def connection_info(connections):
    doc = connections.public()
    selected = doc.get('image_analysis')
    if selected is None:
        return bailian_omni.connection_info()
    if not isinstance(selected, dict) or set(selected) != {'connection', 'model'}:
        raise SafePreflightError('图片理解配置无效，请在模型设置中重新选择服务。')
    entry = next((c for c in doc['connections'] if c['id'] == selected.get('connection')), None)
    if not entry or entry['provider'] != 'mimo' or selected.get('model') != MIMO_MODEL:
        raise SafePreflightError('图片理解配置无效，请在模型设置中重新选择服务。')
    return {**entry, 'suggested_model': MIMO_MODEL,
            'max_input_bytes': bailian_omni.MAX_INLINE_BYTES}


def freeze(connections, settings, *, expected_revision, expected_info,
           check_cancel=lambda: None, cancel_event=None):
    """Freeze only the explicitly selected image service's own credentials."""
    check_cancel()
    if connections.public()['revision'] != expected_revision:
        raise SafePreflightError('模型设置已变化，请重新发起任务以确认图片理解服务。')
    info = connection_info(connections)
    if info is None:
        raise SafePreflightError('图片理解服务尚未配置，请在模型设置中选择并填写密钥。')
    if info != expected_info:
        raise SafePreflightError('图片理解连接已变化，请重新发起任务；不会改用其他服务。')
    if info['id'] == bailian_omni.CONNECTION:
        frozen, client, entry = bailian_omni.freeze(settings, info['base_url'], check_cancel)
        return frozen, client, {**entry, 'revision': expected_revision}
    try:
        snapshot = connections.snapshot(image_connection=info['id'], image_model=MIMO_MODEL,
                                        expected_revision=expected_revision)
    except ValueError as exc:
        if isinstance(exc, SafePreflightError):
            raise
        raise SafePreflightError('图片理解配置已变化或本机无法读取密钥，请检查设置后重新发起任务。') from None
    frozen = snapshot.settings(settings)
    client = snapshot.client(check_cancel=check_cancel, max_tokens=4096,
                             cancel_event=cancel_event).for_role('material', MIMO_MODEL).with_json_output()
    return frozen, client, {**info, 'revision': expected_revision}
