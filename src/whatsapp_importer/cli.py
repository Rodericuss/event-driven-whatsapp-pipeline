from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .ingest import IngestError, ingest_event


def _root() -> Path:
    configured = os.environ.get("IMPORTER_ROOT")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Captura um evento WhatsApp em DRY_RUN")
    parser.add_argument(
        "--stdin-json",
        action="store_true",
        help="lê exatamente um objeto JSON de stdin",
    )
    args = parser.parse_args(argv)
    if not args.stdin_json:
        parser.error("--stdin-json é obrigatório")

    try:
        event = json.load(sys.stdin)
        result = ingest_event(_root(), event)
    except (IngestError, json.JSONDecodeError, OSError) as error:
        print(
            json.dumps(
                {"ok": False, "error": str(error), "dry_run": True},
                ensure_ascii=False,
            )
        )
        return 2

    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

