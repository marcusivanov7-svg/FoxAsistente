# -*- coding: utf-8 -*-
"""permissions.py — Control de permisos (sandbox) para el modo Agente.

Implementa los tres niveles que se eligen en la UI del chat:

    full       → sin restricciones (comportamiento actual de Fox).
    workspace  → solo puede tocar archivos dentro de las carpetas permitidas
                 y no puede ejecutar acciones de control del sistema.
    read_only  → solo lectura: puede leer (web, memoria, archivos dentro del
                 workspace), pero no escribir, crear, borrar ni mover nada.

El enforcement es REAL: se ejecuta en el dispatcher del agente, ANTES de llamar
a la herramienta. No es solo una instrucción en el prompt.

Clasificación de herramientas:
    safe_read      → lectura pura, permitida en todos los modos.
    memory         → estado interno de Fox (memoria/reglas/bandeja), permitido.
    filesystem     → toca archivos; se valida contra las carpetas permitidas.
    system_control → control del PC/sistema; solo en modo full.

La clasificación es un diccionario editable: agregá o mové herramientas acá
según tu criterio.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

# ─────────────────────────────────────────────────────────────────────────────
# Modos
# ─────────────────────────────────────────────────────────────────────────────

MODES = ("full", "workspace", "read_only")
MODE_LABELS = {
    "full": "Acceso completo",
    "workspace": "Solo workspace",
    "read_only": "Solo lectura",
}

# ─────────────────────────────────────────────────────────────────────────────
# Clasificación de herramientas
# ─────────────────────────────────────────────────────────────────────────────

TOOL_KIND: dict[str, str] = {
    # ── Lectura pura ──
    "web_search": "safe_read",
    "weather_report": "safe_read",
    "system_status": "safe_read",
    "flight_finder": "safe_read",
    "window_context": "safe_read",
    "youtube_video": "safe_read",
    "screen_process": "safe_read",
    "close_camera": "safe_read",
    "recall_memory": "safe_read",

    # ── Estado interno de Fox (memoria/reglas/bandeja/recordatorios) ──
    "save_memory": "memory",
    "smart_memory": "memory",
    "rules_engine": "memory",
    "desktop_memory": "memory",
    "productivity_inbox": "memory",
    "user_profile": "memory",
    "reminder": "memory",

    # ── Herramientas que tocan archivos ──
    "file_controller": "filesystem",
    "file_processor": "filesystem",
    "office_builder": "filesystem",
    "image_generator": "filesystem",
    "code_helper": "filesystem",
    "dev_agent": "filesystem",
    "game_updater": "filesystem",
    "desktop_control": "filesystem",
    "undo": "filesystem",
    "project_navigator": "filesystem",

    # ── Control del sistema / PC (solo full) ──
    "open_app": "system_control",
    "computer_settings": "system_control",
    "computer_control": "system_control",
    "windows_settings": "system_control",
    "browser_control": "system_control",
    "manage_monitor": "system_control",
    "shutdown_fox": "system_control",
    "send_message": "system_control",
    "spotify_control": "system_control",
    "telegram_api": "system_control",
    "tuya_control": "system_control",
    "amsy_superpowers": "system_control",
    "delegate_task": "system_control",
}

# Palabras que indican una operación de SOLO LECTURA en una herramienta de
# archivos (para permitirlas en modo read_only).
READ_ACTIONS = {
    "list", "read", "open", "view", "info", "inspect", "search", "find",
    "transcribe", "extract_info", "summarize", "preview", "cat", "get",
    "show", "display", "status", "exists", "size", "count", "parse",
}

_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_DRIVE_RE = re.compile(r"^[a-zA-Z]:[\\/]")


def _is_url(s: str) -> bool:
    return bool(_URL_RE.match(s))


def _looks_like_path(s: str) -> bool:
    """Heurística: ¿este string representa una ruta de archivo/carpeta?"""
    if not s or _is_url(s):
        return False
    if _DRIVE_RE.match(s):
        return True
    if "\\" in s or "/" in s:
        return True
    # Ruta absoluta Unix o existente en disco
    if s.startswith("~"):
        return True
    return False


def iter_path_strings(args: Any) -> Iterable[str]:
    """Recorre los argumentos recursivamente y devuelve strings que parecen rutas."""
    if isinstance(args, str):
        if _looks_like_path(args):
            yield args
    elif isinstance(args, dict):
        for v in args.values():
            yield from iter_path_strings(v)
    elif isinstance(args, (list, tuple)):
        for v in args:
            yield from iter_path_strings(v)


def _has_read_action(args: dict) -> bool:
    """Detecta una operación de solo lectura en los argumentos de la herramienta."""
    if not isinstance(args, dict):
        return False
    for key in ("action", "mode", "operation", "op", "command"):
        val = args.get(key)
        if isinstance(val, str) and val.strip().lower() in READ_ACTIONS:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# PathGuard
# ─────────────────────────────────────────────────────────────────────────────

class PathGuard:
    """Valida rutas contra una lista de carpetas permitidas."""

    def __init__(self, roots: Iterable[str] | None = None) -> None:
        self.roots: list[Path] = [
            Path(r).expanduser().resolve() for r in (roots or []) if r
        ]

    def set_roots(self, roots: Iterable[str] | None) -> None:
        self.roots = [Path(r).expanduser().resolve() for r in (roots or []) if r]

    def resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            # Ruta relativa → se asume dentro del workspace (raíz primaria).
            base = self.roots[0] if self.roots else Path.cwd()
            p = base / p
        try:
            return p.resolve()
        except Exception:
            return p

    def is_inside(self, path: str) -> bool:
        if not self.roots:
            return True  # sin raíces configuradas → no hay restricción de ubicación
        rp = self.resolve(path)
        for root in self.roots:
            try:
                rp.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def check_path(self, path: str) -> tuple[bool, str]:
        if not self.roots:
            return True, ""
        if self.is_inside(path):
            return True, ""
        return False, f"La ruta '{path}' está fuera de las carpetas permitidas."


# ─────────────────────────────────────────────────────────────────────────────
# Política de sandbox
# ─────────────────────────────────────────────────────────────────────────────

def tool_status_kind(tool_name: str, args: dict | None) -> str:
    """Devuelve el verbo de estado para la UI: 'reading' | 'writing' | 'executing'."""
    kind = TOOL_KIND.get(tool_name, "system_control")
    if kind == "filesystem":
        return "reading" if _has_read_action(args or {}) else "writing"
    if kind in ("safe_read", "memory"):
        return "reading"
    return "executing"


def check_tool_call(
    tool_name: str,
    args: dict | None,
    *,
    mode: str = "full",
    roots: Iterable[str] | None = None,
) -> tuple[bool, str]:
    """Decide si una llamada a herramienta está permitida según el modo.

    Returns:
        (permitido, motivo) — motivo vacío si está permitido.
    """
    mode = (mode or "full").strip().lower()
    if mode not in MODES:
        mode = "full"
    if mode == "full":
        return True, ""

    kind = TOOL_KIND.get(tool_name, "system_control")  # desconocido → conservador
    args = args or {}

    if kind == "system_control":
        return False, f"La herramienta '{tool_name}' requiere acceso completo."

    if kind in ("safe_read", "memory"):
        return True, ""

    # filesystem
    guard = PathGuard(roots)
    for p in iter_path_strings(args):
        ok, reason = guard.check_path(p)
        if not ok:
            return False, reason

    if mode == "read_only":
        if not _has_read_action(args):
            return False, (
                f"Modo solo lectura: '{tool_name}' no puede modificar archivos. "
                "Usá una operación de lectura (list/read/info/search)."
            )
    return True, ""


class Sandbox:
    """Envoltura con estado para usar desde la UI / agente."""

    def __init__(self, mode: str = "full", roots: Iterable[str] | None = None) -> None:
        self.mode = mode
        self.roots = list(roots or [])

    def set_mode(self, mode: str) -> None:
        self.mode = mode

    def set_roots(self, roots: Iterable[str]) -> None:
        self.roots = list(roots)

    def check(self, tool_name: str, args: dict | None) -> tuple[bool, str]:
        return check_tool_call(tool_name, args, mode=self.mode, roots=self.roots)
