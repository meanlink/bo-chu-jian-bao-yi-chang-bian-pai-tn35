import logging

from sqlalchemy import inspect, text

from src.config.database import Base, engine
from src import models


def _ensure_columns():
    """Add handover visibility columns to pre-existing demo databases."""
    wanted = {
        'datasets': {'handover_import_id': 'INTEGER NULL'},
        'jobs': {'handover_import_id': 'INTEGER NULL'},
        'reports': {'handover_import_id': 'INTEGER NULL'},
        'deliveries': {'handover_import_id': 'INTEGER NULL'},
    }
    inspector = inspect(engine)
    if not inspector.get_table_names():
        return
    with engine.begin() as connection:
        for table, columns in wanted.items():
            if table not in inspector.get_table_names():
                continue
            existing = {column['name'] for column in inspector.get_columns(table)}
            for name, kind in columns.items():
                if name in existing:
                    continue
                try:
                    if engine.dialect.name == 'sqlite':
                        connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {kind}'))
                    else:
                        connection.execute(text(f'ALTER TABLE `{table}` ADD COLUMN `{name}` {kind}'))
                except Exception:
                    logging.exception('Could not add %s.%s; create_all will retry on a fresh volume',
                                      table, name)


def migrate():
    Base.metadata.create_all(engine)
    _ensure_columns()


if __name__ == '__main__':
    migrate()
