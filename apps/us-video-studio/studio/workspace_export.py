"""Local download of exact selected works; never publishes or adopts them."""
import io
import json
import zipfile

from fastapi import HTTPException, Response
from pydantic import Field

from .http_workspace import Input
from .models import PipelineError
from .workspace_objects import read_object, digest, annotations


class ExportIn(Input):
    request_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    targets: list[dict] = Field(min_length=1, max_length=48)


def build_archive(store, pipeline, run_id, targets, *, frozen_manifest=None):
    from .result_inspection import exact_media_bytes, latest_report
    identities = [digest(target) for target in targets]
    if len(set(identities)) != len(identities):
        raise ValueError('导出清单包含重复对象。')
    files, manifest, total = [], [], 0
    for index, target in enumerate(targets, 1):
        item = read_object(store, pipeline, run_id, target)
        if target['kind'] == 'media':
            item, raw = exact_media_bytes(store, pipeline, run_id, target)
            name = item['data']['material']['name']
            inspection = latest_report(store, run_id, target)
        elif target['kind'] in {'artifact', 'writing'}:
            raw = item['text'].encode('utf-8-sig')
            name = f"{target['stage']}-v{target['version']}.md"
            inspection = None
        else:
            raise ValueError('只能导出准确文字作品或生成结果。')
        total += len(raw)
        if total > 256 * 1024 * 1024:
            raise ValueError('本次导出超过 256 MiB，请分批选择。')
        name = f'{index:02d}-{name}'
        files.append((name, raw))
        import hashlib
        manifest.append({'file': name, 'file_sha256': hashlib.sha256(raw).hexdigest(),
                         'target': target, 'status': item['status'], 'inspection': inspection})
    selected = set(identities)
    notes = [a for a in annotations(store, run_id)['items'] if digest(a['target']) in selected]
    result = {'schema_version': 1, 'run_id': run_id, 'objects': manifest, 'annotations': notes,
              'notice': '导出不表示用户批准、正式采用或市场效果验证。'}
    if frozen_manifest is not None:
        prior_files = [(row['file'], row['file_sha256'], row['target']) for row in frozen_manifest['objects']]
        actual_files = [(row['file'], row['file_sha256'], row['target']) for row in manifest]
        if prior_files != actual_files:
            raise ValueError('原导出文件已变化；没有替换这次请求绑定的文件。')
        result = frozen_manifest
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files:
            archive.writestr(zipfile.ZipInfo(name), raw, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr(zipfile.ZipInfo('manifest.json'),
                         json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8'),
                         compress_type=zipfile.ZIP_DEFLATED)
    return output.getvalue(), result


def install_routes(app, store, pipeline, require_run):
    @app.post('/api/runs/{run_id}/objects/export')
    def export(run_id: str, payload: ExportIn):
        require_run(run_id)
        try:
            with store.run_lock(run_id):
                identity = digest(payload.targets)
                prior = next((e for e in store.read_events(run_id) if e['kind'] == 'workspace_export_created'
                              and e['request_id'] == payload.request_id), None)
                if prior and prior['targets_sha256'] != identity:
                    raise ValueError('相同导出请求已绑定另一组准确对象。')
                raw, manifest = build_archive(store, pipeline, run_id, payload.targets,
                                              frozen_manifest=prior['manifest'] if prior else None)
                if not prior:
                    store.append_event(run_id, 'workspace_export_created', request_id=payload.request_id,
                                       targets_sha256=identity, manifest=manifest)
                return Response(raw, media_type='application/zip', headers={
                    'Content-Disposition': f'attachment; filename="{run_id}-works.zip"',
                    'Cache-Control': 'no-store'})
        except (ValueError, PipelineError, FileNotFoundError) as exc:
            raise HTTPException(409, str(exc)) from exc
