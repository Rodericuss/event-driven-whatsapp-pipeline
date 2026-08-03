from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .process import ProcessError, process_listing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extrai e valida um anúncio em DRY_RUN")
    parser.add_argument("--import-id", required=True)
    args = parser.parse_args(argv)
    root = Path(os.environ.get("IMPORTER_ROOT") or Path(__file__).resolve().parents[2])
    try:
        result = process_listing(root, args.import_id)
    except (ProcessError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error), "dry_run": True}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
