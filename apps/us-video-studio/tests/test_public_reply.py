"""Creator replies follow the persisted gate, never a model-invented button."""
from studio.public_reply import creator_reply
from studio.pipeline import Pipeline
from studio.store import Store


def test_incomplete_direction_discards_false_adoption_and_internal_ids(tmp_path):
    store = Store(tmp_path)
    store.create_run('reply-test', 'Fixture', 'Make a video')
    pipeline = Pipeline(store)
    intake = pipeline.start_attempt('reply-test', 'intake')
    pipeline.submit('reply-test', 'intake', intake.token, content='Product facts', summary='Facts')
    direction = pipeline.start_attempt('reply-test', 'direction')
    pipeline.submit('reply-test', 'direction', direction.token,
                    content='Concept A and B', summary='Two directions')
    text = creator_reply('direction-v1 已好。现在采用 A。',
                         {'goal': 'direction', 'status': 'incomplete'},
                         pipeline.state('reply-test'))
    assert '创意方向已保存' in text
    assert '现在采用' not in text
    assert 'direction-v1' not in text


def test_complete_reply_replaces_internal_artifact_labels_unless_requested(tmp_path):
    store = Store(tmp_path)
    store.create_run('reply-test', 'Fixture', 'Make a video')
    state = Pipeline(store).state('reply-test')
    original = '已保存 intake-v1 和 artifact:direction-v2#' + 'a' * 64
    public = creator_reply(original, {'goal': 'follow_up', 'status': 'complete'}, state)
    assert public == '已保存 需求记录 和 创意方向'
    assert creator_reply(original, {'goal': 'follow_up', 'status': 'complete'}, state,
                         technical_requested=True) == original
