"""
geolocation.py — Ubicación aproximada del usuario (sin GPS, sin permisos).

Fox no pide permiso de ubicación ni usa GPS: deduce el país/ciudad aproximados a
partir de la IP pública, mediante servicios gratuitos que no requieren API key.
La precisión es a nivel de ciudad/pais, nunca la dirección exacta.

La respuesta se cachea en config/api_keys.json (clave `location`) durante 7 días,
así que solo se consulta a la red la primera vez (y una vez por semana para
refrescar). Si no hay internet o todos los servicios fallan, devuelve None y el
llamador decide el fallback (p. ej. noticias del mundo).

El resultado normalizado es siempre:
    {"country": "Paraguay", "country_code": "PY", "city": "Asunción", "ts": 172...}
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

_CACHE_KEY = "location"
_CACHE_TTL_SEC = 7 * 24 * 3600  # refrescar como mucho una vez por semana


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_CONFIG_PATH = _get_base_dir() / "config" / "api_keys.json"


# Cada servicio devuelve el JSON con nombres de campo distintos; estos adaptadores
# lo normalizan a (country, country_code, city). Ordenados por preferencia: todos
# son gratuitos, pero los dos primeros usan HTTPS (ip-api.com gratis es solo HTTP).
def _try_ipapi_co() -> tuple[str, str, str] | None:
    r = requests.get("https://ipapi.co/json/", timeout=5)
    r.raise_for_status()
    d = r.json()
    if not d.get("country_name"):
        return None
    return d.get("country_name"), d.get("country_code"), d.get("city")


def _try_ipwhois() -> tuple[str, str, str] | None:
    r = requests.get("https://ipwho.is/", timeout=5)
    r.raise_for_status()
    d = r.json()
    if not d.get("country"):
        return None
    return d.get("country"), d.get("country_code"), d.get("city")


def _try_ip_api() -> tuple[str, str, str] | None:
    r = requests.get("http://ip-api.com/json/", timeout=5)
    r.raise_for_status()
    d = r.json()
    if d.get("status") != "success" or not d.get("country"):
        return None
    return d.get("country"), d.get("countryCode"), d.get("city")


_SERVICES = (_try_ipwhois, _try_ip_api, _try_ipapi_co)


def _fetch_location() -> dict | None:
    for fn in _SERVICES:
        try:
            res = fn()
            if res and res[0]:
                country, code, city = res
                return {
                    "country": (country or "").strip(),
                    "country_code": (code or "").strip().upper(),
                    "city": (city or "").strip(),
                    "ts": time.time(),
                }
        except Exception as e:
            print(f"[Location] geolocation service failed: {e}")
    return None


def _load_cache() -> dict | None:
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        loc = data.get(_CACHE_KEY)
        if isinstance(loc, dict) and loc.get("country"):
            return loc
    except Exception:
        pass
    return None


def _save_cache(loc: dict) -> None:
    try:
        data: dict = {}
        if _CONFIG_PATH.exists():
            try:
                data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[_CACHE_KEY] = loc
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(json.dumps(data, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"[Location] could not save cache: {e}")


def _fresh(loc: dict) -> bool:
    ts = loc.get("ts") or 0
    try:
        return (time.time() - float(ts)) < _CACHE_TTL_SEC
    except Exception:
        return False


def get_location(refresh: bool = False) -> dict | None:
    """Devuelve {'country','country_code','city','ts'} o None si no se pudo."""
    if not refresh:
        cached = _load_cache()
        if cached and _fresh(cached):
            return cached

    loc = _fetch_location()
    if loc:
        _save_cache(loc)
        print(f"[Location] {loc.get('city') or ''}, {loc.get('country')} "
              f"({loc.get('country_code')})")
    else:
        # Si falla la red, devolvé la caché vieja antes que nada (mejor que el mundo).
        loc = _load_cache()
    return loc


def get_country() -> str | None:
    loc = get_location()
    return (loc or {}).get("country")
