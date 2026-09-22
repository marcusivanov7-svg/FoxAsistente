# -*- coding: utf-8 -*-
"""specialist_llm.py — Capa de compatibilidad.

Este módulo quedó como shim hacia `core/providers.py`, que es ahora la fuente
única de verdad para proveedores, modelos y llamadas de chat. Se mantiene para
no romper las importaciones existentes en `main.py` y `ui.py`.
"""
from __future__ import annotations

from core.providers import (  # noqa: F401
    PROVIDERS,
    REASONING_LEVELS,
    REASONING_CONFIG,
    call_specialist_llm,
    chat,
    get_active_model,
    get_active_provider,
    get_api_key,
    list_local_models,
    list_providers,
    normalize_tools,
    reasoning_levels,
    stream_chat,
    supports_reasoning,
    to_anthropic_tools,
    to_gemini_tools,
)
