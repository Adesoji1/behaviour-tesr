"""
Model registry — versioning, status, and rollback (governance §12, MLOps §6).

Every trained ensemble gets a unique version and a manifest recording: training timestamp,
data window, feature version, hardware, validation metrics, and deployment status. Never
overwrite the active model — deploy only after validation, and keep the previous known-good so
we can roll back. All state is a simple JSON index under artifacts/registry/ (no extra infra).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import config

log = logging.getLogger("ml.registry")
INDEX = config.DIR_REGISTRY / "index.json"


def _load() -> dict:
    return json.loads(INDEX.read_text()) if INDEX.exists() else {"models": [], "active": None}


def _save(idx: dict) -> None:
    INDEX.write_text(json.dumps(idx, indent=2))


def new_version() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y.%m.%d-%H%M%S")
    return f"{config.MODEL_FAMILY}-{ts}"


def register(version: str, manifest: dict) -> None:
    idx = _load()
    manifest = {**manifest, "version": version,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "candidate"}
    idx["models"] = [m for m in idx["models"] if m["version"] != version] + [manifest]
    _save(idx)
    log.info("registry: registered %s (candidate)", version)


def model_dir(version: str) -> Path:
    return config.DIR_MODELS / version


def promote(version: str) -> None:
    """Make a validated candidate the active production model, retaining the previous one."""
    idx = _load()
    prev = idx.get("active")
    for m in idx["models"]:
        m["status"] = ("active" if m["version"] == version
                       else "previous" if m["version"] == prev else m.get("status", "candidate"))
    idx["previous_active"] = prev
    idx["active"] = version
    _save(idx)
    log.info("registry: promoted %s to ACTIVE (previous=%s)", version, prev)


def rollback() -> str | None:
    """Restore the previous known-good model as active."""
    idx = _load()
    prev = idx.get("previous_active")
    if not prev:
        log.warning("registry: nothing to roll back to")
        return None
    cur = idx.get("active")
    promote(prev)
    idx = _load()
    idx["previous_active"] = cur
    _save(idx)
    log.info("registry: rolled back to %s", prev)
    return prev


def active() -> str | None:
    return _load().get("active")


def get(version: str) -> dict | None:
    return next((m for m in _load()["models"] if m["version"] == version), None)


def list_models() -> list:
    return _load()["models"]
