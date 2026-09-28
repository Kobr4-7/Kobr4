"""Mise à jour du site depuis les réglages (administrateur).

Le site ne peut pas se reconstruire lui-même depuis son conteneur : il dépose une
demande (`request`) dans un dossier partagé avec le serveur, que deploy/auto-update.sh
(lancé chaque minute par cron) traite. Le script y publie en retour la version
installée (`version`), les nouveautés disponibles (`pending`) et l'état de la dernière
mise à jour (`state`, `state-at`, `log`).
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from kobr4.web.deps import Current, DbSession, State, audit
from kobr4.web.routes.account import _require_mfa

router = APIRouter(prefix="/api/admin", tags=["administration"])

STALE_REQUEST = timedelta(minutes=5)
"""Une demande non prise en charge après ce délai signale un script absent de cron."""


def _read(d: Path, name: str) -> str:
    try:
        return (d / name).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _require_admin(a: Current) -> None:
    if not a.user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "réservé à l'administrateur du site")


def _status(d: Path | None) -> dict[str, Any]:
    if d is None or not d.is_dir():
        return {"available": False}
    pending = []
    for line in _read(d, "pending").splitlines():
        sha, _, subject = line.partition(" ")
        if sha:
            pending.append({"hash": sha, "subject": subject})
    requested_at = None
    req = d / "request"
    if req.exists():
        requested_at = datetime.fromtimestamp(req.stat().st_mtime, UTC)
    log = _read(d, "log").splitlines()
    return {
        "available": True,
        "version": _read(d, "version") or None,
        "pending": pending,
        "state": _read(d, "state") or "idle",
        "state_at": _read(d, "state-at") or None,
        "requested_at": requested_at.isoformat() if requested_at else None,
        "request_stale": requested_at is not None
        and datetime.now(UTC) - requested_at > STALE_REQUEST,
        "log": log[-15:],
    }


@router.get("/update")
async def update_status(st: State, a: Current) -> dict[str, Any]:
    _require_admin(a)
    return _status(st.config.control_dir)


@router.post("/update", status_code=202)
async def request_update(request: Request, st: State, s: DbSession, a: Current) -> dict[str, Any]:
    _require_admin(a)
    _require_mfa(a)
    d = st.config.control_dir
    if d is None or not d.is_dir():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "mise à jour depuis le site non configurée sur ce serveur (voir deploy/README.md)",
        )
    if _read(d, "state") == "running":
        raise HTTPException(status.HTTP_409_CONFLICT, "une mise à jour est déjà en cours")
    try:
        (d / "request").write_text(
            f"{datetime.now(UTC).isoformat()} {a.user.email}\n", encoding="utf-8"
        )
    except OSError as e:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "impossible de déposer la demande : sur le serveur, lancer "
            "`sudo chown -R kobr4:kobr4 ~/Kobr4/deploy/control && "
            "chmod 777 ~/Kobr4/deploy/control`",
        ) from e
    await audit(s, request, a.user.id, "update_requested")
    await s.commit()
    return _status(d)
