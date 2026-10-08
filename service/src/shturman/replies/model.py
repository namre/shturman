"""Bounded completion output. Parsing proposes work; it never authorizes it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..sanitize import clean_line

OUTCOMES = frozenset({'reply', 'need_source', 'ask_owner', 'decline'})
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['outcome'],
    'properties': {
        'outcome': {'type': 'string', 'enum': sorted(OUTCOMES)},
        'text': {'type': 'string'}, 'question': {'type': 'string'},
        'source_keys': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 20},
        'request': {
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'kind': {'type': 'string', 'enum': ['chat', 'memory', 'external']},
                'source_id': {'type': 'string'}, 'query': {'type': 'string'},
                'since': {'type': 'string'}, 'until': {'type': 'string'},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10},
                'max_chars': {'type': 'integer', 'minimum': 1, 'maximum': 12000},
                'reason': {'type': 'string'},
            }, 'required': ['kind', 'reason'],
        },
    },
}


@dataclass(frozen=True)
class Outcome:
    kind: str
    text: str = ''
    question: str = ''
    source_keys: tuple[str, ...] = ()
    request: dict[str, Any] | None = None


def source_key(ref: dict[str, Any]) -> str:
    receipt = ref.get('receipt_id')
    return f'r:{receipt}' if receipt is not None else f'm:{ref.get("message_id")}'


def parse(raw: Any, refs: list[dict[str, Any]], *, max_chars: int = 4000) -> Outcome:
    if not isinstance(raw, dict) or not isinstance(raw.get('outcome'), str) or raw.get('outcome') not in OUTCOMES:
        raise ValueError('bad_outcome')
    if set(raw) - {'outcome', 'text', 'question', 'source_keys', 'request'}:
        raise ValueError('unexpected_field')
    keys = raw.get('source_keys', [])
    offered = {source_key(ref) for ref in refs}
    if not isinstance(keys, list) or len(keys) > 20 or any(
            not isinstance(key, str) or key not in offered for key in keys):
        raise ValueError('unoffered_source')
    kind = raw['outcome']
    text = raw.get('text', '')
    question = raw.get('question', '')
    if not isinstance(text, str) or not isinstance(question, str):
        raise ValueError('bad_text')
    if len(text) > max_chars or len(question) > 1000:
        raise ValueError('text_too_long')
    if kind == 'reply' and not text.strip():
        raise ValueError('empty_reply')
    if kind == 'ask_owner' and not question.strip():
        raise ValueError('empty_question')
    request = raw.get('request')
    if kind == 'need_source':
        if not isinstance(request, dict):
            raise ValueError('bounded_source_required')
        # The broker performs configured-source validation and canonical bounds checks.
        from ..sources.broker import canonical_source_spec
        from ..sources.registry import SourceError
        try:
            request = canonical_source_spec(request)
        except SourceError as exc:
            raise ValueError("invalid_source_request") from exc
    elif request is not None:
        raise ValueError('unexpected_request')
    return Outcome(kind, text.strip(), clean_line(question, 1000), tuple(dict.fromkeys(keys)), request)
