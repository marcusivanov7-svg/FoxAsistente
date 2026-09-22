# -*- coding: utf-8 -*-
"""agent_memory.py — Extracción de memoria del agente y persistencia.

Después de cada conversación del modo agente, analiza el historial con el LLM
y guarda hechos relevantes (nombre, preferencias, proyectos, etc.) en la
memoria de largo plazo del usuario (memory/long_term.json).

Se ejecuta en background (asyncio.to_thread) para no bloquear el chat.
"""
from __future__ import annotations

import json
from typing import Any


# Prompt para extracción de memoria
_EXTRACT_PROMPT = """\
Analizá esta conversación entre un usuario y un asistente IA (Fox).
Extrae únicamente hechos nuevos sobre el USUARIO (no sobre el asistente ni la tarea en sí).

Buscá:
- Nombre, edad, ciudad, trabajo o datos de identidad
- Preferencias, gustos o disgustos (software, herramientas, idiomas, etc.)
- Proyectos activos o en los que está trabajando
- Personas mencionadas (amigos, familia, colegas)
- Planes o deseos futuros

Si NO hay hechos nuevos sobre el usuario, devolvé exactamente: {}

Devolvé SOLO un JSON válido con este formato (las categorías son opcionales, solo incluí las que tenés datos):
{
  "identity": {"nombre_campo": "valor"},
  "preferences": {"nombre_campo": "valor"},
  "projects": {"nombre_proyecto": "descripción breve"},
  "relationships": {"nombre_persona": "relación o contexto"},
  "wishes": {"nombre_plan": "descripción"}
}

NO incluyas notas sobre el código, tareas técnicas, ni resultados de herramientas.

Conversación:
"""


def _history_to_text(history: list[dict]) -> str:
    """Convierte el historial al formato texto para el prompt."""
    lines = []
    for msg in history:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if not content or role == "tool":
            continue
        prefix = "Usuario" if role == "user" else "Fox"
        lines.append(f"{prefix}: {content[:400]}")
    return "\n".join(lines[-40:])  # Últimos 40 mensajes máximo


def extract_and_save(history: list[dict], provider: str = "deepseek", model: str = "") -> dict:
    """Extrae hechos del historial y los guarda en la memoria de largo plazo.

    Devuelve el dict de hechos extraídos (o {} si no hubo nada relevante).
    Diseñado para ejecutarse en asyncio.to_thread().
    """
    if not history:
        return {}

    text = _history_to_text(history)
    if not text.strip():
        return {}

    try:
        from core import providers
        result = providers.chat(
            [{"role": "user", "content": _EXTRACT_PROMPT + text}],
            tools=None,
            provider=provider,
            model=model or providers.get_active_model(),
            effort="off",
            timeout=30,
        )
        raw = (result.get("content") or "").strip()
        # Buscar JSON en la respuesta
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            return {}
        facts = json.loads(raw[start:end + 1])
        if not isinstance(facts, dict) or not facts:
            return {}

        # Guardar en la memoria de largo plazo
        from memory import memory_manager
        memory_manager.update_memory(facts)
        print(f"[AgentMemory] Guardados {sum(len(v) for v in facts.values() if isinstance(v, dict))} hechos")
        return facts
    except Exception as e:
        print(f"[AgentMemory] Error extrayendo memoria: {e}")
        return {}


def get_memory_context() -> str:
    """Devuelve el bloque de memoria para incluir en el system prompt del agente."""
    try:
        from memory import memory_manager
        mem = memory_manager.load_memory()
        block = memory_manager.format_memory_for_prompt(mem)
        if block:
            return f"\n\n[Memoria del usuario]\n{block}"
        return ""
    except Exception:
        return ""
