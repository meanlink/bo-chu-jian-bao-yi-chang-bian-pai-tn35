"""Handover package export/import between primary and standby stations.

A package is a deterministic ZIP archive:

    manifest.json
    datasets/<origin_id>.json
    jobs/<origin_id>.json
    reports/<origin_id>.json
    deliveries/<origin_id>.meta.json
    deliveries/<origin_id>.csv      # original delivered bytes
    deliveries/<origin_id>.json     # original JSON sidecar bytes

The receiver verifies every listed file before importing anything. Imported
rows stay hidden from the business lists until the whole package is accepted.
Each created row is checkpointed in ``handover_mappings`` so a retried or
resumed import never creates a second copy.
"""

import hashlib
import io
import json
import math
import os
import posixpath
import secrets
import time
import zipfile
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from src.config.settings import ARTIFACT_DIR, SITE_NAME
from src.models import Dataset, Delivery, HandoverImport, HandoverMapping, Job, Report
from src.services.audit import record
from src.services.business import metadata
from src.utils.serialization import checksum

FORMAT = 'bulletin-handover'
VERSION = 1
MAX_PACKAGE_BYTES = 64 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 5000
CLAIM_SECONDS = 90
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)
KIND_ORDER = ('datasets', 'jobs', 'reports', 'deliveries')
STATUS_LABELS = {
    'pending_verification': '待校验',
    'rejected': '已拒绝',
    'verified': '校验通过',
    'importing': '接收中',
    'received': '已接收',
    'failed': '接收中断',
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def visible_condition(model):
    """Business rows created by an unfinished handover stay invisible."""
    received = select(HandoverImport.id).where(HandoverImport.status == 'received')
    return or_(model.handover_import_id.is_(None), model.handover_import_id.in_(received))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')


def _safe_name(value, limit=160, fallback='未命名'):
    text = str(value or '').strip() or fallback
    return text[:limit]


def _package_dir() -> str:
    path = os.path.join(ARTIFACT_DIR, 'handover')
    os.makedirs(path, exist_ok=True)
    return path


def _safe_entry_name(name: str) -> bool:
    if not name or name.startswith('/') or '\\' in name:
        return False
    parts = posixpath.normpath(name).split('/')
    return not any(part in ('..', '') for part in parts)


def _delivery_filename(import_id: int, origin_id: str, original: str) -> str:
    base = posixpath.basename(str(original or 'delivery.csv'))
    base = ''.join(ch for ch in base if ch not in '\\/:*?"<>|').strip() or 'delivery.csv'
    prefix = f'handover-{import_id}-{origin_id}-'
    name = prefix + base
    if len(name) > 160:
        name = name[:160]
    return name


def _check(key: str, label: str, ok: bool, detail: str, status: str | None = None) -> dict:
    return {'key': key, 'label': label, 'status': status or ('passed' if ok else 'failed'),
            'detail': detail}


def _read_items(zf: zipfile.ZipFile, manifest: dict) -> tuple[dict[str, bytes], list[str]]:
    """Read every manifest item from the archive and detect extra entries."""
    items = manifest.get('items')
    if not isinstance(items, list) or not items:
        return {}, ['交接清单没有文件条目']
    problems, blobs = [], {}
    for item in items:
        path = item.get('path') if isinstance(item, dict) else None
        if not isinstance(path, str) or not _safe_entry_name(path):
            problems.append(f'非法文件路径：{path}')
            continue
        if path in blobs:
            problems.append(f'交接清单存在重复路径：{path}')
            continue
        try:
            info = zf.getinfo(path)
            if info.file_size > MAX_UNCOMPRESSED_BYTES:
                problems.append(f'文件过大：{path}')
                continue
            data = zf.read(path)
        except KeyError:
            problems.append(f'缺少交接文件：{path}')
            continue
        except Exception:
            problems.append(f'交接文件不可读：{path}')
            continue
        if len(data) != item.get('size'):
            problems.append(f'文件大小与清单不一致：{path}')
            continue
        if _sha256(data) != item.get('sha256'):
            problems.append(f'文件内容与清单不一致：{path}')
            continue
        blobs[path] = data
    listed = {item.get('path') for item in items if isinstance(item, dict)}
    listed.add('manifest.json')
    for name in zf.namelist():
        if name.endswith('/'):
            continue
        if name not in listed:
            problems.append(f'归档中存在清单未列出的文件：{name}')
    return blobs, problems


def _validate_records(records) -> str | None:
    if not isinstance(records, list) or not records:
        return '原始日记没有记录'
    columns = metadata()['columns']
    for index, row in enumerate(records):
        if not isinstance(row, dict):
            return f'原始日记第 {index + 1} 行不是对象'
        for column in columns:
            value = row.get(column['key'])
            if value is None or value == '':
                return f'原始日记第 {index + 1} 行缺少字段 {column["label"]}'
            if column['type'] == 'number':
                try:
                    if isinstance(value, bool) or not math.isfinite(float(value)):
                        return f'原始日记第 {index + 1} 行 {column["label"]} 不是有限数值'
                except (TypeError, ValueError, OverflowError):
                    return f'原始日记第 {index + 1} 行 {column["label"]} 不是有效数值'
    return None


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def export_package(db, actor: str) -> tuple[bytes, str, dict]:
    jobs = list(db.scalars(select(Job).where(
        Job.approved == True, Job.status == 'completed', visible_condition(Job)).order_by(Job.id)))
    if not jobs:
        raise HTTPException(409, '当前没有已批准的简报，暂无可交接内容')
    job_ids = [job.id for job in jobs]
    datasets = list(db.scalars(select(Dataset).where(
        Dataset.id.in_({job.dataset_id for job in jobs}), visible_condition(Dataset)).order_by(Dataset.id)))
    reports = list(db.scalars(select(Report).where(
        Report.job_id.in_(job_ids), visible_condition(Report)).order_by(Report.id)))
    deliveries = list(db.scalars(select(Delivery).where(
        Delivery.report_id.in_([r.id for r in reports]), visible_condition(Delivery)).order_by(Delivery.id))) if reports else []

    entries: dict[str, bytes] = {}
    items: list[dict] = []

    def add(kind: str, origin_id: Any, path: str, data: bytes, extra: dict | None = None):
        entries[path] = data
        item = {'kind': kind, 'origin_id': str(origin_id), 'path': path,
                'sha256': _sha256(data), 'size': len(data)}
        item.update(extra or {})
        items.append(item)

    for dataset in datasets:
        payload = {'origin_id': dataset.id, 'name': dataset.name, 'source': dataset.source,
                   'checksum': dataset.checksum, 'created_at': dataset.created_at,
                   'records': dataset.records}
        add('dataset', dataset.id, f'datasets/{dataset.id}.json', _json_bytes(payload),
            {'name': dataset.name})

    for job in jobs:
        payload = {'origin_id': job.id, 'name': job.name, 'dataset_origin_id': job.dataset_id,
                   'params': job.params, 'status': job.status, 'result': job.result,
                   'approved': bool(job.approved), 'created_at': job.created_at,
                   'finished_at': job.finished_at}
        add('job', job.id, f'jobs/{job.id}.json', _json_bytes(payload), {'name': job.name})

    for report in reports:
        payload = {'origin_id': report.id, 'name': report.name, 'job_origin_id': report.job_id,
                   'destination': report.destination, 'interval_seconds': report.interval_seconds,
                   'next_run': report.next_run, 'enabled': bool(report.enabled)}
        add('report', report.id, f'reports/{report.id}.json', _json_bytes(payload), {'name': report.name})

    missing = []
    for delivery in deliveries:
        meta = {'origin_id': delivery.id, 'report_origin_id': delivery.report_id,
                'created_at': delivery.created_at, 'checksum': delivery.checksum,
                'filename': delivery.filename, 'destination': delivery.destination}
        add('delivery', delivery.id, f'deliveries/{delivery.id}.meta.json', _json_bytes(meta),
            {'format': 'meta', 'name': delivery.filename})
        for fmt, source in (('csv', delivery.filename), ('json', delivery.filename + '.json')):
            full = os.path.join(ARTIFACT_DIR, posixpath.basename(str(source)))
            if not os.path.isfile(full):
                missing.append(str(source))
                continue
            with open(full, 'rb') as handle:
                data = handle.read()
            add('delivery', delivery.id, f'deliveries/{delivery.id}.{fmt}', data,
                {'format': fmt, 'name': delivery.filename, 'delivery_checksum': delivery.checksum})
    if missing:
        raise HTTPException(409, '归档文件缺失，无法生成完整交接包：' + '、'.join(sorted(set(missing))[:5]))

    manifest = {
        'format': FORMAT,
        'version': VERSION,
        'site': SITE_NAME,
        'exported_at': time.time(),
        'exported_at_text': datetime.now(timezone.utc).isoformat(),
        'exported_by': actor,
        'counts': {'datasets': len(datasets), 'jobs': len(jobs), 'reports': len(reports),
                   'deliveries': len(deliveries)},
        'items': sorted(items, key=lambda entry: (entry['kind'], entry['origin_id'],
                                                   entry.get('format', ''))),
    }
    manifest_bytes = _canonical(manifest)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo('manifest.json', date_time=FIXED_ZIP_TIME)
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, manifest_bytes)
        for path in sorted(entries):
            info = zipfile.ZipInfo(path, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, entries[path])
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    filename = f'handover-{SITE_NAME}-{stamp}.zip'
    return buffer.getvalue(), filename, {'counts': manifest['counts'], 'manifest': manifest}


