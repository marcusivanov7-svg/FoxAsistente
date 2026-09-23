# -*- coding: utf-8 -*-
"""cost_tracker.py - Rastrea el consumo de tokens y calcula costos.

Mantiene un acumulado de los tokens gastados en la sesión actual
y calcula un valor estimado en USD según el modelo usado.
"""

from threading import Lock

class CostTracker:
    def __init__(self):
        self._lock = Lock()
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        
        # Costos estimados por millón de tokens (Referencia Gemini 3.1 Pro / Flash / GPT-4o)
        # Valores de ejemplo (ajustar a la realidad del proveedor)
        self.pricing = {
            "gemini-3.1-pro": {"prompt": 1.25, "completion": 5.00},
            "gemini-3.1-flash": {"prompt": 0.075, "completion": 0.30},
            "gpt-4o": {"prompt": 5.00, "completion": 15.00},
            "default": {"prompt": 1.00, "completion": 3.00}
        }

    def add_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        with self._lock:
            self.session_prompt_tokens += prompt_tokens
            self.session_completion_tokens += completion_tokens

    def get_session_cost(self, model_name: str) -> float:
        """Devuelve el costo estimado en dólares de la sesión actual."""
        with self._lock:
            # Buscar el precio o usar default
            rates = self.pricing.get("default")
            for k, v in self.pricing.items():
                if k in model_name.lower():
                    rates = v
                    break
            
            cost_prompt = (self.session_prompt_tokens / 1_000_000) * rates["prompt"]
            cost_comp = (self.session_completion_tokens / 1_000_000) * rates["completion"]
            return cost_prompt + cost_comp

    def get_summary(self, model_name: str) -> str:
        cost = self.get_session_cost(model_name)
        with self._lock:
            total_tokens = self.session_prompt_tokens + self.session_completion_tokens
            return f"{total_tokens:,} tokens (${cost:.4f})"

# Instancia global para la sesión de UI
GLOBAL_COST_TRACKER = CostTracker()
