# -*- coding: utf-8 -*-
"""working_memory.py - Memoria de trabajo temporal para tareas complejas.

Permite al Agente guardar variables, subtareas, errores y pensamientos,
evitando perder el contexto en loops largos o conversaciones extendidas.
Es thread-safe mediante locks.
"""
from __future__ import annotations

import threading
import time
from typing import Any


class WorkingMemory:
    """Gestiona el estado temporal durante la ejecución del agente."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.goal: str | None = None
        self.sub_tasks: list[dict[str, Any]] = []
        self.variables: dict[str, Any] = {}
        self.thoughts: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []

    def set_goal(self, goal: str) -> None:
        """Establece el objetivo principal actual."""
        with self._lock:
            self.goal = goal

    def add_sub_task(self, name: str, description: str = "") -> None:
        """Añade una sub-tarea pendiente."""
        with self._lock:
            self.sub_tasks.append({
                "id": len(self.sub_tasks) + 1,
                "name": name,
                "description": description,
                "status": "pending",
                "timestamp": time.time()
            })

    def complete_sub_task(self, task_id: int) -> None:
        """Marca una sub-tarea como completada."""
        with self._lock:
            for task in self.sub_tasks:
                if task["id"] == task_id:
                    task["status"] = "completed"
                    break

    def set_variable(self, key: str, value: Any) -> None:
        """Guarda un valor temporal en memoria."""
        with self._lock:
            self.variables[key] = value

    def get_variable(self, key: str) -> Any:
        with self._lock:
            return self.variables.get(key)

    def add_thought(self, text: str) -> None:
        """Registra un paso de razonamiento del agente."""
        with self._lock:
            self.thoughts.append({"text": text, "timestamp": time.time()})

    def log_error(self, module: str, error_msg: str) -> None:
        """Registra un error para tenerlo en contexto y evitar repetirlo."""
        with self._lock:
            self.errors.append({
                "module": module,
                "error": error_msg,
                "timestamp": time.time()
            })

    def clear(self) -> None:
        """Limpia la memoria de trabajo."""
        with self._lock:
            self.goal = None
            self.sub_tasks.clear()
            self.variables.clear()
            self.thoughts.clear()
            self.errors.clear()

    def get_context_string(self) -> str:
        """Genera un string formateado para inyectarlo al prompt del LLM."""
        with self._lock:
            if not self.goal and not self.sub_tasks and not self.variables:
                return ""
                
            ctx = ["[WORKING MEMORY CURRENT STATE]"]
            if self.goal:
                ctx.append(f"Goal: {self.goal}")
            
            pending_tasks = [t for t in self.sub_tasks if t["status"] == "pending"]
            if pending_tasks:
                ctx.append("Pending Sub-tasks:")
                for t in pending_tasks:
                    ctx.append(f" - [{t['id']}] {t['name']}")
                    
            if self.variables:
                ctx.append("Context Variables:")
                for k, v in self.variables.items():
                    ctx.append(f" - {k}: {v}")
                    
            if self.errors:
                ctx.append("Recent Errors to avoid:")
                for e in self.errors[-3:]:  # Últimos 3 errores
                    ctx.append(f" - [{e['module']}] {e['error']}")
                    
            return "\n".join(ctx)
