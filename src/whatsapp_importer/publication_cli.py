from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .publication import PublicationError, publish_to_personal_chat


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publica um teste somente no chat pessoal autorizado"
    )
    parser.add_argument("--import-id", required=True)
    parser.add_argument("--approval", required=True)
    args = parser.parse_args(argv)
    root = Path(os.environ.get("IMPORTER_ROOT") or Path(__file__).resolve().parents[2])

    try:
        result = publish_to_personal_chat(
            root,
            args.import_id,
            approval=args.approval,
        )
    except (PublicationError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2

    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
