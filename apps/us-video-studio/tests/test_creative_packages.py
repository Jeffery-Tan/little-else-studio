import copy
import json
from pathlib import Path

import pytest
from PIL import Image

from studio.creative_packages import CreativePackages
from studio.learning_assets import LearningAssets, REPOSITORY, digest
from studio.store import Store


@pytest.fixture()
def setup(tmp_path):
    store = Store(tmp_path / 'data')
    store.create_run('pack-test', '离线编译', '隔离验收')
    artifact = store.write_artifact('pack-test', 'intake', 1, '测试商品事实',
                                    summary='事实', author_role='orchestrator', attempt='test')
    packages = CreativePackages(store)
    args = dict(stage='intake', version=1, artifact_sha256=artifact.sha256)
    return store, packages, args


def video_manifest(args):
    doc = json.loads((REPOSITORY / 'tracks/us/creative/learning/brief-example.json').read_text(encoding='utf-8-sig'))
    for fact in doc['product_facts']:
        fact['source'] = 'artifact:intake-v1#' + args['artifact_sha256']
    return doc


def test_video_existing_compiler_frozen_package_and_no_approval(setup):
    store, packages, args = setup
    manifest = video_manifest(args)
    pack = packages.compile('pack-test', format='video', manifest=manifest, **args)
    assert pack == packages.compile('pack-test', format='video', manifest=manifest, **args)
    assert len(packages.packages('pack-test')) == 1
    compiled = json.loads(packages.file('pack-test', pack['package_id'], 'manifest.json').read_text(encoding='utf-8'))
    assert compiled['generation_authorized'] is False
    assert compiled['references_approved'] is False
    assert 'review.md' in [f['name'] for f in pack['files']]
    assert len([e for e in store.read_events('pack-test') if e['kind'] == 'creative_package_created']) == 1
    with pytest.raises(ValueError):
        packages.file('pack-test', pack['package_id'], '../../run.json')
    packages.file('pack-test', pack['package_id'], 'prompt.txt').write_text('tampered')
    with pytest.raises(ValueError, match='变化'):
        packages.packages('pack-test')


def test_video_rejects_stale_artifact_and_arbitrary_fact_paths(setup):
    _, packages, args = setup
    manifest = video_manifest(args)
    with pytest.raises(ValueError, match='版本'):
        packages.compile('pack-test', format='video', manifest=manifest, **dict(args, artifact_sha256='0'*64))
    manifest['product_facts'][0]['source'] = 'C:/secret.txt'
    with pytest.raises(ValueError, match='source'):
        packages.compile('pack-test', format='video', manifest=manifest, **args)


def test_video_freezes_material_identity_and_record_integrity(setup):
    store, packages, args = setup
    material = store.run_dir('pack-test') / 'materials/test.png'
    material.parent.mkdir()
    material.write_bytes(b'first-byte-identity')
    manifest = video_manifest(args)
    manifest['product_facts'][0]['source'] = 'material:test.png'
    first = packages.compile('pack-test', format='video', manifest=manifest, **args)
    assert first['source_hashes']['material:test.png'] == digest(material)
    material.write_bytes(b'new-byte-identity')
    second = packages.compile('pack-test', format='video', manifest=manifest, **args)
    assert second['package_id'] != first['package_id']
    record = store.run_dir('pack-test') / 'creative-packages' / second['package_id'] / 'package-record.json'
    corrupted = json.loads(record.read_text(encoding='utf-8'))
    corrupted['files'] = []
    record.write_text(json.dumps(corrupted), encoding='utf-8')
    with pytest.raises(ValueError, match='绑定记录'):
        packages.packages('pack-test')


def photo_manifest(store):
    folder = store.run_dir('pack-test') / 'materials'
    folder.mkdir()
    source = folder / 'product.png'
    Image.new('RGB', (16,16), 'white').save(source)
    return {'schema_version': 1, 'market': 'US', 'music': 'publish-time-on-TikTok',
            'product': {'product_family_id': 'TEST', 'offer_id': 'test', 'variant': 'test',
                        'sources': [{'id': 'photo', 'role': '商品原图', 'path': source.name, 'sha256': digest(source)}]},
            'strategy': {'audience_situation': 'test', 'desired_belief': 'test', 'reason_to_buy': 'test', 'voice': 'test',
                         'method_references': ['仅测试'], 'limitations': ['没有生成或内容审核']},
            'publish_caption': '测试文案',
            'pages': [{'id': f'page-{i}', 'overlay_text': '测试', 'alt_text': '原图',
                       'purchase_role': '说明', 'image_prompt': 'test', 'evidence_refs': ['photo'],
                       'status': 'bound' if i==1 else 'planned',
                       **({'image': {'path': source.name, 'sha256': digest(source), 'kind': 'existing', 'provenance': 'test'}} if i==1 else {})}
                      for i in range(1,3)]}


def test_photo_keeps_original_bytes_and_planned_state(setup):
    store, packages, args = setup
    manifest = photo_manifest(store)
    pack = packages.compile('pack-test', format='photo', manifest=manifest, **args)
    actual = json.loads(packages.file('pack-test', pack['package_id'], 'pack.json').read_text(encoding='utf-8'))
    assert actual['assessment']['new_generation_calls'] == 0
    assert actual['pages'][1]['status'] == 'planned'
    copied = packages.file('pack-test', pack['package_id'], actual['pages'][0]['image']['delivery_path'])
    assert digest(copied) == manifest['pages'][0]['image']['sha256']
    assert not actual['assessment']['published']
    unsafe = copy.deepcopy(manifest)
    unsafe['product']['sources'][0]['path'] = '../product.png'
    with pytest.raises(ValueError, match='文件名'):
        packages.compile('pack-test', format='photo', manifest=unsafe, **args)


def test_learning_media_allowlist_and_digest(tmp_path):
    media = tmp_path / 'media'
    media.mkdir()
    file = media / 'test.mp4'
    file.write_bytes(b'original')
    assets = LearningAssets(tmp_path / 'repo', media)
    catalog = {'cases': [{'id': 'test', 'media': {'path': str(file), 'sha256': digest(file)}}]}
    assert assets.case_media(catalog, 'test') == file
    file.write_bytes(b'changed')
    with pytest.raises(ValueError, match='变化'):
        assets.case_media(catalog, 'test')
    with pytest.raises(ValueError):
        assets.archive('../../secret')
    with pytest.raises(ValueError):
        assets.photo_archive_file('../../secret')
    photo_root = media / 'photo-copy-methods-exercise-20260918/review-v1'
    photo_root.mkdir(parents=True)
    (photo_root / 'caption.txt').write_text('test caption')
    assert assets.photo_archive_file('caption.txt').read_text() == 'test caption'
    outside = tmp_path / 'outside.mp4'
    outside.write_bytes(b'outside')
    catalog['cases'][0]['media'] = {'path': str(outside), 'sha256': digest(outside)}
    with pytest.raises(ValueError, match='目录'):
        assets.case_media(catalog, 'test')
