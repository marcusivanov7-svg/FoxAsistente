"""desktop_memory — save/restore the desktop window state (ported from AMSY).

"guardá mi estado de trabajo" → snapshot of every visible window (title, bounds).
"volvé al estado de anoche" → restore (activate + reposition) that snapshot.
The latest auto-save is always kept under the name "__auto".
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from threading import Lock


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR   = get_base_dir()
STORE_PATH = BASE_DIR / "memory" / "desktop_memory.json"
_lock      = Lock()

_AUTO_NAME = "__auto"


def _load() -> dict:
    if not STORE_PATH.exists():
        return {}
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        STORE_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _list_windows() -> list[dict]:
    try:
        import pygetwindow as gw
        wins = []
        for w in gw.getAllWindows():
            title = (w.title or "").strip()
            if not title:
                continue
            try:
                wins.append({
                    "title":  title[:200],
                    "left":   int(w.left),
                    "top":    int(w.top),
                    "width":  int(w.width),
                    "height": int(w.height),
                })
            except Exception:
                continue
        return wins
    except Exception as e:
        print(f"[desktop_memory] no window list: {e}")
        return []


def _save_state(name: str) -> str:
    name = (name or "").strip() or _AUTO_NAME
    windows = _list_windows()
    if not windows:
        return "No pude listar ventanas (¿pygetwindow disponible?)."
    data = _load()
    data[name] = {"windows": windows, "saved": datetime.now().strftime("%Y-%m-%d %H:%M")}
    _save(data)
    return f"Estado guardado '{name}' con {len(windows)} ventanas."


def _restore_state(name: str) -> str:
    name = (name or "").strip() or _AUTO_NAME
    data = _load()
    snap = data.get(name)
    if not snap:
        if name == _AUTO_NAME and data:
            name = sorted(data.keys())[-1]
            snap = data.get(name)
        if not snap:
            return f"No hay un estado guardado llamado '{name}'."
    windows = snap.get("windows", [])
    try:
        import pygetwindow as gw
    except Exception:
        return "pygetwindow no disponible para restaurar."
    restored = 0
    for win in windows:
        title = win.get("title", "")
        try:
            matches = [w for w in gw.getWindowsWithTitle(title) if w.title.strip() == title]
            target = matches[0] if matches else None
            if target is None:
                matches = [w for w in gw.getAllWindows() if title.lower() in (w.title or "").lower()]
                target = matches[0] if matches else None
            if target is None:
                continue
            try:
                target.restore()
                target.moveTo(win.get("left", 0), win.get("top", 0))
                target.resizeTo(win.get("width", 800), win.get("height", 600))
            except Exception:
                pass
            try:
                target.activate()
            except Exception:
                pass
            restored += 1
        except Exception:
            continue
    return f"Restaurado '{name}': {restored}/{len(windows)} ventanas."


def _auto_list() -> str:
    data = _load()
    if not data:
        return "No hay estados guardados todavía."
    names = sorted(data.keys())
    lines = [f"{n} ({data[n].get('saved', '?')}, {len(data[n].get('windows', []))} ventanas)" for n in names]
    return "Estados guardados:\n" + "\n".join(lines)


def desktop_memory(parameters=None, player=None, **kwargs) -> str:
    p = parameters or {}
    action = (p.get("action") or "list").strip().lower()
    name = (p.get("name") or "").strip()
    if action == "save":
        return _save_state(name)
    if action == "restore":
        return _restore_state(name)
    if action == "auto":
        return _auto_list()
    return _auto_list()

TOOL = {
    "name": 'desktop_memory',
    "description": "Guarda y restaura el estado completo de las ventanas del escritorio. Usá action='save' cuando el usuario quiera conservar su disposición actual, action='restore' para volver a un estado guardado, action='auto' para listar estados.",
    "parameters": {   'type': 'OBJECT',
        'properties': {   'action': {   'type': 'STRING',
                                        'description': 'save | restore | auto'},
                          'name': {   'type': 'STRING',
                                      'description': 'Nombre del estado (ej: '
                                                     "'trabajo')"}},
        'required': []},
    "handler": desktop_memory,
}
