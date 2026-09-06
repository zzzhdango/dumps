"""Offline, dependency-free validation used before stopping the deployed bot."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from state_schema import (
    CURRENT_PROVIDER,
    STATE_SCHEMA_VERSION,
    StateSchemaError,
    validate_current_state_data,
)


class PreflightError(ValueError):
    pass


def validate_state_file(path: str) -> None:
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(f"{source}: unreadable or invalid JSON") from exc
    if not isinstance(data, dict):
        raise PreflightError("state root must be an object")
    current = (
        data.get("schema_version") == STATE_SCHEMA_VERSION
        and data.get("provider") == CURRENT_PROVIDER
    )
    if not current:
        # Legacy and unknown schemas are migrated entry-by-entry by SignalStore.
        return
    try:
        validate_current_state_data(data)
    except StateSchemaError as exc:
        raise PreflightError(str(exc)) from exc


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: state_preflight.py STATE_FILE", file=sys.stderr)
        return 2
    try:
        validate_state_file(argv[1])
    except PreflightError as exc:
        print(f"state validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
