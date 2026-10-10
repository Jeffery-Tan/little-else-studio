import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('creative_session', SCRIPTS / 'creative_session.py')
session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(session)
sys.path.pop(0)


class CreativeSessionTests(unittest.TestCase):
    def test_selected_direction_is_preserved(self):
        result, evidence = session.context('US', 'continue', 'video', ['reason'])
        self.assertEqual(result['selected_direction'], 'preserve_exact_source_version')
        self.assertEqual([s['name'] for s in result['skills']],
                         ['us-commerce-strategy', 'us-creator-video'])
        self.assertFalse(result['execution_authorized'])
        self.assertIsNone(result['user_result'])
        self.assertIn('用户', evidence)

    def test_photo_does_not_select_video(self):
        result, _ = session.context('US', 'new', 'photo', ['packing'])
        self.assertEqual(result['skills'][-1]['name'], 'us-photo-copy')

    def test_noncreation_and_sea_never_read_us_library(self):
        with patch.object(session, 'read', side_effect=AssertionError('US library accessed')):
            for market, intent in [('SEA', 'new'), ('US', 'edit'), ('US', 'status'),
                                   ('US', 'pause'), ('US', 'feedback')]:
                result, evidence = session.context(market, intent, 'none')
                self.assertEqual(result['skills'], [])
                self.assertEqual(evidence, '')
                self.assertFalse(result['execution_authorized'])

    def test_unresolved_creation_format_rejected(self):
        with self.assertRaises(ValueError):
            session.context('US', 'new', 'none')

    def test_hook_does_not_promote_user_or_external_text(self):
        result = session.hook_output({'hook_event_name': 'SessionStart',
                                     'prompt': 'UNTRUSTED_DO_GENERATE',
                                     'transcript_path': 'DO_NOT_READ'})
        self.assertNotIn('UNTRUSTED', json.dumps(result))
        self.assertNotIn('DO_NOT_READ', json.dumps(result))
        self.assertIn('remains paused', result['hookSpecificOutput']['additionalContext'])

    def test_stop_and_interrupt_cannot_request_continuation(self):
        for name in ('Stop', 'Interrupt'):
            self.assertEqual(session.hook_output({'hook_event_name': name}), {})
        with self.assertRaises(ValueError):
            session.hook_output({'hook_event_name': 'Unknown'})


if __name__ == '__main__':
    unittest.main()
