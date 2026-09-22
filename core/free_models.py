# -*- coding: utf-8 -*-
"""free_models.py — Motor estilo FreeLLMAPI: auto-fetch dinámico de modelos gratuitos.

Consulta /v1/models de cada proveedor, filtra modelos gratuitos (pricing=0, :free, tier free),
cachea con TTL y rota automáticamente ante 429/quota/timeout.

Inspirado en FreeLLMAPI: penalty decay (2 min), score-based ordering (reliability + speed + headroom),
headroom guardrail, y provider-level bench.
"""
from __future__ import annotations

import json
import time
import threading
from pathlib import Path
from typing import Callable, Optional
import requests

def _get_base_dir() -> Path:
    return Path(__file__).resolve().parent.parent

_CACHE_PATH = _get_base_dir() / "memory" / "free_models_cache.json"
_CACHE_LOCK = threading.Lock()
_STATS_LOCK = threading.Lock()

# TTL de caché: 12 horas
CACHE_TTL_SECONDS = 12 * 3600

# ── Penalty System (FreeLLMAPI-style) ──────────────────────────────────────────
# Decay: 2 min, max penalty 10 (positions in priority)
PENALTY_PER_429 = 3          # cada 429 suma 3 posiciones
PENALTY_PER_FAIL = 1         # cada fallo genérico suma 1
MAX_PENALTY = 10             # techo para que no se hunda para siempre
DECAY_INTERVAL_MS = 2 * 60 * 1000  # decay cada 2 min
DECAY_AMOUNT = 1             # reduce 1 por intervalo de decay

# Headroom guardrail: no rutear si cuota usada > 90%
HEADROOM_RAMP_START = 0.9    # empezar a penalizar al 90%
HEADROOM_FLOOR = 0.0         # score floor al 100%

# Ventana de rate limit para headroom (aprox)
RATE_WINDOW_MIN_MS = 60 * 1000
RATE_WINDOW_DAY_MS = 24 * 60 * 60 * 1000

# Modelos pre-bloqueados (conocidos problemáticos)
PREBLOCKED_MODELS = {
    "groq/openai/gpt-oss-20b",  # 429 inmediato
    "groq/openai/gpt-oss-120b",  # puede dar 429
    "groq/qwen/qwen3.8-27b",  # 413 Payload Too Large (contexto limitado)
}

# Stats por modelo (en memoria, se persisten en cache)
# key: "provider/model" -> {successes, failures, last_latency_ms, last_used, rpm_used, rpd_used, tpm_used, monthly_used}
_MODEL_STATS: dict[str, dict] = {}

# Penalty tracking (FreeLLMAPI style)
# key: "provider/model" -> {penalty, last_hit, count}
_MODEL_PENALTIES: dict[str, dict] = {}

# Configuración de endpoints para auto-fetch de modelos
PROVIDER_MODELS_CONFIG = {
    "groq": {
        "models_url": "https://api.groq.com/openai/v1/models",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_filter": lambda m: not any(x in m.get("id", "").lower() for x in 
            ["whisper", "audio", "tts", "speech", "guard", "safeguard", "compound", "orpheus", "allam", "prompt-guard"]),
    },
    "gemini": {
        "models_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "auth_header": None,
        "auth_prefix": "",
        "free_filter": lambda m: any(kw in m.get("name", "").lower() for kw in 
            ["flash-lite-latest", "flash-latest", "lite-latest"]),
    },
    "openrouter": {
        "models_url": "https://openrouter.ai/api/v1/models",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_filter": lambda m: (float(m.get("pricing", {}).get("prompt", 1)) == 0 and 
                                   float(m.get("pricing", {}).get("completion", 1)) == 0) 
                                  or ":free" in m.get("id", ""),
    },
    "nvidia": {
        "models_url": "https://integrate.api.nvidia.com/v1/models",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_filter": lambda m: True,
    },
    "bai": {
        "models_url": "https://api.b.ai/v1/models",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_filter": lambda m: True,
    },
    "huggingface": {
        "models_url": "https://huggingface.co/api/models",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_filter": lambda m: m.get("private", True) == False and m.get("gated", True) == False,
    },
    "together": {
        "models_url": "https://api.together.xyz/v1/models",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_filter": lambda m: True,
    },
    "perplexity": {
        "models_url": "https://api.perplexity.ai/v1/models",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_filter": lambda m: "sonar" in m.get("id", "").lower() and "pro" not in m.get("id", "").lower(),
    },
    "cohere": {
        "models_url": "https://api.cohere.com/v1/models",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_filter": lambda m: "command-r" in m.get("name", "").lower() and "plus" not in m.get("name", "").lower(),
    },
    "fireworks": {
        "models_url": "https://api.fireworks.ai/inference/v1/models",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_filter": lambda m: True,
    },
    "replicate": {
        "models_url": "https://api.replicate.com/v1/models",
        "auth_header": "Authorization",
        "auth_prefix": "Token ",
        "free_filter": lambda m: True,
    },
    "deepinfra": {
        "models_url": "https://api.deepinfra.com/v1/openai/models",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_filter": lambda m: True,
    },
    "deepseek": {
        "models_url": "https://api.deepseek.com/v1/models",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_filter": lambda m: "flash" in m.get("id", "").lower(),
    },
    "cerebras": {
        "models_url": "https://api.cerebras.ai/v1/models",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_filter": lambda m: True,
    },
    "mistral": {
        "models_url": "https://api.mistral.ai/v1/models",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_filter": lambda m: "small" in m.get("id", "").lower(),
    },
    "xai": {
        "models_url": "https://api.x.ai/v1/models",
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
        "free_filter": lambda m: "mini" in m.get("id", "").lower(),
    },
}

