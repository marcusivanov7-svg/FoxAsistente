# actions/weather_report.py — FOX: clima con triple redundancia
# 1) wttr.in (gratis, sin API key, instantáneo) para "hoy/ahora"
# 2) Gemini con Google Search (grounding), acotado a 8s
# 3) DuckDuckGo como radar de respaldo (solo texto real, sin títulos sueltos)

from actions.web_search import _gemini_search, _ddg_search, _run_bounded


def _clima_wttr(city: str) -> str:
    """Radar de emergencia: wttr.in (gratis, sin API key, muy confiable)."""
    try:
        import urllib.parse
        import urllib.request
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=3&lang=es"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            texto = r.read().decode("utf-8", errors="ignore").strip()
        if texto and len(texto) < 200:
            return texto
    except Exception:
        pass
    return ""


def weather_action(
    parameters: dict,
    player=None,
    session_memory=None,
) -> str:
    city = parameters.get("city")
    when = parameters.get("time", "hoy")

    if not city or not str(city).strip():
        msg = "No entendí de qué ciudad querés el clima. Por favor repetilo."
        _log(msg, player)
        return msg

    city = str(city).strip()
    when = (str(when) or "hoy").strip()

    if player:
        try:
            player.write_log(f"SYS: ☁️ Consultando el clima en {city}...")
        except Exception:
            pass

    # ── PRIORIDAD 1: wttr.in (gratis, sin API key, instantáneo) ────────────
    # Para "hoy/ahora" no gastamos cuota de grounding ni hacemos scraping:
    # wttr.in devuelve condición y temperatura reales en una sola línea.
    _nowish = when.lower() in ("", "hoy", "ahora", "ya", "now", "actual",
                               "actualmente", "current", "hoy mismo")
    if _nowish:
        wttr = _clima_wttr(city)
        if wttr:
            _log("✅ Clima obtenido por wttr.in (instantáneo, sin cuota).", player)
            return f"El clima en {city} ahora mismo: {wttr}."

    # ── PRIORIDAD 2: Gemini + Google Search (solo si wttr.in falla o es pronóstico) ──
    try:
        reporte = _run_bounded(
            lambda: _gemini_search(
                f"Busca el clima exacto, temperatura y probabilidad de lluvia en "
                f"{city} para {when}. Responde de forma MUY breve y conversacional."
            ),
            timeout=8.0,
            label="Gemini clima",
        )
        if reporte:
            return reporte
    except Exception:
        pass

    # ── PRIORIDAD 3: DuckDuckGo (último recurso) ───────────────────────────
    # Solo devolvemos el texto real (snippet). Los títulos sueltos de páginas
    # confunden al modelo y lo hacen reintentar el clima en bucle.
    try:
        resultados = _ddg_search(f"clima actual en {city} {when}", max_results=3)
        if resultados:
            _snippets = [str(r.get("snippet", "")).strip()
                         for r in resultados if str(r.get("snippet", "")).strip()]
            if _snippets:
                _log("✅ Clima obtenido por radar de respaldo (DuckDuckGo).", player)
                return f"Clima en {city} (búsqueda):\n" + "\n".join(f"- {s}" for s in _snippets[:3])
            # Sin texto real → aviso claro y terminal (evita el bucle de reintentos).
            return (
                f"No pude obtener el clima exacto de {city} ahora mismo. "
                "Probá de nuevo en unos minutos o decime otra ciudad."
            )
    except Exception:
        pass

    return "La red meteorológica no responde en este momento. Intentá de nuevo en unos minutos."


def _log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"SYS: {message}")
        except Exception:
            pass

TOOL = {
    "name": 'weather_report',
    "description": 'Gives the weather report to user',
    "parameters": {   'type': 'OBJECT',
        'properties': {'city': {'type': 'STRING', 'description': 'City name'}},
        'required': ['city']},
    "handler": weather_action,
}