# --------------------------------------------------------------------------- #
# verification
# --------------------------------------------------------------------------- #
def verify_package(data: bytes, name_exists=None) -> tuple[bool, dict, dict]:
    checks: list[dict] = []
    problems: list[str] = []
    manifest: dict = {}
    blobs: dict[str, bytes] = {}

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        names = archive.namelist()
        if len(names) > MAX_ENTRIES:
            raise ValueError('条目数量超过限制')
        if sum(info.file_size for info in archive.infolist()) > MAX_UNCOMPRESSED_BYTES:
            raise ValueError('解压后体积超过限制')
        checks.append(_check('archive', '交接包可读', True, f'ZIP 包含 {len(names)} 个文件'))
    except Exception as error:
        checks.append(_check('archive', '交接包可读', False, f'无法读取交接包：{error}'))
        return False, {'ok': False, 'checks': checks, 'problems': ['交接包不是有效的 ZIP 归档'],
                       'summary': '交接包无法读取，已整包拒绝'}, {}

    try:
        raw_manifest = archive.read('manifest.json')
        manifest = json.loads(raw_manifest.decode('utf-8'))
        if manifest.get('format') != FORMAT:
            raise ValueError('交接清单格式标识不正确')
        if manifest.get('version') != VERSION:
            raise ValueError('交接清单版本不受支持')
        if not isinstance(manifest.get('items'), list) or not manifest['items']:
            raise ValueError('交接清单缺少文件条目')
        if not str(manifest.get('site') or '').strip():
            raise ValueError('交接清单缺少来源站点')
        checks.append(_check('manifest', '交接清单有效', True,
                             f"来源站点 {manifest.get('site')} · 共 {len(manifest['items'])} 个文件"))
    except Exception as error:
        checks.append(_check('manifest', '交接清单有效', False, f'清单校验失败：{error}'))
        problems.append(f'交接清单无效：{error}')
        return False, {'ok': False, 'checks': checks, 'problems': problems,
                       'summary': '交接清单无效，已整包拒绝'}, {}

    # Uniqueness and required keys of the manifest itself.
    seen = set()
    for item in manifest['items']:
        key = (item.get('kind'), str(item.get('origin_id')), item.get('format', ''))
        if not item.get('kind') or not str(item.get('origin_id') or '').strip():
            problems.append('交接清单存在缺少类型或原编号的条目')
            break
        if key in seen:
            problems.append(f'交接清单存在重复条目：{key[0]} #{key[1]}')
            break
        seen.add(key)
    blobs, file_problems = _read_items(archive, manifest)
    problems.extend(file_problems)
    checks.append(_check('files', '文件齐全且未被改动', not file_problems,
                         '所有清单文件均存在，大小与 SHA256 一致' if not file_problems
                         else f'发现 {len(file_problems)} 项文件问题：' + '；'.join(file_problems[:3])))

    def load(kind: str, fmt: str = ''):
        result = {}
        for item in manifest['items']:
            if item.get('kind') == kind and item.get('format', '') == fmt:
                path = item.get('path')
                if path in blobs:
                    try:
                        result[str(item['origin_id'])] = json.loads(blobs[path].decode('utf-8'))
                    except Exception:
                        problems.append(f'{kind} #{item["origin_id"]} 内容不是有效 JSON')
        return result

    datasets = load('dataset')
    jobs = load('job')
    reports = load('report')
    delivery_meta = load('delivery', 'meta')

    content_problems: list[str] = []

    def bad(message):
        content_problems.append(message)
        problems.append(message)

    for origin_id, payload in datasets.items():
        if not isinstance(payload, dict):
            bad(f'原始日记 #{origin_id} 结构不正确')
            continue
        error = _validate_records(payload.get('records'))
        if error:
            bad(f'原始日记 #{origin_id}：{error}')
        if payload.get('checksum') != checksum(payload.get('records')):
            bad(f'原始日记 #{origin_id} 内容指纹与批次指纹不一致')

    for origin_id, payload in jobs.items():
        if not isinstance(payload, dict):
            bad(f'简报 #{origin_id} 结构不正确')
            continue
        if str(payload.get('dataset_origin_id')) not in datasets:
            bad(f'简报 #{origin_id} 引用的原始日记不在包内')
        if payload.get('status') != 'completed' or not payload.get('approved'):
            bad(f'简报 #{origin_id} 不是已批准完成结果')
        if not isinstance(payload.get('params'), dict) or not isinstance(payload.get('result'), dict):
            bad(f'简报 #{origin_id} 缺少参数或计算结果')
        else:
            try:
                _json_bytes(payload['result'])
            except Exception:
                bad(f'简报 #{origin_id} 计算结果不是有效 JSON')

    for origin_id, payload in reports.items():
        if not isinstance(payload, dict) or str(payload.get('job_origin_id')) not in jobs:
            bad(f'分发计划 #{origin_id} 引用的简报不在包内')

    for origin_id, payload in delivery_meta.items():
        if not isinstance(payload, dict) or str(payload.get('report_origin_id')) not in reports:
            bad(f'归档 #{origin_id} 引用的分发计划不在包内')
            continue
        if not isinstance(payload.get('checksum'), str) or len(payload['checksum']) != 64:
            bad(f'归档 #{origin_id} 缺少有效投递指纹')

    for item in manifest['items']:
        if item.get('kind') == 'delivery' and item.get('format') == 'csv':
            origin_id = str(item.get('origin_id'))
            meta = delivery_meta.get(origin_id)
            data = blobs.get(item.get('path'))
            if meta and data is not None and _sha256(data) != meta.get('checksum'):
                bad(f'归档 #{origin_id} 下载字节与投递指纹不一致')

    checks.append(_check('content', '业务内容可复核', not content_problems,
                         '原始日记、参数、结果与投递记录结构完整' if not content_problems
                         else '；'.join(content_problems[:3])))

    counts = manifest.get('counts') or {}
    expected = {'datasets': len(datasets), 'jobs': len(jobs), 'reports': len(reports),
                'deliveries': len(delivery_meta)}
    count_problems = [f'{label}数量与清单不一致' for key, label in
                      (('datasets', '原始日记'), ('jobs', '简报'), ('reports', '分发计划'),
                       ('deliveries', '归档'))
                      if counts.get(key) not in (None, expected[key])]
    link_problems = [p for p in problems if '引用' in p]
    checks.append(_check('links', '包内关联完整', not link_problems and not count_problems,
                         '简报、分发计划与归档的包内引用均可重建' if not link_problems and not count_problems
                         else '；'.join((link_problems + count_problems)[:3])))

    conflict_names = []
    for payload in datasets.values():
        if isinstance(payload, dict) and payload.get('name'):
            if name_exists and name_exists(payload['name']):
                conflict_names.append(str(payload['name']))
    checks.append(_check('conflicts', '站点编号与批次名冲突处理',
                         True,
                         f'发现 {len(conflict_names)} 个同名批次，接收时将保留双方并标注来源'
                         if conflict_names else '未发现批次名冲突，原编号与批次名均可保留',
                         status='warning' if conflict_names else 'passed'))

    ok = not problems
    summary = ('校验通过，可开始接收：已批准简报、原始日记、参数与投递归档均完整'
               if ok else f'校验失败，整包拒绝：发现 {len(problems)} 项问题')
    verification = {'ok': ok, 'checks': checks, 'problems': problems, 'summary': summary,
                    'counts': expected}
    return ok, verification, manifest