# Fallback estático si falla el fetch
STATIC_FALLBACK = {
    "groq": ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"],
    "gemini": ["gemini-flash-lite-latest", "gemini-flash-latest"],
    "openrouter": ["google/gemini-2.5-flash:free", "deepseek/deepseek-chat:free", "meta-llama/llama-3.3-70b-instruct:free"],
    "nvidia": ["nvidia/llama-3.1-nemotron-70b-instruct", "meta/llama-3.1-70b-instruct", "google/gemma-2-9b-it"],
    "bai": ["gpt-5-mini", "gpt-5-nano", "gemini-3.5-flash", "claude-sonnet-4.5"],
    "huggingface": ["meta-llama/Meta-Llama-3.1-8B-Instruct", "mistralai/Mistral-7B-Instruct-v0.3", "google/gemma-2-9b-it"],
    "together": ["meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo", "mistralai/Mistral-7B-Instruct-v0.3", "google/gemma-2-9b-it"],
    "perplexity": ["sonar", "sonar-reasoning"],
    "cohere": ["command-r", "command-r7b-12-2024"],
    "fireworks": ["accounts/fireworks/models/llama-v3p1-8b-instruct", "accounts/fireworks/models/qwen2p5-7b-instruct"],
    "replicate": ["meta/meta-llama-3.1-8b-instruct", "mistralai/Mistral-7B-Instruct-v0.3", "google/gemma-2-9b-it"],
    "deepinfra": ["meta-llama/Meta-Llama-3.1-8B-Instruct", "mistralai/Mistral-7B-Instruct-v0.3", "google/gemma-2-9b-it"],
    "deepseek": ["deepseek-flash"],
    "cerebras": ["llama-3.1-8b"],
    "mistral": ["mistral-small-latest"],
    "xai": ["grok-3-mini"],
    "ollama": [],
}

# Penalty constants (FreeLLMAPI)
PENALTY_PER_429 = 3
PENALTY_PER_FAIL = 1
MAX_PENALTY = 10
DECAY_INTERVAL_MS = 2 * 60 * 1000
DECAY_AMOUNT = 1

def _get_base_dir() -> Path:
    return Path(__file__).resolve().parent.parent

_CACHE_PATH = _get_base_dir() / "memory" / "free_models_cache.json"
_STATS_PATH = _get_base_dir() / "memory" / "free_models_stats.json"
_CACHE_LOCK = threading.Lock()
_STATS_LOCK = threading.Lock()

# TTL de caché: 12 horas
CACHE_TTL_SECONDS = 12 * 3600

# Headroom thresholds
HEADROOM_RAMP_START = 0.9
HEADROOM_FLOOR = 0.0

# Modelos pre-bloqueados
PREBLOCKED_MODELS = {
    "groq/openai/gpt-oss-20b",
    "groq/openai/gpt-oss-120b",
    "groq/qwen/qwen3.8-27b",
}

# Stats y penalties en memoria
_MODEL_STATS: dict[str, dict] = {}
_MODEL_PENALTIES: dict[str, dict] = {}

# ─── Helpers de Penalty (FreeLLMAPI style) ────────────────────────────────────

