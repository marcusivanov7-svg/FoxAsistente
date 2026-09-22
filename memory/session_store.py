# -*- coding: utf-8 -*-
"""session_store.py — Gestión de sesiones del modo agente de Fox.

Guarda/carga historial de conversaciones en memory/agent_sessions.json.
Cada sesión tiene: id, title, created_at, updated_at, provider, model, history.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from threading import Lock
import sys


def _sessions_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "memory" / "agent_sessions.json"
    return Path(__file__).resolve().parent / "agent_sessions.json"


_lock = Lock()


def _load_raw() -> dict:
    p = _sessions_path()
    if not p.exists():
        return {"sessions": {}, "active": None}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"sessions": {}, "active": None}


def _save_raw(data: dict) -> None:
    p = _sessions_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _auto_title(history: list[dict]) -> str:
    """Genera un título a partir del primer mensaje del usuario."""
    for msg in history:
        if msg.get("role") == "user":
            text = str(msg.get("content") or "").strip()
            if text:
                return text[:60] + ("…" if len(text) > 60 else "")
    return "Nueva conversación"


class SessionStore:
    """Interfaz de alto nivel para el historial de sesiones del agente."""

    # ── Listado ───────────────────────────────────────────────────────────────
    @staticmethod
    def list_sessions() -> list[dict]:
        """Devuelve sesiones ordenadas por updated_at desc (más reciente primero)."""
        data = _load_raw()
        sessions = list((data.get("sessions") or {}).values())
        sessions.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
        # No devolver el historial completo (puede ser pesado)
        return [
            {
                "id": s["id"],
                "title": s.get("title") or "Sin título",
                "created_at": s.get("created_at", 0),
                "updated_at": s.get("updated_at", 0),
                "provider": s.get("provider", ""),
                "model": s.get("model", ""),
                "msg_count": len(s.get("history") or []),
            }
            for s in sessions
        ]

    # ── Crear / guardar ───────────────────────────────────────────────────────
    @staticmethod
    def new_session() -> str:
        """Crea una nueva sesión vacía y la marca como activa. Devuelve su id."""
        sid = uuid.uuid4().hex[:12]
        now = time.time()
        session = {
            "id": sid,
            "title": "Nueva conversación",
            "created_at": now,
            "updated_at": now,
            "provider": "",
            "model": "",
            "history": [],
        }
        data = _load_raw()
        data.setdefault("sessions", {})
        data["sessions"][sid] = session
        data["active"] = sid
        _save_raw(data)
        return sid

    @staticmethod
    def save_session(
        session_id: str,
        history: list[dict],
        *,
        provider: str = "",
        model: str = "",
        title: str | None = None,
    ) -> None:
        """Guarda/actualiza una sesión existente."""
        data = _load_raw()
        sessions = data.setdefault("sessions", {})
        existing = sessions.get(session_id) or {}
        now = time.time()
        computed_title = title or _auto_title(history) or existing.get("title") or "Nueva conversación"
        sessions[session_id] = {
            "id": session_id,
            "title": computed_title,
            "created_at": existing.get("created_at", now),
            "updated_at": now,
            "provider": provider or existing.get("provider", ""),
            "model": model or existing.get("model", ""),
            "history": history,
        }
        _save_raw(data)

    # ── Cargar ────────────────────────────────────────────────────────────────
    @staticmethod
    def load_session(session_id: str) -> dict | None:
        """Devuelve la sesión completa (con historial) o None si no existe."""
        data = _load_raw()
        return (data.get("sessions") or {}).get(session_id)

    # ── Renombrar ─────────────────────────────────────────────────────────────
    @staticmethod
    def rename_session(session_id: str, new_title: str) -> None:
        """Cambia el título de una sesión."""
        data = _load_raw()
        s = (data.get("sessions") or {}).get(session_id)
        if s:
            s["title"] = new_title.strip() or "Sin título"
            _save_raw(data)

    # ── Eliminar ──────────────────────────────────────────────────────────────
    @staticmethod
    def delete_session(session_id: str) -> None:
        """Elimina una sesión. Si era la activa, la activa pasa a la más reciente."""
        data = _load_raw()
        sessions = data.get("sessions") or {}
        sessions.pop(session_id, None)
        if data.get("active") == session_id:
            remaining = sorted(sessions.values(), key=lambda s: s.get("updated_at", 0), reverse=True)
            data["active"] = remaining[0]["id"] if remaining else None
        _save_raw(data)

    # ── Sesión activa ─────────────────────────────────────────────────────────
    @staticmethod
    def get_active_id() -> str | None:
        return _load_raw().get("active")

    @staticmethod
    def set_active(session_id: str) -> None:
        data = _load_raw()
        data["active"] = session_id
        _save_raw(data)