# --------------------------------------------------------------------------- #
# upload / verify registration
# --------------------------------------------------------------------------- #
def register_upload(db, data: bytes, filename: str, actor: str) -> HandoverImport:
    if not data:
        raise HTTPException(422, '交接包为空')
    if len(data) > MAX_PACKAGE_BYTES:
        raise HTTPException(413, '交接包超过 64MB 限制')
    digest = _sha256(data)
    existing = db.scalar(select(HandoverImport).where(HandoverImport.package_checksum == digest))
    if existing:
        return existing

    path = os.path.join(_package_dir(), digest + '.zip')
    if not os.path.exists(path):
        temporary = path + '.' + secrets.token_hex(6) + '.tmp'
        try:
            with open(temporary, 'wb') as handle:
                handle.write(data)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)

    probe = lambda name: db.scalar(select(Dataset.id).where(Dataset.name == name)) is not None
    ok, verification, manifest = verify_package(data, probe)

    now = time.time()
    imported = HandoverImport(
        package_checksum=digest,
        filename=_safe_name(posixpath.basename(filename or 'handover.zip'), 255, 'handover.zip'),
        origin_site=_safe_name((manifest or {}).get('site'), 160, ''),
        status='verified' if ok else 'rejected',
        phase='verified' if ok else 'rejected',
        verification=verification,
        manifest={'site': (manifest or {}).get('site', ''),
                  'counts': verification.get('counts', {}),
                  'items': (manifest or {}).get('items', [])},
        progress={key: {'done': 0, 'total': verification.get('counts', {}).get(key, 0)}
                  for key in KIND_ORDER},
        total_items=sum(verification.get('counts', {}).values()),
        processed_items=0,
        package_path=path,
        created_by=actor,
        created_at=now,
        updated_at=now,
        verified_at=now,
        last_error='' if ok else verification.get('summary', '')[:990],
    )
    db.add(imported)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(HandoverImport).where(HandoverImport.package_checksum == digest))
        if existing:
            return existing
        raise
    db.refresh(imported)
    return imported


