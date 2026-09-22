"""
Plugin de búsqueda web para el modo agente de Fox.
Wrapper del action de búsqueda existente.
"""
import sys
from pathlib import Path

AGENT_PLUGIN = {
    "name": "web_search",
    "description": (
        "Busca información actualizada en internet. Usá esta herramienta cuando el usuario "
        "pregunte sobre eventos recientes, noticias, precios, documentación o cualquier "
        "información que pueda haber cambiado. No la uses para tareas de código puro."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {"type": "STRING", "description": "La consulta de búsqueda en lenguaje natural"},
        },
        "required": ["query"],
    },
}


def run(parameters: dict) -> str:
    query = (parameters.get("query") or "").strip()
    if not query:
        return "Error: se requiere una consulta de búsqueda."
    try:
        # Intentar usar el action de búsqueda web existente de Fox
        base = Path(__file__).resolve().parent.parent.parent
        sys.path.insert(0, str(base))
        actions_dir = base / "actions"
        import importlib.util as _ilu
        # Buscar el action de web search
        for candidate in ["web_search", "search_web", "google_search", "buscar_web"]:
            p = actions_dir / f"{candidate}.py"
            if p.exists():
                spec = _ilu.spec_from_file_location(candidate, p)
                if spec and spec.loader:
                    mod = _ilu.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    fn = getattr(mod, "run", None) or getattr(mod, "search", None)
                    if callable(fn):
                        result = fn({"query": query})
                        return str(result)
        # Fallback: requests + DuckDuckGo
        import requests
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=8,
        )
        data = resp.json()
        abstract = data.get("AbstractText") or ""
        related = [r.get("Text", "") for r in (data.get("RelatedTopics") or [])[:3] if r.get("Text")]
        if abstract:
            return f"{abstract}\n\nRelacionado:\n" + "\n".join(f"- {r}" for r in related)
        if related:
            return "Resultados relacionados:\n" + "\n".join(f"- {r}" for r in related)
        return f"No se encontraron resultados directos para: {query}"
    except Exception as e:
        return f"Error en búsqueda web: {e}"
