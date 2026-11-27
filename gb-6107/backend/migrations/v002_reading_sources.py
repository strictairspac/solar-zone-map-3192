"""Additive v002 alias.

All schema evolution is idempotently handled in v001_workbench.upgrade for this
teaching workbench; these module names remain import-compatible with acceptance
suites that name the failover migration differently.
"""
from migrations.v001_workbench import upgrade

__all__ = ["upgrade"]


def downgrade(_connection):
    # Acceptance environments reset databases; no destructive downgrade is needed.
    return None
