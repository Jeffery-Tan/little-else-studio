"""Compile US shoppable photo/copy plans into local, reviewable delivery packs.

No model calls, audio composition, publishing or creative approval. Existing
images are copied byte-for-byte; planned images remain explicitly ungenerated.
"""
import argparse
import copy
import hashlib
import html
import json
import re
import shutil
from pathlib import Path

from PIL import Image


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_new(path, value):
    with Path(path).open('x', encoding='utf-8') as out:
        out.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label}: nonempty text required')


def check_file(asset, image=False):
    path = Path(asset['path'])
    if not path.is_absolute() or not path.is_file() or digest(path) != asset.get('sha256'):
        raise ValueError(f'Missing/changed local source: {path.name}')
    if image:
        with Image.open(path) as im:
            if im.format not in ('PNG', 'JPEG', 'WEBP'):
                raise ValueError('Only PNG/JPEG/WebP final images supported')
            im.verify()
    return path


def validate(doc):
    if doc.get('schema_version') != 1 or doc.get('market') != 'US':
        raise ValueError('Expected schema version 1 / US')
    if doc.get('placement', 'shoppable_photo') != 'shoppable_photo':
        raise ValueError('Only shoppable_photo is supported; paid_carousel/product_listing require separate placement rules')
    if doc.get('music') != 'publish-time-on-TikTok':
        raise ValueError('Music belongs to TikTok publishing, not this compiler')
    product = doc['product']
    for key in ('product_family_id', 'offer_id', 'variant'):
        text(product.get(key), 'product.' + key)
    sources = product.get('sources', [])
    if not isinstance(sources, list) or not sources:
        raise ValueError('At least one local product evidence source is required')
    source_ids = set()
    for src in sources:
        sid = src.get('id')
        text(sid, 'source.id')
        if sid in source_ids:
            raise ValueError('Duplicate source id')
        source_ids.add(sid)
        text(src.get('role'), 'source.role')
        check_file(src)
    strategy = doc['strategy']
    for key in ('audience_situation', 'desired_belief', 'reason_to_buy', 'voice'):
        text(strategy.get(key), 'strategy.' + key)
    for key in ('method_references', 'limitations'):
        if not isinstance(strategy.get(key), list) or not strategy[key]:
            raise ValueError(f'strategy.{key}: nonempty list required')
        for value in strategy[key]:
            text(value, 'strategy.' + key)
    text(doc.get('publish_caption'), 'publish_caption')
    pages = doc.get('pages', [])
    if not isinstance(pages, list) or not 2 <= len(pages) <= 12:
        raise ValueError('Expected 2 to 12 ordered pages; not a platform limit assertion')
    ids = set()
    for page in pages:
        pid = page.get('id', '')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,39}', pid) or pid in ids:
            raise ValueError('Invalid/duplicate page id')
        ids.add(pid)
        for key in ('overlay_text', 'alt_text', 'purchase_role', 'image_prompt'):
            text(page.get(key), pid + '.' + key)
        if len(page['overlay_text']) > 180:
            raise ValueError('Overlay too long for this pack; shorten copy')
        refs = page.get('evidence_refs')
        if not isinstance(refs, list) or not refs or any(ref not in source_ids for ref in refs):
            raise ValueError('Page evidence must reference known local sources')
        if page.get('status') == 'planned':
            if page.get('image'):
                raise ValueError('Planned page cannot claim a bound image')
        elif page.get('status') == 'bound':
            image = page['image']
            if image.get('kind') not in ('existing', 'generated'):
                raise ValueError('Bound image kind must be existing or generated')
            text(image.get('provenance'), pid + '.image.provenance')
            check_file(image, image=True)
        else:
            raise ValueError('Page status must be planned or bound; no implicit approval')
    return doc


def bind(manifest, page_id, image, kind, provenance, output):
    doc = copy.deepcopy(validate(read(manifest)))
    doc.setdefault('placement', 'shoppable_photo')
    page = next((p for p in doc['pages'] if p['id'] == page_id), None)
    if page is None:
        raise ValueError('Unknown page id')
    image = Path(image).resolve()
    asset = {'path': str(image), 'sha256': digest(image), 'kind': kind, 'provenance': provenance}
    check_file(asset, image=True)
    if page.get('image'):
        page.setdefault('image_history', []).append(page['image'])
    page.update(status='bound', image=asset)
    doc['parent_manifest_sha256'] = digest(manifest)
    validate(doc)
    write_new(output, doc)
    return Path(output)


