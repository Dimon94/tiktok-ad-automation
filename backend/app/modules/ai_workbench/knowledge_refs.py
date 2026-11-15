"""Versioned, sanitized knowledge references used by DSH review evidence.

The review loop stores only references and public metadata.  Credentials and model
internal reasoning are deliberately removed before any JSON is persisted or
returned to an API caller.
"""

from __future__ import annotations

import base64
import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID

_SENSITIVE = {
    "token",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "credential",
    "cookie",
    "authorization",
    "api_key",
    "apikey",
    "private_key",
    "chain_of_thought",
    "reasoning",
    "thoughts",
    "model_trace",
    "internal_reasoning",
    "prompt_tokens",
    "completion_tokens",
}

_SENSITIVE_VARIANTS = re.compile(
    r"(?:credential|password|passwd|passphrase|reasoning|chain[_-]?of[_-]?thought|"
    r"model[_-]?trace|internal[_-]?reasoning|thoughts?|secret|token|"
    r"api[_-]?key|private[_-]?key)",
    re.IGNORECASE,
)


def _sensitive_key(key: str) -> bool:
    """Match secret/trace field variants without blocking safe scope fields."""
    lowered = key.lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    if lowered in _SENSITIVE or normalized in _SENSITIVE:
        return True
    if any(
        normalized.endswith(suffix)
        for suffix in ("_token", "_secret", "_password", "_credential", "_trace")
    ):
        return True
    return _SENSITIVE_VARIANTS.search(normalized) is not None


def sanitize_public(value: Any) -> Any:
    """Return JSON-safe evidence with secret and model-trace fields removed."""
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            # Match secret field names and credential/reasoning variants.  Safe
            # optimizer fields such as ``authorization_limits`` remain public.
            if _sensitive_key(key_text):
                continue
            clean[key_text] = sanitize_public(item)
        return clean
    if isinstance(value, (list, tuple, set)):
        return [sanitize_public(item) for item in value]
    if hasattr(value, "model_dump"):
        return sanitize_public(value.model_dump(mode="json"))
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite float cannot be serialized as public evidence")
    if hasattr(value, "isoformat") and not isinstance(value, (str, bytes)):
        return value.isoformat()
    # Evidence is persisted in JSONB.  Unknown scalar objects must never make
    # the persistence boundary fail after a DSH task has completed.
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def versioned_reference(
    *,
    key: str,
    version: str | int,
    source: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the stable public shape used inside frozen review evidence."""
    return {
        "key": key,
        "version": str(version),
        "source": source,
        "metadata": sanitize_public(dict(metadata or {})),
    }


def snapshot_knowledge_refs(
    refs: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Freeze caller-provided knowledge references without exposing private data."""
    result: list[dict[str, Any]] = []
    for ref in refs or ():
        if not ref.get("key") or ref.get("version") is None:
            continue
        result.append(
            versioned_reference(
                key=str(ref["key"]),
                version=ref["version"],
                source=str(ref.get("source", "DSH")),
                metadata=ref.get("metadata")
                if isinstance(ref.get("metadata"), Mapping)
                else {},
            )
        )
    return result
