"""Local compression with real synthetic pixels; no model or private images."""
import hashlib
import io
import random

import pytest
from PIL import Image

from studio.observation_image_input import prepare, MAX_INPUT_BYTES, record_details
from studio.material_observations import ObservationError


def noisy_png(size=(1254, 1254), mode='RGB'):
    pixels = random.Random(17).randbytes(size[0] * size[1] * len(mode))
    output = io.BytesIO()
    Image.frombytes(mode, size, pixels).save(output, format='PNG', compress_level=0)
    return output.getvalue()


def test_large_png_compresses_without_changing_resolution():
    original = noisy_png()
    assert len(original) > MAX_INPUT_BYTES
    sent, mime, info = prepare(original, 'image/png')
    assert mime == 'image/jpeg' and len(sent) < len(original) and len(sent) <= MAX_INPUT_BYTES
    assert info['source_dimensions'] == info['input_dimensions'] == [1254, 1254]
    assert info['input_sha256'] == hashlib.sha256(sent).hexdigest()
    assert info['source_bytes'] == len(original) and info['input_bytes'] == len(sent)
    with Image.open(io.BytesIO(sent)) as image:
        image.load()
        assert image.size == (1254, 1254)


def test_small_input_preserves_exact_bytes():
    original = noisy_png((40, 30))
    sent, mime, info = prepare(original, 'image/png')
    assert sent == original and mime == 'image/png' and info['processing'] == 'original'


def test_transparency_is_preserved_in_observation_copy():
    original = noisy_png((600, 600), 'RGBA')
    sent, mime, _ = prepare(original, 'image/png')
    assert mime == 'image/png'
    with Image.open(io.BytesIO(original)) as source, Image.open(io.BytesIO(sent)) as copy:
        assert copy.mode == 'RGBA' and copy.tobytes() == source.tobytes()


def test_cmyk_photo_preserves_color_space_when_compressed():
    output = io.BytesIO()
    Image.frombytes('CMYK', (1000, 500), random.Random(19).randbytes(2_000_000)).save(
        output, format='JPEG', quality=100, subsampling=0)
    original = output.getvalue()
    assert len(original) > 1_000_000
    sent, mime, _ = prepare(original, 'image/jpeg')
    assert len(sent) < len(original) and mime == 'image/jpeg'
    with Image.open(io.BytesIO(sent)) as image:
        assert image.mode == 'CMYK' and image.size == (1000, 500)


def test_excessive_input_stops_without_endless_quality_reduction():
    original = noisy_png((2500, 2500), 'RGBA')
    with pytest.raises(ObservationError, match='自动压缩后仍超过'):
        prepare(original, 'image/png')


def test_provenance_drops_extra_fields_and_rejects_false_original_binding():
    original = noisy_png((40, 30))
    _, _, info = prepare(original, 'image/png')
    record = dict(level='image_observed', sha256=hashlib.sha256(original).hexdigest(),
                  image_input={**info, 'unexpected': 'untrusted extra'})
    assert record_details(record)['image_input'] == info
    record['image_input']['input_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='original image binding'):
        record_details(record)
