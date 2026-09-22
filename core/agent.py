# -*- coding: utf-8 -*-
"""agent.py — Motor de agente agnóstico de proveedor para Fox.

Este es el "cerebro" del modo Chat/Agente. Orquesta un loop de pasos:

    1. Envía la conversación (con herramientas) al proveedor configurado.
    2. Si el modelo pide herramientas, las ejecuta vía un ``tool_executor``
       inyectado (en la app, el dispatcher de las 38 herramientas de Fox).
    3. Emite eventos de estado para que la UI muestre:
       pensando / ejecutando / leyendo / escribiendo, más el texto en streaming.

Es independiente de ``FoxLive`` y de ``ui.py``: recibe un executor y emite
eventos. Así el mismo motor puede usarse desde la UI de chat, el dashboard o
el especialista de voz.

Eventos emitidos por ``run()``:
    {"type": "status", "status": "thinking"|"executing"|"reading"|"writing", "label": str}
    {"type": "delta", "text": str}
    {"type": "thinking", "text": str}
    {"type": "tool_start", "name": str, "arguments": dict}
    {"type": "tool_result", "name": str, "result": str}
    {"type": "usage", "usage": {prompt_tokens, completion_tokens, total_tokens}}
    {"type": "done", "content": str, "steps": int, "usage": {...}}
    {"type": "error", "error": str}
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, AsyncGenerator, Awaitable, Callable

from core import providers
from core.permissions import tool_status_kind, TOOL_KIND

# tool_executor: async (name, args) -> str
ToolExecutor = Callable[[str, dict], Awaitable[str]]

DEFAULT_SYSTEM_PROMPT = (
    "Eres Fox, un asistente agente con acceso a herramientas del sistema "
    "(archivos, web, aplicaciones, memoria, etc.). "
    "Usá las herramientas cuando haga falta para completar la tarea. "
    "Respondé siempre en el idioma en que te habla el usuario y sé conciso."
)


def _wire_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """Respuesta del proveedor → formato wire de mensaje assistant (arguments como JSON string)."""
    out = []
    for c in tool_calls or []:
        fn = c.get("function", {})
        args = fn.get("arguments", {})
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append({
            "id": c.get("id", ""),
            "type": "function",
            "function": {"name": fn.get("name", ""), "arguments": args},
        })
    return out


class AgentEngine:
    def __init__(
        self,
        tool_executor: ToolExecutor,
        *,
        tools: list[dict] | None = None,
        system_prompt: str | None = None,
        max_steps: int = 8,
        default_timeout: int = 180,
        context_limit: int = 128000,
        compact_threshold: float = 0.75,
    ) -> None:
        self._tool_executor = tool_executor
        self.tools = tools or []
        self.system_prompt = system_prompt if system_prompt is not None else DEFAULT_SYSTEM_PROMPT
        self.max_steps = max_steps
        self.default_timeout = default_timeout
        self.context_limit = context_limit
        self.compact_threshold = compact_threshold

        # Historial canónico (estilo OpenAI, sin el mensaje de sistema).
        self.history: list[dict] = []
        # Uso de tokens acumulado de la última corrida (para la barra de contexto).
        self.last_usage: dict[str, int | None] = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}

    # ── Historial ─────────────────────────────────────────────────────────────
    def add_message(self, role: str, content: str, **extra: Any) -> None:
        self.history.append({"role": role, "content": content, **extra})

    def clear_history(self) -> None:
        self.history = []
        self.last_usage = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}

    def load_history(self, history: list[dict]) -> None:
        """Reemplaza el historial (usado al reanudar una sesión guardada)."""
        self.history = [dict(m) for m in (history or [])]
        self.last_usage = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}

    def set_tools(self, tools: list[dict]) -> None:
        self.tools = tools

    # ── Compactación de contexto ──────────────────────────────────────────────
    @staticmethod
    def _estimate_tokens(history: list[dict]) -> int:
        """Estimación rápida de tokens (heurística ~4 chars/token)."""
        try:
            return len(json.dumps(history, ensure_ascii=False)) // 4
        except Exception:
            return 0

    async def _maybe_compact(self, provider, model, effort, timeout) -> str | None:
        """Resume el historial viejo si se acerca al límite de contexto."""
        if len(self.history) < 4:
            return None
        if self._estimate_tokens(self.history) < int(self.context_limit * self.compact_threshold):
            return None

        # Conservar los últimos ~3 mensajes y resumir el resto.
        split = max(2, len(self.history) - 3)
        old = self.history[:split]
        recent = self.history[split:]
        text = "\n".join(
            f"{m.get('role', '?')}: {m.get('content', '')}" for m in old if m.get("content")
        )
        try:
            summary = await asyncio.to_thread(
                providers.chat,
                [{"role": "user", "content": (
                    "Resumí esta conversación en pocas líneas. Conservá datos clave, "
                    "decisiones y tareas pendientes:\n\n" + text
                )}],
                None,
                provider=provider, model=model, effort="off", timeout=min(timeout, 90),
            )
            result = (summary.get("content") or "").strip()
        except Exception as e:  # noqa: BLE001 — la compactación es best-effort
            print(f"[Agent] Compactación falló: {e}")
            return None
        if not result:
            return None
        self.history = [
            {"role": "user", "content": "[Resumen de la conversación anterior]\n" + result}
        ] + recent
        return result

    # ── Streaming desde un generador bloqueante ───────────────────────────────
    @staticmethod
    async def _run_blocking_stream(factory: Callable[[], Any]) -> AsyncGenerator[dict, None]:
        """Ejecuta un generador síncrono (HTTP) en un hilo y emite sus eventos."""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def _consume() -> None:
            try:
                for ev in factory():
                    loop.call_soon_threadsafe(q.put_nowait, ev)
            except Exception as e:  # noqa: BLE001 — queremos el error en la UI, no en el hilo
                loop.call_soon_threadsafe(q.put_nowait, {"type": "error", "error": str(e)})
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)

        threading.Thread(target=_consume, daemon=True).start()
        while True:
            ev = await q.get()
            if ev is None:
                break
            yield ev

    # ── Loop principal ────────────────────────────────────────────────────────
    async def run(
        self,
        user_text: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        effort: str = "off",
        extra_context: str = "",
        timeout: int | None = None,
        keep_history: bool = True,
    ) -> AsyncGenerator[dict, None, None]:
        """Corre el loop de agente para un turno del usuario."""
        timeout = timeout or self.default_timeout

        if keep_history:
            self.history.append({"role": "user", "content": user_text})
        else:
            self.history = [{"role": "user", "content": user_text}]

        # Compactación preventiva: si el contexto se llena, resumir el historial viejo.
        if (
            len(self.history) >= 4
            and self._estimate_tokens(self.history) >= int(self.context_limit * self.compact_threshold)
        ):
            yield {"type": "status", "status": "writing", "label": "Compactando contexto…"}
        await self._maybe_compact(provider, model, effort, timeout)

        system_content = self.system_prompt
        if extra_context:
            system_content = system_content.rstrip() + "\n\n" + extra_context

        total_usage: dict[str, int | None] = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
        final_content = ""

        for step in range(1, self.max_steps + 1):
            yield {"type": "status", "status": "thinking", "label": "Pensando…"}

            messages = [{"role": "system", "content": system_content}] + list(self.history)

            content = ""
            thinking = ""
            tool_calls: list[dict] = []

            def _factory():
                return providers.stream_chat(
                    messages,
                    self.tools,
                    provider=provider,
                    model=model,
                    effort=effort,
                    timeout=timeout,
                )

            async for ev in self._run_blocking_stream(_factory):
                et = ev.get("type")
                if et == "delta":
                    content += ev.get("text", "")
                    yield ev
                elif et == "thinking":
                    thinking += ev.get("text", "")
                    yield ev
                elif et == "tool_calls":
                    tool_calls = ev.get("tool_calls") or []
                elif et == "done":
                    content = ev.get("content", content)
                    thinking = ev.get("thinking", thinking)
                    tool_calls = ev.get("tool_calls", tool_calls)
                    usage = ev.get("usage") or {}
                    for k in total_usage:
                        if usage.get(k) is not None:
                            total_usage[k] = usage.get(k)
                    yield {"type": "usage", "usage": usage}
                elif et == "error":
                    yield ev
                    return

            if not tool_calls:
                final_content = content.strip()
                self.history.append({"role": "assistant", "content": final_content})
                break

            # Registrar el turno del asistente con sus tool_calls
            self.history.append({
                "role": "assistant",
                "content": content.strip(),
                "tool_calls": _wire_tool_calls(tool_calls),
            })

            # ── Ejecutar herramientas ──────────────────────────────────────
            # Se ejecutan en PARALELO (fan-out) cuando TODAS son lecturas
            # seguras (web_search, weather, system_status, recall_memory…);
            # las que mutan el sistema/archivos se serializan por seguridad.
            calls_info = []
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                call_id = call.get("id", name)
                calls_info.append((name, args, call_id))

            _labels = {
                "reading": "📖 Leyendo {name}…",
                "writing": "✍️ Escribiendo con {name}…",
                "executing": "🔧 Ejecutando {name}…",
            }
            for name, args, call_id in calls_info:
                _kind = tool_status_kind(name, args)
                yield {"type": "tool_start", "name": name, "arguments": args}
                yield {"type": "status", "status": _kind, "label": _labels.get(_kind, f"🔧 {name}…").format(name=name), "tool": name}

            parallel = all(TOOL_KIND.get(n) == "safe_read" for n, _a, _c in calls_info)

            async def _run_one(name: str, args: dict) -> str:
                try:
                    return str(await self._tool_executor(name, args))
                except Exception as e:  # noqa: BLE001
                    return f"Error al ejecutar {name}: {e}"

            if parallel and len(calls_info) > 1:
                results = await asyncio.gather(*(_run_one(n, a) for n, a, _c in calls_info))
            else:
                results = [await _run_one(n, a) for n, a, _c in calls_info]

            for (name, args, call_id), result in zip(calls_info, results):
                yield {"type": "tool_result", "name": name, "result": result}
                self.history.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": result,
                })

        else:
            # Se agotó max_steps sin respuesta final limpia
            final_content = final_content or "Llegué al límite de pasos."

        self.last_usage = total_usage
        yield {
            "type": "done",
            "content": final_content,
            "steps": step,
            "usage": total_usage,
        }
