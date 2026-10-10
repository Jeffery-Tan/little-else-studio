"""Local US photo/music composer. No generation APIs, uploads or publishing.

prepare -> inspect preview -> record layout review -> render. Pillow composes
existing images and text; FFmpeg encodes once and muxes licensed local audio.
"""
import argparse
import hashlib
import html
import json
import math
import re
import struct
import subprocess
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True)


def probe(path):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)]).stdout)


def checked_file(item):
    path = Path(item['path'])
    if not path.is_file() or digest(path) != item['sha256']:
        raise ValueError(f'Missing or changed input: {path.name}')
    return path


def validate(doc):
    if doc.get('schema_version') != 1 or doc.get('market') != 'US':
        raise ValueError('Expected schema version 1 / US')
    if doc.get('mode') not in ('technical', 'commercial_local'):
        raise ValueError('Invalid mode')
    for key in ('product_family_id', 'offer_id', 'variant', 'creative_intent', 'limitations'):
        if not doc.get(key):
            raise ValueError(f'Missing {key}')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,20}', doc['product_family_id']):
        raise ValueError('Product family identifier must fit the display label')
    checked_file(doc['font'])
    if doc.get('fps') != 30 or doc.get('size') != [720, 1280]:
        raise ValueError('Current layout supports 720x1280 / 30fps only')
    scenes = doc.get('scenes', [])
    if not 2 <= len(scenes) <= 8:
        raise ValueError('Expected 2 to 8 scenes')
    for s in scenes:
        checked_file(s['image'])
        if s.get('variant') != doc['variant']:
            raise ValueError('Scene variant mismatch')
        if not s.get('evidence') or not s.get('new_information'):
            raise ValueError('Every scene needs evidence and an information purpose')
        if not isinstance(s.get('frames'), int) or not 45 <= s['frames'] <= 150:
            raise ValueError('Scene must last 1.5 to 5 seconds in whole frames')
        if not isinstance(s.get('headline'), str) or not s['headline'].strip() or len(s['headline']) > 70:
            raise ValueError('Missing or overlong headline')
        with Image.open(s['image']['path']) as im:
            im = ImageOps.exif_transpose(im)
            crop = s.get('crop', [0, 0, im.width, im.height])
            if len(crop) != 4 or not (0 <= crop[0] < crop[2] <= im.width and 0 <= crop[1] < crop[3] <= im.height):
                raise ValueError('Crop exceeds source')
        if s['image'].get('rights') not in ('internal_review', 'commercial'):
            raise ValueError('Image rights must be declared')
        if doc['mode'] == 'commercial_local':
            if s['image']['rights'] != 'commercial':
                raise ValueError('Commercial image rights missing')
            checked_file(s['image']['rights_evidence'])
    audio = doc.get('audio', {})
    if audio.get('kind') == 'technical_beats':
        if doc['mode'] != 'technical' or audio.get('bpm') != 120:
            raise ValueError('Test beats are restricted to technical mode / 120bpm')
    elif audio.get('kind') == 'licensed_file':
        p = checked_file(audio)
        license_info = audio.get('license', {})
        if not all(license_info.get(k) for k in ('holder', 'source', 'scope', 'verified_by', 'verified_at')):
            raise ValueError('Incomplete audio license record')
        checked_file(license_info['evidence'])
        if doc['mode'] == 'commercial_local' and license_info.get('scope') != 'US_TikTok_commercial':
            raise ValueError('Music license does not cover intended commercial scope')
        audio_probe = probe(p)
        if not any(s['codec_type'] == 'audio' for s in audio_probe['streams']):
            raise ValueError('Music input has no audio stream')
        if float(audio_probe['format']['duration']) < sum(s['frames'] for s in scenes) / doc['fps']:
            raise ValueError('Music too short; no implicit looping')
    else:
        raise ValueError('Supply licensed music or explicit technical beats')
    return doc


def text_lines(draw, text, font, width):
    lines = ['']
    for word in text.split():
        candidate = (lines[-1] + ' ' + word).strip()
        if draw.textlength(candidate, font=font) > width:
            if not lines[-1] or draw.textlength(word, font=font) > width:
                raise ValueError('Text cannot fit safe width')
            lines.append(word)
        else:
            lines[-1] = candidate
    if len(lines) > 3:
        raise ValueError('Text exceeds safe layout; shorten copy')
    return lines