def compile_pack(manifest, out):
    doc = validate(read(manifest))
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    delivery = copy.deepcopy(doc)
    delivery.setdefault('placement', 'shoppable_photo')
    delivery['source_manifest_sha256'] = digest(manifest)
    delivery['assessment'] = {'creative': 'not_accepted', 'user_image_approval': 'not_recorded_by_this_tool',
                              'published': False, 'market_effect': None, 'new_generation_calls': 0}
    cards = []
    prompt_blocks = []
    esc = html.escape
    for index, page in enumerate(delivery['pages'], 1):
        if page['status'] == 'bound':
            source = check_file(page['image'], image=True)
            with Image.open(source) as im:
                suffix = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}[im.format]
            dest = out / f'{index:02d}-{page["id"]}{suffix}'
            shutil.copyfile(source, dest)
            if digest(dest) != page['image']['sha256']:
                raise ValueError('Image changed during copy')
            page['image']['delivery_path'] = dest.name
            visual = f'<img loading="lazy" src="{esc(dest.name)}" alt="{esc(page["alt_text"], quote=True)}"><p class="state">图片已绑定；未表示图内文字已合成或图片已审核</p>'
        else:
            visual = '<div class="planned">图片待生成<br><small>当前仅有策划和提示词</small></div>'
        cards.append(f'<article id="{esc(page["id"])}"><p class="eyebrow">{index:02d} / {len(delivery["pages"]):02d} · {esc(page["status"])}</p>{visual}<h2>{esc(page["overlay_text"])}</h2><p class="state">上方文字为拟用图内文案，尚未自动烧录到图片</p><dl><dt>购买作用</dt><dd>{esc(page["purchase_role"])}</dd><dt>图片说明 / alt text</dt><dd>{esc(page["alt_text"])}</dd><dt>证据引用</dt><dd>{esc(", ".join(page["evidence_refs"]))}</dd></dl><details><summary>图像生成提示词</summary><pre>{esc(page["image_prompt"])}</pre></details></article>')
        prompt_blocks.append(f'## {index:02d} / {page["id"]} / {page["status"]}\n\n图内文字：{page["overlay_text"]}\n\n购买作用：{page["purchase_role"]}\n\nAlt text：{page["alt_text"]}\n\n证据：{", ".join(page["evidence_refs"])}\n\n提示词：\n{page["image_prompt"]}')
    write_new(out / 'pack.json', delivery)
    (out / 'caption.txt').write_text(doc['publish_caption'] + '\n', encoding='utf-8')
    (out / 'prompts.md').write_text('# 图片与文案交付\n\n音乐在 TikTok 发布端添加。本包未自动生成图片或通过创意审核。\n\n' + '\n\n'.join(prompt_blocks) + '\n', encoding='utf-8-sig')
    strategy_html = ''.join(f'<dt>{esc(key)}</dt><dd>{esc(str(value))}</dd>' for key, value in doc['strategy'].items())
    source_html = ''.join(f'<li>{esc(s["id"])} · {esc(s["role"])} · SHA256 <code>{esc(s["sha256"])}</code></li>' for s in doc['product']['sources'])
    page_html = '<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>图文带货策划与审阅</title><style>body{font:16px/1.7 system-ui;background:#141815;color:#eee;margin:0;padding:32px}main{max-width:1100px;margin:auto}h1{font-size:32px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:20px}article,section{background:#202820;padding:24px;border-radius:12px;margin:20px 0}img{width:100%;height:360px;object-fit:contain;background:#101510}.planned{height:240px;display:grid;place-content:center;background:#101510;text-align:center;color:#c8cebb}pre{white-space:pre-wrap;overflow-wrap:anywhere}dt{color:#becfa9;margin-top:12px}dd{margin:0}.state,.eyebrow{font-size:13px;color:#b2c4a4}code{overflow-wrap:anywhere}a{color:#c2d7a1}h2{font-size:22px}</style><main><h1>图文带货 · 图片与文案</h1><p>' + esc(doc['product']['product_family_id'] + ' / ' + doc['product']['variant']) + '</p><p>本地策划与审阅包。音乐直接在 TikTok 发布端选择；本工具不合成音乐或视频，不自动发布或判定创意通过。</p><section><h2>创作依据</h2><dl>' + strategy_html + '</dl></section><div class="grid">' + ''.join(cards) + '</div><section><h2>发布文案</h2><pre>' + esc(doc['publish_caption']) + '</pre><p><a href="caption.txt">复制文本文件</a> · <a href="pack.json">完整 JSON</a> · <a href="prompts.md">逐页提示词与文案</a></p></section><section><h2>商品依据</h2><ul>' + source_html + '</ul></section></main></html>'
    page_html = page_html.replace('<h1>图文带货 · 图片与文案</h1>', '<h1>图文带货 · 图片与文案</h1><p class="state">发布用途：shoppable_photo / 自然挂车图文；不适用于付费轮播或商品详情页规则。</p>')
    (out / 'index.html').write_text(page_html, encoding='utf-8')
    return out / 'index.html'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('validate', 'compile', 'bind'):
        cmd = commands.add_parser(name)
        cmd.add_argument('--manifest', required=True, type=Path)
        if name == 'compile':
            cmd.add_argument('--out', required=True, type=Path)
        if name == 'bind':
            cmd.add_argument('--page', required=True)
            cmd.add_argument('--image', required=True, type=Path)
            cmd.add_argument('--kind', required=True, choices=['existing', 'generated'])
            cmd.add_argument('--provenance', required=True)
            cmd.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'compile':
            print(compile_pack(args.manifest, args.out))
        elif args.command == 'bind':
            print(bind(args.manifest, args.page, args.image, args.kind, args.provenance, args.output))
        else:
            validate(read(args.manifest))
            print('Inputs structurally valid; creative acceptance is not assessed.')
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(1, f'Blocked: {exc}\n')


if __name__ == '__main__':
    main()