# --------------------------------------------------------------------------- #
# accept / import (resumable + single receiver)
# --------------------------------------------------------------------------- #
def _mapping(db, import_id: int, entity: str, origin_id: str) -> HandoverMapping | None:
    return db.scalar(select(HandoverMapping).where(
        HandoverMapping.import_id == import_id,
        HandoverMapping.entity_type == entity,
        HandoverMapping.origin_id == origin_id))


def _add_mapping(db, rec: HandoverImport, entity: str, origin_id: str, origin_name: str,
                 local_id: int, local_name: str):
    mapping = HandoverMapping(import_id=rec.id, entity_type=entity, origin_id=str(origin_id),
                              origin_name=_safe_name(origin_name, 200, ''),
                              local_id=local_id, local_name=_safe_name(local_name, 200, ''),
                              created_at=time.time())
    db.add(mapping)
    return mapping


def _unique_dataset_name(db, desired: str, rec: HandoverImport, origin_id: str) -> str:
    desired = _safe_name(desired, 160, '未命名批次')
    if not db.scalar(select(Dataset.id).where(Dataset.name == desired)):
        return desired
    suffix = f'（{rec.origin_site or "来源站"} #{origin_id} · 交接 {rec.id}）'
    return desired[:max(1, 160 - len(suffix))] + suffix


