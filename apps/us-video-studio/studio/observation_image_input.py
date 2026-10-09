"""Local image preparation for observation; never changes source/approved bytes."""
from __future__ import annotations

import hashlib
import io
import re

from PIL import Image, ImageOps

from .material_observations import ObservationError

COMPRESS_ABOVE_BYTES = 1_000_000
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_RESIZED_EDGE = 2048
COMPRESSION_LIMITATION = '本次模型读取自动压缩副本；商品细节和小字需以保留的原图核对。'


def record_details(record: dict) -> dict:
    """Keep only validated input provenance when reading adopted observations."""
    if 'image_input' not in record:
        return {}
    info = record['image_input']
    if not isinstance(info, dict) or record.get('level') != 'image_observed':
        raise ValueError('invalid observation image input')
    sizes = [info.get(key) for key in ('source_bytes', 'input_bytes')]
    from .materials import MAX_UPLOAD_BYTES
    if (any(type(size) is not int for size in sizes)
            or not 0 < sizes[1] <= MAX_INPUT_BYTES or not sizes[1] <= sizes[0] <= MAX_UPLOAD_BYTES):
        raise ValueError('invalid observation image size')
    for key in ('source_dimensions', 'input_dimensions'):
        dimensions = info.get(key)
        if (not isinstance(dimensions, list) or len(dimensions) != 2
                or any(type(edge) is not int or not 0 < edge <= 8000 for edge in dimensions)
                or dimensions[0] * dimensions[1] > 20_000_000):
            raise ValueError('invalid observation image dimensions')
    if (not re.fullmatch('[a-f0-9]{64}', str(info.get('input_sha256', '')))
            or info.get('mime') not in ('image/png', 'image/jpeg', 'image/webp')
            or info.get('processing') not in ('original', 'jpeg_quality_92', 'png_optimize',
                                              'resize+jpeg_quality_92', 'resize+png_optimize')):
        raise ValueError('invalid observation image processing')
    if info['processing'] == 'original' and info['input_sha256'] != record.get('sha256'):
        raise ValueError('invalid original image binding')
    return {'image_input': {key: info[key] for key in (
        'source_bytes', 'source_dimensions', 'input_bytes', 'input_dimensions',
        'input_sha256', 'mime', 'processing')}}


def prepare(raw: bytes, mime: str) -> tuple[bytes, str, dict]:
    """Take already validated source pixels and return bounded, traceable input."""
    with Image.open(io.BytesIO(raw)) as source:
        source_size = list(source.size)
        image = ImageOps.exif_transpose(source)
        image.load()
        input_size = source_size
        sent, sent_mime, processing = raw, mime, 'original'
        if len(raw) > COMPRESS_ABOVE_BYTES:
            transparent = 'A' in image.getbands() or 'transparency' in image.info
            # Preserve CMYK JPEG color space alongside its ICC profile.
            prepared = image.copy() if image.mode == 'CMYK' else image.convert('RGBA' if transparent else 'RGB')

            def encode():
                output = io.BytesIO()
                try:
                    if transparent:
                        prepared.save(output, format='PNG', optimize=True)
                    else:
                        # Keep 4:4:4 for small text. The extra optimize pass in
                        # Pillow can fail on high-entropy 4:4:4 images; standard
                        # JPEG encoding streams the same quality reliably.
                        prepared.save(output, format='JPEG', quality=92, subsampling=0,
                                      **({'icc_profile': image.info['icc_profile']} if image.info.get('icc_profile') else {}))
                except (OSError, ValueError):
                    raise ObservationError('图片观察副本编码失败，原图已保留，尚未调用模型。') from None
                return output.getvalue()

            encoded = encode()
            resized = False
            if len(encoded) > MAX_INPUT_BYTES and len(raw) > MAX_INPUT_BYTES:
                prepared.thumbnail((MAX_RESIZED_EDGE, MAX_RESIZED_EDGE), Image.Resampling.LANCZOS)
                encoded = encode()
                resized = list(prepared.size) != list(image.size)
            if len(encoded) < len(raw):
                sent = encoded
                sent_mime = 'image/png' if transparent else 'image/jpeg'
                input_size = list(prepared.size)
                processing = ('resize+' if resized else '') + ('png_optimize' if transparent else 'jpeg_quality_92')
        if len(sent) > MAX_INPUT_BYTES:
            raise ObservationError('图片自动压缩后仍超过观察输入上限 4 MiB；原图已保留，请提供较小的图片或局部细节图。')
        return sent, sent_mime, {
            'source_bytes': len(raw), 'source_dimensions': source_size,
            'input_bytes': len(sent), 'input_dimensions': input_size,
            'input_sha256': hashlib.sha256(sent).hexdigest(), 'mime': sent_mime,
            'processing': processing,
        }
