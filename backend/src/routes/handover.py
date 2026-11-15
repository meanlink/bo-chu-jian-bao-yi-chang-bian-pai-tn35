import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select

from src.config.database import get_db
from src.middlewares.auth import can_write, current_user
from src.models import HandoverImport
from src.services import handover
from src.services.audit import record

router = APIRouter(prefix='/handover')


@router.get('/export')
def export(db=Depends(get_db), user=Depends(current_user)):
    data, filename, meta = handover.export_package(db, user.email)
    record(db, user.email, 'handover.export', {'filename': filename, 'counts': meta['counts']})
    db.commit()
    return Response(content=data, media_type='application/zip', headers={
        'Content-Disposition': "attachment; filename=\"handover.zip\"; filename*=UTF-8''"
                               + quote(filename),
        'X-Handover-Filename': quote(filename),
        'X-Handover-Counts': json.dumps(meta['counts'], ensure_ascii=False),
    })


@router.get('/imports')
def listing(db=Depends(get_db), user=Depends(current_user)):
    rows = db.scalars(select(HandoverImport).order_by(HandoverImport.id.desc()).limit(100))
    return [handover.import_summary(row) for row in rows]


@router.get('/imports/{import_id}')
def detail(import_id: int, db=Depends(get_db), user=Depends(current_user)):
    row = db.get(HandoverImport, import_id)
    if not row:
        raise HTTPException(404, '交接包不存在')
    return handover.import_detail(db, row)


@router.post('/imports', status_code=201)
async def upload(request: Request, filename: str = 'handover.zip', db=Depends(get_db),
                 user=Depends(can_write)):
    data = await request.body()
    if len(data) > handover.MAX_PACKAGE_BYTES:
        raise HTTPException(413, '交接包超过 64MB 限制')
    row = handover.register_upload(db, data, filename, user.email)
    record(db, user.email, 'handover.upload',
           {'id': row.id, 'status': row.status, 'checksum': row.package_checksum})
    db.commit()
    return handover.import_detail(db, row)


@router.post('/imports/{import_id}/accept')
def accept(import_id: int, db=Depends(get_db), user=Depends(can_write)):
    row = handover.accept_import(db, import_id, user)
    return handover.import_detail(db, row)
