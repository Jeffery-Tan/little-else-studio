"""Read-only, integrity-checked planning handoff. No model or persistent writes."""
import io
import json
import zipfile

from .materials import list_materials
from .models import PipelineError
from .vocabulary import stage_label


def delivery_snapshot(store, pipeline, packages, run_id):
    with store.run_lock(run_id):
        state = pipeline.state(run_id).to_dict()
        documents = []
        for stage in state['stages']:
            artifact = stage['latest']
            if not artifact:
                continue
            current = (stage['integrity_ok'] and not stage['stale']
                       and artifact.get('digest_algorithm') == 'artifact-v2')
            approved = bool(current and stage['status'] == 'approved')
            documents.append({'stage': stage['stage'], 'label': stage_label(stage['stage']),
                              'version': artifact['version'], 'sha256': artifact['sha256'],
                              'integrity_ok': stage['integrity_ok'], 'current': current,
                              'adopted': approved, 'status': '已采用' if approved else
                              '已整理' if current and stage['status'] == 'done' else
                              '待审阅' if current else '历史或需更新',
                              'summary': artifact.get('summary', ''),
                              'limitations': artifact.get('limitations', [])})
        return {'run': state['run'], 'ready': bool(documents) and all(d['current'] and d['status'] in {'已整理', '已采用'} for d in documents) and len(documents) == len(state['stages']),
                'documents': documents, 'materials': list_materials(store, run_id),
                'packages': packages.packages(run_id),
                'missing': [stage_label(s['stage']) for s in state['stages']
                            if not s['latest'] or s['stale'] or not s['integrity_ok']
                            or s['latest'].get('digest_algorithm') != 'artifact-v2'
                            or s['status'] not in {'done', 'approved'}],
                'notice': '这是前期策划资料，不包含新生成的图片或视频。历史及待审阅版本会明确标注，导出不会改变采用状态。'}


def delivery_zip(store, pipeline, packages, run_id):
    with store.run_lock(run_id):
        snapshot = delivery_snapshot(store, pipeline, packages, run_id)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('delivery-manifest.json', json.dumps(snapshot, ensure_ascii=False, indent=2))
            notes = [f'# {snapshot["run"]["title"]}', '', snapshot['notice'], '',
                     '## 项目要求', snapshot['run']['brief'], '', '## 版本清单']
            for doc in snapshot['documents']:
                stage, version = doc['stage'], doc['version']
                if not doc['integrity_ok'] or not store.verify_artifact(run_id, stage, version):
                    raise PipelineError('delivery_integrity', '有文件与保存记录不一致，暂不能导出。')
                text = store.read_artifact_text(run_id, stage, version)
                # Recheck after reading, so a changed file is never knowingly packaged.
                if not store.verify_artifact(run_id, stage, version):
                    raise PipelineError('delivery_integrity', '导出时文件发生变化，请重新检查。')
                filename = f'{stage}-v{version}.md'
                archive.writestr(filename, '\ufeff' + text)
                notes.append(f'- {doc["label"]} v{version} · {doc["status"]} · {filename}')
            notes += ['', '## 仍需完成', *(f'- {label}' for label in snapshot['missing']),
                      '', '## 素材', '素材仅列入清单，不复制媒体文件；文件存在不代表已分析。']
            archive.writestr('README.md', '\ufeff' + '\n'.join(notes))
        return buffer.getvalue()
