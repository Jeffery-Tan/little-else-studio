"""Temporary synthetic stores only; no provider, service or business data."""
from copy import deepcopy
import hashlib
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from studio.materials import accept_upload
from studio.models import PipelineError
from studio.production import (DraftIn, Production, _read_state_without_service, prepare_draft,
                               target_shots, attempt_records, video_sequence)
from studio.shot_identity import bind_rows, dependency_hashes
from studio.revisions import apply_cells
from studio.structured import validate_canonical_input
from test_shots import prepared, RUN, image_input


def revise(store, pipe, rows, *, adopt=True):
    attempt = pipe.start_attempt(RUN, 'writing')
    artifact = pipe.submit(RUN, 'writing', attempt.token, content='Synthetic revised script',
                           summary='Synthetic', structured={'segments':rows})
    if adopt:
        pipe.record_approval(RUN, 'writing', nonce=pipe.issue_nonce(RUN, 'writing'), decision='approved')
    return artifact


def success(store, draft, number):
    buffer = io.BytesIO(); Image.new('RGB', (16, 16), (number, 20, 30)).save(buffer, 'PNG')
    raw = buffer.getvalue(); name = f'original-{number}.png'; sha = hashlib.sha256(raw).hexdigest()
    accept_upload(store, RUN, name, raw)
    attempt = f'attempt-{number}'
    store.append_event(RUN, 'production_attempt', attempt_id=attempt, draft_id=draft['draft_id'],
                       draft_sha256=draft['draft_sha256'], media_kind='image', status='succeeded', output=None)
    store.append_event(RUN, 'production_output', attempt_id=attempt, output={'name':name,'sha256':sha})
    store.append_event(RUN, 'production_material_approval', name=name, sha256=sha, approved=True, actor='user')
    return attempt


def test_insert_delete_and_reorder_retain_identity_without_recycling_deleted_ids(tmp_path):
    store, pipe = prepared(tmp_path)
    first = deepcopy(store.latest_artifact(RUN, 'writing').structured['segments'])
    inserted = {**first[0], 'picture':'New inserted view'}; inserted.pop('shot_id')
    second = revise(store, pipe, [first[2], inserted, first[0]])
    assert [row['shot_id'] for row in second.structured['segments']] == ['shot-03','shot-04','shot-01']
    assert [(row['ordinal'],row['shot_id']) for row in target_shots(store,RUN)['shots']] == [(1,'shot-03'),(2,'shot-04'),(3,'shot-01')]
    third = revise(store, pipe, [*second.structured['segments'], {**inserted,'picture':'Another view'}])
    assert third.structured['segments'][-1]['shot_id'] == 'shot-05'


@pytest.mark.parametrize('claims', [('shot-01','shot-01'),('shot-99','shot-02'),(17,'shot-02')])
def test_duplicate_unknown_or_non_string_identity_is_rejected_before_new_version(tmp_path, claims):
    store, pipe = prepared(tmp_path)
    rows = deepcopy(store.latest_artifact(RUN, 'writing').structured['segments'])[:2]
    for row, identity in zip(rows, claims): row['shot_id'] = identity
    with pytest.raises(PipelineError): revise(store,pipe,rows)
    assert store.latest_artifact(RUN,'writing').version == 1


def test_photo_pages_keep_server_page_identity_separate_from_position():
    first = bind_rows([{'picture':'First'},{'picture':'Second'}], [], [], photo=True)
    second = bind_rows([first[1],{'picture':'New'},first[0]], first, [first], photo=True)
    assert [row['page_id'] for row in second] == ['page-02','page-03','page-01']
    with pytest.raises(ValueError): bind_rows([{'picture':'fake','page_id':'page-99'}],first,[first],photo=True)


def test_copy_cannot_steal_identity_claimed_by_later_original():
    first = bind_rows([{'picture':'First'}], [], [])
    copied = {'picture':'First'}
    second = bind_rows([copied,first[0]],first,[first])
    assert [row['shot_id'] for row in second] == ['shot-02','shot-01']