def _decay_penalty(key: str, now: float) -> int:
    """Aplica decay temporal y retorna penalty actual."""
    entry = _MODEL_PENALTIES.get(key)
    if not entry:
        return 0
    
    elapsed = now - entry["last_hit"]
    decay_steps = int(elapsed / DECAY_INTERVAL_MS)
    decayed = max(0, entry["penalty"] - decay_steps * DECAY_AMOUNT)
    
    if decayed == 0:
        with _STATS_LOCK:
            _MODEL_PENALTIES.pop(key, None)
        return 0
    
    entry["penalty"] = decayed
    return decayed

def get_penalty(provider: str, model: str) -> int:
    """Penalty actual con decay aplicado."""
    key = f"{provider}/{model}"
    return _decay_penalty(key, time.time())

def add_penalty(provider: str, model: str, weight: int = PENALTY_PER_FAIL) -> int:
    """Suma penalty y retorna el nuevo valor."""
    key = f"{provider}/{model}"
    now = time.time()
    with _STATS_LOCK:
        entry = _MODEL_PENALTIES.get(key)
        if entry:
            # decay first
            elapsed = now - entry["last_hit"]
            decay_steps = int(elapsed / DECAY_INTERVAL_MS)
            entry["penalty"] = max(0, entry["penalty"] - decay_steps * DECAY_AMOUNT)
            entry["penalty"] = min(entry["penalty"] + weight, MAX_PENALTY)
            entry["last_hit"] = now
            entry["count"] = entry.get("count", 0) + 1
        else:
            _MODEL_PENALTIES[key] = {"penalty": weight, "last_hit": now, "count": 1}
        return _MODEL_PENALTIES[key]["penalty"]

def record_success(provider: str, model: str, latency_ms: float = 0) -> None:
    """Registra éxito: reduce penalty y actualiza stats."""
    key = f"{provider}/{model}"
    now = time.time()
    with _STATS_LOCK:
        # Reduce penalty
        entry = _MODEL_PENALTIES.get(key)
        if entry:
            entry["penalty"] = max(0, entry["penalty"] - 1)
            if entry["penalty"] == 0:
                _MODEL_PENALTIES.pop(key, None)
        
        # Update stats
        stats = _MODEL_STATS.setdefault(key, {
            "successes": 0, "failures": 0, "last_latency_ms": 0,
            "last_used": 0, "rpm_used": 0, "rpd_used": 0, "tpm_used": 0, "monthly_used": 0
        })
        stats["successes"] += 1
        stats["last_latency_ms"] = latency_ms
        stats["last_used"] = now
        _persist_stats_locked()

def record_failure(provider: str, model: str, is_rate_limit: bool = False) -> int:
    """Registra fallo: suma penalty y actualiza stats. Retorna nuevo penalty."""
    weight = PENALTY_PER_429 if is_rate_limit else PENALTY_PER_FAIL
    penalty = add_penalty(provider, model, weight)
    
    key = f"{provider}/{model}"
    with _STATS_LOCK:
        stats = _MODEL_STATS.setdefault(key, {
            "successes": 0, "failures": 0, "last_latency_ms": 0,
            "last_used": 0, "rpm_used": 0, "rpd_used": 0, "tpm_used": 0, "monthly_used": 0
        })
        stats["failures"] += 1
        _persist_stats_locked()
    return penalty

def mark_model_failed(provider: str, model: str) -> None:
    """Compatibility wrapper for record_failure (legacy API)."""
    record_failure(provider, model, is_rate_limit=True)

# ─── Stats Persistence ────────────────────────────────────────────────────────

