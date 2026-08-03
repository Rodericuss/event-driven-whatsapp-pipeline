from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .marketplace import (
    MarketplaceAPIError,
    MarketplaceContractError,
    execute_marketplace_live,
    prepare_marketplace_request,
    submit_marketplace_dry_run,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepara o contrato Phoenix em DRY_RUN")
    parser.add_argument("--import-id", required=True)
    parser.add_argument(
        "--send-dry-run",
        action="store_true",
        help="valida o pacote na API local sem criar produto nem enviar imagens",
    )
    parser.add_argument(
        "--execute-live",
        action="store_true",
        help="cria o produto, envia imagens e aplica a visibilidade configurada ao finalizar",
    )
    parser.add_argument(
        "--approval",
        help="deve corresponder à visibilidade configurada para o import-id",
    )
    args = parser.parse_args(argv)
    root = Path(os.environ.get("IMPORTER_ROOT") or Path(__file__).resolve().parents[2])
    try:
        if args.send_dry_run and args.execute_live:
            raise MarketplaceContractError("Escolha somente um modo de execução.")
        if args.execute_live:
            result = execute_marketplace_live(
                root,
                args.import_id,
                approval=args.approval or "",
            )
        elif args.send_dry_run:
            result = submit_marketplace_dry_run(root, args.import_id)
        else:
            result = prepare_marketplace_request(root, args.import_id)
    except (MarketplaceAPIError, MarketplaceContractError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error), "dry_run": True}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
