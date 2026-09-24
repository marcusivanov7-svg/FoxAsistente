# -*- coding: utf-8 -*-
"""agent_web/server.py — Servidor web del modo agente (estilo DeepSeek Harness).

Expone el AgentEngine existente a través de una web UI (HTML/JS/CSS) y un
WebSocket para streaming, más persistencia de sesiones, feedback y plan mode.
Corre dentro del mismo event-loop de Fox (uvicorn como tarea asyncio),
compartiendo el motor agente, el sandbox y las herramientas sin duplicar nada.
"""
from __future__ import annotations

import asyncio
import json
import time
import traceback
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from core import providers
from core.agent import AgentEngine
from core.permissions import MODES, MODE_LABELS

from agent_web.sessions import WorkspaceStore, default_store, title_from_history

STATIC_DIR = Path(__file__).resolve().parent / "static"
BASE_DIR = Path(__file__).resolve().parent.parent

PLAN_PROMPT = (
    "Hacé un plan paso a paso para completar la siguiente tarea. "
    "Respondé SOLO con una lista numerada breve de pasos concretos. "
    "No ejecutes nada todavía, solo el plan.\n\nTarea: {task}"
)


class AgentWebServer:
    def __init__(
        self,
        agent_engine: Any,
        sandbox: Any,
        lock: asyncio.Lock | None = None,
        *,
        history_store: WorkspaceStore | None = None,
        feedback_path: str | Path | None = None,
    ) -> None:
        self.agent = agent_engine
        self.sandbox = sandbox
        self._lock = lock or asyncio.Lock()
        self._store = history_store or default_store()
        self._feedback_path = Path(feedback_path) if feedback_path else BASE_DIR / "memory" / "agent_feedback.json"
        self._pending_plan: dict | None = None
        self.app = FastAPI(title="Fox Agent", docs_url=None, redoc_url=None)
        self._build_routes()

    # ── Helpers ──────────────────────────────────────────────────────────────
    def _record_feedback(self, req: dict) -> None:
        entry = {
            "session_id": req.get("session_id"),
            "rating": req.get("rating"),
            "content": (req.get("content") or "")[:2000],
            "ts": time.time(),
        }
        data: list = []
        if self._feedback_path.exists():
            try:
                data = json.loads(self._feedback_path.read_text(encoding="utf-8"))
            except Exception:
                data = []
        if not isinstance(data, list):
            data = []
        data.append(entry)
        self._feedback_path.parent.mkdir(parents=True, exist_ok=True)
        self._feedback_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    async def _generate_plan(self, task: str, provider, model, timeout) -> str:
        result = await asyncio.to_thread(
            providers.chat,
            [{"role": "user", "content": PLAN_PROMPT.format(task=task)}],
            None,
            provider=provider, model=model, effort="off", timeout=min(timeout, 90),
        )
        return (result.get("content") or "").strip()

    def _build_routes(self) -> None:
        app = self.app

        @app.get("/", response_class=HTMLResponse)
        async def index():
            content = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            return HTMLResponse(content, headers={"Cache-Control": "no-store, max-age=0"})

        @app.get("/app.js")
        async def app_js():
            return FileResponse(STATIC_DIR / "app.js", headers={"Cache-Control": "no-store, max-age=0"})

        @app.get("/style.css")
        async def style_css():
            return FileResponse(STATIC_DIR / "style.css")

        @app.get("/api/meta")
        async def meta():
            return JSONResponse({
                "providers": providers.list_providers(),
                "modes": [{"id": m, "label": MODE_LABELS[m]} for m in MODES],
                "reasoning_levels": providers.REASONING_LEVELS,
                "active": {
                    "provider": providers.get_active_provider(),
                    "model": providers.get_active_model(),
                },
            })

        @app.get("/api/models/{pid}")
        async def get_models(pid: str):
            models = await asyncio.to_thread(providers.fetch_remote_models, pid)
            return JSONResponse({"models": models})

        @app.post("/api/key")
        async def save_key(request: Request):
            try:
                data = await request.json()
            except Exception:
                return JSONResponse({"ok": False, "error": "JSON inválido"}, status_code=400)
            provider = (data.get("provider") or "").strip()
            api_key = data.get("api_key") or ""
            providers.set_api_key(provider, api_key)
            return JSONResponse({"ok": True, "providers": providers.list_providers()})

        @app.post("/api/provider")
        async def add_provider(request: Request):
            try:
                data = await request.json()
            except Exception:
                return JSONResponse({"ok": False, "error": "JSON inválido"}, status_code=400)
            name = (data.get("name") or "").strip()
            url = (data.get("url") or "").strip()
            api_key = data.get("api_key") or ""
            models = data.get("models") or []
            if not name or not url:
                return JSONResponse({"ok": False, "error": "Nombre y URL son obligatorios"}, status_code=400)
            pid = providers.add_custom_provider(name, url, api_key, models)
            return JSONResponse({"ok": True, "id": pid, "providers": providers.list_providers()})

        @app.delete("/api/provider/{pid}")
        async def remove_provider(pid: str):
            providers.remove_custom_provider(pid)
            return JSONResponse({"ok": True, "providers": providers.list_providers()})

        @app.get("/api/fs/pick")
        async def fs_pick():
            def _pick():
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes('-topmost', True)
                folder = filedialog.askdirectory(title="Seleccionar carpeta para Fox")
                root.destroy()
                return folder
            folder_path = await asyncio.to_thread(_pick)
            return JSONResponse({"path": folder_path})

        @app.get("/api/fs/tree")
        async def fs_tree(path: str):
            def _scan(p: Path, max_depth=2, current_depth=0):
                if current_depth > max_depth:
                    return None
                try:
                    result = []
                    for item in p.iterdir():
                        if item.name.startswith((".", "__")): continue
                        if item.is_dir():
                            children = _scan(item, max_depth, current_depth + 1)
                            result.append([item.name, children])
                        else:
                            result.append([item.name, None])
                    result.sort(key=lambda x: (x[1] is None, x[0].lower()))
                    return result
                except Exception:
                    return None

            p = Path(path)
            if not p.is_dir():
                return JSONResponse({"tree": []})
            tree = await asyncio.to_thread(_scan, p)
            return JSONResponse({"tree": tree or []})

        @app.get("/api/sessions")
        async def sessions():
            return JSONResponse({
                "workspaces": self._store.list_workspaces(),
                "active": self._store.active_session,
            })

        @app.post("/api/sessions")
        async def create_session(request: Request):
            try:
                data = await request.json()
            except Exception:
                data = {}
            ws_name = data.get("workspace", "default")
            sid = self._store.create_session(workspace_name=ws_name, history=self.agent.history)
            return JSONResponse({"session_id": sid, "workspaces": self._store.list_workspaces()})

        @app.get("/api/sessions/{sid}")
        async def load_session(sid: str):
            history = self._store.load_session(sid)
            self.agent.load_history(history)
            return JSONResponse({"session_id": sid, "messages": history})

        @app.delete("/api/sessions/{sid}")
        async def delete_session(sid: str):
            self._store.delete_session(sid)
            return JSONResponse({"workspaces": self._store.list_workspaces()})

        @app.websocket("/ws/agent")
        async def ws(websocket: WebSocket):
            await websocket.accept()

            # ── Helper: envío seguro que propaga WebSocketDisconnect ────────
            async def safe_send(data: dict) -> None:
                """Envía un mensaje al cliente. Si el cliente ya se desconectó,
                lanza WebSocketDisconnect para romper limpiamente el loop."""
                try:
                    await websocket.send_json(data)
                except WebSocketDisconnect:
                    raise
                except Exception as e:
                    # Starlette lanza ClientDisconnected internamente a veces
                    if "disconnect" in str(e).lower() or "closed" in str(e).lower():
                        raise WebSocketDisconnect(code=1006)
                    print(f"[WS] Error enviando: {e}")
                    raise

            try:
                while True:
                    raw = await websocket.receive_text()
                    try:
                        req = json.loads(raw)
                    except json.JSONDecodeError:
                        await safe_send({"type": "error", "error": "JSON inválido"})
                        continue

                    action = req.get("action", "send")

                    if action == "ping":
                        await safe_send({"type": "pong"})
                        continue

                    if action == "get_workspaces" or action == "list":
                        await safe_send({
                            "type": "workspaces",
                            "workspaces": self._store.list_workspaces(),
                            "active": self._store.active_session,
                        })
                        continue

                    if action == "new":
                        ws_name = req.get("workspace", "default")
                        sid = self._store.create_session(workspace_name=ws_name)
                        self.agent.clear_history()
                        self._pending_plan = None
                        await safe_send({
                            "type": "session_new",
                            "session_id": sid,
                            "workspaces": self._store.list_workspaces(),
                        })
                        continue

                    if action == "load":
                        sid = req.get("session_id")
                        history = self._store.load_session(sid) if sid else []
                        self.agent.load_history(history)
                        self._pending_plan = None
                        await safe_send({
                            "type": "history",
                            "session_id": sid,
                            "messages": history,
                        })
                        continue

                    if action == "delete":
                        self._store.delete_session(req.get("session_id"))
                        await safe_send({
                            "type": "workspaces",
                            "workspaces": self._store.list_workspaces(),
                            "active": self._store.active_session,
                        })
                        continue

                    if action == "clear":
                        self.agent.clear_history()
                        self._pending_plan = None
                        await safe_send({"type": "cleared"})
                        continue

                    if action == "subagent":
                        task = (req.get("task") or "").strip()
                        if not task:
                            await safe_send({"type": "error", "error": "Falta la tarea del subagente."})
                            continue
                        sub = AgentEngine(
                            tool_executor=self.agent._tool_executor,
                            tools=self.agent.tools,
                            system_prompt=(
                                "Sos un subagente de Fox. Completá la tarea asignada "
                                "y devolvé un resumen breve del resultado."
                            ),
                            max_steps=4,
                            context_limit=self.agent.context_limit,
                        )
                        await safe_send({"type": "subagent_start", "task": task})
                        try:
                            async with self._lock:
                                async for ev in sub.run(
                                    task,
                                    provider=req.get("provider"),
                                    model=req.get("model"),
                                    effort=req.get("effort") or "off",
                                ):
                                    tagged = dict(ev)
                                    tagged["type"] = "subagent_" + ev["type"]
                                    await safe_send(tagged)
                            await safe_send({"type": "subagent_done"})
                        except WebSocketDisconnect:
                            print("[WS] Cliente desconectado durante subagente")
                            return
                        continue

                    if action == "feedback":
                        self._record_feedback(req)
                        await safe_send({"type": "feedback_saved", "rating": req.get("rating")})
                        continue

                    if action == "reject_plan":
                        self._pending_plan = None
                        await safe_send({"type": "plan_rejected"})
                        continue

                    if action == "approve_plan":
                        pending = self._pending_plan
                        self._pending_plan = None
                        if not pending:
                            await safe_send({"type": "error", "error": "No hay plan pendiente."})
                            continue
                        sid = pending.get("session_id") or self._store.active_session
                        ws_name = pending.get("workspace", "default")
                        if not sid:
                            sid = self._store.create_session(workspace_name=ws_name)
                        self.sandbox.set_mode(pending.get("sandbox") or "full")
                        self.sandbox.set_roots(pending.get("folders") or [])
                        await safe_send({"type": "plan_approved", "plan": pending.get("plan", "")})
                        try:
                            async with self._lock:
                                async for ev in self.agent.run(
                                    pending.get("task", ""),
                                    provider=pending.get("provider"),
                                    model=pending.get("model"),
                                    effort=pending.get("effort") or "off",
                                    extra_context="Plan aprobado por el usuario:\n" + pending.get("plan", "") + ("\n\n[INFO DE ENTORNO] Tienes acceso completo de lectura y escritura a las siguientes carpetas del proyecto: " + ", ".join(pending.get("folders", [])) if pending.get("folders") else ""),
                                ):
                                    await safe_send(ev)
                            self._store.save_session(sid, ws_name, self.agent.history, title_from_history(self.agent.history))
                            await safe_send({
                                "type": "session_saved",
                                "session_id": sid,
                                "title": title_from_history(self.agent.history),
                                "workspaces": self._store.list_workspaces(),
                            })
                        except WebSocketDisconnect:
                            print("[WS] Cliente desconectado durante approve_plan")
                            return
                        continue

                    # ── action == "send" ────────────────────────────────────
                    text = (req.get("text") or "").strip()
                    if not text:
                        continue
                    ws_name = req.get("workspace", "default")
                    req_folders = req.get("folders") or []

                    req_sid = req.get("session_id")
                    if req_sid is None:
                        sid = self._store.create_session(workspace_name=ws_name)
                        self.agent.clear_history()
                        self._pending_plan = None
                    else:
                        if req_sid != self._store.active_session:
                            history = self._store.load_session(req_sid) or []
                            self.agent.load_history(history)
                            self._store.active_session = req_sid
                        sid = req_sid

                    if req_folders:
                        self._store.set_session_folders(sid, req_folders)
                        folders = req_folders
                    else:
                        folders = self._store.get_session_folders(sid)

                    self.sandbox.set_mode(req.get("sandbox") or "full")
                    self.sandbox.set_roots(folders)

                    if req_sid is None:
                        try:
                            await safe_send({
                                "type": "session_saved",
                                "session_id": sid,
                                "title": (text[:40] + "…") if len(text) > 40 else text,
                                "workspaces": self._store.list_workspaces(),
                            })
                        except WebSocketDisconnect:
                            print("[WS] Cliente desconectado antes de confirmar sesión nueva")
                            return

                    provider = req.get("provider") or None
                    model = req.get("model") or None
                    effort = req.get("effort") or "off"

                    api_keys = req.get("api_keys")
                    if api_keys:
                        for provider_id, key_val in api_keys.items():
                            providers.set_api_key(provider_id, key_val)

                    files = req.get("files") or []
                    if files:
                        import base64
                        for f in files:
                            try:
                                data = base64.b64decode(f.get("data", "")).decode("utf-8")
                                text += f"\n\n--- Archivo adjunto: {f.get('name')} ---\n{data}\n--- Fin de {f.get('name')} ---\n"
                            except Exception:
                                text += f"\n[Archivo adjunto (binario/no texto): {f.get('name')} ({f.get('type')})]"

                    if req.get("plan"):
                        try:
                            plan = await self._generate_plan(text, provider, model, 180)
                        except Exception as e:
                            try:
                                await safe_send({"type": "error", "error": f"No pude armar el plan: {e}"})
                            except WebSocketDisconnect:
                                return
                            continue
                        self._pending_plan = {
                            "task": text,
                            "plan": plan,
                            "session_id": sid,
                            "workspace": ws_name,
                            "provider": provider,
                            "model": model,
                            "effort": effort,
                            "sandbox": req.get("sandbox") or "full",
                            "folders": folders,
                        }
                        try:
                            await safe_send({"type": "plan", "plan": plan, "session_id": sid})
                        except WebSocketDisconnect:
                            return
                        continue

                    ctx_lines = []
                    if folders:
                        paths = ", ".join(folders)
                        ctx_lines.append(f"\n[INFO DE ENTORNO] El usuario te ha dado acceso de lectura/escritura a estas carpetas: {paths}.")
                        ctx_lines.append("Usa tus herramientas (list_dir, view_file, grep_search, find_by_name) para inspeccionarlas. Asume que el usuario se refiere a estos directorios si habla de 'el proyecto' o 'esta carpeta'.")

                    extra_context = "\n".join(ctx_lines)

                    if self._lock.locked():
                        try:
                            await safe_send({
                                "type": "error",
                                "error": "El agente está ocupado con otro turno. Esperá a que termine o recargá la página.",
                            })
                        except WebSocketDisconnect:
                            return
                        continue

                    # ── Ejecutar el agente con manejo robusto de desconexión ──
                    agent_completed = False
                    try:
                        async with self._lock:
                            async for ev in self.agent.run(
                                text,
                                provider=provider,
                                model=model,
                                effort=effort,
                                extra_context=extra_context,
                            ):
                                await safe_send(ev)
                        agent_completed = True
                    except WebSocketDisconnect:
                        print(f"[WS] Cliente desconectado durante turno de agente (sid={sid})")
                        # Guardar historial parcial igual para no perder lo que se generó
                        if self.agent.history:
                            try:
                                self._store.save_session(sid, ws_name, self.agent.history, title_from_history(self.agent.history))
                            except Exception as e:
                                print(f"[WS] No se pudo guardar sesión parcial: {e}")
                        return
                    except Exception as e:
                        traceback.print_exc()
                        try:
                            await safe_send({"type": "error", "error": f"Error del agente: {str(e)}"})
                        except WebSocketDisconnect:
                            return

                    if agent_completed:
                        self._store.save_session(sid, ws_name, self.agent.history, title_from_history(self.agent.history))
                        try:
                            await safe_send({
                                "type": "session_saved",
                                "session_id": sid,
                                "title": title_from_history(self.agent.history),
                                "workspaces": self._store.list_workspaces(),
                            })
                        except WebSocketDisconnect:
                            print(f"[WS] Cliente desconectado antes de confirmar session_saved (sid={sid})")
                            return

            except WebSocketDisconnect:
                print("[WS] Conexión WebSocket cerrada (código normal)")
            except Exception as e:
                print(f"[WS] Error inesperado en handler WebSocket: {e}")
                traceback.print_exc()
                try:
                    await websocket.send_json({"type": "error", "error": str(e)})
                except Exception:
                    pass


async def run_server(
    server: AgentWebServer,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    """Corre uvicorn dentro del event-loop actual (tarea asyncio de Fox)."""
    import uvicorn

    config = uvicorn.Config(server.app, host=host, port=port, log_level="warning")
    srv = uvicorn.Server(config)
    await srv.serve()