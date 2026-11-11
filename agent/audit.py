"""Append-only JSON Lines audit log."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path | str | None = None) -> None:
        configured_path = path or os.getenv("AGENT_AUDIT_LOG")
        self.path = (
            Path(configured_path).expanduser()
            if configured_path
            else Path.home() / ".ai-file-agent" / "audit.jsonl"
        )

    def record(self, **fields: Any) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as log:
                log.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            raise RuntimeError(f"Could not write audit log at {self.path}: {exc}") from exc
