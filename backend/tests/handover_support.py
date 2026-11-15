"""Shared fixtures for handover tests: a real primary site export in a temp dir."""
import io
import os
import sys
import tempfile
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

WORK = tempfile.mkdtemp(prefix='handover-tests-')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(WORK, 'primary.sqlite'))
os.environ.setdefault('ARTIFACT_DIR', os.path.join(WORK, 'artifacts'))
os.environ.setdefault('SEED_DEMO', 'false')
os.environ.setdefault('SITE_NAME', '海棠主站')

from src.config.database import Base, Session, engine
from src import models  # noqa: F401  (registers tables)
from src.models import Dataset, Job, Report, User
from src.services.business import analyze, metadata, seed_records
from src.services.reports import deliver
from src.utils.security import hash_password
from src.utils.serialization import checksum


def build_primary_export() -> bytes:
    """Create a primary-site database with one approved brief and return its ZIP."""
    Base.metadata.create_all(engine)
    with Session() as db:
        admin = User(email='admin@demo.local', name='林知', role='admin',
                     password=hash_password('Demo123!'))
        db.add(admin)
        db.flush()
        rows = seed_records()
        dataset = Dataset(name=metadata()['dataset_name'], source='seed.csv', records=rows,
                          checksum=checksum(rows), created_at=time.time())
        db.add(dataset)
        db.flush()
        params = {item['key']: item['default'] for item in metadata()['parameters']}
        job = Job(name='基线复核', dataset_id=dataset.id, params=params, status='completed',
                  result=analyze(rows, params), approved=True, created_by=admin.id,
                  created_at=time.time(), finished_at=time.time())
        db.add(job)
        db.flush()
        report = Report(name='每日业务复核简报', job_id=job.id, destination='本地收件箱',
                        interval_seconds=86400, next_run=time.time(), enabled=True)
        db.add(report)
        db.flush()
        deliver(db, report)
        db.commit()
    from src.services import handover
    with Session() as db:
        data, _name, _meta = handover.export_package(db, 'admin@demo.local')
    return data


def fresh_standby(label: str, dataset_name: str | None = None):
    """A fresh standby-site database with fictional user names."""
    global WORK
    WORK = tempfile.mkdtemp(prefix=f'handover-{label}-')
    engine.dispose()
    import src.config.settings as settings
    import src.config.database as database
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    database.engine = create_engine('sqlite:///' + os.path.join(WORK, 'standby.sqlite'))
    database.Session = sessionmaker(database.engine, expire_on_commit=False)
    settings.ARTIFACT_DIR = os.path.join(WORK, 'artifacts')
    os.environ['ARTIFACT_DIR'] = settings.ARTIFACT_DIR
    import src.services.handover as handover_service
    handover_service.ARTIFACT_DIR = settings.ARTIFACT_DIR
    database.Base.metadata.create_all(database.engine)
    with database.Session() as db:
        db.add(User(email='admin@demo.local', name='备站负责人', role='admin',
                    password=hash_password('Demo123!')))
        db.add(User(email='viewer@demo.local', name='观察员', role='viewer',
                    password=hash_password('Demo123!')))
        if dataset_name:
            db.add(Dataset(name=dataset_name, source='本地', records=[], checksum='local',
                           created_at=time.time()))
        db.commit()
        admin = db.scalar(select(User).where(User.email == 'admin@demo.local'))
        viewer = db.scalar(select(User).where(User.email == 'viewer@demo.local'))
    return db, admin, viewer


def change_package(data: bytes, path: str, transform) -> bytes:
    """Return a copy of the ZIP with one entry added/removed/modified."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    if transform is None:
        entries.pop(path, None)
    else:
        entries[path] = transform(entries[path])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            archive.writestr(name, entries[name])
    return buffer.getvalue()
