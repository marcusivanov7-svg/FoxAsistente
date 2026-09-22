"""productivity_inbox — persistent capture of ideas/tasks (ported from AMSY).

Bandeja persistente para capturar tareas, ideas y recordatorios al vuelo, con
prioridades y fijados. Todo local, en memory/inbox.json.
"""
from __future__ import annotations

import json
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
STORE_PATH = BASE_DIR / "memory" / "inbox.json"
_lock      = Lock()

_PRIORITIES = {"low": 2, "normal": 1, "high": 0}


def _load() -> list:
    if not STORE_PATH.exists():
        return []
    try:
        d = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _save(items: list) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        STORE_PATH.write_text(
            json.dumps(items, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def capture(text: str, priority: str = "normal") -> str:
    text = (text or "").strip()
    if not text:
        return "inbox: falta el texto a capturar."
    priority = priority if priority in _PRIORITIES else "normal"
    items = _load()
    items.append({
        "id":       "in" + uuid.uuid4().hex[:6],
        "text":     text[:400],
        "priority": priority,
        "done":     False,
        "pinned":   False,
        "created":  _now(),
    })
    _save(items)
    return f"Anotado en tu inbox: {text[:80]}"


def _find(items: list, ref: str):
    ref = (ref or "").strip().lower()
    if not ref:
        return None
    for it in items:
        if ref == it["id"]:
            return it
    for it in items:
        if ref in it["text"].lower():
            return it
    return None


def complete(ref: str) -> str:
    items = _load()
    it = _find(items, ref)
    if it is None:
        return f"No encontré '{ref}' en tu inbox."
    it["done"] = True
    _save(items)
    return f"Completado: {it['text'][:60]}"


def pin(ref: str) -> str:
    items = _load()
    it = _find(items, ref)
    if it is None:
        return f"No encontré '{ref}' en tu inbox."
    it["pinned"] = not it.get("pinned", False)
    _save(items)
    return ("Fijado: " if it["pinned"] else "Desfijado: ") + it["text"][:60]


def clear_done() -> str:
    items = [i for i in _load() if not i.get("done")]
    _save(items)
    return "Inbox: completados limpiados."


def list_items() -> str:
    items = [i for i in _load() if not i.get("done")]
    if not items:
        return "Tu inbox está vacío."
    items.sort(key=lambda x: (not x.get("pinned"), _PRIORITIES.get(x.get("priority"), 1)))
    lines = []
    for it in items:
        mark = "📌" if it.get("pinned") else " "
        lines.append(f"{mark}[{it['id']}] ({it['priority']}) {it['text']}")
    return "Inbox pendiente:\n" + "\n".join(lines)


def summary() -> str:
    items = _load()
    pend = [i for i in items if not i.get("done")]
    if not pend:
        return "No tenés pendientes en el inbox."
    high = [i for i in pend if i.get("priority") == "high"]
    head = f"Tenés {len(pend)} pendientes" + (f" ({len(high)} alta prioridad)" if high else "") + ":"
    return head + "\n" + "\n".join(f"- {i['text'][:80]}" for i in pend[:5])


def productivity_inbox(parameters=None, player=None, **kwargs) -> str:
    p = parameters or {}
    action = (p.get("action") or "list").strip().lower()
    if action in ("capture", "add"):
        return capture(p.get("text"), p.get("priority", "normal"))
    if action in ("complete", "done"):
        return complete(p.get("id") or p.get("text"))
    if action == "pin":
        return pin(p.get("id") or p.get("text"))
    if action == "clear_done":
        return clear_done()
    if action == "summary":
        return summary()
    if action == "status":
        items = _load()
        pend = sum(1 for i in items if not i.get("done"))
        return f"Pendientes: {pend} | Completados: {len(items) - pend}"
    return list_items()

TOOL = {
    "name": 'productivity_inbox',
    "description": "Bandeja persistente para capturar ideas, tareas y recordatorios al vuelo. action='capture' con text (y priority low|normal|high) para anotar. action='list' para ver pendientes, 'summary' para resumen hablado, 'complete' para marcar hecho, 'pin' para fijar, 'clear_done' para limpiar. Usalo proactivamente cuando el usuario mencione tareas sueltas o 'no me olvides de...'.",
    "parameters": {   'type': 'OBJECT',
        'properties': {   'action': {   'type': 'STRING',
                                        'description': 'capture | list | summary | '
                                                       'complete | pin | '
                                                       'clear_done | status'},
                          'text': {   'type': 'STRING',
                                      'description': 'Texto a capturar'},
                          'priority': {   'type': 'STRING',
                                          'description': 'low | normal | high'},
                          'id': {   'type': 'STRING',
                                    'description': 'id o texto de la tarea a '
                                                   'completar/fijar'}},
        'required': []},
    "handler": productivity_inbox,
}
