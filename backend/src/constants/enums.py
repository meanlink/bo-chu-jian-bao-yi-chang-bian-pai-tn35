from enum import Enum


class Role(str, Enum):
    admin = 'admin'
    analyst = 'analyst'
    viewer = 'viewer'


class JobStatus(str, Enum):
    queued = 'queued'
    running = 'running'
    completed = 'completed'
    failed = 'failed'


class HandoverStatus(str, Enum):
    pending_verification = 'pending_verification'
    rejected = 'rejected'
    verified = 'verified'
    importing = 'importing'
    received = 'received'
    failed = 'failed'
