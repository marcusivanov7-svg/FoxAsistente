# -*- coding: utf-8 -*-
"""providers.py — Capa unificada de proveedores de IA para Fox.

Un único punto de entrada para hablar con Gemini, OpenRouter, DeepSeek, Groq,
OpenAI, Anthropic y Ollama local. Reemplaza la lógica dispersa de
`specialist_llm.py` y `llm_client.py` con una interfaz común:

    - ``stream_chat()`` — generador de eventos (delta / thinking / tool_calls / done)
    - ``chat()``        — versión no-streaming que devuelve {content, thinking, tool_calls, usage}

Cada proveedor declara su *api_style* (openai | gemini | anthropic) y qué niveles
de "capacidad de pensamiento" soporta (off/low/medium/high/max).

Formato canónico de herramientas (interno): estilo OpenAI
    {"type": "function", "function": {"name", "description", "parameters": {json-schema minúsculas}}}

Formato canónico de mensajes (interno): estilo OpenAI
    {"role": system|user|assistant|tool, "content": str,
     "tool_calls": [{id, type:"function", function:{name, arguments:"<json string>"}}],
     "tool_call_id": str}   # solo role=tool

Los adaptadores traducen este formato canónico a Gemini/Anthropic y viceversa.
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Generator, Iterable

import requests

# ─────────────────────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────────────────────

def _config_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "config" / "api_keys.json"
    return Path(__file__).resolve().parent.parent / "config" / "api_keys.json"


def _load_cfg() -> dict:
    try:
        return json.loads(_config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Niveles de razonamiento ("capacidad de pensamiento")
# ─────────────────────────────────────────────────────────────────────────────

# Orden canónico. "off" = sin razonamiento, el resto en intensidad creciente.
REASONING_LEVELS = ["off", "low", "medium", "high", "max"]

# Cómo implementa cada proveedor la capacidad de pensamiento:
#   reasoner_model     → cambia de modelo (deepseek-chat ↔ deepseek-reasoner)
#   thinking_budget    → thinkingConfig de Gemini (presupuesto de tokens)
#   reasoning_effort   → reasoning_effort de OpenAI/o-series
#   anthropic_thinking → thinking.budget_tokens de Anthropic
#   none               → no soporta razonamiento controlado
REASONING_CONFIG: dict[str, dict[str, Any]] = {
    "deepseek":    {"kind": "reasoner_model",     "levels": ["high", "max"]},
    "gemini":      {"kind": "thinking_budget",    "levels": ["low", "medium", "high", "max"]},
    "anthropic":   {"kind": "anthropic_thinking", "levels": ["low", "medium", "high", "max"]},
    "openai":      {"kind": "reasoning_effort",   "levels": ["low", "medium", "high"]},
    "openrouter":  {"kind": "reasoning_effort",   "levels": ["low", "medium", "high"]},
    "groq":        {"kind": "none",               "levels": []},
    "ollama":      {"kind": "none",               "levels": []},
}

# Presupuestos de "pensamiento" en tokens por nivel.
_GEMINI_BUDGET = {"low": 1024, "medium": 4096, "high": 16384, "max": 32768}
_ANTHROPIC_BUDGET = {"low": 2048, "medium": 4096, "high": 8192, "max": 16000}
_OPENAI_EFFORT = {"low": "minimal", "medium": "low", "high": "medium", "max": "high"}


def reasoning_levels(provider: str) -> list[str]:
    """Niveles disponibles para un proveedor (siempre incluye 'off')."""
    lv = REASONING_CONFIG.get(provider, {}).get("levels", [])
    return ["off"] + [l for l in REASONING_LEVELS[1:] if l in lv]


def supports_reasoning(provider: str) -> bool:
    return REASONING_CONFIG.get(provider, {}).get("kind", "none") != "none"


# ─────────────────────────────────────────────────────────────────────────────
# Registro de proveedores
# ─────────────────────────────────────────────────────────────────────────────

PROVIDERS: dict[str, dict[str, Any]] = {
    "gemini": {
        "name": "Gemini (Google)",
        "api_style": "gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent",
        "models": [
            "gemini-1.5-flash", "gemini-1.5-flash-8b", "gemini-1.5-pro",
            "gemini-2.0-flash-exp", "gemini-2.0-flash-lite-exp",
        ],
        "default_model": "gemini-1.5-flash",
        "needs_api_key": True,
        "key_field": "gemini_api_key",
        "include_usage": False,   # Gemini manda usageMetadata en su propio formato
    },
    "openrouter": {
        "name": "OpenRouter",
        "api_style": "openai",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "models": [
            "openai/gpt-4o", "openai/gpt-4o-mini", "openai/o3-mini",
            "anthropic/claude-3.7-sonnet", "anthropic/claude-sonnet-4",
            "google/gemini-2.5-flash", "google/gemini-2.5-pro",
            "deepseek/deepseek-chat", "deepseek/deepseek-r1",
            "meta-llama/llama-3.3-70b-instruct", "meta-llama/llama-4-maverick",
            "mistralai/mistral-large-latest", "mistralai/mistral-small-latest",
            "x-ai/grok-3-mini", "qwen/qwen-3-235b-a22b",
        ],
        "default_model": "google/gemini-2.5-flash",
        "needs_api_key": True,
        "key_field": "openrouter_api_key",
        "include_usage": True,
        "extra_headers": {
            "HTTP-Referer": "https://fox-assistant.local",
            "X-Title": "Fox Assistant",
        },
    },
    "deepseek": {
        "name": "DeepSeek",
        "api_style": "openai",
        "url": "https://api.deepseek.com/chat/completions",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "default_model": "deepseek-chat",
        "needs_api_key": True,
        "key_field": "deepseek_api_key",
        "include_usage": True,
    },
    "groq": {
        "name": "Groq",
        "api_style": "openai",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "llama-3.2-90b-text-preview",
            "llama-3.2-11b-text-preview",
            "qwen-qwq-32b",
            "qwen-2.5-coder-32b",
            "gemma2-9b-it",
            "mixtral-8x7b-32768",
            "llama3-70b-8192",
            "llama3-8b-8192",
        ],
        "default_model": "llama-3.3-70b-versatile",
        "needs_api_key": True,
        "key_field": "groq_api_key",
        "include_usage": True,
    },
    "openai": {
        "name": "OpenAI",
        "api_style": "openai",
        "url": "https://api.openai.com/v1/chat/completions",
        "models": [
            "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini",
            "gpt-4.1-nano", "o3-mini", "o4-mini",
        ],
        "default_model": "gpt-4o-mini",
        "needs_api_key": True,
        "key_field": "openai_api_key",
        "include_usage": True,
    },
    "anthropic": {
        "name": "Anthropic (Claude)",
        "api_style": "anthropic",
        "url": "https://api.anthropic.com/v1/messages",
        "models": [
            "claude-3-5-haiku-latest", "claude-3-5-sonnet-latest",
            "claude-3-7-sonnet-latest", "claude-sonnet-4-20250514",
            "claude-opus-4-20250514", "claude-haiku-4-20250514",
        ],
        "default_model": "claude-3-5-sonnet-latest",
        "needs_api_key": True,
        "key_field": "anthropic_api_key",
        "include_usage": False,   # Anthropic manda usage en sus eventos propios
    },
    "mistral": {
        "name": "Mistral AI",
        "api_style": "openai",
        "url": "https://api.mistral.ai/v1/chat/completions",
        "models": [
            "mistral-large-latest", "mistral-medium-latest",
            "mistral-small-latest", "codestral-latest", "ministral-8b-latest",
        ],
        "default_model": "mistral-large-latest",
        "needs_api_key": True,
        "key_field": "mistral_api_key",
        "include_usage": True,
    },
    "xai": {
        "name": "xAI (Grok)",
        "api_style": "openai",
        "url": "https://api.x.ai/v1/chat/completions",
        "models": ["grok-3", "grok-3-mini", "grok-2-latest"],
        "default_model": "grok-3-mini",
        "needs_api_key": True,
        "key_field": "xai_api_key",
        "include_usage": True,
    },
    "perplexity": {
        "name": "Perplexity",
        "api_style": "openai",
        "url": "https://api.perplexity.ai/chat/completions",
        "models": ["sonar", "sonar-pro", "sonar-reasoning"],
        "default_model": "sonar",
        "needs_api_key": True,
        "key_field": "perplexity_api_key",
        "include_usage": True,
    },
    "together": {
        "name": "Together AI",
        "api_style": "openai",
        "url": "https://api.together.xyz/v1/chat/completions",
        "models": [
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
            "deepseek-ai/DeepSeek-V3",
            "Qwen/Qwen3-235B-A22B-Instruct-Turbo",
            "mistralai/Mixtral-8x7B-Instruct-v0.1",
        ],
        "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "needs_api_key": True,
        "key_field": "together_api_key",
        "include_usage": True,
    },
    "cohere": {
        "name": "Cohere",
        "api_style": "openai",
        "url": "https://api.cohere.com/compatibility/v1/chat/completions",
        "models": ["command-r-plus", "command-r", "command-a"],
        "default_model": "command-r-plus",
        "needs_api_key": True,
        "key_field": "cohere_api_key",
        "include_usage": True,
    },
    "cerebras": {
        "name": "Cerebras",
        "api_style": "openai",
        "url": "https://api.cerebras.ai/v1/chat/completions",
        "models": [
            "llama3.3-70b", "llama-3.1-8b",
            "llama-4-scout-17b-16e-instruct",
        ],
        "default_model": "llama3.3-70b",
        "needs_api_key": True,
        "key_field": "cerebras_api_key",
        "include_usage": True,
    },
    "ollama": {
        "name": "Ollama (local)",
        "api_style": "openai",
        "url": "http://localhost:11434/v1/chat/completions",
        "models": [],   # se obtienen de la instalación local
        "default_model": "llama3.2",
        "needs_api_key": False,
        "key_field": "",
        "is_local": True,
        "include_usage": False,
    },
    "nvidia": {
        "name": "NVIDIA NIM",
        "api_style": "openai",
        "url": "https://integrate.api.nvidia.com/v1/chat/completions",
        "models": [
            "nvidia/nemotron-3-ultra",
            "nvidia/llama-3.1-nemotron-70b-instruct",
            "meta/llama-3.1-405b-instruct",
            "meta/llama-3.1-70b-instruct",
            "meta/llama-3.1-8b-instruct",
            "mistralai/mistral-7b-instruct-v0.3",
            "google/gemma-2-9b-it",
        ],
        "default_model": "nvidia/llama-3.1-nemotron-70b-instruct",
        "needs_api_key": True,
        "key_field": "nvidia_api_key",
        "include_usage": True,
    },
    "bai": {
        "name": "B.AI",
        "api_style": "openai",
        "url": "https://api.b.ai/v1/chat/completions",
        "models": [
            "bai/gpt-4o-mini",
            "bai/claude-3.5-sonnet",
            "bai/gemini-1.5-flash",
            "bai/llama-3.1-70b",
            "bai/llama-3.1-8b",
        ],
        "default_model": "bai/gpt-4o-mini",
        "needs_api_key": True,
        "key_field": "bai_api_key",
        "include_usage": True,
    },
    "huggingface": {
        "name": "HuggingFace (Inference API)",
        "api_style": "openai",
        "url": "https://api-inference.huggingface.co/v1/chat/completions",
        "models": [
            "meta-llama/Meta-Llama-3.1-70B-Instruct",
            "meta-llama/Meta-Llama-3.1-8B-Instruct",
            "mistralai/Mistral-7B-Instruct-v0.3",
            "google/gemma-2-9b-it",
            "microsoft/Phi-3.5-mini-instruct",
            "Qwen/Qwen2.5-7B-Instruct",
        ],
        "default_model": "meta-llama/Meta-Llama-3.1-8B-Instruct",
        "needs_api_key": True,
        "key_field": "huggingface_api_key",
        "include_usage": False,
    },
    "together": {
        "name": "Together AI",
        "api_style": "openai",
        "url": "https://api.together.xyz/v1/chat/completions",
        "models": [
            "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
            "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
            "mistralai/Mistral-7B-Instruct-v0.3",
            "google/gemma-2-9b-it",
            "Qwen/Qwen2.5-7B-Instruct",
        ],
        "default_model": "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
        "needs_api_key": True,
        "key_field": "together_api_key",
        "include_usage": True,
    },
    "perplexity": {
        "name": "Perplexity (Sonar)",
        "api_style": "openai",
        "url": "https://api.perplexity.ai/chat/completions",
        "models": [
            "sonar",
            "sonar-pro",
            "sonar-reasoning",
            "sonar-reasoning-pro",
        ],
        "default_model": "sonar",
        "needs_api_key": True,
        "key_field": "perplexity_api_key",
        "include_usage": True,
    },
    "cohere": {
        "name": "Cohere",
        "api_style": "openai",
        "url": "https://api.cohere.com/compatibility/v1/chat/completions",
        "models": [
            "command-r-plus",
            "command-r",
            "command-a",
            "command-r7b-12-2024",
        ],
        "default_model": "command-r",
        "needs_api_key": True,
        "key_field": "cohere_api_key",
        "include_usage": True,
    },
    "fireworks": {
        "name": "Fireworks AI",
        "api_style": "openai",
        "url": "https://api.fireworks.ai/inference/v1/chat/completions",
        "models": [
            "accounts/fireworks/models/llama-v3p1-70b-instruct",
            "accounts/fireworks/models/llama-v3p1-8b-instruct",
            "accounts/fireworks/models/mixtral-8x7b-instruct",
            "accounts/fireworks/models/qwen2p5-7b-instruct",
            "accounts/fireworks/models/phi-3-5-mini-instruct",
        ],
        "default_model": "accounts/fireworks/models/llama-v3p1-8b-instruct",
        "needs_api_key": True,
        "key_field": "fireworks_api_key",
        "include_usage": True,
    },
    "replicate": {
        "name": "Replicate",
        "api_style": "openai",
        "url": "https://api.replicate.com/v1/models/{model}/predictions",
        "models": [
            "meta/meta-llama-3.1-70b-instruct",
            "meta/meta-llama-3.1-8b-instruct",
            "mistralai/mistral-7b-instruct-v0.3",
            "google/gemma-2-9b-it",
        ],
        "default_model": "meta/meta-llama-3.1-8b-instruct",
        "needs_api_key": True,
        "key_field": "replicate_api_key",
        "include_usage": False,
    },
    "deepinfra": {
        "name": "DeepInfra",
        "api_style": "openai",
        "url": "https://api.deepinfra.com/v1/openai/chat/completions",
        "models": [
            "meta-llama/Meta-Llama-3.1-70B-Instruct",
            "meta-llama/Meta-Llama-3.1-8B-Instruct",
            "mistralai/Mistral-7B-Instruct-v0.3",
            "google/gemma-2-9b-it",
            "Qwen/Qwen2.5-7B-Instruct",
            "microsoft/Phi-3.5-mini-instruct",
        ],
        "default_model": "meta-llama/Meta-Llama-3.1-8B-Instruct",
        "needs_api_key": True,
        "key_field": "deepinfra_api_key",
        "include_usage": True,
    },
    "auto_free": {
        "name": "[Auto] Modelos Gratuitos (Rotación)",
        "api_style": "auto",
        "url": "",
        "models": ["auto"],
        "default_model": "auto",
        "needs_api_key": False,
        "key_field": "",
        "is_auto_free": True,
        "include_usage": False,
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Proveedores personalizados (OpenAI-compatible)
# ─────────────────────────────────────────────────────────────────────────────

def _custom_providers() -> dict[str, dict]:
    """Devuelve los proveedores personalizados desde la config, indexados por id."""
    out: dict[str, dict] = {}
    for cp in (_load_cfg().get("custom_providers") or []):
        if isinstance(cp, dict) and cp.get("id") and cp.get("url"):
            out[cp["id"]] = cp
    return out


def _resolve_provider(provider_id: str) -> dict | None:
    """Devuelve el entry del proveedor (built-in o personalizado), o None."""
    if provider_id in PROVIDERS:
        return PROVIDERS[provider_id]
    cp = _custom_providers().get(provider_id)
    if cp:
        models = cp.get("models") or []
        default = cp.get("default_model") or (models[0] if models else "")
        url = cp["url"]
        if not url.endswith("/chat/completions"):
            url = f"{url.rstrip('/')}/chat/completions"
        return {
            "name": cp.get("name") or provider_id,
            "api_style": "openai",
            "url": url,
            "models": models,
            "default_model": default,
            "needs_api_key": True,
            "key_field": "",          # la key del custom vive inline
            "is_custom": True,
            "include_usage": False,
            "_custom": cp,
        }
    return None


def add_custom_provider(name: str, url: str, api_key: str = "", models: list[str] | None = None) -> str:
    """Agrega un proveedor OpenAI-compatible a la config y devuelve su id."""
    cfg = _load_cfg()
    cps = [c for c in (cfg.get("custom_providers") or []) if isinstance(c, dict)]
    pid = "custom_" + uuid.uuid4().hex[:8]
    models_list = [m.strip() for m in (models or []) if m and m.strip()]
    cps.append({
        "id": pid,
        "name": (name or pid).strip(),
        "url": (url or "").strip().rstrip("/"),
        "api_key": (api_key or "").strip(),
        "models": models_list,
        "default_model": models_list[0] if models_list else "",
    })
    cfg["custom_providers"] = cps
    try:
        _config_path().write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[Providers] No se pudo guardar el proveedor custom: {e}")
    return pid


def remove_custom_provider(provider_id: str) -> None:
    cfg = _load_cfg()
    cps = [c for c in (cfg.get("custom_providers") or [])
           if isinstance(c, dict) and c.get("id") != provider_id]
    cfg["custom_providers"] = cps
    try:
        _config_path().write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[Providers] No se pudo borrar el proveedor custom: {e}")


def list_providers() -> list[dict]:
    """Lista de proveedores (built-in + personalizados) para la UI."""
    out = []
    for pid, p in PROVIDERS.items():
        out.append({
            "id": pid,
            "name": p["name"],
            "models": p["models"],
            "default_model": p["default_model"],
            "needs_api_key": p["needs_api_key"],
            "is_local": p.get("is_local", False),
            "is_custom": False,
            "has_key": (not p["needs_api_key"]) or bool(get_api_key(pid)),
            "reasoning": reasoning_levels(pid),
        })
    for pid, cp in _custom_providers().items():
        models = cp.get("models") or []
        out.append({
            "id": pid,
            "name": cp.get("name") or pid,
            "models": models,
            "default_model": cp.get("default_model") or (models[0] if models else ""),
            "needs_api_key": True,
            "is_local": False,
            "is_custom": True,
            "has_key": bool(cp.get("api_key")),
            "reasoning": ["off"],
        })
    return out


def get_active_provider() -> str:
    """Devuelve el proveedor activo para el modo agente/chat."""
    cfg = _load_cfg()
    provider = (cfg.get("agent_provider") or cfg.get("specialist_provider") or "deepseek").strip().lower()
    if _resolve_provider(provider) is None:
        return "deepseek"
    return provider


def get_active_model() -> str:
    """Devuelve el modelo activo para el modo agente/chat."""
    cfg = _load_cfg()
    provider = get_active_provider()
    entry = _resolve_provider(provider) or PROVIDERS["deepseek"]
    default = entry["default_model"]
    return (cfg.get("agent_model") or cfg.get("specialist_model") or default).strip()


def save_agent_selection(provider: str, model: str) -> None:
    """Persiste el proveedor y modelo seleccionados para el modo agente."""
    try:
        cfg = _load_cfg()
        cfg["agent_provider"] = provider
        cfg["agent_model"] = model
        _config_path().write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[Providers] No se pudo guardar la selección: {e}")


def get_api_key(provider: str) -> str:
    """API key del proveedor desde la configuración ('' si no necesita)."""
    entry = _resolve_provider(provider)
    if entry is None:
        return ""
    if entry.get("is_custom"):
        return (entry["_custom"].get("api_key") or "").strip()
    key_field = entry.get("key_field", "")
    if not key_field:
        return ""
    return (_load_cfg().get(key_field) or "").strip()


def set_api_key(provider: str, api_key: str) -> None:
    """Guarda la API key de un proveedor en config/api_keys.json."""
    entry = _resolve_provider(provider)
    if entry is None:
        return
    if entry.get("is_custom"):
        # Actualizar la key inline del proveedor personalizado.
        cfg = _load_cfg()
        for cp in (cfg.get("custom_providers") or []):
            if isinstance(cp, dict) and cp.get("id") == provider:
                cp["api_key"] = (api_key or "").strip()
        cfg["custom_providers"] = [c for c in (cfg.get("custom_providers") or []) if isinstance(c, dict)]
        _config_path().write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
        return
    key_field = entry.get("key_field", "")
    if not key_field:
        return
    cfg = _load_cfg()
    cfg[key_field] = (api_key or "").strip()
    try:
        _config_path().write_text(
            json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:  # noqa: BLE001
        print(f"[Providers] No se pudo guardar la key de {provider}: {e}")


def list_local_models(url: str | None = None) -> list[str]:
    """Devuelve los modelos disponibles en una instalación local de Ollama."""
    base = (url or _load_cfg().get("ollama_url") or "http://localhost:11434").rstrip("/")
    try:
        resp = requests.get(f"{base}/api/tags", timeout=5)
        resp.raise_for_status()
        return [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception:
        return []


_MODEL_CACHE: dict[str, tuple[float, list[str]]] = {}


def fetch_remote_models(provider_id: str) -> list[str]:
    """Consulta el endpoint de modelos del proveedor y devuelve la lista disponible.

    Para proveedores OpenAI-compatibles usa GET <base>/v1/models.
    Para Gemini usa la API de listado de modelos.
    Para Anthropic usa GET /v1/models con la API key.
    Si falla (sin red, sin key, timeout) devuelve la lista estática del registro.
    """
    import time
    if provider_id in _MODEL_CACHE:
        ts, cached_models = _MODEL_CACHE[provider_id]
        if time.time() - ts < 300:
            return cached_models

    import re as _re

    entry = _resolve_provider(provider_id)
    if entry is None:
        return []

    api_style = entry.get("api_style", "openai")
    static_models: list[str] = list(entry.get("models") or [])

    # Ollama → usa list_local_models
    if provider_id == "ollama":
        local = list_local_models()
        return local or static_models

    api_key = get_api_key(provider_id)

    # ── Gemini ───────────────────────────────────────────────────────────────
    if api_style == "gemini":
        if not api_key:
            return static_models
        try:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models"
                f"?key={api_key}&pageSize=200"
            )
            resp = requests.get(url, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            names: list[str] = []
            for m in data.get("models", []):
                methods = m.get("supportedGenerationMethods", [])
                if "generateContent" in methods or "streamGenerateContent" in methods:
                    short = m.get("name", "").replace("models/", "")
                    if short:
                        names.append(short)
            return names if names else static_models
        except Exception:
            return static_models

    # ── Anthropic ────────────────────────────────────────────────────────────
    if api_style == "anthropic":
        if not api_key:
            return static_models
        try:
            resp = requests.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                timeout=8,
            )
            resp.raise_for_status()
            names = [m.get("id", "") for m in resp.json().get("data", []) if m.get("id")]
            return names if names else static_models
        except Exception:
            return static_models

    # ── OpenAI-compatible (DeepSeek, OpenAI, Groq, OpenRouter, Mistral…) ─────
    raw_url: str = entry.get("url", "")
    # Derivar la base del URL de completions → /v1/models
    #   "https://api.deepseek.com/chat/completions"      → "https://api.deepseek.com/v1/models"
    #   "https://api.openai.com/v1/chat/completions"     → "https://api.openai.com/v1/models"
    #   "https://openrouter.ai/api/v1/chat/completions"  → "https://openrouter.ai/api/v1/models"
    base_url = _re.sub(r"/(v\d+/)?chat/completions$", "", raw_url).rstrip("/")
    if _re.search(r"/v\d+$", base_url):
        models_url = f"{base_url}/models"
    else:
        models_url = f"{base_url}/v1/models"

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    extra = entry.get("extra_headers") or {}
    headers.update(extra)

    try:
        resp = requests.get(models_url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        ids: list[str] = []
        # Formato OpenAI estándar: {"data": [{"id": "model-name"}, …]}
        for m in data.get("data", []):
            mid = m.get("id", "")
            if mid:
                ids.append(mid)
        # Algunos proveedores devuelven {"models": [...]}
        if not ids:
            for m in data.get("models", []):
                if isinstance(m, dict):
                    mid = m.get("id", "") or m.get("name", "")
                else:
                    mid = str(m)
                if mid:
                    ids.append(mid)
        if ids:
            # Combinar: static primero (aliases conocidos) + dinámicos nuevos
            seen = set(ids)
            extra_static = [m for m in static_models if m not in seen]
            combined = extra_static + sorted(ids)
            _MODEL_CACHE[provider_id] = (time.time(), combined)
            return combined
        
        _MODEL_CACHE[provider_id] = (time.time(), static_models)
        return static_models
    except Exception:
        return static_models



# ─────────────────────────────────────────────────────────────────────────────
# Helpers multimodal (imagen → base64)
# ─────────────────────────────────────────────────────────────────────────────

def _encode_image(image_path: str) -> tuple[str, str]:
    """Lee una imagen y devuelve (base64_data, mime_type)."""
    import base64
    import mimetypes
    path = Path(image_path)
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return data, mime


def _build_user_content_openai(text: str, image_path: str | None) -> Any:
    """Construye el content de un mensaje user para OpenAI vision."""
    if not image_path:
        return text
    try:
        data, mime = _encode_image(image_path)
        return [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}},
        ]
    except Exception as e:
        print(f"[Providers] Error codificando imagen: {e}")
        return text


def _build_user_content_gemini(text: str, image_path: str | None) -> list[dict]:
    """Construye las parts de un mensaje user para Gemini con imagen."""
    parts: list[dict] = []
    if image_path:
        try:
            import base64
            import mimetypes
            mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
            data = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
            parts.append({"inline_data": {"mime_type": mime, "data": data}})
        except Exception as e:
            print(f"[Providers] Error codificando imagen para Gemini: {e}")
    parts.append({"text": text})
    return parts


def _build_user_content_anthropic(text: str, image_path: str | None) -> Any:
    """Construye el content de un mensaje user para Anthropic con imagen."""
    if not image_path:
        return text
    try:
        import base64
        import mimetypes
        mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
        data = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
        return [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}},
            {"type": "text", "text": text},
        ]
    except Exception as e:
        print(f"[Providers] Error codificando imagen para Anthropic: {e}")
        return text


# ─────────────────────────────────────────────────────────────────────────────
# Normalización de herramientas
# ─────────────────────────────────────────────────────────────────────────────

def _uppercase_types(node: Any) -> Any:
    """Convierte tipos minúsculas → MAYÚSCULAS (formato nativo de Gemini)."""
    if isinstance(node, dict):
        out = dict(node)
        if isinstance(out.get("type"), str):
            out["type"] = out["type"].upper()
        if isinstance(out.get("properties"), dict):
            out["properties"] = {k: _uppercase_types(v) for k, v in out["properties"].items()}
        if "items" in out:
            out["items"] = _uppercase_types(out["items"])
        return out
    if isinstance(node, list):
        return [_uppercase_types(x) for x in node]
    return node


def _lowercase_types(node: Any) -> Any:
    """Convierte tipos MAYÚSCULAS → minúsculas (JSON Schema de OpenAI)."""
    if isinstance(node, dict):
        out = dict(node)
        if isinstance(out.get("type"), str):
            out["type"] = out["type"].lower()
        if isinstance(out.get("properties"), dict):
            out["properties"] = {k: _lowercase_types(v) for k, v in out["properties"].items()}
        if "items" in out:
            out["items"] = _lowercase_types(out["items"])
        return out
    if isinstance(node, list):
        return [_lowercase_types(x) for x in node]
    return node


def normalize_tools(tools: Iterable[dict] | None) -> list[dict]:
    """Normaliza cualquier formato de tools al canónico (OpenAI-wrapped, minúsculas).

    Acepta:
      - Declaraciones estilo Gemini: {"name", "description", "parameters": {tipo MAYÚSCULA}}
      - Ya envueltas estilo OpenAI:  {"type":"function","function":{...}}
    """
    if not tools:
        return []
    result: list[dict] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if "function" in t:
            # Ya envuelto → solo garantizar minúsculas
            fn = dict(t["function"])
            if "parameters" in fn:
                fn["parameters"] = _lowercase_types(fn["parameters"])
            result.append({"type": "function", "function": fn})
        else:
            # Declaración cruda estilo Gemini
            fn = {"name": t.get("name", ""), "description": t.get("description", "")}
            params = t.get("parameters")
            if isinstance(params, dict):
                fn["parameters"] = _lowercase_types(params)
            result.append({"type": "function", "function": fn})
    return result


def to_gemini_tools(tools: Iterable[dict] | None) -> list[dict]:
    """Canónico → declaraciones nativas de Gemini (tipos MAYÚSCULA, sin wrapper)."""
    canonical = normalize_tools(tools)
    out = []
    for t in canonical:
        fn = dict(t["function"])
        if "parameters" in fn:
            fn["parameters"] = _uppercase_types(fn["parameters"])
        out.append(fn)
    return out


def to_anthropic_tools(tools: Iterable[dict] | None) -> list[dict]:
    """Canónico → formato Anthropic {name, description, input_schema}."""
    canonical = normalize_tools(tools)
    out = []
    for t in canonical:
        fn = t["function"]
        out.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Razonamiento ("capacidad de pensamiento")
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_effort(effort: str | None) -> str:
    e = (effort or "off").strip().lower()
    return e if e in REASONING_LEVELS else "off"


def _reasoner_model_switch(provider: str, model: str, effort: str) -> str:
    """DeepSeek: mapea la intensidad a chat ↔ reasoner."""
    e = _resolve_effort(effort)
    if e in ("high", "max"):
        return "deepseek-reasoner"
    return "deepseek-chat"


def _apply_reasoning_openai(provider: str, model: str, effort: str, payload: dict) -> str:
    """Aplica razonamiento a payload OpenAI-compatible. Devuelve el modelo efectivo."""
    kind = REASONING_CONFIG.get(provider, {}).get("kind", "none")
    e = _resolve_effort(effort)
    if kind == "reasoner_model":
        return _reasoner_model_switch(provider, model, e)
    if kind == "reasoning_effort" and e != "off":
        level = _OPENAI_EFFORT.get(e)
        if level:
            # OpenAI o-series usa reasoning_effort; OpenRouter usa reasoning.effort
            if provider == "openrouter":
                payload["reasoning"] = {"effort": level}
            else:
                payload["reasoning_effort"] = level
    return model


# ─────────────────────────────────────────────────────────────────────────────
# Adaptador OpenAI-compatible (DeepSeek, Groq, OpenRouter, OpenAI, Ollama)
# ─────────────────────────────────────────────────────────────────────────────

def _openai_tool_calls_from_wire(raw_calls: list[dict]) -> list[dict]:
    """Normaliza tool_calls de wire (arguments como str) → formato de respuesta."""
    out = []
    for tc in raw_calls or []:
        fn = tc.get("function", {})
        args = fn.get("arguments", "{}")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        out.append({
            "id": tc.get("id", ""),
            "function": {"name": fn.get("name", ""), "arguments": args},
        })
    return out


def _stream_openai(
    provider: str,
    url: str,
    model: str,
    api_key: str,
    messages: list[dict],
    tools: list[dict] | None,
    effort: str,
    timeout: int,
) -> Generator[dict, None, None]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    
    prov_data = _resolve_provider(provider) or {}
    headers.update(prov_data.get("extra_headers", {}) or {})

    # Preprocessing: convertir mensajes con image_path al formato vision
    processed_messages = []
    for msg in messages:
        if msg.get("role") == "user" and msg.get("image_path"):
            msg = dict(msg)
            msg["content"] = _build_user_content_openai(
                msg.get("content") or "", msg.pop("image_path", None)
            )
        processed_messages.append({k: v for k, v in msg.items() if k != "image_path"})

    payload: dict[str, Any] = {
        "model": model,
        "messages": processed_messages,
        "stream": True,
    }
    model = _apply_reasoning_openai(provider, model, effort, payload)
    payload["model"] = model
    if tools:
        payload["tools"] = normalize_tools(tools)
        payload["tool_choice"] = "auto"
    
    # Algunas APIs mueren si mandas stream_options
    if prov_data.get("include_usage"):
        payload["stream_options"] = {"include_usage": True}

    full_content = ""
    full_thinking = ""
    usage: dict[str, int | None] = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    tc_fragments: dict[int, dict] = {}
    finish_reason = "stop"

    def _flush_tool_calls() -> list[dict]:
        calls: list[dict] = []
        for idx in sorted(tc_fragments):
            frag = tc_fragments[idx]
            args = frag["function"]["arguments"]
            try:
                args = json.loads(args)
            except Exception:
                args = {}
            calls.append({
                "id": frag["id"],
                "function": {"name": frag["function"]["name"], "arguments": args},
            })
        return calls

    with requests.post(url, json=payload, headers=headers, timeout=timeout, stream=True) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue

            if "usage" in chunk and chunk["usage"]:
                u = chunk["usage"]
                details = u.get("prompt_tokens_details") or {}
                comp_details = u.get("completion_tokens_details") or {}
                usage = {
                    "prompt_tokens": u.get("prompt_tokens"),
                    "completion_tokens": u.get("completion_tokens"),
                    "total_tokens": u.get("total_tokens"),
                    "cached_tokens": details.get("cached_tokens"),
                    "reasoning_tokens": comp_details.get("reasoning_tokens"),
                }

            if not chunk.get("choices"):
                continue
            choice = chunk["choices"][0]
            delta = choice.get("delta", {})

            text = delta.get("content") or ""
            if text:
                full_content += text
                yield {"type": "delta", "text": text}

            # Razonamiento (DeepSeek: reasoning_content; OpenAI o-series: reasoning)
            think = delta.get("reasoning_content") or ""
            if not think and isinstance(delta.get("reasoning"), str):
                think = delta["reasoning"]
            if think:
                full_thinking += think
                yield {"type": "thinking", "text": think}

            for tc in (delta.get("tool_calls") or []):
                idx = tc.get("index", 0)
                if idx not in tc_fragments:
                    tc_fragments[idx] = {"id": "", "function": {"name": "", "arguments": ""}}
                frag = tc_fragments[idx]
                frag["id"] = frag["id"] or tc.get("id", "")
                fn = tc.get("function", {})
                frag["function"]["name"] += fn.get("name") or ""
                frag["function"]["arguments"] += fn.get("arguments") or ""

            fr = choice.get("finish_reason")
            if fr:
                finish_reason = fr

    tool_calls = _flush_tool_calls() if tc_fragments else []
    if tool_calls:
        yield {"type": "tool_calls", "tool_calls": tool_calls}

    yield {
        "type": "done",
        "content": full_content.strip(),
        "thinking": full_thinking.strip(),
        "tool_calls": tool_calls,
        "usage": usage,
        "finish_reason": finish_reason,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Adaptador Gemini (nativo)
# ─────────────────────────────────────────────────────────────────────────────

def _gemini_convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI-style messages → (system_instruction, contents) de Gemini."""
    system_instruction = ""
    contents: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        if role == "system":
            system_instruction += (msg.get("content") or "") + "\n"
            continue
        if role == "tool":
            # Resultado de herramienta → functionResponse
            contents.append({
                "role": "user",
                "parts": [{
                    "functionResponse": {
                        "name": msg.get("name", ""),
                        "response": {"result": msg.get("content", "")},
                    },
                }],
            })
            continue
        parts: list[dict] = []
        # Llamadas a herramienta del asistente → functionCall
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            parts.append({"functionCall": {"name": fn.get("name", ""), "args": args}})
        content = msg.get("content") or ""
        image_path = msg.get("image_path")
        if image_path and role == "user":
            parts.extend(_build_user_content_gemini(content, image_path))
        elif content:
            parts.append({"text": content})
        contents.append({
            "role": "model" if role == "assistant" else "user",
            "parts": parts or [{"text": ""}],
        })
    return system_instruction.strip(), contents


