"""LLM client + configuration for AI run summaries.

Talks to any OpenAI-compatible chat-completions endpoint (the default is Google's
`https://generativelanguage.googleapis.com/v1beta/openai`). The API key lives in
the secret store — never in `config.json`.

Some models (e.g. Gemma "thinking" variants) stream interleaved reasoning chunks
flagged `extra_content.google.thought`; we surface those as `{"type":"thinking"}`
status (so the UI can show progress) and only emit the real answer as
`{"type":"content"}`, stripping any stray `<thought>` tags.

Public surface:
- `is_configured()`, `get_api_key()`
- `stream_chat(messages, ...) -> AsyncIterator[dict]`  events: thinking/content/done/error
- `test_connection()` — fast auth/model check via the models list
- `router` — FastAPI APIRouter at `/config/llm`
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx
from fastapi import APIRouter
from pydantic import BaseModel

from . import config, secrets

log = logging.getLogger(__name__)

_MAX_TOKENS = 2048


def get_api_key(cfg: config.Config | None = None) -> str | None:
    cfg = cfg or config.load()
    return secrets.get_secret(cfg.llm.api_key_secret_name)


def is_configured(cfg: config.Config | None = None) -> bool:
    cfg = cfg or config.load()
    return bool(cfg.llm.base_url and cfg.llm.model and get_api_key(cfg))


def _strip_thought_tags(s: str) -> str:
    return s.replace("<thought>", "").replace("</thought>", "")


async def stream_chat(
    messages: list[dict],
    *,
    max_tokens: int = _MAX_TOKENS,
    cfg: config.Config | None = None,
) -> AsyncIterator[dict]:
    """Stream a chat completion as a sequence of typed events."""
    cfg = cfg or config.load()
    key = get_api_key(cfg)
    if not (cfg.llm.base_url and cfg.llm.model and key):
        yield {"type": "error", "message": "LLM is not configured"}
        return

    url = cfg.llm.base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": cfg.llm.model,
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=15.0)) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", "replace")
                    yield {"type": "error",
                           "message": f"HTTP {resp.status_code}: {detail[:300]}"}
                    return
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choice = (obj.get("choices") or [{}])[0]
                    delta = choice.get("delta", {})
                    content = delta.get("content")
                    if not content:
                        continue
                    thought = bool(
                        delta.get("extra_content", {}).get("google", {}).get("thought")
                    )
                    if thought:
                        yield {"type": "thinking"}
                    else:
                        text = _strip_thought_tags(content)
                        if text:
                            yield {"type": "content", "text": text}
        yield {"type": "done"}
    except httpx.HTTPError as e:
        yield {"type": "error", "message": f"request failed: {e}"}
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "message": str(e)}


async def test_connection(cfg: config.Config | None = None) -> dict:
    """Fast check: list models with the configured key/base and confirm the
    configured model exists (no generation, so it returns in well under a second)."""
    cfg = cfg or config.load()
    key = get_api_key(cfg)
    if not (cfg.llm.base_url and key):
        return {"ok": False, "error": "base URL and API key are required"}
    url = cfg.llm.base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {key}"})
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
        names = [m.get("id") or m.get("name", "") for m in data.get("data", [])]
        model_ok = not cfg.llm.model or any(
            cfg.llm.model in n for n in names
        )
        return {
            "ok": True,
            "model": cfg.llm.model,
            "model_available": model_ok,
            "models_count": len(names),
        }
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"request failed: {e}"}


# --- HTTP: /config/llm ------------------------------------------------------

router = APIRouter(prefix="/config/llm", tags=["llm"])


class LLMSettings(BaseModel):
    base_url: str | None = None
    model: str | None = None
    configured: bool = False
    has_key: bool = False
    continuous_summaries: bool = False


class LLMUpdate(BaseModel):
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None  # write-only; stored in the secret store
    continuous_summaries: bool | None = None


def _settings_view(cfg: config.Config) -> LLMSettings:
    return LLMSettings(
        base_url=cfg.llm.base_url,
        model=cfg.llm.model,
        configured=is_configured(cfg),
        has_key=bool(get_api_key(cfg)),
        continuous_summaries=cfg.llm.continuous_summaries,
    )


@router.get("", response_model=LLMSettings)
def http_get() -> LLMSettings:
    return _settings_view(config.load())


@router.put("", response_model=LLMSettings)
def http_put(data: LLMUpdate) -> LLMSettings:
    cfg = config.load()
    if data.base_url is not None:
        new_base = data.base_url.strip() or None
        # The stored key is bound to the base_url it was entered for: changing
        # the base without re-entering the key drops the secret, so a redirected
        # base_url can never receive a key stored for the old host.
        if new_base != cfg.llm.base_url and not data.api_key:
            secrets.delete_secret(cfg.llm.api_key_secret_name)
        cfg.llm.base_url = new_base
    if data.model is not None:
        cfg.llm.model = data.model.strip() or None
    if data.continuous_summaries is not None:
        cfg.llm.continuous_summaries = data.continuous_summaries
    config.save(cfg)
    if data.api_key:  # only set when a non-empty key is provided
        secrets.set_secret(cfg.llm.api_key_secret_name, data.api_key.strip())
    return _settings_view(config.load())


@router.post("/test")
async def http_test() -> dict:
    return await test_connection(config.load())