def test_identity_only_unknown_row_is_not_silently_dropped(tmp_path):
    store,pipe = prepared(tmp_path)
    rows = deepcopy(store.latest_artifact(RUN,'writing').structured['segments'])
    with pytest.raises(PipelineError): revise(store,pipe,[*rows,{'shot_id':'shot-99'}])
    assert store.latest_artifact(RUN,'writing').version == 1


def test_photo_images_alias_canonicalizes_to_one_page_list(tmp_path):
    store,pipe = prepared(tmp_path)
    store.append_event(RUN,'creative_context_selected',selection={'intent':'new','format':'photo'})
    rows = deepcopy(store.latest_artifact(RUN,'writing').structured['segments'])
    attempt = pipe.start_attempt(RUN,'writing')
    artifact = pipe.submit(RUN,'writing',attempt.token,content='',summary='Photo fixture',
                           structured={'images':rows})
    assert 'images' not in artifact.structured
    assert [row['page_id'] for row in artifact.structured['segments']] == ['page-01','page-02','page-03']
    assert target_shots(store,RUN)['shots'][1]['page_id'] == 'page-02'


def test_canonical_local_revision_keeps_identity_but_cannot_edit_it(tmp_path):
    store,pipe = prepared(tmp_path)
    base = store.latest_artifact(RUN,'writing')
    updated,_ = apply_cells(base,[{'row':1,'field':'line','value':'Changed line'}])
    validate_canonical_input('writing',updated)
    attempt = pipe.start_attempt(RUN,'writing')
    artifact = pipe.submit(RUN,'writing',attempt.token,content='',summary='Canonical fixture',structured=updated)
    assert [row['shot_id'] for row in artifact.structured['segments']] == ['shot-01','shot-02','shot-03']
    with pytest.raises(PipelineError): apply_cells(artifact,[{'row':1,'field':'shot_id','value':'shot-01'}])


def test_one_line_change_limits_reuse_and_new_output_never_inherits_approval(tmp_path):
    store, pipe = prepared(tmp_path)
    drafts = [image_input(store, pipe, f'old-{i}', shot_id=f'shot-{i:02d}') for i in range(1,4)]
    attempts = [success(store,draft,i) for i,draft in enumerate(drafts,1)]
    rows = deepcopy(store.latest_artifact(RUN,'writing').structured['segments'])
    rows[1]['line'] = 'Changed spoken sentence'
    artifact = revise(store,pipe,rows)
    projection = _read_state_without_service(store,pipe,RUN)
    candidates = {row['shot_id']:row for row in projection['reuse_candidates']}
    assert candidates['shot-01']['eligible'] and candidates['shot-03']['eligible']
    assert candidates['shot-02']['changed_dependencies'] == ['words']
    assert not candidates['shot-02']['eligible']
    old_events = deepcopy(store.read_events(RUN))
    payload = DraftIn(request_id='reuse-one',kind='image',prompt=drafts[0]['shot_prompt'],
        source_version=artifact.version,source_sha256=artifact.sha256,model='codex-image',
        shot_id='shot-01',reuse_attempt_id=attempts[0])
    result = prepare_draft(store,pipe,RUN,payload)
    assert result['source_sha256'] == artifact.sha256
    outputs = [row for row in attempt_records(store,RUN).values() if row['draft_id'] == result['draft_id']]
    assert len(outputs)==1 and outputs[0]['provider']=='local_reuse'
    old_output=attempt_records(store,RUN)[attempts[0]]['output']; new_output=outputs[0]['output']
    assert old_output['sha256']==new_output['sha256'] and old_output['name']!=new_output['name']
    after=_read_state_without_service(store,pipe,RUN)
    assert not next(row for row in after['materials'] if row['name']==new_output['name'])['approved']
    assert after['shots']['image']['shots'][0]['stage']=='completed'
    new_draft=next(row for row in after['drafts'] if row['draft_id']==result['draft_id'])
    assert not new_draft['can_generate'] and '重新核对费用' in new_draft['reason']
    service=SimpleNamespace(store=store,get_draft=lambda *args:result,approvals=lambda _: {})
    with pytest.raises(PipelineError,match='generation_reuse_only'):
        Production.validate_generation_set(service,RUN,[(result['draft_id'],result['draft_sha256'])])
    assert store.read_events(RUN)[:len(old_events)]==old_events
    before_retry=len(store.read_events(RUN));prepare_draft(store,pipe,RUN,payload)
    assert len(store.read_events(RUN))==before_retry


