from sqlalchemy import String, JSON, Double, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class HandoverImport(Base):
    """A received handover package and its verification/import checkpoint."""

    __tablename__ = 'handover_imports'
    id: Mapped[int] = mapped_column(primary_key=True)
    package_checksum: Mapped[str] = mapped_column(String(64), unique=True)
    filename: Mapped[str] = mapped_column(String(255), default='')
    origin_site: Mapped[str] = mapped_column(String(160), default='')
    status: Mapped[str] = mapped_column(String(30), default='pending_verification')
    phase: Mapped[str] = mapped_column(String(30), default='pending')
    verification: Mapped[dict] = mapped_column(JSON, default=dict)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    total_items: Mapped[int] = mapped_column(Integer, default=0)
    processed_items: Mapped[int] = mapped_column(Integer, default=0)
    package_path: Mapped[str] = mapped_column(String(500), default='')
    created_by: Mapped[str] = mapped_column(String(150), default='')
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)
    verified_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    accepted_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[float] = mapped_column(Double, default=0)
    last_error: Mapped[str] = mapped_column(String(1000), default='')


class HandoverMapping(Base):
    """Origin -> local identity map. It is the resume checkpoint and audit trail."""

    __tablename__ = 'handover_mappings'
    __table_args__ = (
        UniqueConstraint('import_id', 'entity_type', 'origin_id', name='uq_handover_mapping'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    import_id: Mapped[int] = mapped_column(Integer)
    entity_type: Mapped[str] = mapped_column(String(20))
    origin_id: Mapped[str] = mapped_column(String(80))
    origin_name: Mapped[str] = mapped_column(String(200), default='')
    local_id: Mapped[int] = mapped_column(Integer)
    local_name: Mapped[str] = mapped_column(String(200), default='')
    created_at: Mapped[float] = mapped_column(Double)
