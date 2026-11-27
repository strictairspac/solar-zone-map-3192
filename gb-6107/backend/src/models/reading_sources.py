"""Compatibility model module for reading-source/failover tests."""
from .source_controls import reading_source_controls

reading_sources = reading_source_controls

__all__ = ["reading_source_controls", "reading_sources"]
