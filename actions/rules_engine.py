"""rules_engine — intent-based automations (ported from AMSY).

"cada vez que me llegue una factura, guardala y avisame" →
a persisted rule {trigger, do, created} that is injected into the system prompt
every session, so the assistant follows it with no extra round trip.
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
RULES_PATH = BASE_DIR / "memory" / "rules.json"
_lock      = Lock()


def _load() -> list:
    if not RULES_PATH.exists():
        return []
    try:
        data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(rules: list) -> None:
    RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        RULES_PATH.write_text(
            json.dumps(rules, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def add_rule(trigger: str, do: str) -> str:
    trigger = (trigger or "").strip()
    do = (do or "").strip()
    if not trigger or not do:
        return "rules_engine: necesito 'trigger' (cuándo) y 'do' (qué hacer)."
    rules = _load()
    rules.append({
        "id":      uuid.uuid4().hex[:8],
        "trigger": trigger[:200],
        "do":      do[:400],
        "created": datetime.now().strftime("%Y-%m-%d"),
    })
    _save(rules)
    return f"Regla creada: cuando '{trigger}' → {do}"


def list_rules() -> str:
    rules = _load()
    if not rules:
        return "No hay reglas todavía."
    return "\n".join(
        f"{i + 1}. [{r['id']}] {r['trigger']} → {r['do']}"
        for i, r in enumerate(rules)
    )


def remove_rule(ref: str) -> str:
    ref = (ref or "").strip().lower()
    if not ref:
        return "Indicá el id o número de la regla a borrar."
    rules = _load()
    target = None
    for i, r in enumerate(rules):
        if ref == r["id"] or ref == str(i + 1):
            target = i
            break
    if target is None:
        for i, r in enumerate(rules):
            if ref in r["trigger"].lower():
                target = i
                break
    if target is None:
        return f"No encontré una regla con '{ref}'."
    removed = rules.pop(target)
    _save(rules)
    return f"Regla borrada: {removed['trigger']}"


def format_rules_for_prompt() -> str:
    rules = _load()
    if not rules:
        return ""
    lines = ["[AUTOMATIZACIONES ACTIVAS — cumplilas siempre que apliquen, sin anunciarlas]"]
    for r in rules:
        lines.append(f"- Cuando {r['trigger']} → {r['do']}")
    return "\n".join(lines) + "\n"


def rules_engine(parameters=None, player=None, **kwargs) -> str:
    p = parameters or {}
    action = (p.get("action") or "list").strip().lower()
    if action == "add":
        return add_rule(p.get("trigger"), p.get("do"))
    if action in ("remove", "delete"):
        return remove_rule(p.get("id") or p.get("trigger"))
    return list_rules()

TOOL = {
    "name": 'rules_engine',
    "description": "Crea, lista o borra automatizaciones por intención. Cuando el usuario diga frases como 'cada vez que X, hacé Y' o 'siempre que X, entonces Y', usá action='add' con trigger (cuándo) y do (qué hacer) INMEDIATAMENTE, sin explicar cómo funciona.",
    "parameters": {   'type': 'OBJECT',
        'properties': {   'action': {   'type': 'STRING',
                                        'description': 'add | list | remove'},
                          'trigger': {   'type': 'STRING',
                                         'description': 'La condición/disparador '
                                                        "(ej: 'me llegue una "
                                                        "factura')"},
                          'do': {   'type': 'STRING',
                                    'description': "Qué hacer (ej: 'guardala y "
                                                   "avisame')"},
                          'id': {   'type': 'STRING',
                                    'description': 'id o número de regla a '
                                                   'borrar'}},
        'required': []},
    "handler": rules_engine,
}
