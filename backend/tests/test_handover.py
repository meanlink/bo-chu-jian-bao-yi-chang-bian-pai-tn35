import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from handover_support import build_primary_export, fresh_standby, change_package


class HandoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = build_primary_export()

    def test_export_contains_manifest_and_original_artifacts(self):
        import io
        import json
        import zipfile

        with zipfile.ZipFile(io.BytesIO(self.package)) as archive:
            names = sorted(archive.namelist())
            manifest = json.loads(archive.read('manifest.json'))
        self.assertIn('manifest.json', names)
        self.assertIn('datasets/1.json', names)
        self.assertIn('jobs/1.json', names)
        self.assertIn('reports/1.json', names)
        self.assertIn('deliveries/1.csv', names)
        self.assertEqual(manifest['format'], 'bulletin-handover')
        self.assertEqual(manifest['site'], '海棠主站')
        self.assertEqual(manifest['counts'],
                         {'datasets': 1, 'jobs': 1, 'reports': 1, 'deliveries': 1})
        for item in manifest['items']:
            self.assertEqual(len(item['sha256']), 64)

    def test_verified_package_is_hidden_until_accepted(self):
        from src.config.database import Session
        from src.models import Dataset, Delivery, Job, Report
        from src.services import handover
        from src.services.handover import visible_condition
        from sqlalchemy import select

        db, admin, _viewer = fresh_standby('verified')
        row = handover.register_upload(db, self.package, 'package.zip', admin.email)
        self.assertEqual(row.status, 'verified')
        self.assertIn('校验通过', row.verification['summary'])
        for model in (Dataset, Job, Report, Delivery):
            self.assertEqual(db.scalar(select(model).where(visible_condition(model) )), None)
        accepted = handover.accept_import(db, row.id, admin)
        self.assertEqual(accepted.status, 'received')
        self.assertEqual(len(list(db.scalars(select(Dataset).where(visible_condition(Dataset))))), 1)

    def test_tampered_file_rejects_whole_package(self):
        from src.models import Dataset
        from src.services import handover
        from src.config.database import Session
        from sqlalchemy import select

        tampered = change_package(self.package, 'deliveries/1.csv', lambda b: b + b'\n1,2')
        db, admin, _ = fresh_standby('tampered')
        row = handover.register_upload(db, tampered, 'tampered.zip', admin.email)
        self.assertEqual(row.status, 'rejected')
        checks = {check['key']: check for check in row.verification['checks']}
        self.assertEqual(checks['files']['status'], 'failed')
        with self.assertRaises(Exception) as caught:
            handover.accept_import(db, row.id, admin)
        self.assertEqual(getattr(caught.exception, 'status_code', None), 409)
        self.assertEqual(len(list(db.scalars(select(Dataset)))), 0)

    def test_missing_file_rejects_whole_package(self):
        from src.config.database import Session
        from src.services import handover

        partial = change_package(self.package, 'jobs/1.json', None)
        db, admin, _ = fresh_standby('missing')
        row = handover.register_upload(db, partial, 'missing.zip', admin.email)
        self.assertEqual(row.status, 'rejected')
        joined = '；'.join(row.verification['problems'])
        self.assertIn('缺少交接文件', joined)

    def test_retry_and_concurrent_accept_do_not_duplicate(self):
        from src.config.database import Session
        from src.models import Dataset, Delivery, Job, Report
        from src.services import handover
        from src.services.handover import visible_condition
        from sqlalchemy import func, select

        db, admin, _ = fresh_standby('idempotent')
        row = handover.register_upload(db, self.package, 'package.zip', admin.email)
        first = handover.accept_import(db, row.id, admin)
        self.assertEqual(first.status, 'received')
        counts = {model.__name__: db.scalar(select(func.count()).select_from(model))
                  for model in (Dataset, Job, Report, Delivery)}
        again = handover.accept_import(db, row.id, admin)
        self.assertEqual(again.status, 'received')
        self.assertEqual(counts, {model.__name__: db.scalar(select(func.count()).select_from(model))
                                  for model in (Dataset, Job, Report, Delivery)})
        duplicate = handover.accept_import(db, row.id, admin)
        self.assertEqual(duplicate.package_checksum, row.package_checksum)

    def test_resume_after_interruption(self):
        from src.config.database import Session
        from src.models import Dataset, Delivery, Job, Report
        from src.services import handover
        from sqlalchemy import func, select

        db, admin, _ = fresh_standby('resume')
        row = handover.register_upload(db, self.package, 'package.zip', admin.email)
        original = handover._save_progress
        calls = {'count': 0}

        def interrupted(db_session, record, token, progress):
            calls['count'] += 1
            if calls['count'] == 3:
                raise RuntimeError('simulated interruption')
            return original(db_session, record, token, progress)

        handover._save_progress = interrupted
        try:
            with self.assertRaises(Exception):
                handover.accept_import(db, row.id, admin)
        finally:
            handover._save_progress = original
        failed = db.get(type(row), row.id)
        db.refresh(failed)
        self.assertEqual(failed.status, 'failed')
        self.assertTrue(failed.progress['deliveries']['done'] < failed.progress['deliveries']['total'])
        handover.accept_import(db, row.id, admin)
        self.assertEqual(db.scalar(select(func.count()).select_from(Dataset)), 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(Job)), 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(Report)), 1)
        self.assertEqual(db.scalar(select(func.count()).select_from(Delivery)), 1)

    def test_archives_are_byte_identical_and_plans_stopped(self):
        import hashlib
        import os

        from src.config.database import Session
        from src.models import Delivery, Report
        from src.services import handover
        from src.services.handover import visible_condition
        from sqlalchemy import select

        db, admin, _ = fresh_standby('bytes')
        row = handover.register_upload(db, self.package, 'package.zip', admin.email)
        handover.accept_import(db, row.id, admin)
        report = db.scalar(select(Report).where(visible_condition(Report)))
        self.assertFalse(report.enabled)
        delivery = db.scalar(select(Delivery).where(visible_condition(Delivery)))
        with open(os.path.join(os.environ['ARTIFACT_DIR'], delivery.filename), 'rb') as handle:
            saved = handle.read()
        self.assertEqual(hashlib.sha256(saved).hexdigest(), delivery.checksum)

    def test_name_conflict_keeps_both_sides_and_maps_origins(self):
        from src.config.database import Session
        from src.models import Dataset
        from src.services import handover
        from sqlalchemy import select

        db, admin, _ = fresh_standby('conflict', dataset_name='海棠融媒_三链路120日播出日记.csv')
        row = handover.register_upload(db, self.package, 'package.zip', admin.email)
        conflicts = [check for check in row.verification['checks'] if check['key'] == 'conflicts'][0]
        self.assertEqual(conflicts['status'], 'warning')
        detail = handover.import_detail(db, handover.accept_import(db, row.id, admin))
        names = sorted(dataset.name for dataset in db.scalars(select(Dataset)))
        self.assertEqual(len(names), 2)
        self.assertTrue(any('交接' in name for name in names))
        mapped = {(entry['entity_type'], entry['origin_id']): entry['local_id']
                  for entry in detail['mappings']}
        self.assertEqual(len(mapped), 4)
        self.assertTrue(all(entry['origin_id'] and entry['local_id'] for entry in detail['mappings']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
