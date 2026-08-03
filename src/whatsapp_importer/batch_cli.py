from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .batch import flush_all, flush_stream, stage_event
from .ingest import IngestError


def _root() -> Path:
    configured = os.environ.get("IMPORTER_ROOT")
    return Path(configured) if configured else Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fila persistente de entrada WhatsApp")
    parser.add_argument("operation", choices=("stage", "flush"))
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        if args.operation == "stage":
            result = stage_event(_root(), payload)
        elif payload.get("all") is True:
            result = flush_all(_root())
        else:
            result = flush_stream(
                _root(),
                str(payload.get("chat_id") or ""),
                str(payload.get("sender_id") or ""),
            )
    except (IngestError, json.JSONDecodeError, OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