def _renew(db, rec: HandoverImport, token: str) -> bool:
    now = time.time()
    result = db.execute(update(HandoverImport).where(
        HandoverImport.id == rec.id, HandoverImport.claim_token == token).values(
        claim_expires_at=now + CLAIM_SECONDS, updated_at=now))
    db.commit()
    return result.rowcount == 1


def _mapped_done(db, import_id: int, entity: str) -> int:
    return int(db.scalar(select(func.count()).select_from(HandoverMapping).where(
        HandoverMapping.import_id == import_id,
        HandoverMapping.entity_type == entity)) or 0)


def _save_progress(db, rec: HandoverImport, token: str, progress: dict):
    for key, entity in (('datasets', 'dataset'), ('jobs', 'job'),
                        ('reports', 'report'), ('deliveries', 'delivery')):
        progress.setdefault(key, {})
        progress[key]['done'] = _mapped_done(db, rec.id, entity)
    total = sum(int(part.get('total', 0)) for part in progress.values())
    done = sum(int(part.get('done', 0)) for part in progress.values())
    saved = db.execute(update(HandoverImport).where(
        HandoverImport.id == rec.id, HandoverImport.claim_token == token).values(
        progress=progress, total_items=total, processed_items=done,
        updated_at=time.time(), claim_expires_at=time.time() + CLAIM_SECONDS))
    db.commit()
    if saved.rowcount != 1:
        raise HTTPException(409, '接收权已被其他进程接管，请稍后刷新')


