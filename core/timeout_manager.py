# -*- coding: utf-8 -*-
"""timeout_manager.py - Gestor de tiempos de ejecución por herramienta.

Previene que el agente se quede colgado indefinidamente si una 
herramienta (como scraping o una petición de red) no responde.
"""

import asyncio
from typing import Callable, Awaitable, Any

# Tiempos de espera máximos (en segundos) según la herramienta
DEFAULT_TIMEOUTS = {
    "web_search": 15,
    "fetch_url": 25,
    "python_repl": 30,
    "get_weather": 10,
    "code_helper": 240,
    # Por defecto, cualquier otra herramienta tendrá 20s
    "default": 20
}

async def with_timeout(tool_name: str, coro: Awaitable[Any]) -> Any:
    """
    Ejecuta una corrutina con un tiempo límite específico basado en el nombre de la herramienta.
    """
    max_time = DEFAULT_TIMEOUTS.get(tool_name, DEFAULT_TIMEOUTS["default"])
    try:
        return await asyncio.wait_for(coro, timeout=max_time)
    except asyncio.TimeoutError:
        raise TimeoutError(f"La herramienta '{tool_name}' excedió el tiempo límite de {max_time} segundos y fue cancelada.")
