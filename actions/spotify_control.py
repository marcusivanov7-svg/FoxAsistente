"""spotify_control — Spotify playback control (ported from AMSY).

Requires: `pip install spotipy`, and Spotify Developer credentials in
config/api_keys.json (spotify_client_id, spotify_client_secret,
spotify_redirect_uri). First use opens a browser to authorize once; the token
is cached afterwards. No-ops gracefully when unconfigured.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir()
CACHE    = str(BASE_DIR / "memory" / ".spotify_token.json")


def _cfg() -> dict:
    try:
        return json.loads(
            (BASE_DIR / "config" / "api_keys.json").read_text(encoding="utf-8")
        )
    except Exception:
        return {}


def _client():
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth
    except ImportError:
        raise RuntimeError("spotipy no está instalado. Ejecutá: pip install spotipy")

    cfg = _cfg()
    cid = (cfg.get("spotify_client_id") or "").strip()
    sec = (cfg.get("spotify_client_secret") or "").strip()
    red = (cfg.get("spotify_redirect_uri") or "http://127.0.0.1:8888/callback").strip()
    if not cid or not sec:
        raise RuntimeError(
            "Spotify no configurado: faltan spotify_client_id / spotify_client_secret "
            "en config/api_keys.json."
        )
    auth = spotipy.SpotifyOAuth(
        client_id=cid,
        client_secret=sec,
        redirect_uri=red,
        scope="user-modify-playback-state user-read-playback-state",
        cache_path=CACHE,
    )
    return spotipy.Spotify(auth_manager=auth)


def _play(sp, query: str) -> str:
    if not query:
        return "Falta el nombre de la canción/artista (query)."
    results = sp.search(q=query, type="track", limit=1)
    items = results.get("tracks", {}).get("items", [])
    if not items:
        return f"No encontré '{query}' en Spotify."
    sp.start_playback(uris=[items[0]["uri"]])
    name = items[0]["name"]
    artist = items[0]["artists"][0]["name"]
    return f"Reproduciendo: {name} — {artist}"


def spotify_control(parameters=None, player=None, **kwargs) -> str:
    p = parameters or {}
    action = (p.get("action") or "play").strip().lower()
    try:
        sp = _client()
    except Exception as e:
        return f"Spotify: {e}"
    try:
        if action in ("play", "search"):
            return _play(sp, p.get("query") or "")
        if action == "pause":
            sp.pause_playback()
            return "Spotify en pausa."
        if action == "resume":
            sp.start_playback()
            return "Spotify reproduciendo."
        if action == "next":
            sp.next_track()
            return "Siguiente tema."
        if action in ("prev", "previous"):
            sp.previous_track()
            return "Tema anterior."
        if action in ("volume", "vol"):
            v = int(p.get("value") or 50)
            sp.volume(max(0, min(100, v)))
            return f"Volumen {max(0, min(100, v))}%."
        if action == "current":
            cur = sp.currently_playing()
            if cur and cur.get("item"):
                it = cur["item"]
                return f"Sonando: {it['name']} — {it['artists'][0]['name']}"
            return "No hay nada sonando."
        return "spotify_control: action debe ser play | pause | resume | next | prev | volume | current."
    except Exception as e:
        return f"Spotify error: {e}"

TOOL = {
    "name": 'spotify_control',
    "description": 'Controla Spotify: reproducir/buscar, pausar, reanudar, siguiente, anterior, volumen, tema actual. Requiere credenciales de Spotify en config (spotify_client_id/secret). Usalo para cualquier pedido de música por voz.',
    "parameters": {   'type': 'OBJECT',
        'properties': {   'action': {   'type': 'STRING',
                                        'description': 'play | pause | resume | '
                                                       'next | prev | volume | '
                                                       'current'},
                          'query': {   'type': 'STRING',
                                       'description': 'Canción/artista a buscar '
                                                      '(play)'},
                          'value': {   'type': 'INTEGER',
                                       'description': 'Volumen 0-100 (volume)'}},
        'required': []},
    "handler": spotify_control,
}