def _load_manifest(rec: HandoverImport) -> tuple[dict, dict[str, bytes]]:
    if not rec.package_path or not os.path.isfile(rec.package_path):
        raise HTTPException(409, '交接包文件已不存在，请重新上传后再接收')
    with open(rec.package_path, 'rb') as handle:
        data = handle.read()
    if _sha256(data) != rec.package_checksum:
        raise HTTPException(409, '交接包文件在校验后被改动，已停止接收；请重新上传原始交接包')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            manifest = json.loads(archive.read('manifest.json').decode('utf-8'))
            blobs, problems = _read_items(archive, manifest)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(409, f'交接包内容在校验后损坏：{str(error)[:160]}；请重新上传原始交接包') from error
    if problems:
        raise HTTPException(409, '交接包内容在校验后被改动：' + '；'.join(problems[:3])
                            + '；请重新上传原始交接包')
    return manifest, blobs


def _payload(blobs: dict[str, bytes], items: list[dict], kind: str, origin_id: str,
             fmt: str = '') -> dict:
    for item in items:
        if item.get('kind') == kind and str(item.get('origin_id')) == str(origin_id) \
                and item.get('format', '') == fmt:
            return json.loads(blobs[item['path']].decode('utf-8'))
    raise ValueError(f'交接包缺少 {kind} #{origin_id} 内容')


