import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path

from PIL import Image

spec = importlib.util.spec_from_file_location('photo_copy', Path(__file__).resolve().parents[1] / 'scripts/photo_copy_pack.py')
pack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pack)


class PhotoCopyPackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'fixture.png'
        Image.new('RGB', (40, 60), 'gray').save(self.source)
        page = {'id': 'page-1', 'status': 'planned', 'overlay_text': 'Picture text',
                'alt_text': 'Test image description', 'purchase_role': 'Test role, not validated marketing',
                'evidence_refs': ['source-1'], 'image_prompt': 'Synthetic test prompt only'}
        second = dict(page, id='page-2')
        self.doc = {'schema_version': 1, 'market': 'US', 'music': 'publish-time-on-TikTok',
                    'product': {'product_family_id': 'TEST', 'offer_id': 'fixture', 'variant': 'gray',
                                'sources': [{'id': 'source-1', 'path': str(self.source),
                                             'sha256': pack.digest(self.source), 'role': 'Synthetic test evidence only'}]},
                    'strategy': {'audience_situation': 'Test context', 'desired_belief': 'Test belief',
                                 'reason_to_buy': 'Test hypothesis', 'voice': 'Test voice',
                                 'method_references': ['Test method; no effectiveness claim'],
                                 'limitations': ['Synthetic test, no real product']},
                    'publish_caption': 'Test caption', 'pages': [page, second]}
        self.manifest = self.root / 'manifest.json'
        self.save()

    def save(self):
        self.manifest.write_text(__import__('json').dumps(self.doc), encoding='utf-8')

    def test_planned_pack_has_prompts_caption_and_no_fake_image_or_acceptance(self):
        out = self.root / 'delivery'
        html = pack.compile_pack(self.manifest, out).read_text(encoding='utf-8')
        data = pack.read(out / 'pack.json')
        self.assertIn('图片待生成', html)
        self.assertNotIn('<img ', html)
        self.assertEqual('not_accepted', data['assessment']['creative'])
        self.assertFalse(data['assessment']['published'])
        self.assertEqual('publish-time-on-TikTok', data['music'])
        self.assertEqual('Test caption\n', (out / 'caption.txt').read_text())
        self.assertIn('Synthetic test prompt', (out / 'prompts.md').read_text(encoding='utf-8-sig'))
        self.assertEqual(0, data['assessment']['new_generation_calls'])

    def test_bind_existing_image_creates_new_version_and_exact_copy(self):
        original = pack.digest(self.manifest)
        bound = self.root / 'bound.json'
        pack.bind(self.manifest, 'page-2', self.source, 'existing', 'Fixture image only', bound)
        data = pack.read(bound)
        self.assertEqual('planned', pack.read(self.manifest)['pages'][1]['status'])
        self.assertEqual('bound', data['pages'][1]['status'])
        self.assertEqual(original, data['parent_manifest_sha256'])
        out = self.root / 'bound-delivery'
        pack.compile_pack(bound, out)
        result = pack.read(out / 'pack.json')
        self.assertEqual(pack.digest(self.source), pack.digest(out / result['pages'][1]['image']['delivery_path']))
        self.assertEqual(['page-1', 'page-2'], [p['id'] for p in result['pages']])

    def test_changed_product_source_blocks_compile(self):
        self.source.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed local source'):
            pack.compile_pack(self.manifest, self.root / 'delivery')

    def test_no_unknown_evidence_references(self):
        self.doc['pages'][0]['evidence_refs'] = ['unseen']
        with self.assertRaisesRegex(ValueError, 'known local sources'):
            pack.validate(self.doc)

    def test_cannot_call_planned_picture_generated(self):
        self.doc['pages'][0]['image'] = {'path': str(self.source)}
        with self.assertRaisesRegex(ValueError, 'Planned page'):
            pack.validate(self.doc)

    def test_no_duplicate_pages(self):
        self.doc['pages'][1]['id'] = 'page-1'
        with self.assertRaisesRegex(ValueError, 'duplicate page'):
            pack.validate(self.doc)

    def test_music_has_no_local_production_requirement(self):
        self.assertIs(self.doc, pack.validate(self.doc))
        self.assertNotIn('duration', self.doc)
        self.assertNotIn('audio', self.doc)
        self.doc['music'] = 'locally-generated-song'
        with self.assertRaisesRegex(ValueError, 'TikTok publishing'):
            pack.validate(self.doc)

    def test_legacy_placement_defaults_are_explicit_in_html_and_pack(self):
        out = self.root / 'delivery'
        html = pack.compile_pack(self.manifest, out).read_text(encoding='utf-8')
        self.assertEqual('shoppable_photo', pack.read(out / 'pack.json')['placement'])
        self.assertIn('发布用途：shoppable_photo', html)
        self.assertNotIn('placement', pack.read(self.manifest))

    def test_explicit_shoppable_placement_is_preserved(self):
        self.doc['placement'] = 'shoppable_photo'
        self.save()
        out = self.root / 'delivery'
        pack.compile_pack(self.manifest, out)
        self.assertEqual('shoppable_photo', pack.read(out / 'pack.json')['placement'])

    def test_paid_carousel_and_product_listing_are_not_treated_as_natural_photos(self):
        for placement in ('paid_carousel', 'product_listing'):
            with self.subTest(placement=placement):
                self.doc['placement'] = placement
                with self.assertRaisesRegex(ValueError, 'require separate placement rules'):
                    pack.validate(self.doc)

    def test_review_escapes_html_in_untrusted_copy_and_prompt(self):
        self.doc['pages'][0]['overlay_text'] = '<script>alert(1)</script>'
        self.doc['pages'][0]['image_prompt'] = '</pre><script>alert(2)</script>'
        self.save()
        html = pack.compile_pack(self.manifest, self.root / 'delivery').read_text(encoding='utf-8')
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)

    def test_rebinding_preserves_previous_image_and_protects_output(self):
        first = self.root / 'first.json'
        pack.bind(self.manifest, 'page-1', self.source, 'existing', 'Test', first)
        second = self.root / 'second.json'
        pack.bind(first, 'page-1', self.source, 'generated', 'Test generated status; test asset only', second)
        self.assertEqual('existing', pack.read(second)['pages'][0]['image_history'][0]['kind'])
        with self.assertRaises(FileExistsError):
            pack.bind(first, 'page-1', self.source, 'existing', 'Test', second)

    def test_delivery_directory_cannot_be_overwritten(self):
        out = self.root / 'delivery'
        pack.compile_pack(self.manifest, out)
        with self.assertRaises(FileExistsError):
            pack.compile_pack(self.manifest, out)


if __name__ == '__main__':
    unittest.main()
