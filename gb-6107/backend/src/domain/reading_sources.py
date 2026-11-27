"""Compatibility name for tests looking for a reading-source adapter module."""
from .reading_failover import *  # noqa: F401,F403
from .reading_failover import (  # noqa: F401
    PRIMARY, STANDBY, DictReadingSource as ReadingSource,
    DictReadingSource, SourceDisconnected, PageNotArrived,
)
