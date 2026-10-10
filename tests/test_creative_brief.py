import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
try:
    import creative_brief as brief
finally:
    # Do not let scripts/intel.py shadow the existing intel package in the
    # complete test suite after importing this standalone CLI.
    sys.path.pop(0)


def fixture():
    # Synthetic schema fixture: not product or market evidence.
    plan = dict.fromkeys(('id', 'purpose', 'audience_situation', 'current_alternative', 'buying_hesitation',
                         'belief_change', 'speaker_relationship', 'form_reason', 'opening_reason_to_watch',
                         'payoff', 'failure_condition'), 'synthetic test')
    plan.update(schema_version=1, market='US', product_family_id='LUN', audience_status='hypothesis',
                reference_decisions=[{'evidence': 't02-family-picnic/semantics', 'decision': 'adapt',
                                      'reason': 'Explain a concrete choice', 'transfer_limit': 'Do not copy capacity claims'}],
                product_facts=[{'id': 'f1', 'statement': 'Fixture only', 'source': 'synthetic test', 'limit': 'Not real evidence'}],
                scenes=[{'id': 's1', 'start': 0, 'end': 3, 'visual': 'Visible person talks',
                         'new_information': 'Choice', 'viewer_can_infer': 'Reason', 'delivery': 'Conversational',
                         'action_end_state': 'Ready', 'speech_mode': 'on_camera', 'face_visible': True,
                         'spoken_text': 'A test phrase.', 'speech_function': 'Explain', 'product_fact_ids': ['f1']},
                        {'id': 's2', 'start': 3, 'end': 6, 'visual': 'Hands only',
                         'new_information': 'Demonstration', 'viewer_can_infer': 'Result', 'delivery': 'Voiceover',
                         'action_end_state': 'Done', 'speech_mode': 'voiceover', 'face_visible': False,
                         'spoken_text': 'Another test phrase.', 'speech_function': 'Support', 'product_fact_ids': ['f1']}])
    return plan


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.library = brief.read(brief.DEFAULT)
        self.plan = fixture()

    def test_mixed_speech_preserved(self):
        brief.validate(self.plan, self.library)
        prompt = brief.compile_prompt(self.plan)
        self.assertIn('synchronized mouth movement', prompt)
        self.assertIn('off-camera voiceover for this scene', prompt)
        self.assertNotIn('No lip sync', prompt)

    def test_invisible_speaker_refused(self):
        self.plan['scenes'][0]['face_visible'] = False
        with self.assertRaisesRegex(ValueError, 'visible face'):
            brief.validate(self.plan, self.library)

    def test_expression_intent_reaches_actual_prompt(self):
        original = brief.compile_prompt(self.plan)
        for field in ('speech_function', 'viewer_can_infer'):
            plan = copy.deepcopy(self.plan)
            plan['scenes'][0][field] = 'Explain a current concern to a friend, not a feature announcement.'
            self.assertNotEqual(original, brief.compile_prompt(plan))
            self.assertIn(plan['scenes'][0][field], brief.compile_prompt(plan))
        self.plan['form_reason'] = 'Preserve a personal exchange before showing a detail.'
        self.assertNotEqual(original, brief.compile_prompt(self.plan))

    def test_speech_cannot_disappear_into_silent_mode(self):
        self.plan['scenes'][0]['speech_mode'] = 'none'
        with self.assertRaisesRegex(ValueError, 'non-speaking'):
            brief.validate(self.plan, self.library)

    def test_holdout_cannot_be_used_to_write_brief(self):
        case = next(c for c in self.library['cases'] if c['id'] == 't02-family-picnic')
        case['partition'] = 'holdout'
        self.library['methods'] = []
        with self.assertRaisesRegex(ValueError, 'calibration'):
            brief.validate(self.plan, self.library)

    def test_unknown_facts_and_invalid_clock_refused(self):
        for change in ({'product_fact_ids': ['invented']}, {'end': float('nan')}, {'start': 1}):
            with self.subTest(change=change):
                plan = copy.deepcopy(self.plan)
                plan['scenes'][0].update(change)
                with self.assertRaises(ValueError):
                    brief.validate(plan, self.library)

    def test_actual_prompt_drift_and_frozen_brief_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / 'plan.json'
            plan.write_text(json.dumps(self.plan), encoding='utf-8')
            compiled = root / 'v1'
            brief.compile_files(plan, brief.DEFAULT, compiled)
            prompt = compiled / 'prompt.txt'
            result = brief.verify(compiled, prompt)
            self.assertTrue(result['prompt_matches_brief'])
            self.assertFalse(result['generation_authorization'])
            actual = root / 'actual.txt'
            actual.write_bytes(prompt.read_bytes() + b'No lip sync throughout the entire video.')
            with self.assertRaisesRegex(ValueError, 'differs'):
                brief.verify(compiled, actual)
            (compiled / 'brief.json').write_text('{}', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'changed'):
                brief.verify(compiled, prompt)

    def test_existing_output_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'plan.json'
            path.write_text(json.dumps(self.plan), encoding='utf-8')
            with self.assertRaises(FileExistsError):
                brief.compile_files(path, brief.DEFAULT, Path(directory))

    def test_original_fact_based_brief_compiles_without_false_references(self):
        self.plan.update(reference_mode='original_product_facts',
                         reference_reason='No relevant calibration case for this product situation.',
                         reference_decisions=[])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'plan.json'
            path.write_text(json.dumps(self.plan), encoding='utf-8')
            compiled = root / 'original'
            record = brief.compile_files(path, brief.DEFAULT, compiled)
            self.assertEqual(record['schema_version'], 2)
            self.assertTrue(brief.verify(compiled, compiled / 'prompt.txt')['prompt_matches_brief'])
            self.assertEqual(brief.read(compiled / 'library-snapshot.json')['cases'], [])
            self.assertIn(self.plan['reference_reason'], (compiled / 'review.md').read_text(encoding='utf-8-sig'))
            self.assertIn(self.plan['reference_reason'], (compiled / 'prompt.txt').read_text(encoding='utf-8'))
        self.plan['reference_reason'] = ''
        with self.assertRaisesRegex(ValueError, 'reference_reason'):
            brief.validate(self.plan, self.library)

    def test_original_mode_cannot_bypass_existing_reference_checks(self):
        self.plan.update(reference_mode='original_product_facts', reference_reason='No match')
        with self.assertRaisesRegex(ValueError, 'cannot contain'):
            brief.validate(self.plan, self.library)
        self.plan.pop('reference_mode')
        self.plan['reference_decisions'][0]['evidence'] = 't02-family-picnic/missing'
        with self.assertRaisesRegex(ValueError, 'Unresolved'):
            brief.validate(self.plan, self.library)
        self.plan['reference_decisions'] = []
        with self.assertRaisesRegex(ValueError, 'Explicit reference'):
            brief.validate(self.plan, self.library)

    def test_all_compiler_exports_exclude_holdout_and_unscoped_root_metadata(self):
        marker = 'SYNTHETIC_HOLDOUT_ANSWER_DO_NOT_EXPORT'
        holdout = copy.deepcopy(self.library['cases'][0])
        holdout.update(id='synthetic-holdout', partition='holdout', title=marker,
                       comparison_group='synthetic-holdout', source_key='synthetic-holdout',
                       source_url=None, media=None)
        holdout['evidence'][0]['observation'] = marker
        self.library['cases'].append(holdout)
        self.library['methods'].append({'id': 'retired-holdout', 'status': 'retired',
                                        'when': marker, 'decision': marker, 'countercondition': marker,
                                        'supports': ['synthetic-holdout/' + holdout['evidence'][0]['id']]})
        self.library['evaluation_answers'] = marker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, library, compiled = root / 'plan.json', root / 'library.json', root / 'compiled'
            plan.write_text(json.dumps(self.plan), encoding='utf-8')
            library.write_text(json.dumps(self.library), encoding='utf-8')
            record = brief.compile_files(plan, library, compiled)
            self.assertEqual(record['library_sha256'], brief.digest(library.read_bytes()))
            self.assertNotEqual(record['library_sha256'], record['library_snapshot_sha256'])
            snapshot = brief.read(compiled / 'library-snapshot.json')
            self.assertTrue(all(case['partition'] == 'calibration' for case in snapshot['cases']))
            for path in compiled.rglob('*'):
                if path.is_file():
                    self.assertNotIn(marker.encode(), path.read_bytes(), str(path))
            self.assertTrue(brief.verify(compiled, compiled / 'prompt.txt')['prompt_matches_brief'])

    def test_legacy_frozen_package_still_verifies_without_rewriting_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            compiled = Path(directory)
            data = {'brief.json': json.dumps(self.plan).encode('utf-8'),
                    'library-snapshot.json': brief.DEFAULT.read_bytes(),
                    'prompt.txt': brief.compile_prompt(self.plan).encode('utf-8')}
            for name, content in data.items():
                (compiled / name).write_bytes(content)
            record = {'schema_version': 1, 'brief_sha256': brief.digest(data['brief.json']),
                      'library_sha256': brief.digest(data['library-snapshot.json']),
                      'prompt_sha256': brief.digest(data['prompt.txt'])}
            (compiled / 'manifest.json').write_text(json.dumps(record), encoding='utf-8')
            data['manifest.json'] = (compiled / 'manifest.json').read_bytes()
            self.assertTrue(brief.verify(compiled, compiled / 'prompt.txt')['prompt_matches_brief'])
            for name, content in data.items():
                self.assertEqual((compiled / name).read_bytes(), content)


if __name__ == '__main__':
    unittest.main()