def frame(doc, scene, index):
    canvas = Image.new('RGB', (720, 1280), '#101916')
    draw = ImageDraw.Draw(canvas)
    fonts = [ImageFont.truetype(doc['font']['path'], x) for x in (20, 46, 22)]
    family_label = 'LUNCH' if doc['product_family_id'] == 'LUN' else doc['product_family_id']
    draw.text((48, 78), 'LITTLE ELSE / ' + family_label, font=fonts[0], fill='#D0D8C2')
    for i, line in enumerate(text_lines(draw, scene['headline'], fonts[1], 550)):
        draw.text((48, 142 + i * 55), line, font=fonts[1], fill='#F4F2E8')
    with Image.open(scene['image']['path']) as source:
        source = ImageOps.exif_transpose(source).convert('RGB')
        if 'crop' in scene:
            source = source.crop(scene['crop'])
        source.thumbnail((600, 670), Image.Resampling.LANCZOS)
        canvas.paste(source, (48 + (600 - source.width) // 2, 340 + (670 - source.height) // 2))
    draw.text((48, 1050), doc['variant'], font=fonts[2], fill='#E6E5D7')
    draw.text((48, 1094), f'{index + 1:02d} / {len(doc["scenes"]):02d}', font=fonts[0], fill='#94A796')
    if doc['mode'] == 'technical':
        draw.text((48, 1140), 'TECHNICAL PREVIEW / TEST BEAT', font=fonts[0], fill='#C4BA92')
    return canvas


def prepare(manifest, out):
    doc = validate(read(manifest))
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    frames = []
    for i, scene in enumerate(doc['scenes']):
        p = out / f'slide-{i:02d}.png'
        frame(doc, scene, i).save(p)
        frames.append({'path': p.name, 'sha256': digest(p), 'frames': scene['frames'], 'headline': scene['headline']})
    sheet = Image.new('RGB', (360 * len(frames), 640), '#101916')
    for i, f in enumerate(frames):
        with Image.open(out / f['path']) as im:
            sheet.paste(im.resize((360, 640)), (i * 360, 0))
    sheet.save(out / 'layout.jpg', quality=95)
    write(out / 'layout.json', {'manifest_sha256': digest(manifest), 'preview_sha256': digest(out / 'layout.jpg'), 'slides': frames})
    return out / 'layout.jpg'


def synth_beats(path, seconds):
    # Original deterministic sine/noise rhythm, a test signal, no sampled music.
    rate = 48000
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        chunk = bytearray()
        for i in range(round(seconds * rate)):
            t = i / rate
            phase = t % .5
            kick = .30 * math.sin(2 * math.pi * 70 * phase) * math.exp(-phase * 35)
            tick = .06 * math.sin(2 * math.pi * 4000 * phase) * math.exp(-phase * 130)
            envelope = min(1, t / .02, (seconds - t) / .1)
            chunk += struct.pack('<h', round((kick + tick) * envelope * 32767))
        wav.writeframes(chunk)


def render(manifest, out, approval):
    doc = validate(read(manifest))
    out = Path(out)
    layout, decision = read(out / 'layout.json'), read(approval)
    if layout['manifest_sha256'] != digest(manifest) or layout['preview_sha256'] != digest(out / 'layout.jpg'):
        raise ValueError('Layout stale; prepare a new output directory')
    if not (decision.get('approved') is True and decision.get('reviewer') and decision.get('reviewed_at')
            and decision.get('manifest_sha256') == layout['manifest_sha256']
            and decision.get('preview_sha256') == layout['preview_sha256']
            and decision.get('layout_sha256') == digest(out / 'layout.json')):
        raise ValueError('Layout review missing or mismatched; no automatic approval')
    if len(layout['slides']) != len(doc['scenes']):
        raise ValueError('Layout scene count differs from manifest')
    for i, (slide, scene) in enumerate(zip(layout['slides'], doc['scenes'])):
        if slide['path'] != f'slide-{i:02d}.png' or slide['frames'] != scene['frames'] or slide['headline'] != scene['headline']:
            raise ValueError('Layout sequence/timing differs from manifest')
    video = out / 'photo-music.mp4'
    if video.exists():
        raise ValueError('Output already exists; choose a new run')
    seconds = sum(s['frames'] for s in doc['scenes']) / 30
    audio_path = Path(doc['audio'].get('path', out / 'technical-beats.wav'))
    if doc['audio']['kind'] == 'technical_beats':
        if audio_path.exists():
            raise ValueError('Test audio already exists; do not overwrite')
        synth_beats(audio_path, seconds)
    args = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error']
    for s in layout['slides']:
        if digest(out / s['path']) != s['sha256']:
            raise ValueError('Rendered slide changed after layout review')
        args += ['-loop', '1', '-framerate', '30', '-t', str(s['frames'] / 30), '-i', str(out / s['path'])]
    args += ['-i', str(audio_path)]
    n = len(layout['slides'])
    vf = ''.join(f'[{i}:v]' for i in range(n)) + f'concat=n={n}:v=1:a=0,format=yuv420p[v]'
    args += ['-filter_complex', vf, '-map', '[v]', '-map', f'{n}:a:0', '-t', str(seconds),
             '-c:v', 'libx264', '-crf', '17', '-preset', 'slow', '-c:a', 'aac', '-b:a', '192k',
             '-af', f'afade=t=out:st={seconds - .15}:d=0.15', '-movflags', '+faststart', str(video)]
    write(out / 'render-command.json', {'args': args})
    run(args)
    metadata = probe(video)
    run(['ffmpeg', '-v', 'error', '-i', str(video), '-f', 'null', '-'])
    vs = next(s for s in metadata['streams'] if s['codec_type'] == 'video')
    if (vs['width'], vs['height']) != (720, 1280) or int(vs['nb_frames']) != round(seconds * 30):
        raise ValueError('Output dimensions/frame count mismatch')
    if not any(s['codec_type'] == 'audio' for s in metadata['streams']):
        raise ValueError('Output audio missing')
    elapsed = 0
    samples = []
    for i, s in enumerate(doc['scenes']):
        sample = out / f'actual-{i:02d}.png'
        at = elapsed + s['frames'] / 60
        run(['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-ss', str(at), '-i', str(video), '-frames:v', '1', str(sample)])
        samples.append({'time': at, 'path': sample.name, 'sha256': digest(sample)})
        elapsed += s['frames'] / 30
    write(out / 'verification.json', {'decode_passed': True, 'duration_seconds': seconds, 'frames': int(vs['nb_frames']),
          'manifest_sha256': digest(manifest), 'video_sha256': digest(video), 'audio_sha256': digest(audio_path),
          'audio_kind': doc['audio']['kind'], 'probe': metadata, 'sample_frames': samples,
          'content_accepted': False, 'original_speed_listening': 'not_verified', 'market_effect': None,
          'new_paid_calls': 0, 'publishing_authorized': False})
    limitations = ''.join('<li>' + html.escape(x) + '</li>' for x in doc['limitations'])
    (out / 'review.html').write_text('<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>图文＋音乐技术样片</title><style>body{background:#101916;color:#eee;font:16px/1.7 system-ui;max-width:980px;margin:40px auto;padding:24px}video{width:min(100%,360px);border-radius:12px}img{max-width:100%}a{color:#c2d7a1}</style><h1>图文＋音乐 · 本地技术样片</h1><p>US / ' + html.escape(doc['variant']) + ' · ' + str(seconds) + '秒。版式、音乐入口和导出验证；不是已通过的带货创意。</p><video controls playsinline preload="metadata" src="photo-music.mp4"></video><h2>限制与待验证</h2><ul>' + limitations + '</ul><p>文字在画面中保留完整阅读时段。切换落在本地测试节拍上；没有人工原速听感通过记录。未发布、未调用生成服务。</p><h2>布局预览</h2><img src="layout.jpg"><p><a href="verification.json">技术验证记录</a> · <a href="photo-music.mp4">MP4</a></p></html>', encoding='utf-8')
    return video


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['validate', 'prepare', 'render'])
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--layout-approval', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'validate':
            validate(read(args.manifest))
            print('Valid inputs; does not imply marketing approval.')
        elif args.command == 'prepare':
            if not args.out:
                parser.error('--out is required')
            print(prepare(args.manifest, args.out))
        else:
            if not args.out or not args.layout_approval:
                parser.error('--out and --layout-approval are required')
            print(render(args.manifest, args.out, args.layout_approval))
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Blocked: {exc}\n')


if __name__ == '__main__':
    main()
