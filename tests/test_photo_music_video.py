import copy
import importlib.util
import json
import shutil
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from PIL import Image

spec = importlib.util.spec_from_file_location('photo_music', Path(__file__).resolve().parents[1] / 'scripts/photo_music_video.py')
pm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pm)


class PhotoMusicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        fonts = [Path('C:/Windows/Fonts/arial.ttf'), Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
        font = next((p for p in fonts if p.is_file()), None)
        if not font:
            self.skipTest('No test font available')
        source = self.root / 'source.png'
        Image.new('RGB', (500, 500), '#556655').save(source)
        scene = {'image': {'path': str(source), 'sha256': pm.digest(source), 'rights': 'internal_review'},
                 'variant': 'fixture', 'headline': 'Fixture only', 'frames': 45,
                 'evidence': 'synthetic unit-test image, not product evidence', 'new_information': 'test only'}
        self.doc = {'schema_version': 1, 'market': 'US', 'mode': 'technical',
                    'product_family_id': 'TEST', 'offer_id': 'fixture', 'variant': 'fixture',
                    'creative_intent': 'test only', 'limitations': ['Not product material'],
                    'fps': 30, 'size': [720, 1280],
                    'font': {'path': str(font), 'sha256': pm.digest(font)},
                    'scenes': [scene, copy.deepcopy(scene)], 'audio': {'kind': 'technical_beats', 'bpm': 120}}
        self.manifest = self.root / 'manifest.json'
        self.out = self.root / 'prepared'

    def save(self):
        pm.write(self.manifest, self.doc)

    def approve_fixture(self):
        data = pm.read(self.out / 'layout.json')
        data.update(approved=True, reviewer='automated-test-fixture', reviewed_at='test-only',
                    layout_sha256=pm.digest(self.out / 'layout.json'))
        path = self.root / 'review.json'
        pm.write(path, data)
        return path

    def test_changed_source_rejected(self):
        Path(self.doc['scenes'][0]['image']['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed input'):
            pm.validate(self.doc)

    def test_product_variant_mismatch_rejected(self):
        self.doc['scenes'][1]['variant'] = 'different'
        with self.assertRaisesRegex(ValueError, 'variant mismatch'):
            pm.validate(self.doc)

    def test_crop_outside_original_rejected(self):
        self.doc['scenes'][0]['crop'] = [0, 0, 501, 500]
        with self.assertRaisesRegex(ValueError, 'Crop exceeds'):
            pm.validate(self.doc)

    def test_missing_evidence_rejected(self):
        del self.doc['scenes'][0]['evidence']
        with self.assertRaisesRegex(ValueError, 'evidence'):
            pm.validate(self.doc)

    def test_unknown_audio_blocks_instead_of_silent_export(self):
        self.doc['audio'] = {}
        with self.assertRaisesRegex(ValueError, 'Supply licensed music'):
            pm.validate(self.doc)

    def test_image_rights_block_commercial_mode(self):
        self.doc['mode'] = 'commercial_local'
        with self.assertRaisesRegex(ValueError, 'image rights'):
            pm.validate(self.doc)

    def test_test_beats_cannot_be_claimed_commercial(self):
        self.doc['mode'] = 'commercial_local'
        evidence = self.root / 'fixture-license.txt'
        evidence.write_text('Test fixture; not actual commercial authorization')
        for scene in self.doc['scenes']:
            scene['image'].update(rights='commercial', rights_evidence={'path': str(evidence), 'sha256': pm.digest(evidence)})
        with self.assertRaisesRegex(ValueError, 'restricted to technical'):
            pm.validate(self.doc)

    def test_audio_license_record_and_hash_required(self):
        p = self.root / 'test.wav'
        pm.synth_beats(p, 3)
        self.doc['audio'] = {'kind': 'licensed_file', 'path': str(p), 'sha256': pm.digest(p)}
        with self.assertRaisesRegex(ValueError, 'Incomplete audio license'):
            pm.validate(self.doc)

    def licensed_fixture(self, seconds):
        p = self.root / 'licensed-fixture.wav'
        pm.synth_beats(p, seconds)
        evidence = self.root / 'test-license.txt'
        evidence.write_text('Automated fixture, not a real music license')
        self.doc['audio'] = {'kind': 'licensed_file', 'path': str(p), 'sha256': pm.digest(p),
                             'license': {'holder': 'test fixture', 'source': 'synthetic local test',
                                         'scope': 'internal_review', 'verified_by': 'test fixture',
                                         'verified_at': 'test only',
                                         'evidence': {'path': str(evidence), 'sha256': pm.digest(evidence)}}}

    @unittest.skipUnless(shutil.which('ffprobe'), 'FFprobe unavailable')
    def test_licensed_local_audio_entry_accepts_complete_fixture(self):
        self.licensed_fixture(3)
        self.assertIs(self.doc, pm.validate(self.doc))

    @unittest.skipUnless(shutil.which('ffprobe'), 'FFprobe unavailable')
    def test_short_music_does_not_implicitly_loop(self):
        self.licensed_fixture(1)
        with self.assertRaisesRegex(ValueError, 'Music too short'):
            pm.validate(self.doc)

    def test_changed_license_evidence_rejected(self):
        self.licensed_fixture(3)
        Path(self.doc['audio']['license']['evidence']['path']).write_text('Changed fixture')
        with self.assertRaisesRegex(ValueError, 'changed input'):
            pm.validate(self.doc)

    def test_overlong_copy_blocks_layout(self):
        self.doc['scenes'][0]['headline'] = 'W' * 68
        self.save()
        with self.assertRaisesRegex(ValueError, 'safe width'):
            pm.prepare(self.manifest, self.out)

    def test_changed_manifest_invalidates_layout_review(self):
        self.save()
        pm.prepare(self.manifest, self.out)
        approval = self.approve_fixture()
        self.doc['scenes'][0]['headline'] = 'Changed'
        self.save()
        with self.assertRaisesRegex(ValueError, 'Layout stale'):
            pm.render(self.manifest, self.out, approval)

    def test_non_lunch_family_is_not_mislabeled_lunch(self):
        original = pm.ImageDraw.ImageDraw.text
        labels = []
        def capture(draw, xy, text, *args, **kwargs):
            labels.append(text)
            return original(draw, xy, text, *args, **kwargs)
        self.doc['product_family_id'] = 'BAG'
        with patch.object(pm.ImageDraw.ImageDraw, 'text', capture):
            pm.frame(self.doc, self.doc['scenes'][0], 0)
        self.assertIn('LITTLE ELSE / BAG', labels)
        self.assertNotIn('LITTLE ELSE / LUNCH', labels)

    def test_layout_timing_tamper_invalidates_review(self):
        self.save()
        pm.prepare(self.manifest, self.out)
        approval = self.approve_fixture()
        layout = pm.read(self.out / 'layout.json')
        layout['slides'][0]['frames'] = 60
        layout['slides'][1]['frames'] = 30
        pm.write(self.out / 'layout.json', layout)
        with self.assertRaisesRegex(ValueError, 'review missing or mismatched'):
            pm.render(self.manifest, self.out, approval)

    def test_layout_reordering_is_rejected_even_with_fresh_review(self):
        self.save()
        pm.prepare(self.manifest, self.out)
        layout = pm.read(self.out / 'layout.json')
        layout['slides'].reverse()
        pm.write(self.out / 'layout.json', layout)
        with self.assertRaisesRegex(ValueError, 'sequence/timing'):
            pm.render(self.manifest, self.out, self.approve_fixture())

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg unavailable')
    def test_end_to_end_has_audio_exact_frames_and_no_content_acceptance(self):
        self.save()
        pm.prepare(self.manifest, self.out)
        video = pm.render(self.manifest, self.out, self.approve_fixture())
        result = pm.read(self.out / 'verification.json')
        self.assertTrue(video.is_file())
        self.assertEqual(90, result['frames'])
        self.assertEqual(3, result['duration_seconds'])
        self.assertEqual('technical_beats', result['audio_kind'])
        self.assertFalse(result['content_accepted'])
        self.assertFalse(result['publishing_authorized'])
        self.assertEqual(0, result['new_paid_calls'])
        self.assertEqual(2, len(result['sample_frames']))
        with self.assertRaisesRegex(ValueError, 'already exists'):
            pm.render(self.manifest, self.out, self.approve_fixture())


if __name__ == '__main__':
    unittest.main()
