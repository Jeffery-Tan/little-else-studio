"""Independent native video observer routing; never a media generator.

No provider fallback: an explicit MiMo selection must use its own frozen key.
Missing legacy selection keeps the existing Beijing Omni observation route.
"""
from .agents import bailian_omni
from .agents.client import SafePreflightError

MIMO_MODEL = 'mimo-v2.6-flash'
MIMO_MAX_INPUT_BYTES = 35_000_000  # Base64 plus prefix remains below 50 MB.


def connection_info(connections):
    doc = connections.public()
    selected = doc.get('video_analysis')
    if selected is None:
        return bailian_omni.connection_info()
    if not isinstance(selected, dict) or not isinstance(selected.get('connection'), str):
        raise SafePreflightError('视频理解配置无效，请在模型设置中重新选择服务。')
    entry = next((c for c in doc['connections'] if c['id'] == selected.get('connection')), None)
    if not entry or entry['provider'] != 'mimo' or selected.get('model') != MIMO_MODEL:
        raise SafePreflightError('视频理解配置无效，请在模型设置中重新选择服务。')
    return {**entry, 'suggested_model': MIMO_MODEL, 'native_video': True,
            'video_fps': 2, 'media_resolution': 'default',
            'max_input_bytes': MIMO_MAX_INPUT_BYTES}


def freeze(connections, settings, *, expected_revision, expected_endpoint,
           connection_id=None, model=None, check_cancel=lambda: None):
    if connections.public()['revision'] != expected_revision:
        raise SafePreflightError('模型设置已变化，请重新确认视频理解服务。')
    if connection_id is None:
        info = connection_info(connections)
        if info is None:
            raise SafePreflightError('视频理解服务尚未配置，请检查模型设置。')
        connection_id, model = info['id'], info['suggested_model']
    if connection_id == bailian_omni.CONNECTION:
        if model != bailian_omni.MODEL:
            raise SafePreflightError('视频理解模型与百炼连接不匹配。')
        return bailian_omni.freeze(settings, expected_endpoint, check_cancel)
    snapshot = connections.snapshot(image_connection=connection_id, image_model=model,
                                    expected_revision=expected_revision)
    entry = next(c for c in snapshot.public()['connections'] if c['id'] == connection_id)
    if entry['provider'] != 'mimo' or model != MIMO_MODEL or entry['base_url'] != expected_endpoint:
        raise SafePreflightError('MiMo 视频理解连接或模型已变化，请重新确认；不会改用其他服务。')
    frozen = snapshot.settings(settings)
    client = snapshot.client(check_cancel=check_cancel, max_tokens=8192).for_role('material', model)
    return frozen, client, {**entry, 'suggested_model': model, 'native_video': True,
                           'video_fps': 2, 'media_resolution': 'default',
                           'max_input_bytes': MIMO_MAX_INPUT_BYTES}


def prepare_video(raw, frozen, check_cancel=lambda: None):
    from .audiovisual_observation import prepare_video as prepare
    max_bytes = frozen[2].get('max_input_bytes', bailian_omni.MAX_INLINE_BYTES)
    sent, packet = prepare(raw, check_cancel, max_bytes=max_bytes)
    packet.update(video_fps=2, media_resolution='default')
    return sent, packet
