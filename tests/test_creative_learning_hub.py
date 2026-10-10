import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('creative_hub', SCRIPTS / 'creative_learning_hub.py')
hub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hub)
sys.path.pop(0)


class CreativeLearningHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.learning = self.repo / 'tracks/us/creative/learning'
        self.learning.mkdir(parents=True)
        for name in ('README.md', 'batch-001.md', 'strategy-guide.md', 'execution.md'):
            (self.learning / name).write_text('Fixture document', encoding='utf-8-sig')
        self.lib = self.learning / 'library.json'
        self.lib.write_text(json.dumps({'schema_version': 1, 'market': 'US', 'cases': [
            {'id': 'known', 'title': 'KNOWN_CALIBRATION', 'partition': 'calibration',
             'eligible_for': ['message'], 'evidence': [{'id': 'observed'}]},
            {'id': 'holdout', 'title': 'DO_NOT_LEAK_HOLDOUT', 'partition': 'holdout',
             'eligible_for': ['message'], 'evidence': [{'id': 'answer'}]}],
            'evaluation_notes': 'DO_NOT_LEAK_ROOT_ANSWER',
            'methods': [{'id': 'fixture-method', 'supports': ['known/observed']},
                        {'id': 'secret-method', 'decision': 'DO_NOT_LEAK_METHOD_ANSWER',
                         'supports': ['known/observed'], 'counterexamples': ['holdout/answer']}]}), encoding='utf-8')
        core = self.repo / 'tracks/us/creative/lunch/core-videos'
        core.mkdir(parents=True)
        for name in ('reference-original.mp4', 'reference-oumomo.mp4', 'reference-remake-v1-original.mp4'):
            (core / name).write_bytes(b'fixture-copy-test-only-not-video-validation')
        self.photo, self.comparison, self.brief = [self.root / n for n in ('photo', 'comparison', 'brief')]
        for folder, names in (
            (self.photo, ('review.html', 'photo-music.mp4', 'layout.jpg', 'verification.json')),
            (self.comparison, ('comparison.json', 'comparison.md')),
            (self.brief, ('review.md', 'prompt.txt', 'manifest.json'))):
            folder.mkdir()
            for name in names:
                (folder / name).write_text('Fixture artifact, not actual media')
        for name, value in (('ROOT', self.repo), ('DEFAULT', self.lib)):
            p = patch.object(hub, name, value)
            p.start()
            self.addCleanup(p.stop)
        # This suite tests hub rendering/copy boundaries; library validation has
        # its own suite. The fixture deliberately contains no real case media.
        p = patch.object(hub, 'validate', return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def build(self, copy_run=None):
        return hub.build(self.root / 'hub', self.photo, self.comparison, self.brief, copy_run)

    def rendered_case_html(self, page):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is needed to exercise the exported browser JavaScript')
        library = page.split('<script type="application/json" id="library">')[1].split('</script>')[0]
        script = page.rsplit('<script>', 1)[1].split('</script>')[0]
        harness = '''
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
const elements={library:{textContent:input.library},search:{value:''},purpose:{value:'all'}};
global.document={getElementById:id=>{const e=elements[id]??={};e.addEventListener=()=>{};return e;},querySelectorAll:()=>[]};
global.history={replaceState:()=>{}};global.window={scrollTo:()=>{}};global.location={hash:''};
eval(input.script);
process.stdout.write(elements['case-list'].innerHTML);
'''
        result = subprocess.run([node, '-e', harness], input=json.dumps({'library': library, 'script': script}),
                                text=True, encoding='utf-8', capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def test_appended_feedback_is_visible_scoped_and_escaped_in_actual_case_markup(self):
        doc = hub.read(self.lib)
        case = doc['cases'][0]
        case.update(tags=['pace'], platform='fixture', limits=['Synthetic fixture only'],
                    user_feedback={'scope': '旧版节奏认可'}, market_effect=None)
        case['evidence'][0].update(kind='user_feedback', observation='观察与原话分开')
        case['feedback_history'] = [{'id': 'feedback-fixture', 'run': 'synthetic-run',
            'source_event': 'synthetic-message-1', 'artifact_version': 'v2-only',
            'recorded_at': '2026-09-18', 'library_version': 2, 'source': 'fixture://source',
            'quote': '只认可节奏，先暂停 <script>alert("x")</script>', 'scope': 'pace',
            'action': 'stop', 'observation': '只覆盖这一版', 'hypothesis': None,
            'improvement_effect': None}]
        self.lib.write_text(json.dumps(doc), encoding='utf-8')
        page = self.build().read_text(encoding='utf-8')
        markup = self.rendered_case_html(page)
        for text in ('历史认可摘要（原字段）', '旧版节奏认可', '追加反馈历史',
                     '只认可节奏，先暂停', '范围：pace', '用户实际要求：stop', 'v2-only',
                     '2026-09-18', 'fixture://source', '只覆盖这一版', 'null（尚未确定）',
                     'null（未观察到改善）'):
            self.assertIn(text, markup)
        self.assertIn('&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;', markup)
        self.assertNotIn('<script>', markup)
        self.assertNotIn('DO_NOT_LEAK_', markup)

    def pack_fixture(self):
        out = self.root / 'photo-copy'
        out.mkdir()
        for name in ('index.html', 'caption.txt', 'prompts.md'):
            (out / name).write_text('Test compiled pack')
        image = out / '01-cover.png'
        image.write_bytes(b'copied-byte-fixture-not-decode-test')
        (out / 'pack.json').write_text(json.dumps({'market': 'US', 'music': 'publish-time-on-TikTok',
            'pages': [{'id': 'cover', 'status': 'bound', 'image': {'delivery_path': image.name,
                       'sha256': hashlib.sha256(image.read_bytes()).hexdigest()}},
                      {'id': 'next', 'status': 'planned'}]}))
        return out

    def test_existing_four_argument_build_remains_compatible_without_fake_pack(self):
        page = hub.build(self.root / 'hub', self.photo, self.comparison, self.brief).read_text(encoding='utf-8')
        self.assertIn('尚未编译图文策划包', page)
        self.assertNotIn('href="../photo-copy/index.html"', page)
        self.assertIn('带货图片＋文案', page)
        self.assertIn('音乐直接在 TikTok 发布时选择', page)
        photo_section = page.split('<section id="photo"')[1].split('</section>')[0]
        self.assertNotIn('<video', photo_section)
        self.assertIn('历史记录', photo_section)
        self.assertNotIn('DO_NOT_LEAK_HOLDOUT', page)
        self.assertNotIn('DO_NOT_LEAK_ROOT_ANSWER', page)
        self.assertNotIn('DO_NOT_LEAK_METHOD_ANSWER', page)
        for exported in (self.root / 'hub').rglob('*'):
            if exported.is_file():
                self.assertNotIn(b'DO_NOT_LEAK_', exported.read_bytes())

    def test_actual_pack_copied_completely_and_statuses_not_approved(self):
        source = self.pack_fixture()
        extra = source / 'nested'
        extra.mkdir()
        (extra / 'notes.txt').write_text('Supplemental actual pack artifact')
        page = self.build(source).read_text(encoding='utf-8')
        dest = self.root / 'hub/photo-copy'
        self.assertEqual((source / '01-cover.png').read_bytes(), (dest / '01-cover.png').read_bytes())
        self.assertTrue((dest / 'nested/notes.txt').is_file())
        self.assertIn('2页图片与文案 · 1张图片已绑定', page)
        self.assertIn('已绑定不等于已审核', page)
        self.assertIn('../photo-copy/prompts.md', page)

    def test_missing_pack_artifact_blocks_before_build(self):
        source = self.pack_fixture()
        (source / 'caption.txt').unlink()
        with self.assertRaisesRegex(ValueError, 'Missing photo/copy artifact'):
            self.build(source)
        self.assertFalse((self.root / 'hub').exists())

    def test_output_cannot_recursively_copy_its_own_source(self):
        source = self.pack_fixture()
        with self.assertRaisesRegex(ValueError, 'cannot be inside'):
            hub.build(source / 'hub', self.photo, self.comparison, self.brief, source)
        self.assertFalse((source / 'hub').exists())

    def test_changed_delivered_image_blocks_copy(self):
        source = self.pack_fixture()
        (source / '01-cover.png').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'image hash changed'):
            self.build(source)

    def test_pack_image_cannot_escape_delivery_directory(self):
        source = self.pack_fixture()
        doc = hub.read(source / 'pack.json')
        doc['pages'][0]['image']['delivery_path'] = '../brief/prompt.txt'
        (source / 'pack.json').write_text(json.dumps(doc))
        with self.assertRaisesRegex(ValueError, 'inside its delivery pack'):
            self.build(source)

    def test_only_existing_research_and_skills_get_links(self):
        (self.learning / 'commerce-methods-research.md').write_text('READ_THIS <script>bad()</script>', encoding='utf-8-sig')
        source = self.repo / '.agents/skills/us-commerce-strategy/SKILL.md'
        source.parent.mkdir(parents=True)
        source.write_text('Actual fixture skill body', encoding='utf-8-sig')
        page = self.build().read_text(encoding='utf-8')
        self.assertIn('href="commerce-methods-research.md"', page)
        self.assertNotIn('href="photo-copy-methods-research.md"', page)
        self.assertIn('href="us-commerce-strategy-SKILL.md"', page)
        self.assertNotIn('href="us-creator-video-SKILL.md"', page)
        self.assertIn('&lt;script&gt;bad()', page)
        self.assertNotIn('<script>bad()', page)
        self.assertEqual(source.read_bytes(), (self.root / 'hub/documents/us-commerce-strategy-SKILL.md').read_bytes())

    def test_research_records_are_counted_without_inflating_case_counts(self):
        for name in ('commerce-methods-sources.json', 'photo-copy-methods-sources.json'):
            (self.learning / name).write_text(json.dumps({'sources': [{'url': 'same-source'}] * 8}))
        (self.learning / 'tiktok-field-study.md').write_text('Two PDP observations, not video viewing')
        page = self.build().read_text(encoding='utf-8')
        self.assertIn('<strong>16</strong><span>研究来源记录', page)
        self.assertIn('<strong>1</strong><span>历史校准案例', page)
        self.assertIn('2个PDP静态研究', page)
        self.assertIn('不是完整观看带货视频', page)
        self.assertIn('不等于相同数量的独立来源', page)


if __name__ == '__main__':
    unittest.main()
