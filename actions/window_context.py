"""window_context — detect what the user is doing right now (ported from AMSY).

Lets the assistant adapt its help to the active window/app (e.g. "Teams activo
→ ¿preparo las notas de la reunión?"), without any screen capture.
"""
from __future__ import annotations

import sys


def _foreground_title() -> str:
    try:
        import pygetwindow as gw
        w = gw.getActiveWindow()
        if w and (w.title or "").strip():
            return (w.title or "").strip()
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.strip():
                return buf.value.strip()
        except Exception:
            pass
    return ""


def _foreground_process() -> str:
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        import psutil
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            return psutil.Process(pid.value).name()
        except Exception:
            return ""
    except Exception:
        return ""


def window_context(parameters=None, player=None, **kwargs) -> str:
    p = parameters or {}
    action = (p.get("action") or "context_info").strip().lower()
    title = _foreground_title()
    proc = _foreground_process()
    if title:
        msg = f"Ventana activa: {title}" + (f" ({proc})" if proc else "")
    else:
        msg = "No pude detectar la ventana activa."
    if action in ("context_info", "context", "info"):
        return msg + "\nUsá esto para adaptar tu ayuda al contexto actual del usuario."
    return msg

TOOL = {
    "name": 'window_context',
    "description": 'Detecta en qué ventana/aplicación está trabajando el usuario ahora mismo. Usalo para adaptar tu ayuda al contexto actual (ej: si Teams está activo, preparar contexto de reunión).',
    "parameters": {   'type': 'OBJECT',
        'properties': {   'action': {   'type': 'STRING',
                                        'description': 'context_info (default)'}},
        'required': []},
    "handler": window_context,
}
