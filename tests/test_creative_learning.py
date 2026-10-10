import copy
import importlib.util
import json
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('creative_learning', ROOT/'scripts/creative_learning.py')
learning = importlib.util.module_from_spec(spec)
spec.loader.exec_module(learning)


class CreativeLearningTests(unittest.TestCase):
    def setUp(self):
        self.doc = learning.read(learning.DEFAULT)

    def feedback(self, **changes):
        # Synthetic events stay in this fixture and temporary output libraries.
        event = {'run': 'synthetic-run', 'artifact_version': 'speed-v1',
                 'source_event': 'synthetic-message-1', 'source': 'fixture://feedback',
                 'recorded_at': '2026-09-18', 'quote': '这个节奏可以', 'scope': 'pace',
                 'action': 'diagnose', 'observation': '只认可该版本节奏，未认可全片。',
                 'hypothesis': None}
        return dict(event, **changes)

    def test_feedback_is_idempotent_versioned_and_preserves_source_and_history(self):
        before = copy.deepcopy(self.doc)
        cid = 'lun-25l-speed'
        once = learning.record_feedback(self.doc, cid, self.feedback())
        twice = learning.record_feedback(once, cid, self.feedback())
        self.assertEqual(once, twice)
        self.assertEqual(before, self.doc)
        self.assertEqual(2, once['library_version'])
        updated = next(c for c in once['cases'] if c['id'] == cid)
        old = next(c for c in before['cases'] if c['id'] == cid)
        for key in ('source_key', 'comparison_group', 'media', 'user_feedback', 'market_effect', 'partition', 'eligible_for'):
            self.assertEqual(old[key], updated[key])
        self.assertEqual(old['evidence'], updated['evidence'][:-1])
        self.assertIsNone(updated['feedback_history'][0]['improvement_effect'])
        next_doc = learning.record_feedback(once, cid, self.feedback(source_event='synthetic-message-2', quote='先暂停', action='stop'))
        self.assertEqual(3, next_doc['library_version'])
        next_case = next(c for c in next_doc['cases'] if c['id'] == cid)
        self.assertEqual(updated['feedback_history'][0], next_case['feedback_history'][0])
        self.assertEqual('stop', next_case['feedback_history'][-1]['action'])
        self.assertEqual([], learning.validate(next_doc))

    def test_feedback_conflicts_and_unknown_cases_fail_without_mutation(self):
        once = learning.record_feedback(self.doc, 'lun-25l-speed', self.feedback())
        before = copy.deepcopy(once)
        for cid, event in [('lun-25l-speed', self.feedback(quote='全片通过')),
                           ('gym-19s-rejected', self.feedback())]:
            with self.assertRaisesRegex(ValueError, 'Conflicting feedback identity'):
                learning.record_feedback(once, cid, event)
        with self.assertRaisesRegex(ValueError, 'Unknown case_id'):
            learning.record_feedback(once, 'unregistered-case', self.feedback())
        self.assertEqual(before, once)

    def test_feedback_cannot_claim_success_or_change_identity_or_approval(self):
        for changes in ({'improvement_effect': 'improved'}, {'approved': True},
                        {'source_key': 'replacement'}, {'scope': 'everything'},
                        {'action': 'generate'}, {'recorded_at': 'not-a-date'},
                        {'quote': ''}, {'evidence_refs': ['missing/evidence']}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                learning.record_feedback(self.doc, 'lun-25l-speed', self.feedback(**changes))

    def test_feedback_cold_reload_is_visible_with_exact_scope_and_version(self):
        updated = learning.record_feedback(self.doc, 'lun-25l-speed', self.feedback())
        with tempfile.TemporaryDirectory() as temp:
            library = Path(temp)/'library.json'
            learning.write_new(library, json.dumps(updated, ensure_ascii=False))
            reloaded = learning.read(library)
        context = learning.render(reloaded, learning.select(reloaded, ['family', 'pace'], 'message'), 'message')
        self.assertIn('这个节奏可以', context)
        self.assertIn('"scope": "pace"', context)
        self.assertIn('"improvement_effect": null', context)
        self.assertIn('学习库版本：2', context)
        self.assertIn('市场效果：null', context)

    def test_top_one_seed_keeps_supporting_negative_evidence_and_counterconditions(self):
        seeds = learning.select(self.doc, ['persona', 'reason', 'speech'], 'message', 1)
        subset = learning.creator_library(self.doc, {c['id'] for c in seeds})
        ids = {c['id'] for c in subset['cases']}
        self.assertGreater(len(ids), 1)
        method = next(m for m in subset['methods'] if m['id'] == 'reason-not-filler')
        self.assertTrue({'n01-nurse-packing', 'gym-19s-rejected'} <= ids)
        context = learning.render(self.doc, seeds, 'message')
        self.assertIn(method['countercondition'], context)
        self.assertIn('No lip sync', context)
        self.assertEqual([], learning.validate(subset))

    def test_performance_dependencies_keep_their_actual_evidence_purpose(self):
        case = next(c for c in self.doc['cases'] if c['id'] == 'n01-nurse-packing')
        case['eligible_for'].append('performance')
        case['evidence'].append({'id': 'synthetic-full-review', 'kind': 'full_av_review',
                                'source': 'fixture://review', 'observation': 'Synthetic review for boundary test only',
                                'reviewer': 'test', 'reviewed_at': '2026-09-18', 'scope': 'synthetic fixture'})
        self.assertEqual([], learning.validate(self.doc))
        seeds = learning.select(self.doc, ['persona'], 'performance', 1)
        context = learning.render(self.doc, seeds, 'performance')
        seed_section = context.split('## n01-nurse-packing')[1].split('\n## ')[0]
        dependency_section = context.split('## gym-19s-rejected')[1].split('\n## ')[0]
        self.assertIn('按本次用途检索的种子案例', seed_section)
        self.assertIn('仅为方法支持/反例补齐的依赖案例', dependency_section)
        self.assertIn('不具备该用途资格', dependency_section)
        self.assertIn('eligible_for）：message, storyboard。', dependency_section)
        self.assertIn('No lip sync', dependency_section)
        self.assertIn('reason-not-filler', context)

    def test_explicit_counterexamples_expand_but_do_not_train_from_holdout(self):
        method = self.doc['methods'][0]
        method['counterexamples'] = ['lun-remake-v2/feedback']
        subset = learning.creator_library(self.doc, {'n01-nurse-packing'})
        self.assertIn('lun-remake-v2', {c['id'] for c in subset['cases']})
        next(c for c in self.doc['cases'] if c['id'] == 'lun-remake-v2')['partition'] = 'holdout'
        context = learning.render(self.doc, [next(c for c in self.doc['cases'] if c['id'] == 'n01-nurse-packing')], 'message')
        self.assertNotIn(method['id'], context)
        self.assertNotIn('lun-remake-v2', context)

    def test_creator_export_excludes_unrelated_holdout_root_data_and_retired_methods(self):
        self.doc['private_test_answers'] = 'SYNTHETIC-HOLDOUT-ANSWER'
        item = learning.candidate('synthetic-holdout', 'SYNTHETIC-HOLDOUT-ANSWER', ['speech'],
                                  url='https://www.tiktok.com/@test/video/9999888')
        item['partition'] = 'holdout'
        self.doc['cases'].append(item)
        self.doc['methods'].append(dict(self.doc['methods'][0], id='SYNTHETIC-RETIRED-ANSWER', status='retired'))
        subset = learning.creator_library(self.doc, {'n01-nurse-packing'})
        serialized = json.dumps(subset)
        self.assertNotIn('SYNTHETIC-', serialized)
        self.assertNotIn('t01-tote-reference', serialized)
        self.assertEqual([], learning.validate(subset))
        self.assertEqual([], learning.creator_library(self.doc, set())['cases'])
        with self.assertRaises(ValueError):
            learning.creator_library(self.doc, {'synthetic-holdout'})

    def test_feedback_cli_new_library_repeat_and_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            event, output, repeated = [Path(temp)/name for name in ('event.json', 'first.json', 'repeat.json')]
            event.write_text(json.dumps(self.feedback()), encoding='utf-8')
            def run(*args):
                return subprocess.run([sys.executable, str(ROOT/'scripts/creative_learning.py'), *args],
                                      capture_output=True, text=True, encoding='utf-8')
            args = ['record-feedback', '--case-id', 'lun-25l-speed', '--feedback', str(event)]
            result = run(*args, '--output', str(output))
            self.assertEqual(0, result.returncode, result.stderr)
            result = run('--library', str(output), *args, '--output', str(repeated))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(json.loads(result.stdout)['appended'])
            self.assertEqual(output.read_bytes(), repeated.read_bytes())
            event.write_text(json.dumps(self.feedback(quote='conflicting edit')), encoding='utf-8')
            result = run('--library', str(output), *args, '--output', str(Path(temp)/'conflict.json'))
            self.assertNotEqual(0, result.returncode)
            self.assertFalse((Path(temp)/'conflict.json').exists())

    def test_blocked_feedback_dependencies_are_removed_before_method_expansion(self):
        item = learning.candidate('synthetic-holdout', 'SYNTHETIC-HOLDOUT-ANSWER', ['speech'],
                                  url='https://www.tiktok.com/@test/video/9999888')
        item.update(partition='holdout', evidence=[{'id': 'answer', 'kind': 'user_feedback',
                                                  'source': 'fixture://test', 'observation': 'SYNTHETIC-HOLDOUT-ANSWER'}])
        self.doc['cases'].append(item)
        updated = learning.record_feedback(self.doc, 'n01-nurse-packing', self.feedback(
            quote='SYNTHETIC-HOLDOUT-ANSWER', observation='SYNTHETIC-HOLDOUT-ANSWER',
            evidence_refs=['synthetic-holdout/answer']))
        fid = learning.feedback_key(self.feedback())
        updated['methods'].append(dict(updated['methods'][0], id='blocked-dependency',
                                        supports=[f'n01-nurse-packing/{fid}', 't01-tote-reference/semantics']))
        seeds = {'n01-nurse-packing'}
        subset = learning.creator_library(updated, seeds)
        self.assertEqual(subset, learning.creator_library(subset, seeds))
        self.assertEqual([], learning.validate(subset))
        self.assertNotIn('SYNTHETIC-HOLDOUT-ANSWER', json.dumps(subset))
        self.assertNotIn('blocked-dependency', json.dumps(subset))
        self.assertNotIn('t01-tote-reference', json.dumps(subset))

    def test_actual_library_has_scoped_evidence_and_no_fake_listening(self):
        self.assertEqual([], learning.validate(self.doc))
        self.assertEqual(8, len(self.doc['cases']))
        self.assertEqual([], learning.select(self.doc, ['speech'], 'performance'))

    def test_known_failure_is_retrieved_alongside_relevant_reference(self):
        cases = learning.select(self.doc, ['packing', 'persona', 'speech', 'reason'], 'message')
        self.assertTrue({'gym-19s-rejected', 'n01-nurse-packing', 't02-family-picnic'} <= {c['id'] for c in cases})
        text = learning.render(self.doc, cases, 'message')
        self.assertIn('reason-not-filler', text)
        self.assertIn('No lip sync', text)
        self.assertIn('市场效果：null', text)

    def test_holdout_and_pending_do_not_leak_into_context(self):
        for c in self.doc['cases']:
            c['partition'] = 'holdout' if c['id'].startswith('n01') else 'pending'
        self.assertEqual([], learning.select(self.doc, ['packing'], 'message'))
        self.assertTrue(any('holdout evidence' in x for x in learning.validate(self.doc)))

    def test_url_tracking_and_creator_alias_do_not_duplicate_same_video(self):
        one = learning.source_key('https://www.tiktok.com/@a/video/12345?share_id=abc')
        two = learning.source_key('https://tiktok.com/@b/video/12345')
        self.assertEqual(one, two)
        item = copy.deepcopy(self.doc['cases'][0]); item['id'] = 'duplicate-id'
        with self.assertRaisesRegex(ValueError, 'duplicate source_key'):
            learning.register(self.doc, item)

    def test_local_copy_cannot_be_counted_as_independent_case(self):
        item = copy.deepcopy(self.doc['cases'][0])
        item.update(id='copy-case', source_key='new-alias')
        with self.assertRaisesRegex(ValueError, 'duplicate media hash'):
            learning.register(self.doc, item)

    def test_manual_merge_cannot_hide_duplicate_url_with_alias_and_no_media(self):
        item = copy.deepcopy(self.doc['cases'][0])
        item.update(id='alias-example', source_key='alias', media=None)
        with self.assertRaisesRegex(ValueError, 'duplicate canonical video'):
            learning.register(self.doc, item)

    def test_derivative_of_calibration_source_cannot_be_unseen_holdout(self):
        item = copy.deepcopy(self.doc['cases'][0])
        item.update(id='derived-example', source_key='different-render', source_url=None, media=None, partition='holdout')
        with self.assertRaisesRegex(ValueError, 'calibration/holdout leakage'):
            learning.register(self.doc, item)

    def test_pending_case_cannot_support_active_method(self):
        next(c for c in self.doc['cases'] if c['id'] == 'n01-nurse-packing')['partition'] = 'pending'
        self.assertTrue(any('pending/holdout evidence' in x for x in learning.validate(self.doc)))

    def test_asr_cannot_be_promoted_to_performance_by_eligibility_flag(self):
        self.doc['cases'][0]['eligible_for'].append('performance')
        self.assertTrue(any('cannot qualify performance' in x for x in learning.validate(self.doc)))

    def test_dangling_method_and_unscoped_user_approval_rejected(self):
        self.doc['methods'][0]['supports'] = ['missing/not-real']
        self.doc['cases'][0]['user_feedback'] = {'approved': True}
        errors = learning.validate(self.doc)
        self.assertTrue(any('unresolved evidence' in x for x in errors))
        self.assertTrue(any('exact scope' in x for x in errors))

    def test_intake_is_not_auto_learned_and_never_overwrites(self):
        c = learning.candidate('new-example', 'Example', ['speech'], url='https://www.tiktok.com/@a/video/99999')
        doc = learning.register(self.doc, c)
        self.assertEqual('pending', c['partition'])
        self.assertNotIn('new-example', {x['id'] for x in learning.select(doc, ['speech'], 'message', 8)})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'existing.txt'; path.write_text('user work')
            with self.assertRaises(FileExistsError):
                learning.write_new(path, 'replacement')
            self.assertEqual('user work', path.read_text())

    def test_other_market_is_rejected(self):
        self.doc['cases'][0]['market'] = 'SEA'
        self.assertTrue(any('cross-market' in x for x in learning.validate(self.doc)))

    def test_untrusted_redirect_or_credential_url_not_fetched(self):
        for url in ['https://evil.example/@x/video/123', 'https://user:secret@www.tiktok.com/@x/video/123', 'https://vm.tiktok.com/short']:
            with self.assertRaises(ValueError):
                learning.source_key(url)

    def test_cli_intake_merge_and_empty_performance_route(self):
        import json
        script = str(ROOT/'scripts/creative_learning.py')
        def run(*args):
            return subprocess.run([sys.executable, script, *args], capture_output=True, text=True, encoding='utf-8')
        with tempfile.TemporaryDirectory() as temp:
            item, merged = Path(temp)/'case.json', Path(temp)/'library.json'
            result = run('intake', '--id', 'cli-example', '--title', 'CLI example', '--tags', 'speech',
                         '--url', 'https://www.tiktok.com/@example/video/999999999999', '--output', str(item))
            self.assertEqual(0, result.returncode, result.stderr)
            result = run('merge', '--case', str(item), '--output', str(merged))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual('pending', json.loads(merged.read_text(encoding='utf-8'))['cases'][-1]['partition'])
            duplicate = run('--library', str(merged), 'merge', '--case', str(item), '--output', str(Path(temp)/'duplicate.json'))
            self.assertNotEqual(0, duplicate.returncode)
            self.assertFalse((Path(temp)/'duplicate.json').exists())
        result = run('context', '--tags', 'speech', '--purpose', 'performance')
        self.assertEqual(2, result.returncode)
        self.assertIn('没有满足证据覆盖', result.stdout)


if __name__ == '__main__':
    unittest.main()