def test_reorder_and_insert_reuse_only_existing_unchanged_shots(tmp_path):
    store,pipe = prepared(tmp_path)
    drafts = [image_input(store,pipe,f'old-{i}',shot_id=f'shot-{i:02d}') for i in range(1,4)]
    for i,draft in enumerate(drafts,1): success(store,draft,i)
    rows = deepcopy(store.latest_artifact(RUN,'writing').structured['segments'])
    new = {**rows[0],'picture':'A new view'}; new.pop('shot_id')
    revise(store,pipe,[rows[2],new,rows[0]])
    candidates = {row['shot_id']:row for row in _read_state_without_service(store,pipe,RUN)['reuse_candidates']}
    assert candidates['shot-01']['eligible'] and candidates['shot-03']['eligible']
    assert candidates['shot-02']['changed_dependencies'] == ['shot_removed']
    assert not candidates['shot-02']['eligible'] and 'shot-04' not in candidates


def test_video_bytes_rebind_to_reused_current_frame_without_inheriting_image_review(tmp_path):
    store,pipe=prepared(tmp_path)
    image=image_input(store,pipe,'image-old',shot_id='shot-01')
    image_attempt=success(store,image,1)
    frame=attempt_records(store,RUN)[image_attempt]['output']
    source=store.latest_artifact(RUN,'writing')
    old_video=prepare_draft(store,pipe,RUN,DraftIn(request_id='video-old',kind='video',
        prompt='Synthetic video movement',source_version=source.version,source_sha256=source.sha256,
        model='wan3.0-video',duration=3,resolution='720P',shot_id='shot-01',reference=frame))
    raw=b'Explicit synthetic fixture only; never decoded or generated.'
    video_name='old-fixture.mp4';video_sha=hashlib.sha256(raw).hexdigest()
    accept_upload(store,RUN,video_name,raw)
    store.append_event(RUN,'production_attempt',attempt_id='old-video-result',draft_id=old_video['draft_id'],
        draft_sha256=old_video['draft_sha256'],media_kind='video',status='succeeded',output=None)
    store.append_event(RUN,'production_output',attempt_id='old-video-result',output={'name':video_name,'sha256':video_sha})
    new_source=revise(store,pipe,deepcopy(source.structured['segments']))
    new_image=prepare_draft(store,pipe,RUN,DraftIn(request_id='image-reused',kind='image',
        prompt=image['shot_prompt'],source_version=new_source.version,source_sha256=new_source.sha256,
        model='codex-image',shot_id='shot-01',reuse_attempt_id=image_attempt))
    new_frame=next(row['output'] for row in attempt_records(store,RUN).values() if row['draft_id']==new_image['draft_id'])
    prepare_draft(store,pipe,RUN,DraftIn(request_id='video-reused',kind='video',
        prompt=old_video['shot_prompt'],source_version=new_source.version,source_sha256=new_source.sha256,
        model='wan3.0-video',duration=3,resolution='720P',shot_id='shot-01',
        reference={key:new_frame[key] for key in ('name','sha256')},reuse_attempt_id='old-video-result'))
    state=_read_state_without_service(store,pipe,RUN)
    assert state['video_sequence'][0]['video_draft_id']=='video-reused'
    assert not state['video_sequence'][0]['approved']
    assert state['shots']['video']['shots'][0]['stage']=='completed'
    new_attempt=next(row for row in attempt_records(store,RUN).values() if row['draft_id']=='video-reused')
    assert new_attempt['provider']=='local_reuse' and new_attempt['output']['sha256']==video_sha
    assert new_attempt['output']['name']!=video_name


