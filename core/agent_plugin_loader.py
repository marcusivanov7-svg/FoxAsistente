# -*- coding: utf-8 -*-
"""agent_plugin_loader.py — Carga de plugins para el modo agente de Fox.

Escanea la carpeta plugins/agent/ y registra las herramientas declaradas
con AGENT_PLUGIN = {...} para que el AgentEngine las pueda llamar.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable


def _plugins_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "plugins" / "agent"
    return Path(__file__).resolve().parent.parent / "plugins" / "agent"


def load_agent_plugins() -> tuple[list[dict], dict[str, Callable]]:
    """Carga los plugins del agente desde plugins/agent/.

    Devuelve:
        tools        — lista de tool declarations (formato canónico OpenAI)
        executors    — dict {tool_name: callable(params) -> str}
    """
    tools: list[dict] = []
    executors: dict[str, Callable] = {}
    pdir = _plugins_dir()
    if not pdir.exists():
        return tools, executors

    for path in sorted(pdir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"fox_agent_plugin_{path.stem}", path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[attr-defined]
            plugin = getattr(mod, "AGENT_PLUGIN", None)
            if not isinstance(plugin, dict) or not plugin.get("name"):
                continue
            run_fn = getattr(mod, "run", None)
            if not callable(run_fn):
                continue

            name = plugin["name"]
            description = plugin.get("description", "")
            parameters = plugin.get("parameters", {"type": "object", "properties": {}})

            # Convertir a formato canónico OpenAI
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": _lowercase_types(parameters),
                },
            })
            executors[name] = run_fn
            print(f"[AgentPlugins] Cargado: {name}")
        except Exception as e:
            print(f"[AgentPlugins] Error cargando {path.name}: {e}")

    return tools, executors


def _lowercase_types(node: Any) -> Any:
    """Convierte tipos MAYÚSCULAS → minúsculas (JSON Schema)."""
    if isinstance(node, dict):
        out = dict(node)
        if isinstance(out.get("type"), str):
            out["type"] = out["type"].lower()
        if isinstance(out.get("properties"), dict):
            out["properties"] = {k: _lowercase_types(v) for k, v in out["properties"].items()}
        if "items" in out:
            out["items"] = _lowercase_types(out["items"])
        return out
    if isinstance(node, list):
        return [_lowercase_types(x) for x in node]
    return node
