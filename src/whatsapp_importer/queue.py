from __future__ import annotations

import sqlite3
import os
import shutil
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def enqueue(root: Path, import_id: str) -> bool:
    db = root / "anuncios" / "queue.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS jobs (import_id TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL)")
        row = conn.execute("SELECT status FROM jobs WHERE import_id=?", (import_id,)).fetchone()
        if row and row[0] in {"queued", "running"}:
            return False
        conn.execute("INSERT OR REPLACE INTO jobs VALUES (?, 'queued', ?)", (import_id, _now()))
    return True


def mark(root: Path, import_id: str, status: str) -> None:
    db = root / "anuncios" / "queue.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE jobs SET status=?, updated_at=? WHERE import_id=?", (status, _now(), import_id))


@contextmanager
def publication_lock(root: Path):
    """Serialize final group deliveries while allowing candidates to process in parallel."""
    lock_path = root / "anuncios" / "recebendo" / ".publication.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def approved_for_live(status: dict[str, Any], *, shadow_mode: bool = False) -> bool:
    """Allow validated listings to continue, except while running a shadow test."""
    return (
        status.get("status") == "ready_for_review"
        and status.get("validated") is True
        and (
            not shadow_mode
            or status.get("publication_confirmed") is True
        )
    )


def requires_publication_without_site(listing: dict[str, Any]) -> bool:
    return (
        listing.get("category") == "maquinas"
        and listing.get("type") == "Confirmar com o vendedor"
        and listing.get("seller_confirmation_required") is True
    )


def resolve_fly_binary(*, home: Path | None = None) -> str:
    configured = os.environ.get("FLY_BIN")
    candidates = [
        Path(configured).expanduser() if configured else None,
        (home or Path.home()) / ".fly" / "bin" / "fly",
    ]
    discovered = shutil.which("fly")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate and candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    raise RuntimeError(
        "Fly CLI não encontrado; configure FLY_BIN ou instale em ~/.fly/bin/fly."
    )
