import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('creative_comparison', ROOT / 'scripts/creative_comparison.py')
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


def fake_probe(path):
    return {'duration_seconds': 15.0, 'width': 480, 'height': 854, 'has_audio': True}


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.doc = comparison.template('comparison-test')
        self.video = self.asset('video.mp4', 'fixture bytes, not actual media')
        self.image = self.asset('reference.png', 'fixture image')
        self.prompt = self.asset('user.txt', 'A specific usage task')
        self.backend = self.asset('backend.txt', 'A specific usage task\nNo lip sync')
        self.evidence = self.asset('receipt.json', '{}')
        self.doc['reference_video'] = self.video
        for run in self.doc['runs']:
            run.update(reference_video=self.video, reference_images=[self.image], reference_images_status='supplied',
                       user_prompt=self.prompt, backend_prompt=self.backend, output_video=self.video,
                       input_binding_evidence=self.evidence,
                       model_parameters={'status': 'documented', 'values': {'model': 'test-only'}, 'source': self.evidence})

    def asset(self, name, value):
        path = self.root / name
        path.write_text(value, encoding='utf-8')
        return comparison.artifact(path)

    def test_complete_materials_enable_observable_comparison_not_architecture_inference(self):
        result = comparison.compare(self.doc, fake_probe)
        self.assertEqual('ready_for_observable_comparison', result['status'])
        self.assertTrue(result['same_inputs_verified'])
        self.assertEqual('unknown', result['hidden_architecture'])
        self.assertEqual('not_assessed', result['creative_acceptance'])
        self.assertIn('+No lip sync', result['runs'][0]['prompt_delta'])

    def test_similar_outputs_cannot_replace_missing_input_receipts(self):
        self.doc['runs'][0]['input_binding_evidence'] = None
        result = comparison.compare(self.doc, fake_probe)
        self.assertFalse(result['same_inputs_verified'])
        self.assertEqual('not_ready_for_same_input_comparison', result['status'])
        self.assertIn('oumomo.input_binding_evidence', result['missing'])

    def test_unknown_images_are_not_treated_as_zero_images(self):
        run = self.doc['runs'][0]
        run['reference_images'] = None
        run['reference_images_status'] = 'unknown'
        result = comparison.compare(self.doc, fake_probe)
        self.assertFalse(result['same_inputs_verified'])
        self.assertIn('oumomo.reference_images', result['missing'])

    def test_explicit_no_images_can_match(self):
        for run in self.doc['runs']:
            run.update(reference_images=[], reference_images_status='confirmed_none')
        self.assertTrue(comparison.compare(self.doc, fake_probe)['same_inputs_verified'])

    def test_same_image_set_in_different_order_is_not_identical_input(self):
        second = self.asset('second.png', 'second fixture')
        self.doc['runs'][0]['reference_images'] = [self.image, second]
        self.doc['runs'][1]['reference_images'] = [second, self.image]
        self.assertFalse(comparison.compare(self.doc, fake_probe)['same_inputs_verified'])

    def test_changed_media_is_rejected_before_analysis(self):
        Path(self.video['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'sha256 mismatch'):
            comparison.compare(self.doc, fake_probe)

    def test_reference_trim_is_a_different_input(self):
        self.doc['runs'][1]['reference_video'] = self.asset('trim.mp4', '15 second trim')
        self.assertFalse(comparison.compare(self.doc, fake_probe)['same_inputs_verified'])

    def test_asr_cannot_attach_to_wrong_output(self):
        self.doc['runs'][0]['output_asr'] = self.asset('asr.json', json.dumps({'sha256': '0' * 64, 'segments': []}))
        with self.assertRaisesRegex(ValueError, 'ASR media hash mismatch'):
            comparison.compare(self.doc, fake_probe)

    def test_existing_asr_keeps_listening_scope_limitation(self):
        self.doc['reference_asr'] = self.asset('asr.json', json.dumps({'sha256': self.video['sha256'], 'segments': [{'start': 5.6, 'end': 7, 'text': 'Incomplete opening'}]}))
        report = comparison.compare(self.doc, fake_probe)
        self.assertIn('not verified pronunciation', report['reference']['asr']['scope'])

    def test_asr_rejects_outside_duration_negative_reversed_and_nonfinite_times(self):
        bad_times = [(-0.01, 1), (2, 1), (14, 15.051), (float('inf'), float('inf')),
                     (0, float('inf')), (float('nan'), 1), (0, float('nan')),
                     ('0', 1), (0, '1'), (False, 1), (0, True)]
        for start, end in bad_times:
            for side in ('reference', 'output'):
                with self.subTest(start=start, end=end, side=side):
                    doc = copy.deepcopy(self.doc)
                    asr = self.asset('invalid-time.json', json.dumps({'sha256': self.video['sha256'], 'segments': [{'start': start, 'end': end, 'text': 'test'}]}))
                    if side == 'reference':
                        doc['reference_asr'] = asr
                    else:
                        doc['runs'][0]['output_asr'] = asr
                    with self.assertRaisesRegex(ValueError, 'ASR (segment outside|timestamps must be)'):
                        comparison.compare(doc, fake_probe)

    def test_asr_allows_only_small_explicit_end_rounding_without_changing_times(self):
        self.doc['runs'][0]['output_asr'] = self.asset('rounded-asr.json', json.dumps({'sha256': self.video['sha256'], 'segments': [{'start': 14, 'end': 15.04, 'text': 'test'}]}))
        result = comparison.compare(self.doc, fake_probe)['runs'][0]['asr']
        self.assertEqual(15.04, result['segments'][0]['end'])
        self.assertEqual(15.0, result['timing_validation']['source_duration_seconds'])
        self.assertEqual(0.05, result['timing_validation']['end_rounding_tolerance_seconds'])
        self.assertFalse(result['timing_validation']['timestamps_modified'])

    def test_unknown_parameters_do_not_look_like_a_controlled_experiment(self):
        self.doc['runs'][0]['model_parameters'] = {'status': 'unknown', 'values': {}, 'source': None}
        result = comparison.compare(self.doc, fake_probe)
        self.assertEqual('ready_for_observable_comparison', result['status'])
        self.assertEqual('not_established', result['causal_effect'])
        self.assertTrue(any('oumomo: parameters unknown' in s for s in result['parameter_limits']))

    def test_parameter_claims_need_evidence(self):
        self.doc['runs'][0]['model_parameters']['source'] = None
        with self.assertRaisesRegex(ValueError, 'need source evidence'):
            comparison.compare(self.doc, fake_probe)

    def test_no_runtime_outputs_into_git_or_overwriting_existing_data(self):
        with self.assertRaisesRegex(ValueError, 'outside the Git workspace'):
            comparison.write_new(ROOT / 'comparison-runtime.json', '{}')
        target = self.root / 'new.json'
        comparison.write_new(target, '{}')
        with self.assertRaises(FileExistsError):
            comparison.write_new(target, 'changed')
        self.assertEqual('{}', target.read_text())

    def test_scope_cannot_be_promoted_to_full_listening_review(self):
        self.doc['observations'] = [{'kind': 'full_av_review', 'claim': 'great', 'limits': 'none', 'source': self.evidence}]
        with self.assertRaisesRegex(ValueError, 'actual evidence scope'):
            comparison.compare(self.doc, fake_probe)

    def test_us_sea_isolation_and_duplicate_runs(self):
        invalid = copy.deepcopy(self.doc)
        invalid['market'] = 'SEA'
        with self.assertRaisesRegex(ValueError, 'market=US'):
            comparison.compare(invalid, fake_probe)
        self.doc['runs'][1]['id'] = 'oumomo'
        with self.assertRaisesRegex(ValueError, 'unique'):
            comparison.compare(self.doc, fake_probe)

    def test_cli_template_keeps_all_inputs_unknown(self):
        target = self.root / 'intake.json'
        self.assertEqual(0, comparison.main(['init', 'next-example', '--output', str(target)]))
        doc = comparison.read(target)
        self.assertIsNone(doc['runs'][0]['reference_images'])
        self.assertEqual('unknown', doc['runs'][0]['model_parameters']['status'])

    def test_cli_partial_run_writes_report_but_returns_not_ready_exit_code(self):
        manifest = self.root / 'partial.json'
        manifest.write_text(json.dumps(comparison.template('partial-test')), encoding='utf-8')
        dest = self.root / 'reports'
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/creative_comparison.py'), 'compare', str(manifest), '--output-dir', str(dest)], capture_output=True, text=True)
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertEqual('not_ready_for_same_input_comparison', json.loads((dest / 'comparison.json').read_text())['status'])
        self.assertTrue((dest / 'comparison.md').exists())


if __name__ == '__main__':
    unittest.main()