def test_same_identity_cannot_borrow_old_media_after_content_is_swapped(tmp_path):
    store,pipe=prepared(tmp_path); draft=image_input(store,pipe,'old',shot_id='shot-01')
    attempt=success(store,draft,1)
    rows=deepcopy(store.latest_artifact(RUN,'writing').structured['segments'])
    rows[0]['picture'],rows[1]['picture']=rows[1]['picture'],rows[0]['picture']
    artifact=revise(store,pipe,rows)
    with pytest.raises(ValueError,match='action'):
        prepare_draft(store,pipe,RUN,DraftIn(request_id='swapped',kind='image',prompt=draft['shot_prompt'],
            source_version=artifact.version,source_sha256=artifact.sha256,model='codex-image',
            shot_id='shot-01',reuse_attempt_id=attempt))


def test_unknown_provider_result_never_becomes_reuse_or_fallback_generation(tmp_path):
    store,pipe=prepared(tmp_path);draft=image_input(store,pipe,'old',shot_id='shot-01')
    attempt=success(store,draft,1)
    store.append_event(RUN,'production_status',attempt_id=attempt,update={'status':'unknown'})
    artifact=revise(store,pipe,deepcopy(store.latest_artifact(RUN,'writing').structured['segments']))
    before=len(store.read_events(RUN))
    with pytest.raises(ValueError):
        prepare_draft(store,pipe,RUN,DraftIn(request_id='blocked',kind='image',prompt=draft['shot_prompt'],
            source_version=artifact.version,source_sha256=artifact.sha256,model='codex-image',
            shot_id='shot-01',reuse_attempt_id=attempt))
    assert len(store.read_events(RUN))==before


def test_changed_output_bytes_are_never_reused_or_overwritten(tmp_path):
    store,pipe=prepared(tmp_path);draft=image_input(store,pipe,'old',shot_id='shot-01')
    attempt=success(store,draft,1)
    artifact=revise(store,pipe,deepcopy(store.latest_artifact(RUN,'writing').structured['segments']))
    output=attempt_records(store,RUN)[attempt]['output']
    path=store.run_dir(RUN)/'materials'/output['name']
    changed=io.BytesIO();Image.new('RGB',(16,16),'red').save(changed,'PNG')
    path.write_bytes(changed.getvalue())
    state=_read_state_without_service(store,pipe,RUN)
    candidate=next(row for row in state['reuse_candidates'] if row['source_attempt_id']==attempt)
    assert not candidate['eligible'] and '字节' in candidate['reason']
    before=len(store.read_events(RUN))
    with pytest.raises(ValueError):
        prepare_draft(store,pipe,RUN,DraftIn(request_id='blocked-changed-bytes',kind='image',
            prompt=draft['shot_prompt'],source_version=artifact.version,source_sha256=artifact.sha256,
            model='codex-image',shot_id='shot-01',reuse_attempt_id=attempt))
    assert len(store.read_events(RUN))==before and path.read_bytes()==changed.getvalue()


