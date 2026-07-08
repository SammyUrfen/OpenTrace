"""Local secret store.

A small, swappable interface for storing the kinds of values that should not
live in `config.json` (API keys, signed tokens, …). Phase 0 implementation is
file-based: each secret is a single file under `~/.opentrace/secrets/` with
the file at mode 0600 and the directory at 0700.

This is deliberately the same shape we'd keep when switching to an OS
keychain backend later (libsecret / secretstorage / Windows credential
manager). The implementation can swap; callers don't change.

Public surface (stable):
- `get_secret(name) -> str | None`
- `set_secret(name, value) -> None`
- `delete_secret(name) -> bool`
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import paths


_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _secrets_dir() -> Path:
    d = paths.home() / "secrets"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        # Re-assert mode in case the directory pre-existed with looser perms.
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _path_for(name: str) -> Path:
    if not _SAFE_NAME.match(name):
        raise ValueError(f"invalid secret name: {name!r}")
    return _secrets_dir() / name


def get_secret(name: str) -> str | None:
    p = _path_for(name)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def set_secret(name: str, value: str) -> None:
    p = _path_for(name)
    # Write via a temp file + atomic rename so we never expose a partial value.
    tmp = p.with_suffix(p.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, p)
    os.chmod(p, 0o600)


def delete_secret(name: str) -> bool:
    p = _path_for(name)
    if p.exists():
        p.unlink()
        return True
    return False