def _persist_stats_locked() -> None:
    """Persiste stats y penalties a disco (llamar con _STATS_LOCK)."""
    try:
        _STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATS_PATH.write_text(json.dumps({
            "timestamp": time.time(),
            "stats": _MODEL_STATS,
            "penalties": _MODEL_PENALTIES
        }, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def _load_stats() -> None:
    """Carga stats y penalties desde disco."""
    global _MODEL_STATS, _MODEL_PENALTIES
    try:
        with _STATS_LOCK:
            if _STATS_PATH.exists():
                data = json.loads(_STATS_PATH.read_text(encoding="utf-8"))
                _MODEL_STATS = data.get("stats", {})
                _MODEL_PENALTIES = data.get("penalties", {})
    except Exception:
        _MODEL_STATS = {}
        _MODEL_PENALTIES = {}

# Cargar al importar
_load_stats()

# ─── Headroom & Scoring ───────────────────────────────────────────────────────

def _get_model_stats(key: str) -> dict:
    with _STATS_LOCK:
        return _MODEL_STATS.get(key, {}).copy()

def get_headroom_factor(provider: str, model: str) -> float:
    """Factor de headroom 0.0-1.0 (1.0 = full capacity, 0.0 = exhausted).
    
    FreeLLMAPI usa monthly budget + rate windows (RPM/RPD/TPM).
    Aquí aproximamos con failure rate y recencia.
    """
    key = f"{provider}/{model}"
    stats = _get_model_stats(key)
    
    successes = stats.get("successes", 0)
    failures = stats.get("failures", 0)
    total = successes + failures
    
    if total < 5:
        return 1.0  # insuficientes datos → optimista
    
    failure_rate = failures / total
    # Penalizar si failure rate > 10%
    if failure_rate > 0.1:
        return max(0.0, 1.0 - (failure_rate - 0.1) * 5)
    
    # Penalizar si no se ha usado en 10 min (posible cooldown oculto)
    last_used = stats.get("last_used", 0)
    if time.time() - last_used > 600:
        return 0.5
    
    return 1.0

def calculate_score(provider: str, model: str, latency_ms: float = 0) -> float:
    """Score compuesto estilo FreeLLMAPI: reliability + speed + headroom.
    
    Returns 0.0 - 1.0 (mayor = mejor).
    """
    key = f"{provider}/{model}"
    stats = _get_model_stats(key)
    
    successes = stats.get("successes", 0)
    failures = stats.get("failures", 0)
    total = successes + failures
    
    # Reliability: Beta posterior mean (Laplace smoothing)
    reliability = (successes + 1) / (total + 2)
    
    # Speed: inversa de latencia (normalizada 0-1, 100ms = 1.0, 5000ms = 0.0)
    if latency_ms > 0:
        speed = max(0.0, 1.0 - min(latency_ms, 5000) / 5000)
    else:
        avg_latency = stats.get("last_latency_ms", 1000)
        speed = max(0.0, 1.0 - min(avg_latency, 5000) / 5000)
    
    # Headroom
    headroom = get_headroom_factor(provider, model)
    
    # Penalty reduce score efectivo
    penalty = get_penalty(provider, model)
    penalty_factor = max(0.0, 1.0 - penalty / MAX_PENALTY)
    
    # Pesos estilo FreeLLMAPI (balanced preset)
    score = (0.4 * reliability + 0.3 * speed + 0.2 * headroom + 0.1 * penalty_factor)
    return score

# ─── Cache & Model Discovery ──────────────────────────────────────────────────

def _get_base_dir() -> Path:
    return Path(__file__).resolve().parent.parent

_CACHE_PATH = _get_base_dir() / "memory" / "free_models_cache.json"
_STATS_PATH = _get_base_dir() / "memory" / "free_models_stats.json"
_CACHE_LOCK = threading.Lock()

CACHE_TTL_SECONDS = 12 * 3600

def _load_cache() -> dict:
    try:
        with _CACHE_LOCK:
            if _CACHE_PATH.exists():
                data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
                if time.time() - data.get("timestamp", 0) < CACHE_TTL_SECONDS:
                    return data.get("models", {})
    except Exception:
        pass
    return {}

def _save_cache(models_by_provider: dict) -> None:
    try:
        with _CACHE_LOCK:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_PATH.write_text(json.dumps({
                "timestamp": time.time(),
                "models": models_by_provider
            }, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def _fetch_models_for_provider(provider: str, api_key: str) -> list[str]:
    config = PROVIDER_MODELS_CONFIG.get(provider)
    if not config:
        return STATIC_FALLBACK.get(provider, [])
    
    url = config["models_url"]
    headers = {}
    
    if config["auth_header"] and api_key:
        if provider == "gemini":
            url += f"?key={api_key}"
        else:
            headers[config["auth_header"]] = f"{config['auth_prefix']}{api_key}"
    
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return STATIC_FALLBACK.get(provider, [])
        
        data = resp.json()
        models = []
        
        if "data" in data:
            items = data["data"]
        elif isinstance(data, list):
            items = data
        else:
            items = data.get("models", [])
        
        free_filter = config["free_filter"]
        for m in items:
            if isinstance(m, dict):
                model_id = m.get("id") or m.get("name") or m.get("modelId", "")
                if model_id and free_filter(m):
                    models.append(model_id)
        
        return models if models else STATIC_FALLBACK.get(provider, [])
    except Exception:
        return STATIC_FALLBACK.get(provider, [])

def get_free_models_for(provider: str, api_key: str = "") -> list[str]:
    if provider == "ollama":
        try:
            from core.providers import list_local_models
            return list_local_models() or ["llama3.2"]
        except Exception:
            return ["llama3.2"]
    
    cache = _load_cache()
    if provider in cache:
        return cache[provider]
    
    models = _fetch_models_for_provider(provider, api_key)
    if models:
        cache[provider] = models
        _save_cache(cache)
    return models

# ─── Core Selection Logic (FreeLLMAPI-style) ──────────────────────────────────

def is_model_blocked(provider: str, model: str) -> bool:
    """Hard block: penalty >= MAX_PENALTY."""
    return get_penalty(provider, model) >= MAX_PENALTY

def get_next_available_free_model(provider: str, api_key: str = "") -> tuple[str, str]:
    """Retorna (provider, model) del mejor modelo disponible (score-based).
    
    Lanza ValueError si no hay modelos disponibles.
    """
    models = get_free_models_for(provider, api_key)
    if not models:
        raise ValueError(f"No hay modelos gratuitos para {provider}")
    
    # Filtrar preblocked y hard-blocked, ordenar por score descendente
    candidates = []
    for m in models:
        key = f"{provider}/{m}"
        if key in PREBLOCKED_MODELS:
            continue
        if is_model_blocked(provider, m):
            continue
        
        score = calculate_score(provider, m)
        candidates.append((score, m))
    
    if not candidates:
        raise ValueError(f"Todos los modelos de {provider} están bloqueados/preblocked")
    
    # Ordenar por score descendente (mejor primero)
    candidates.sort(key=lambda x: x[0], reverse=True)
    best_model = candidates[0][1]
    
    # Log para debug
    print(f"[FreeRotator] {provider}: elegidos {len(candidates)}/{len(models)} modelos. Top: {best_model} (score={candidates[0][0]:.3f})")
    for s, m in candidates[:3]:
        print(f"  - {m}: score={s:.3f}, penalty={get_penalty(provider, m)}")
    
    return provider, best_model

def get_next_available_free_model_legacy(provider: str, api_key: str = "") -> tuple[str, str]:
    """Compatibilidad: orden fijo legacy (para callers que no quieren scoring)."""
    models = get_free_models_for(provider, api_key)
    if not models:
        raise ValueError(f"No hay modelos gratuitos para {provider}")
    
    for m in models:
        key = f"{provider}/{m}"
        if key in PREBLOCKED_MODELS:
            continue
        if not is_model_blocked(provider, m):
            return provider, m
    
    raise ValueError(f"Todos los modelos de {provider} están bloqueados/preblocked")

# ─── Cache & Refresh ──────────────────────────────────────────────────────────

def _load_cache() -> dict:
    try:
        with _CACHE_LOCK:
            if _CACHE_PATH.exists():
                data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
                if time.time() - data.get("timestamp", 0) < CACHE_TTL_SECONDS:
                    return data.get("models", {})
    except Exception:
        pass
    return {}

def _save_cache(models_by_provider: dict) -> None:
    try:
        with _CACHE_LOCK:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_PATH.write_text(json.dumps({
                "timestamp": time.time(),
                "models": models_by_provider
            }, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

# ─── Auto Refresh ─────────────────────────────────────────────────────────────

def start_background_refresh(get_api_key_fn: Callable[[str], str]) -> None:
    def _refresh_loop():
        while True:
            time.sleep(CACHE_TTL_SECONDS)
            try:
                cache = {}
                for prov in PROVIDER_MODELS_CONFIG:
                    api_key = get_api_key_fn(prov)
                    if api_key or prov == "ollama":
                        cache[prov] = _fetch_models_for_provider(prov, api_key)
                _save_cache(cache)
                print("[FreeRotator] 🔄 Caché de modelos gratuitos actualizada")
            except Exception as e:
                print(f"[FreeRotator] Error en auto-refresh: {e}")
    
    thread = threading.Thread(target=_refresh_loop, daemon=True, name="free-models-refresh")
    thread.start()

# ─── Compatibilidad ───────────────────────────────────────────────────────────

KNOWN_FREE_MODELS = STATIC_FALLBACK

# Inicialización
_load_stats()