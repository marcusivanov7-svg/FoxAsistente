"""smart_memory — semantic, multi-layer memory (ported from AMSY).

Unlike save_memory (flat key→value facts), smart_memory stores *meaning*: a
summary plus detail, a category, and an importance weight (1-5). Recall is a
local lexical search (no network, no second model) that ranks by relevance ×
importance, so the assistant can retrieve "how I prefer to work" without that
fact ever riding in the prompt.
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path
from threading import Lock


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR   = get_base_dir()
STORE_PATH = BASE_DIR / "memory" / "smart_memory.json"
_lock      = Lock()

_CATEGORIES = {
    "identity", "preference", "habit", "project", "relationship",
    "wish", "note", "pattern", "contact", "workflow",
}
_MAX_ENTRIES = 500


def _load() -> list:
    if not STORE_PATH.exists():
        return []
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(entries: list) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        STORE_PATH.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _score(query: str, e: dict) -> float:
    """Relevance × importance. No embeddings, no network — well under a ms."""
    words = [w for w in re.split(r"[^\w]+", (query or "").lower()) if len(w) > 1]
    summary = (e.get("summary", "") or "").lower()
    detail  = (e.get("detail", "") or "").lower()
    cat     = (e.get("category", "") or "").lower()
    if not words:
        return float(e.get("importance", 1))
    s = 0.0
    for w in words:
        if w in summary:
            s += 4
        if w in detail:
            s += 2
        if w in cat:
            s += 1
    return s * (0.5 + 0.5 * (int(e.get("importance", 1)) / 5))


def store(category: str, summary: str, detail: str = "", importance: int = 3) -> str:
    category = (category or "note").strip().lower()
    if category not in _CATEGORIES:
        category = "note"
    summary = (summary or "").strip()
    if not summary:
        return "smart_memory: falta un resumen (summary)."
    importance = max(1, min(5, int(importance or 3)))

    entries = _load()
    entry = {
        "id":         uuid.uuid4().hex[:8],
        "category":   category,
        "summary":    summary[:300],
        "detail":     (detail or "").strip()[:1200],
        "importance": importance,
        "created":    _now(),
        "updated":    _now(),
    }
    entries.append(entry)
    if len(entries) > _MAX_ENTRIES:
        entries = entries[-_MAX_ENTRIES:]
    _save(entries)
    return f"Guardado ({category}, importancia {importance}): {summary}"


def recall(query: str, limit: int = 5) -> str:
    entries = _load()
    if not entries:
        return "No hay memorias semánticas guardadas todavía."
    scored = sorted(entries, key=lambda e: _score(query, e), reverse=True)
    top = scored[: max(1, int(limit or 5))]
    lines = []
    for e in top:
        line = f"[{e['category']} · imp {e['importance']}] {e['summary']}"
        if e.get("detail"):
            line += f" — {e['detail'][:180]}"
        lines.append(line)
    head = (
        f"Memorias relevantes para '{query}':"
        if query else "Memorias semánticas más importantes:"
    )
    return head + "\n" + "\n".join(lines)


def extract() -> str:
    """Surface what is already stored, ordered by importance, so the model can
    continue from context without a live transcript buffer."""
    entries = sorted(_load(), key=lambda e: -int(e.get("importance", 1)))[:10]
    if not entries:
        return "No hay nada para extraer todavía."
    return "Contexto guardado:\n" + "\n".join(
        f"[{e['category']}] {e['summary']}" for e in entries
    )


def smart_memory(parameters=None, player=None, **kwargs) -> str:
    p = parameters or {}
    action = (p.get("action") or "recall").strip().lower()
    if action == "store":
        return store(
            p.get("category"),
            p.get("summary"),
            p.get("detail", ""),
            p.get("importance", 3),
        )
    if action == "extract":
        return extract()
    # default / recall
    return recall(p.get("query", ""), p.get("limit", 5))

TOOL = {
    "name": 'smart_memory',
    "description": "Guarda y recupera significado profundo sobre el usuario (no datos sueltos): preferencias con contexto, hábitos, patrones, proyectos. action='store' para guardar (category, summary, detail, importance 1-5). action='recall' para buscar memorias relevantes por query. action='extract' para ver el contexto ya guardado. Usá esto cuando el usuario revele algo que cambia su forma de trabajar o preferir.",
    "parameters": {   'type': 'OBJECT',
        'properties': {   'action': {   'type': 'STRING',
                                        'description': 'store | recall | extract'},
                          'category': {   'type': 'STRING',
                                          'description': 'preference | habit | '
                                                         'project | relationship | '
                                                         'wish | note | pattern | '
                                                         'contact | workflow | '
                                                         'identity'},
                          'summary': {   'type': 'STRING',
                                         'description': 'Resumen corto del '
                                                        'significado a guardar'},
                          'detail': {   'type': 'STRING',
                                        'description': 'Detalle/contexto opcional'},
                          'importance': {   'type': 'INTEGER',
                                            'description': 'Importancia 1-5 '
                                                           '(default 3)'},
                          'query': {   'type': 'STRING',
                                       'description': 'Búsqueda para recall'},
                          'limit': {   'type': 'INTEGER',
                                       'description': 'Máx resultados de recall '
                                                      '(default 5)'}},
        'required': []},
    "handler": smart_memory,
}
