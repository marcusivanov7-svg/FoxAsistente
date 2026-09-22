"""project_navigator — navigate drives and open projects in editors (ported from AMSY).

"abrime el proyecto X en VS Code" → opens the path in the configured editor.
"qué unidades tengo" → lists drives.
"""
from __future__ import annotations

import os
import subprocess
import sys


def get_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_EDITORS = {
    "vscode": "code", "code": "code", "cursor": "cursor", "intellij": "idea",
    "idea": "idea", "pycharm": "pycharm", "sublime": "subl", "notepad": "notepad.exe",
}


def _open_in_editor(path: str, editor: str) -> bool:
    exe = _EDITORS.get((editor or "vscode").lower(), editor or "code")
    try:
        subprocess.Popen(
            [exe, path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def _list_drives() -> str:
    if sys.platform == "win32":
        import string
        drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
        return "Unidades disponibles: " + ", ".join(drives)
    return "Unidades: / (raíz)"


def project_navigator(parameters=None, player=None, **kwargs) -> str:
    p = parameters or {}
    action = (p.get("action") or "open").strip().lower()
    path = (p.get("path") or "").strip()

    if action in ("drives", "list"):
        return _list_drives()

    if action == "open" and path:
        path = os.path.expandvars(os.path.expanduser(path))
        if os.path.isfile(path) or os.path.isdir(path):
            editor = p.get("editor") or "vscode"
            if _open_in_editor(path, editor):
                return f"Abierto {path} en {editor}."
            return f"No pude abrir {path} en {editor} (¿está instalado?)."
        return f"Ruta no encontrada: {path}"

    return "project_navigator: usá action='open' con path, o action='drives'."

TOOL = {
    "name": 'project_navigator',
    "description": "Navega el sistema de archivos y abre proyectos en editores. action='open' con path para abrir un proyecto/archivo en VS Code, Cursor, IntelliJ, etc. action='drives' para listar unidades.",
    "parameters": {   'type': 'OBJECT',
        'properties': {   'action': {   'type': 'STRING',
                                        'description': 'open | drives'},
                          'path': {   'type': 'STRING',
                                      'description': 'Ruta del proyecto/archivo a '
                                                     'abrir'},
                          'editor': {   'type': 'STRING',
                                        'description': 'vscode | cursor | intellij '
                                                       '| pycharm | sublime | '
                                                       'notepad'}},
        'required': []},
    "handler": project_navigator,
}
