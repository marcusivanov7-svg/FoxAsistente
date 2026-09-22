"""user_profile — learn the user's style over time (ported from AMSY).

Records writing tone, common phrases and preferences so the assistant can adapt
its own output ("escribí un mail como yo lo haría"). Stored locally; retrieval
is a local search, like smart_memory.
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
STORE_PATH = BASE_DIR / "memory" / "user_profile.json"
_lock      = Lock()

_KINDS = {"tone", "phrase", "preference", "greeting", "signature", "habit"}


def _load() -> dict:
    if not STORE_PATH.exists():
        return {"samples": [], "traits": {}}
    try:
        d = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return {"samples": [], "traits": {}}
        d.setdefault("samples", [])
        d.setdefault("traits", {})
        return d
    except Exception:
        return {"samples": [], "traits": {}}


def _save(d: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        STORE_PATH.write_text(
            json.dumps(d, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def record(kind: str, value: str) -> str:
    kind = (kind or "tone").strip().lower()
    if kind not in _KINDS:
        kind = "tone"
    value = (value or "").strip()
    if not value:
        return "user_profile: falta el valor a registrar."
    d = _load()
    d["samples"].append({
        "id":      uuid.uuid4().hex[:8],
        "kind":    kind,
        "value":   value[:600],
        "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    d["samples"] = d["samples"][-200:]
    _save(d)
    return f"Estilo registrado ({kind}): {value[:60]}"


def profile() -> str:
    d = _load()
    samples = d.get("samples", [])
    if not samples:
        return ("Todavía no registré tu estilo. "
                "Puedo empezar cuando vea cómo escribís — pedime que lo recuerde.")
    by_kind: dict[str, list[str]] = {}
    for s in samples[-40:]:
        by_kind.setdefault(s["kind"], []).append(s["value"])
    lines = ["[PERFIL DE ESTILO DEL USUARIO — adaptá tu tono usando esto]"]
    for kind in sorted(by_kind):
        vals = by_kind[kind][-3:]
        lines.append(f"- {kind}: " + " | ".join(vals))
    return "\n".join(lines)


def user_profile(parameters=None, player=None, **kwargs) -> str:
    p = parameters or {}
    action = (p.get("action") or "profile").strip().lower()
    if action == "record":
        return record(p.get("kind", "tone"), p.get("value"))
    return profile()

TOOL = {
    "name": 'user_profile',
    "description": "Registra y recupera el estilo del usuario (tono al escribir, frases, preferencias) para adaptar tu propia respuesta. action='record' con kind y value para aprender; sin action (o action='profile') para ver el perfil aprendido. Usalo cuando el usuario pida 'escribí un mail como yo lo haría' o notes su forma de expresarse.",
    "parameters": {   'type': 'OBJECT',
        'properties': {   'action': {   'type': 'STRING',
                                        'description': 'record | profile'},
                          'kind': {   'type': 'STRING',
                                      'description': 'tone | phrase | preference | '
                                                     'greeting | signature | '
                                                     'habit'},
                          'value': {   'type': 'STRING',
                                       'description': 'El texto/muestra a '
                                                      'registrar'}},
        'required': []},
    "handler": user_profile,
}