def accept_import(db, import_id: int, user) -> HandoverImport:
    now = time.time()
    rec = db.get(HandoverImport, import_id)
    if not rec:
        raise HTTPException(404, '交接包不存在')
    if rec.status == 'received':
        return rec
    if rec.status == 'rejected':
        raise HTTPException(409, '该交接包校验未通过，不能接收')
    if rec.status == 'pending_verification':
        raise HTTPException(409, '该交接包仍在校验中，请稍后重试')

    token = secrets.token_hex(16)
    claimed = db.execute(update(HandoverImport).where(
        HandoverImport.id == import_id,
        HandoverImport.status.in_(('verified', 'importing', 'failed')),
        or_(HandoverImport.claim_token.is_(None), HandoverImport.claim_expires_at < now),
    ).values(status='importing', phase='importing', claim_token=token,
             claim_expires_at=now + CLAIM_SECONDS, last_error='', updated_at=now))
    db.commit()
    if claimed.rowcount != 1:
        db.refresh(rec)
        if rec.status == 'received':
            return rec
        raise HTTPException(409, '另一个接收进程正在处理该交接包，请稍后刷新')
    db.refresh(rec)

    try:
        progress = dict(rec.progress or {})
        for key in KIND_ORDER:
            progress.setdefault(key, {'done': 0, 'total': 0})
        manifest, blobs = _load_manifest(rec)
        items = manifest.get('items', [])

        def datasets_items():
            return [item for item in items if item.get('kind') == 'dataset']

        def jobs_items():
            return [item for item in items if item.get('kind') == 'job']

        def reports_items():
            return [item for item in items if item.get('kind') == 'report']

        def deliveries_items():
            return sorted({str(item.get('origin_id')) for item in items
                           if item.get('kind') == 'delivery'})

        # 1. original diaries -------------------------------------------------
        progress['datasets']['total'] = len(datasets_items())
        for item in datasets_items():
            origin_id = str(item.get('origin_id'))
            mapping = _mapping(db, rec.id, 'dataset', origin_id)
            if mapping and db.get(Dataset, mapping.local_id):
                progress['datasets']['done'] += 1
                continue
            payload = _payload(blobs, items, 'dataset', origin_id)
            if payload.get('checksum') != checksum(payload.get('records')):
                raise ValueError(f'原始日记 #{origin_id} 接收时指纹校验失败')
            dataset = Dataset(
                name=_unique_dataset_name(db, payload.get('name'), rec, origin_id),
                source=_safe_name(payload.get('source'), 200, '') or 'handover',
                records=payload['records'], checksum=payload['checksum'],
                created_at=float(payload.get('created_at') or time.time()),
                handover_import_id=rec.id)
            db.add(dataset)
            db.flush()
            if mapping:
                mapping.local_id, mapping.local_name = dataset.id, dataset.name
            else:
                _add_mapping(db, rec, 'dataset', origin_id, payload.get('name'), dataset.id, dataset.name)
            db.commit()
            progress['datasets']['done'] += 1
            _save_progress(db, rec, token, progress)

        # 2. approved briefs and parameters ----------------------------------
        progress['jobs']['total'] = len(jobs_items())
        for item in jobs_items():
            origin_id = str(item.get('origin_id'))
            mapping = _mapping(db, rec.id, 'job', origin_id)
            if mapping and db.get(Job, mapping.local_id):
                progress['jobs']['done'] += 1
                continue
            payload = _payload(blobs, items, 'job', origin_id)
            dataset_map = _mapping(db, rec.id, 'dataset', str(payload.get('dataset_origin_id')))
            dataset = db.get(Dataset, dataset_map.local_id) if dataset_map else None
            if not dataset:
                raise ValueError(f'简报 #{origin_id} 的原始日记尚未接收')
            job = Job(
                name=_safe_name(payload.get('name'), 160, '交接简报'),
                dataset_id=dataset.id, params=payload.get('params') or {},
                status='completed', result=payload.get('result') or {},
                error='', approved=bool(payload.get('approved')),
                created_by=user.id,
                created_at=float(payload.get('created_at') or time.time()),
                finished_at=payload.get('finished_at'),
                handover_import_id=rec.id)
            db.add(job)
            db.flush()
            if mapping:
                mapping.local_id, mapping.local_name = job.id, job.name
            else:
                _add_mapping(db, rec, 'job', origin_id, payload.get('name'), job.id, job.name)
            db.commit()
            progress['jobs']['done'] += 1
            _save_progress(db, rec, token, progress)

        # 3. distribution plans - always stopped on arrival ------------------
        progress['reports']['total'] = len(reports_items())
        for item in reports_items():
            origin_id = str(item.get('origin_id'))
            mapping = _mapping(db, rec.id, 'report', origin_id)
            if mapping and db.get(Report, mapping.local_id):
                progress['reports']['done'] += 1
                continue
            payload = _payload(blobs, items, 'report', origin_id)
            job_map = _mapping(db, rec.id, 'job', str(payload.get('job_origin_id')))
            job = db.get(Job, job_map.local_id) if job_map else None
            if not job:
                raise ValueError(f'分发计划 #{origin_id} 的简报尚未接收')
            interval = int(payload.get('interval_seconds') or 86400)
            report = Report(
                name=_safe_name(payload.get('name'), 160, '交接分发计划'),
                job_id=job.id,
                destination=_safe_name(payload.get('destination'), 120, '业务复核组'),
                interval_seconds=interval,
                next_run=time.time() + interval,
                enabled=False,
                handover_import_id=rec.id)
            db.add(report)
            db.flush()
            if mapping:
                mapping.local_id, mapping.local_name = report.id, report.name
            else:
                _add_mapping(db, rec, 'report', origin_id, payload.get('name'), report.id, report.name)
            db.commit()
            progress['reports']['done'] += 1
            _save_progress(db, rec, token, progress)

        # 4. delivered archives - byte-identical copies ----------------------
        progress['deliveries']['total'] = len(deliveries_items())
        for origin_id in deliveries_items():
            mapping = _mapping(db, rec.id, 'delivery', origin_id)
            if mapping and db.get(Delivery, mapping.local_id):
                progress['deliveries']['done'] += 1
                continue
            payload = _payload(blobs, items, 'delivery', origin_id, 'meta')
            report_map = _mapping(db, rec.id, 'report', str(payload.get('report_origin_id')))
            report = db.get(Report, report_map.local_id) if report_map else None
            if not report:
                raise ValueError(f'归档 #{origin_id} 的分发计划尚未接收')
            csv_item = next((item for item in items if item.get('kind') == 'delivery'
                             and str(item.get('origin_id')) == origin_id
                             and item.get('format') == 'csv'), None)
            json_item = next((item for item in items if item.get('kind') == 'delivery'
                              and str(item.get('origin_id')) == origin_id
                              and item.get('format') == 'json'), None)
            if not csv_item or not json_item:
                raise ValueError(f'归档 #{origin_id} 缺少原始下载文件')
            csv_bytes = blobs[csv_item['path']]
            json_bytes = blobs[json_item['path']]
            if _sha256(csv_bytes) != payload.get('checksum'):
                raise ValueError(f'归档 #{origin_id} 字节与投递指纹不一致')
            filename = _delivery_filename(rec.id, origin_id, payload.get('filename'))
            os.makedirs(ARTIFACT_DIR, exist_ok=True)
            with open(os.path.join(ARTIFACT_DIR, filename), 'wb') as handle:
                handle.write(csv_bytes)
            with open(os.path.join(ARTIFACT_DIR, filename + '.json'), 'wb') as handle:
                handle.write(json_bytes)
            delivery = Delivery(
                report_id=report.id,
                created_at=float(payload.get('created_at') or time.time()),
                checksum=payload['checksum'], filename=filename,
                destination=_safe_name(payload.get('destination'), 120, '业务复核组'),
                handover_import_id=rec.id)
            db.add(delivery)
            db.flush()
            if mapping:
                mapping.local_id, mapping.local_name = delivery.id, filename
            else:
                _add_mapping(db, rec, 'delivery', origin_id, payload.get('filename'), delivery.id, filename)
            db.commit()
            progress['deliveries']['done'] += 1
            _save_progress(db, rec, token, progress)

        if not _renew(db, rec, token):
            raise ValueError('接收凭据已失效，请重新发起接收')
        _save_progress(db, rec, token, progress)
        total = sum(int(part.get('total', 0)) for part in progress.values())
        done = sum(int(part.get('done', 0)) for part in progress.values())
        db.execute(update(HandoverImport).where(
            HandoverImport.id == rec.id, HandoverImport.claim_token == token).values(
            status='received', phase='complete', progress=progress, total_items=total,
            processed_items=done, accepted_at=time.time(), claim_token=None,
            claim_expires_at=0, updated_at=time.time()))
        db.commit()
        record(db, getattr(user, 'email', 'handover'), 'handover.accept',
               {'id': rec.id, 'site': rec.origin_site, 'counts': {k: v['done'] for k, v in progress.items()}})
        db.commit()
        db.refresh(rec)
        return rec
    except HTTPException as error:
        db.rollback()
        _mark_failed(db, import_id, token, error)
        raise
    except Exception as error:
        db.rollback()
        _mark_failed(db, import_id, token, error)
        raise HTTPException(500, f'接收中断：{str(error)[:180]}；可再次发起接收继续，已接收部分不会重复') from error


