"""Service-owned, bounded access to memory and external read-only sources."""

from .broker import (canonical_source_spec, disclosure_allowed, grant_for_task,
                     read_for_task, refs_valid, resolve_request, validate_refs)

__all__ = ["canonical_source_spec", "disclosure_allowed", "grant_for_task", "read_for_task",
           "refs_valid", "resolve_request", "validate_refs"]
