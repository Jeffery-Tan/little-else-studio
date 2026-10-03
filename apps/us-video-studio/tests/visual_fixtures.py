"""Explicit synthetic visual artifacts for offline boundary tests."""
from copy import deepcopy


def visual_design():
    return {
        'schema_version': 1, 'market': 'US',
        'audience': 'Synthetic US weekday office commuters; not market evidence.',
        'everyday_context': 'Carry lunch from a home kitchen to an office break room.',
        'localization': 'A modest US apartment and shared office; plausible everyday objects, no tourist landmarks.',
        'style': 'Natural creator demonstration, daylight and eye-level camera.',
        'product_appearance': 'Only the uploaded product shape and adopted color; no unsupported performance claims.',
        'characters': [],
        'scenes': [{'id': 'table', 'label': '商品桌面', 'location': 'A neutral tabletop in an office break room.',
                    'layout': 'Plain table with room for the product demonstration.',
                    'props': 'A reusable lunch container, with no visible branding.',
                    'lighting': 'Soft daylight, consistent white balance.',
                    'local_details': 'A neutral product close-up within the US commute context; no location claim in this shot.'}],
        'continuity': ['Preserve the same product shape and adopted color across shots.'],
        'assumptions': ['All people and spaces are fictional design choices, not observed customer evidence.'],
    }


def stage_structure(stage):
    if stage == 'material_plan':
        return {'items': [{'shot': 'Product demonstration', 'purpose': 'Show the product',
                           'status': '需要生成', 'note': 'Synthetic fixture', 'file': ''}],
                'visual_design': visual_design()}
    if stage == 'writing':
        return {'segments': [{'tc': '0:00–0:05', 'picture': 'Product on a neutral tabletop.',
                              'line': '', 'sound': 'Room tone', 'duration': '5', 'file': '',
                              'character_ids': '', 'scene_id': 'table'}]}
    return {}


def designed_script(script):
    result = deepcopy(script)
    for row in result.get('segments', []):
        row.update(character_ids='', scene_id='table')
    return result