def test_timecode_length_change_blocks_reuse_but_same_length_placement_does_not(tmp_path):
    store,pipe=prepared(tmp_path)
    rows=deepcopy(store.latest_artifact(RUN,'writing').structured['segments'])
    rows[0]['tc']='0:00–0:02'
    revise(store,pipe,rows)
    draft=image_input(store,pipe,'old',shot_id='shot-01')
    attempt=success(store,draft,1)
    rows[0]['tc']='0:12–0:14'
    revise(store,pipe,rows)
    candidate=next(row for row in _read_state_without_service(store,pipe,RUN)['reuse_candidates']
                   if row['source_attempt_id']==attempt)
    assert candidate['eligible']
    rows[0]['tc']='0:12–0:16'
    artifact=revise(store,pipe,rows)
    candidate=next(row for row in _read_state_without_service(store,pipe,RUN)['reuse_candidates']
                   if row['source_attempt_id']==attempt)
    assert not candidate['eligible'] and candidate['changed_dependencies']==['timing']
    with pytest.raises(ValueError,match='timing'):
        prepare_draft(store,pipe,RUN,DraftIn(request_id='blocked-length',kind='image',
            prompt=draft['shot_prompt'],source_version=artifact.version,source_sha256=artifact.sha256,
            model='codex-image',shot_id='shot-01',reuse_attempt_id=attempt))


def test_character_changes_are_scoped_but_product_changes_invalidate_related_content():
    kwargs=dict(segment={'picture':'Action','character_ids':'lead','scene_id':'room'},
        design={'style':'Natural','characters':[{'id':'lead','appearance':'A'},{'id':'other','appearance':'B'}],
                'scenes':[{'id':'room','location':'US'}]},product={'color':'yellow'},source_materials={},
        prompt='exact',references=[],kind='image',model='codex-image',duration=None,resolution=None)
    original=dependency_hashes(**kwargs)
    kwargs['design']['characters'][1]['appearance']='C'
    assert dependency_hashes(**kwargs)==original
    kwargs['design']['characters'][0]['appearance']='D'
    assert [key for key,value in dependency_hashes(**kwargs).items() if original[key]!=value]==['characters']
    kwargs['product']['color']='blue'
    assert dependency_hashes(**kwargs)['product']!=original['product']


def test_scene_changes_invalidate_only_the_shots_that_use_that_scene():
    kwargs=dict(segment={'picture':'Action','character_ids':'','scene_id':'room'},
        design={'style':'Natural','characters':[],
                'scenes':[{'id':'room','location':'US'},{'id':'street','location':'US'}]},
        product={},source_materials={},prompt='exact',references=[],kind='image',
        model='codex-image',duration=None,resolution=None)
    original=dependency_hashes(**kwargs)
    kwargs['design']['scenes'][1]['location']='A different outdoor setting'
    assert dependency_hashes(**kwargs)==original
    kwargs['design']['scenes'][0]['location']='A different indoor setting'
    assert [key for key,value in dependency_hashes(**kwargs).items() if original[key]!=value]==['scene']


def test_unreadable_timecodes_require_exact_text_before_reuse():
    kwargs=dict(segment={'picture':'Action','tc':'short opening','duration':''},design={},
        product={},source_materials={},prompt='exact',references=[],kind='video',
        model='wan3.0-video',duration=3,resolution='720P')
    original=dependency_hashes(**kwargs)
    kwargs['segment']['tc']='long opening'
    assert [key for key,value in dependency_hashes(**kwargs).items() if original[key]!=value]==['timing']


def test_photo_page_position_change_does_not_change_media_dependencies():
    kwargs=dict(segment={'shot_id':'shot-01','page_id':'page-01','picture':'Action',
                         'tc':'Page 1','duration':''},design={},product={},source_materials={},
        prompt='exact',references=[],kind='image',model='codex-image',duration=None,resolution=None)
    original=dependency_hashes(**kwargs)
    kwargs['segment']['tc']='Page 3'
    assert dependency_hashes(**kwargs)==original


def test_video_sequence_uses_script_order_instead_of_numeric_identity():
    drafts=[{'draft_id':identity,'shot_id':identity,'kind':'image','source_ready':True,'is_current':True,'attempts':[]}
            for identity in ['shot-01','shot-03','shot-04']]
    result=video_sequence({'drafts':drafts,'materials':[],
        'targets':{'image':{'shots':[{'shot_id':identity,'ordinal':index} for index,identity in enumerate(['shot-03','shot-04','shot-01'],1)]}}})
    assert [row['shot_id'] for row in result]==['shot-03','shot-04','shot-01']
