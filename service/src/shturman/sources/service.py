"""Load service-owned connector credentials without exposing them through the archive API."""
from contextlib import asynccontextmanager

from .registry import load_registry


@asynccontextmanager
async def lifespan(state):
    registry = load_registry(getattr(state.config, "sources_file", None))
    state.extras["source_registry"] = registry
    try:
        yield
    finally:
        state.extras.pop("source_registry", None)