def _stream_gemini(
    provider: str,
    url_template: str,
    model: str,
    api_key: str,
    messages: list[dict],
    tools: list[dict] | None,
    effort: str,
    timeout: int,
) -> Generator[dict, None, None]:
    system_instruction, contents = _gemini_convert_messages(messages)

    url = url_template.format(model=model) + f"?alt=sse&key={api_key}"
    payload: dict[str, Any] = {"contents": contents}
    if system_instruction:
        payload["system_instruction"] = {"parts": [{"text": system_instruction}]}
    if tools:
        payload["tools"] = [{"function_declarations": to_gemini_tools(tools)}]

    # Razonamiento vía thinkingConfig
    kind = REASONING_CONFIG.get(provider, {}).get("kind", "none")
    e = _resolve_effort(effort)
    generation_config: dict[str, Any] = {}
    if kind == "thinking_budget" and e != "off":
        budget = _GEMINI_BUDGET.get(e, 0)
        generation_config["thinkingConfig"] = {"thinkingBudget": budget, "includeThoughts": True}
    if generation_config:
        payload["generationConfig"] = generation_config

    full_content = ""
    full_thinking = ""
    usage: dict[str, int | None] = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    calls: list[dict] = []

    with requests.post(url, json=payload, timeout=timeout, stream=True) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue

            um = chunk.get("usageMetadata")
            if um:
                usage = {
                    "prompt_tokens": um.get("promptTokenCount"),
                    "completion_tokens": um.get("candidatesTokenCount"),
                    "total_tokens": um.get("totalTokenCount"),
                }

            for cand in (chunk.get("candidates") or []):
                parts = (cand.get("content") or {}).get("parts") or []
                for part in parts:
                    if "functionCall" in part:
                        fc = part["functionCall"]
                        calls.append({
                            "id": fc.get("name", "") + "_" + str(len(calls)),
                            "function": {"name": fc.get("name", ""), "arguments": fc.get("args", {})},
                        })
                    elif "text" in part:
                        txt = part.get("text", "")
                        if part.get("thought"):
                            full_thinking += txt
                            yield {"type": "thinking", "text": txt}
                        else:
                            full_content += txt
                            yield {"type": "delta", "text": txt}

    if calls:
        yield {"type": "tool_calls", "tool_calls": calls}

    yield {
        "type": "done",
        "content": full_content.strip(),
        "thinking": full_thinking.strip(),
        "tool_calls": calls,
        "usage": usage,
        "finish_reason": "tool_calls" if calls else "stop",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Adaptador Anthropic (Claude)
# ─────────────────────────────────────────────────────────────────────────────

def _anthropic_convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI-style messages → (system, anthropic_messages)."""
    system = ""
    out: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        if role == "system":
            system += (msg.get("content") or "") + "\n"
            continue
        if role == "tool":
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": msg.get("content", ""),
                }],
            })
            continue
        if role == "assistant":
            blocks: list[dict] = []
            for tc in (msg.get("tool_calls") or []):
                fn = tc.get("function", {})
                args = fn.get("arguments", "{}")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": args,
                })
            text = msg.get("content") or ""
            if text:
                blocks.insert(0, {"type": "text", "text": text})
            out.append({"role": "assistant", "content": blocks})
            continue
        # user
        content = msg.get("content") or ""
        image_path = msg.get("image_path")
        if image_path:
            out.append({"role": "user", "content": _build_user_content_anthropic(content, image_path)})
        elif isinstance(content, str):
            out.append({"role": "user", "content": content})
        else:
            out.append({"role": "user", "content": content})
    return system.strip(), out


def _stream_anthropic(
    provider: str,
    url: str,
    model: str,
    api_key: str,
    messages: list[dict],
    tools: list[dict] | None,
    effort: str,
    timeout: int,
) -> Generator[dict, None, None]:
    system, an_messages = _anthropic_convert_messages(messages)

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    payload: dict[str, Any] = {
        "model": model,
        "messages": an_messages,
        "stream": True,
        "max_tokens": 4096,
    }
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = to_anthropic_tools(tools)

    # Razonamiento (extended thinking)
    kind = REASONING_CONFIG.get(provider, {}).get("kind", "none")
    e = _resolve_effort(effort)
    if kind == "anthropic_thinking" and e != "off":
        budget = _ANTHROPIC_BUDGET.get(e, 4096)
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        payload["max_tokens"] = max(payload["max_tokens"], budget + 1024)

    full_content = ""
    full_thinking = ""
    usage: dict[str, int | None] = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    tool_uses: dict[int, dict] = {}
    current_block_type = ""

    with requests.post(url, json=payload, headers=headers, timeout=timeout, stream=True) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines():
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue

            etype = ev.get("type", "")

            if etype == "message_start":
                u = (ev.get("message") or {}).get("usage") or {}
                if u:
                    usage["prompt_tokens"] = u.get("input_tokens")
                    usage["total_tokens"] = u.get("input_tokens")
            elif etype == "content_block_start":
                block = ev.get("content_block") or {}
                current_block_type = block.get("type", "")
                if current_block_type == "thinking":
                    pass
                elif current_block_type == "tool_use":
                    idx = ev.get("index", 0)
                    tool_uses[idx] = {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input": {},
                    }
            elif etype == "content_block_delta":
                delta = ev.get("delta") or {}
                dt = delta.get("type", "")
                if dt == "thinking_delta":
                    txt = delta.get("thinking", "")
                    full_thinking += txt
                    yield {"type": "thinking", "text": txt}
                elif dt == "text_delta":
                    txt = delta.get("text", "")
                    full_content += txt
                    yield {"type": "delta", "text": txt}
                elif dt == "input_json_delta":
                    idx = ev.get("index", 0)
                    if idx in tool_uses:
                        tool_uses[idx]["_partial"] = tool_uses[idx].get("_partial", "") + delta.get("partial_json", "")
            elif etype == "content_block_stop":
                current_block_type = ""
            elif etype == "message_delta":
                u = ev.get("usage") or {}
                if u.get("output_tokens") is not None:
                    usage["completion_tokens"] = u.get("output_tokens")
                    if usage["total_tokens"] is not None:
                        usage["total_tokens"] = usage["total_tokens"] + u["output_tokens"]

    # Parsear tool_use inputs
    calls: list[dict] = []
    for idx in sorted(tool_uses):
        tu = tool_uses[idx]
        partial = tu.pop("_partial", "")
        try:
            inp = json.loads(partial) if partial else tu.get("input", {})
        except Exception:
            inp = {}
        calls.append({
            "id": tu.get("id", ""),
            "function": {"name": tu.get("name", ""), "arguments": inp},
        })

    if calls:
        yield {"type": "tool_calls", "tool_calls": calls}

    yield {
        "type": "done",
        "content": full_content.strip(),
        "thinking": full_thinking.strip(),
        "tool_calls": calls,
        "usage": usage,
        "finish_reason": "tool_calls" if calls else "stop",
    }


# ─────────────────────────────────────────────────────────────────────────────
# API pública
# ─────────────────────────────────────────────────────────────────────────────

def stream_chat(
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    effort: str = "off",
    timeout: int = 180,
) -> Generator[dict, None, None]:
    """Generador de eventos para una llamada de chat con streaming.

    Yields:
        {"type": "delta", "text": str}
        {"type": "thinking", "text": str}
        {"type": "tool_calls", "tool_calls": [{"id","function":{"name","arguments":dict}}]}
        {"type": "done", "content": str, "thinking": str, "tool_calls": [...], "usage": {...}, "finish_reason": str}
    """
    provider = (provider or get_active_provider()).strip().lower()
    p = _resolve_provider(provider)
    if p is None:
        provider = "deepseek"
        p = PROVIDERS["deepseek"]

    # ── Auto-Free Mode: rotación automática de modelos gratuitos (FreeLLMAPI-style) ──
    if p.get("is_auto_free"):
        from core.free_models import (
            get_next_available_free_model, mark_model_failed, get_free_models_for,
            record_success, record_failure, get_penalty, calculate_score
        )
        # Lista dinámica de proveedores con API key configurada
        preferred_order = [
            "groq", "gemini", "openrouter", "nvidia", "bai", "huggingface", 
            "together", "perplexity", "cohere", "fireworks", "replicate", 
            "deepinfra", "deepseek", "cerebras", "mistral", "xai", "ollama"
        ]
        free_providers = [prov for prov in preferred_order 
                          if (PROVIDERS.get(prov) and (get_api_key(prov) or prov == "ollama"))]
        
        last_error = None
        for prov in free_providers:
            prov_entry = PROVIDERS.get(prov)
            if not prov_entry:
                continue
            prov_api_key = get_api_key(prov)
            if not prov_api_key and prov != "ollama":
                continue  # Saltar proveedores sin API key configurada
            
            try:
                real_provider, real_model = get_next_available_free_model(prov, prov_api_key)
                style = prov_entry["api_style"]
                url = prov_entry["url"]
                
                if prov == "ollama":
                    base = (_load_cfg().get("ollama_url") or "http://localhost:11434").rstrip("/")
                    url = f"{base}/v1/chat/completions"
                
                print(f"[AutoFree] Intentando {real_provider} / {real_model} (penalty={get_penalty(real_provider, real_model)}, score={calculate_score(real_provider, real_model):.3f})…")
                
                start_time = time.time()
                success = False
                try:
                    if style == "gemini":
                        yield from _stream_gemini(real_provider, url, real_model, prov_api_key, messages, tools, effort, timeout)
                    elif style == "anthropic":
                        yield from _stream_anthropic(real_provider, url, real_model, prov_api_key, messages, tools, effort, timeout)
                    else:
                        yield from _stream_openai(real_provider, url, real_model, prov_api_key, messages, tools, effort, timeout)
                    success = True
                    return  # Éxito: salir del generador
                except Exception as e:
                    err_str = str(e).lower()
                    is_rate_limit = any(k in err_str for k in (
                        "429", "rate limit", "quota", "too many requests", "overloaded", 
                        "503", "429"
                    ))
                    is_retryable = (
                        isinstance(e, ValueError) or
                        any(k in err_str for k in (
                            "429", "rate limit", "quota", "too many requests", "overloaded", 
                            "timeout", "503", "404", "413", "400", "not found", "model not found", 
                            "does not exist", "payload too large", "token limit", "max tokens", "context length"
                        ))
                    )
                    if is_retryable:
                        if not isinstance(e, ValueError):
                            record_failure(real_provider, real_model, is_rate_limit=is_rate_limit)
                    last_error = e
                    print(f"[AutoFree] ⚡ Fallo en {real_provider}/{real_model}: {e} → Rotando al siguiente…")
                    continue  # Probar siguiente proveedor
                else:
                    # Error no recuperable (auth, bad request, etc.) -> re-lanzar
                    raise
                finally:
                    latency_ms = (time.time() - start_time) * 1000
                    if success:
                        record_success(real_provider, real_model, latency_ms)
            except ValueError as e:
                # This is from get_next_available_free_model when all models are blocked
                print(f"[AutoFree] !! {e} -> Rotando al siguiente...")
                last_error = e
                continue  # Probar siguiente proveedor
        
        # Si llegamos aquí, todos fallaron
        raise RuntimeError(f"Auto-Free: todos los modelos gratuitos fallaron. Último error: {last_error}")

    model = (model or "").strip() or p["default_model"] or get_active_model()
    api_key = api_key if api_key is not None else get_api_key(provider)
    style = p["api_style"]
    url = p["url"]

    if provider == "ollama":
        # URL configurable para instalaciones locales alternativas
        base = (_load_cfg().get("ollama_url") or "http://localhost:11434").rstrip("/")
        url = f"{base}/v1/chat/completions"

    if style == "gemini":
        yield from _stream_gemini(provider, url, model, api_key, messages, tools, effort, timeout)
    elif style == "anthropic":
        yield from _stream_anthropic(provider, url, model, api_key, messages, tools, effort, timeout)
    else:
        yield from _stream_openai(provider, url, model, api_key, messages, tools, effort, timeout)


def chat(
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    effort: str = "off",
    timeout: int = 180,
) -> dict:
    """Versión no-streaming: drena el stream y devuelve el resultado final.

    Returns:
        {"content": str, "thinking": str, "tool_calls": [...], "usage": {...}, "finish_reason": str}
    """
    content = ""
    thinking = ""
    tool_calls: list[dict] = []
    usage: dict[str, int | None] = {}
    finish_reason = "stop"
    for ev in stream_chat(
        messages, tools,
        provider=provider, model=model, api_key=api_key, effort=effort, timeout=timeout,
    ):
        if ev["type"] == "delta":
            content += ev["text"]
        elif ev["type"] == "thinking":
            thinking += ev["text"]
        elif ev["type"] == "tool_calls":
            tool_calls = ev["tool_calls"]
        elif ev["type"] == "done":
            content = ev.get("content", content)
            thinking = ev.get("thinking", thinking)
            tool_calls = ev.get("tool_calls", tool_calls)
            usage = ev.get("usage", usage)
            finish_reason = ev.get("finish_reason", finish_reason)
    return {
        "content": content.strip(),
        "thinking": thinking.strip(),
        "tool_calls": tool_calls,
        "usage": usage,
        "finish_reason": finish_reason,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Compatibilidad con el antiguo specialist_llm
# ─────────────────────────────────────────────────────────────────────────────

def call_specialist_llm(
    messages: list[dict],
    tools: list[dict] | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    timeout: int = 120,
    max_steps: int = 5,
) -> dict:
    """Wrapper compatible con specialist_llm.call_specialist_llm.

    max_steps se conserva por compatibilidad de firma, pero la ejecución de
    herramientas ahora vive en el loop del agente (core/agent.py), no aquí.
    """
    del max_steps  # conservado solo por compatibilidad de firma
    result = chat(
        messages, tools,
        provider=provider, model=model, api_key=api_key, effort="off", timeout=timeout,
    )
    return {"content": result["content"], "tool_calls": result["tool_calls"]}


# ─────────────────────────────────────────────────────────────────────────────
# Inicialización del auto-refresh de modelos gratuitos (estilo FreeLLMAPI)
# ─────────────────────────────────────────────────────────────────────────────

def _init_free_models_refresh() -> None:
    """Inicia el hilo de auto-refresh de modelos gratuitos al importar el módulo."""
    try:
        from core.free_models import start_background_refresh
        start_background_refresh(get_api_key)
    except Exception as e:
        print(f"[Providers] No se pudo iniciar auto-refresh de modelos gratuitos: {e}")

# Ejecutar al importar
_init_free_models_refresh()
