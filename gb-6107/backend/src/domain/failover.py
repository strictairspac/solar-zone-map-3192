"""Compatibility module name for failover acceptance tests."""
from .reading_failover import *  # noqa: F401,F403
from .reading_failover import (  # noqa: F401
    PRIMARY, STANDBY, BACKUP, REPLICA, DictReadingSource, FetchedPage,
    PageNotArrived, PrimaryDisconnected, ReadingInterrupted, RequiredPagesMissing,
    SnapshotMismatch, SourceDisconnected, advance_one, build_receipt, build_snapshot,
    execute, initial_progress, normalize_page_numbers, normalize_source,
    restore_progress, run_until_blocked, source_label,
)
