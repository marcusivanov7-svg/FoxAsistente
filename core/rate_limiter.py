# -*- coding: utf-8 -*-
"""rate_limiter.py - Controla las peticiones a APIs externas.

Implementa decoradores y funciones para evitar exceder los límites
(Rate Limits) de proveedores como Gemini, OpenAI, o búsquedas web.
"""
import asyncio
import time
from functools import wraps
from typing import Callable, Any

class RateLimiter:
    def __init__(self):
        self.limits = {
            "gemini": {"max_req": 15, "per_seconds": 60, "calls": []},
            "openai": {"max_req": 50, "per_seconds": 60, "calls": []},
            "web_search": {"max_req": 10, "per_seconds": 60, "calls": []},
            "weather": {"max_req": 30, "per_seconds": 60, "calls": []},
            "default": {"max_req": 20, "per_seconds": 60, "calls": []}
        }
    
    def _clean_old_calls(self, api_name: str, now: float):
        cfg = self.limits.get(api_name, self.limits["default"])
        cfg["calls"] = [t for t in cfg["calls"] if now - t < cfg["per_seconds"]]

    async def acquire(self, api_name: str) -> None:
        cfg = self.limits.get(api_name, self.limits["default"])
        
        while True:
            now = time.time()
            self._clean_old_calls(api_name, now)
            
            if len(cfg["calls"]) < cfg["max_req"]:
                cfg["calls"].append(now)
                break
                
            # Calcular cuánto esperar
            oldest_call = cfg["calls"][0]
            wait_time = cfg["per_seconds"] - (now - oldest_call)
            if wait_time > 0:
                print(f"[RateLimiter] Límite alcanzado para {api_name}. Esperando {wait_time:.1f}s...")
                await asyncio.sleep(wait_time)

# Instancia global
GLOBAL_LIMITER = RateLimiter()

def rate_limit(api_name: str):
    """Decorador asíncrono para limitar peticiones a una API."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            await GLOBAL_LIMITER.acquire(api_name)
            return await func(*args, **kwargs)
        return wrapper
    return decorator
