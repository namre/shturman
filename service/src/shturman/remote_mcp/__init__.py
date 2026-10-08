"""Опциональный внешний read-only MCP с OAuth и независимым решением в Telegram."""
from .gateway import Gateway, lifespan

__all__ = ["Gateway", "lifespan"]
