"""Config schema and persistence.

Config lives at `paths.config_file()` as JSON. Selected values can be
overridden at runtime by environment variables — overrides apply to the
in-memory `Config` returned by `load()` but are never persisted back to disk.

Public surface (stable):
- `Config`, `LLMConfig` — pydantic models
- `load() -> Config`
- `save(cfg: Config) -> None`
"""
from __future__ import annotations

import logging
import os

from pydantic import BaseModel, Field

from . import paths

log = logging.getLogger(__name__)


class LLMConfig(BaseModel):
    base_url: str | None = None
    model: str | None = None
    api_key_secret_name: str = "llm_api_key"
    # monitor mode: auto-explain each detected incident with a short AI note.
    continuous_summaries: bool = False


class UIConfig(BaseModel):
    theme: str = "auto"  # 'auto' | 'dark' | 'light'
    sidebar_width: int = 280


class TerminalConfig(BaseModel):
    font_size: int = 13
    scrollback: int = 5000


class CollectorsConfig(BaseModel):
    """Which collectors run for a traced command. strace and psutil are
    functional today; ltrace/perf are reserved for Phase 6 (opt-in profiling)."""
    strace: bool = True   # syscalls / I/O / network / processes / logs
    psutil: bool = True   # CPU / memory / FDs / threads
    ltrace: bool = False  # library + malloc/free calls (Phase 6)
    perf: bool = False    # hardware counters / flamegraph (Phase 6)


class TracingConfig(BaseModel):
    default_enabled: bool = False
    collectors: CollectorsConfig = Field(default_factory=CollectorsConfig)


class Config(BaseModel):
    version: int = 1
    llm: LLMConfig = Field(default_factory=LLMConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    terminal: TerminalConfig = Field(default_factory=TerminalConfig)
    tracing: TracingConfig = Field(default_factory=TracingConfig)


_ENV_OVERRIDES = {
    "OPENTRACE_LLM_BASE_URL": ("llm", "base_url"),
    "OPENTRACE_LLM_MODEL": ("llm", "model"),
}


def _apply_env_overrides(cfg: Config) -> Config:
    for env_var, (section, key) in _ENV_OVERRIDES.items():
        value = os.environ.get(env_var)
        if value:
            setattr(getattr(cfg, section), key, value)
    return cfg


def load() -> Config:
    """Read config from disk, creating defaults on first run.

    Env overrides are applied after disk read and are not saved back.
    """
    path = paths.config_file()
    if path.exists():
        cfg = Config.model_validate_json(path.read_text())
    else:
        cfg = Config()
        save(cfg)
        log.info("created default config at %s", path)
    return _apply_env_overrides(cfg)


def save(cfg: Config) -> None:
    """Persist config to disk. Caller is responsible for not saving a
    config with env-override values in it (load a fresh one if unsure)."""
    paths.ensure_dirs()
    paths.config_file().write_text(cfg.model_dump_json(indent=2))