def _mark_failed(db, import_id: int, token: str, error: Exception | None = None):
    now = time.time()
    rec = db.get(HandoverImport, import_id)
    if not rec or rec.status == 'received':
        return
    detail = getattr(error, 'detail', None) or str(error or '') or rec.last_error or '接收中断'
    try:
        db.execute(update(HandoverImport).where(
            HandoverImport.id == import_id, HandoverImport.claim_token == token).values(
            status='failed', phase='failed', last_error=str(detail)[:990],
            claim_token=None, claim_expires_at=0, updated_at=now))
        db.commit()
    except Exception:
        db.rollback()


# --------------------------------------------------------------------------- #
# serialization
# --------------------------------------------------------------------------- #
def import_summary(rec: HandoverImport) -> dict:
    verification = rec.verification or {}
    return {
        'id': rec.id, 'filename': rec.filename, 'origin_site': rec.origin_site,
        'status': rec.status, 'status_label': STATUS_LABELS.get(rec.status, rec.status),
        'phase': rec.phase, 'package_checksum': rec.package_checksum,
        'created_at': rec.created_at, 'verified_at': rec.verified_at,
        'accepted_at': rec.accepted_at, 'total_items': rec.total_items,
        'processed_items': rec.processed_items,
        'progress': rec.progress or {},
        'ok': bool(verification.get('ok')),
        'problem_count': len(verification.get('problems') or []),
        'summary': verification.get('summary', ''),
        'last_error': rec.last_error,
    }


def import_detail(db, rec: HandoverImport) -> dict:
    verification = rec.verification or {}
    items = (rec.manifest or {}).get('items', [])
    mappings = list(db.scalars(select(HandoverMapping).where(
        HandoverMapping.import_id == rec.id).order_by(HandoverMapping.id)))
    return {
        **import_summary(rec),
        'verification': {'ok': verification.get('ok'), 'summary': verification.get('summary', ''),
                         'checks': verification.get('checks', []),
                         'problems': verification.get('problems', []),
                         'counts': verification.get('counts', {})},
        'items': [{'kind': item.get('kind'), 'origin_id': item.get('origin_id'),
                   'name': item.get('name', ''), 'format': item.get('format', ''),
                   'size': item.get('size', 0), 'sha256': item.get('sha256', '')} for item in items],
        'mappings': [{'entity_type': mapping.entity_type, 'origin_id': mapping.origin_id,
                      'origin_name': mapping.origin_name, 'local_id': mapping.local_id,
                      'local_name': mapping.local_name} for mapping in mappings],
        'origin_site': rec.origin_site,
    }
