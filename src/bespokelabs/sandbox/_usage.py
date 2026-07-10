"""Parse agent CLI output into a normalized :class:`Usage`.

Today this only understands Claude Code's JSON output — both
``--output-format json`` (a single JSON object) and ``--output-format
stream-json`` (newline-delimited JSON whose final ``type == "result"`` record
carries the run totals).  A Codex parser can drop in here as a sibling later;
``Sandbox.run_agent`` is the single caller.
"""

from __future__ import annotations

import json

from bespokelabs.sandbox.types import Usage


def parse_claude_result(stdout: str) -> dict | None:
    """Return Claude Code's final result record, or None if not found.

    Never raises on malformed/partial output — unparseable lines are skipped.
    """
    text = stdout.strip()
    if not text:
        return None

    # Fast path: the whole payload is one JSON value (`--output-format json`,
    # or a JSON array of stream records).
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        obj = None
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, list):
        for item in reversed(obj):
            if isinstance(item, dict) and _looks_like_result(item):
                return item
        return None

    # stream-json: scan NDJSON lines and keep the last result record.
    result: dict | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(record, dict) and _looks_like_result(record):
            result = record
    return result


def usage_from_result(record: dict | None) -> Usage | None:
    """Build a :class:`Usage` from a parsed result record.

    Returns None when the record carries no token usage and no cost — i.e.
    there is nothing to attribute.
    """
    if record is None:
        return None
    usage = record.get("usage") if isinstance(record.get("usage"), dict) else None
    if usage is None and "total_cost_usd" not in record:
        return None
    usage = usage or {}
    return Usage(
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
        cache_creation_tokens=_int(usage.get("cache_creation_input_tokens")),
        llm_cost_usd=_float(record.get("total_cost_usd")),
    )


def parse_claude_usage(stdout: str) -> Usage | None:
    """Extract token usage and cost from Claude Code JSON output."""
    return usage_from_result(parse_claude_result(stdout))


def result_text(record: dict | None) -> str | None:
    """Return the assistant's text answer from a result record, if present."""
    if isinstance(record, dict) and isinstance(record.get("result"), str):
        return record["result"]
    return None


def _looks_like_result(record: dict) -> bool:
    return record.get("type") == "result" or "total_cost_usd" in record or "usage" in record


def _int(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _float(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
