# -*- coding: utf-8 -*-
"""agent_web/sessions.py — Persistencia de sesiones del agente.

Guarda el historial de cada conversación en un JSON separado por workspace
(directorio) para que sobrevivan al reinicio y organicen el árbol lateral.
"""
from __future__ import annotations

import json
import time
import uuid
import shutil
from pathlib import Path
from typing import Any

class WorkspaceStore:
    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.active_session = None
        self.active_workspace = None

    def list_workspaces(self) -> list[dict]:
        workspaces = []
        for ws_path in self.base_dir.iterdir():
            if ws_path.is_dir():
                sessions = []
                # Leer metadata del workspace (ej: rutas del disco)
                meta = {}
                meta_file = ws_path / "workspace.json"
                if meta_file.exists():
                    try:
                        meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                
                for sess_file in ws_path.glob("*.json"):
                    if sess_file.name == "workspace.json":
                        continue
                    try:
                        data = json.loads(sess_file.read_text(encoding="utf-8"))
                        sessions.append({
                            "id": sess_file.stem,
                            "title": data.get("title", "Conversación"),
                            "mode": data.get("mode", "Standard mode"),
                            "updated_at": data.get("updated_at", 0)
                        })
                    except Exception:
                        pass
                sessions.sort(key=lambda x: x["updated_at"], reverse=True)
                workspaces.append({
                    "id": ws_path.name,
                    "name": ws_path.name,
                    "sessions": sessions,
                    "folders": meta.get("folders", [])
                })
        
        if not workspaces:
            default_ws = self.base_dir / "default"
            default_ws.mkdir(parents=True, exist_ok=True)
            workspaces.append({
                "id": "default",
                "name": "default",
                "sessions": [],
                "folders": []
            })
            
        workspaces.sort(key=lambda x: x["name"])
        return workspaces
        
    def set_workspace_folders(self, workspace_name: str, folders: list[str]) -> None:
        """Guarda las rutas absolutas asociadas a un workspace."""
        if not workspace_name:
            return
        ws_path = self.base_dir / workspace_name
        ws_path.mkdir(parents=True, exist_ok=True)
        meta_file = ws_path / "workspace.json"
        
        meta = {}
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        
        meta["folders"] = folders
        meta_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def _get_session_path(self, sid: str, workspace_name: str = None) -> Path | None:
        if workspace_name:
            p = self.base_dir / workspace_name / f"{sid}.json"
            if p.exists(): return p
        for ws_path in self.base_dir.iterdir():
            if ws_path.is_dir():
                p = ws_path / f"{sid}.json"
                if p.exists(): return p
        return None

    def create_session(self, workspace_name: str, history: list[dict] = None, title: str = "Nueva conversación", mode: str = "Standard mode") -> str:
        sid = uuid.uuid4().hex[:12]
        now = time.time()
        # Fallback to default if workspace_name is not provided or empty
        workspace_name = workspace_name or "default"
        ws_path = self.base_dir / workspace_name
        ws_path.mkdir(parents=True, exist_ok=True)
        
        sess_file = ws_path / f"{sid}.json"
        data = {
            "id": sid,
            "title": title,
            "mode": mode,
            "created_at": now,
            "updated_at": now,
            "history": list(history or [])
        }
        sess_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.active_session = sid
        self.active_workspace = workspace_name
        return sid

    def save_session(self, sid: str, workspace_name: str, history: list[dict], title: str = None, mode: str = None) -> None:
        workspace_name = workspace_name or "default"
        path = self._get_session_path(sid, workspace_name)
        if not path:
            path = self.base_dir / workspace_name / f"{sid}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "id": sid,
                "title": title or "Nueva conversación",
                "mode": mode or "Standard mode",
                "created_at": time.time(),
                "updated_at": time.time(),
                "history": list(history or [])
            }
        else:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {"id": sid, "created_at": time.time(), "history": []}
            
            data["history"] = list(history or [])
            data["updated_at"] = time.time()
            if title: data["title"] = title
            if mode: data["mode"] = mode
            
            if path.parent.name != workspace_name:
                new_path = self.base_dir / workspace_name / f"{sid}.json"
                new_path.parent.mkdir(parents=True, exist_ok=True)
                path.unlink(missing_ok=True)
                path = new_path

        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.active_session = sid
        self.active_workspace = workspace_name

    def load_session(self, sid: str) -> list[dict]:
        path = self._get_session_path(sid)
        if path:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.active_session = sid
                self.active_workspace = path.parent.name
                return data.get("history", [])
            except Exception:
                pass
        return []

    def delete_session(self, sid: str) -> None:
        path = self._get_session_path(sid)
        if path:
            path.unlink(missing_ok=True)
        if self.active_session == sid:
            self.active_session = None

def title_from_history(history: list[dict]) -> str:
    """Deriva un título corto a partir del primer mensaje del usuario."""
    for m in history or []:
        if m.get("role") == "user":
            t = str(m.get("content", "")).strip().replace("\n", " ")
            return (t[:40] + "…") if len(t) > 40 else (t or "Conversación")
    return "Conversación"

def default_store() -> WorkspaceStore:
    """Devuelve el store por defecto."""
    base = Path(__file__).resolve().parent.parent
    return WorkspaceStore(base / "memory" / "workspaces")
