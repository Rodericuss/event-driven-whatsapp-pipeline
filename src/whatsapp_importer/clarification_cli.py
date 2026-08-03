from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .clarification import ClarificationError, handle_clarification_event


def _root() -> Path:
    configured = os.environ.get("IMPORTER_ROOT")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2]


def main() -> int:
    try:
        event = json.load(sys.stdin)
        result = handle_clarification_event(_root(), event)
    except (ClarificationError, json.JSONDecodeError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
