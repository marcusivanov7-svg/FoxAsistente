"""
image_generator.py — Fox AI image generation (v2)

Generates images through Pollinations.ai (free, no API key) and saves them,
then opens the result. Improvements over the v1 version:

- v2 tool signature: `image_generator(parameters, player, speak)`.
- Size/aspect presets (square, landscape, wide, portrait) plus raw width/height.
- Model selection (`flux` by default), seed for reproducibility, enhance toggle.
- Content-type check, retries, and a graceful timeout (no hard crash).
- Saves to Desktop/FoxAI/images by default (configurable via `save_path`).
"""

from __future__ import annotations

import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import requests

# Pollinations free endpoint. `nologo=true` removes their watermark banner.
_IMAGE_URL = "https://image.pollinations.ai/prompt/{prompt}"

# Aspect-ratio presets → (width, height). Pollinations accepts arbitrary sizes;
# these are just friendly shorthands for "square", "horizontal", "vertical", etc.
_ASPECTS = {
    "square":    (1024, 1024),
    "cuadrado":  (1024, 1024),
    "landscape": (1280, 720),
    "horizontal": (1280, 720),
    "paisaje":   (1280, 720),
    "wide":      (1536, 640),
    "panoramico": (1536, 640),
    "portrait":  (720, 1280),
    "vertical":  (720, 1280),
    "retrato":   (720, 1280),
    "tall":      (768, 1344),
}

_KNOWN_MODELS = {"flux", "turbo", "gptimage", "kontext"}


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _default_output_dir() -> Path:
    d = Path.home() / "Desktop" / "FoxAI" / "images"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(prompt: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "", (prompt or "").strip())
    safe = re.sub(r"\s+", " ", safe).strip().replace(" ", "_").lower()
    return safe[:60] or "fox_image"


def _resolve_size(parameters: dict) -> tuple[int, int]:
    try:
        w = int(parameters.get("width") or 0)
        h = int(parameters.get("height") or 0)
    except Exception:
        w = h = 0
    if w > 0 and h > 0:
        return max(64, min(w, 4096)), max(64, min(h, 4096))

    key = (parameters.get("aspect") or parameters.get("size") or "square").strip().lower()
    return _ASPECTS.get(key, (1024, 1024))


def _resolve_model(parameters: dict) -> str:
    m = (parameters.get("model") or "").strip().lower()
    return m if m in _KNOWN_MODELS else "flux"


def _build_url(prompt: str, parameters: dict, width: int, height: int, seed: int) -> str:
    params = {
        "width": width,
        "height": height,
        "nologo": "true" if parameters.get("nologo", True) else "false",
        "model": _resolve_model(parameters),
    }
    if seed is not None:
        params["seed"] = seed
    if parameters.get("enhance") is True:
        params["enhance"] = "true"
    qs = urllib.parse.urlencode(params)
    return _IMAGE_URL.format(prompt=urllib.parse.quote(prompt)) + "?" + qs


def image_generator(parameters=None, player=None, speak=None, **kwargs) -> str:
    p = parameters or {}
    prompt = (p.get("prompt") or p.get("description") or "").strip()
    if not prompt:
        return "Decime qué querés que dibuje (falta el `prompt`)."

    # Optional style/negative hints get folded into the prompt so the model
    # reads them naturally (Pollinations has no separate negative field).
    style = (p.get("style") or "").strip()
    negative = (p.get("negative") or "").strip()
    if style:
        prompt = f"{prompt}, in the style of {style}"
    if negative:
        prompt = f"{prompt}. Avoid: {negative}"

    width, height = _resolve_size(p)
    try:
        seed = int(p.get("seed")) if p.get("seed") not in (None, "") else None
    except Exception:
        seed = None

    if player:
        player.write_log(f"SYS: 🎨 Generando imagen ({width}x{height})...")
    if speak:
        speak("Iniciando renderizado de la imagen. Dame unos segundos.")

    # Output location: explicit path > custom folder > default Desktop/FoxAI/images
    save_path = (p.get("save_path") or "").strip()
    if save_path:
        out = Path(save_path).expanduser()
        if out.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
            out = out.with_suffix(".jpg")
    else:
        folder = (p.get("folder") or "").strip()
        if folder:
            out = Path(folder).expanduser()
            out.mkdir(parents=True, exist_ok=True)
        else:
            out = _default_output_dir()
        out = out / f"{_slug(prompt)}_{int(time.time())}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)

    url = _build_url(prompt, p, width, height, seed)

    last_error = ""
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=60, headers={"Accept": "image/*"})
            if resp.status_code == 200:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "image" not in ctype and not resp.content[:4].startswith((b"\xff\xd8", b"\x89PNG", b"RIFF")):
                    last_error = f"El servidor devolvió un tipo no esperado ({ctype or 'desconocido'})."
                    time.sleep(2 * (attempt + 1))
                    continue
                out.write_bytes(resp.content)
                size_kb = len(resp.content) // 1024
                if player:
                    player.write_log(f"SYS: ✅ Imagen guardada en {out}")
                # Abrir automáticamente en el visor por defecto.
                if p.get("auto_open", True):
                    try:
                        if os.name == "nt":
                            os.startfile(str(out))  # type: ignore[attr-defined]
                        elif sys.platform == "darwin":
                            os.system(f'open "{out}"')
                        else:
                            os.system(f'xdg-open "{out}" &')
                    except Exception:
                        pass
                return (
                    f"Imagen generada ({width}x{height}, {size_kb} KB) y guardada en: {out}. "
                    "Avisale al usuario que ya está abierta en su pantalla."
                )
            last_error = f"El servidor de imágenes respondió con código {resp.status_code}."
        except requests.exceptions.Timeout:
            last_error = "El servidor de imágenes tardó demasiado en responder."
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
        time.sleep(2 * (attempt + 1))

    return f"No pude generar la imagen tras varios intentos. Detalle: {last_error}"

TOOL = {
    "name": 'image_generator',
    "description": 'Generates an image from a text description (free, no API key needed), saves it and opens it on screen. Use whenever the user asks to draw, create, generá or imagine a picture, image, wallpaper, logo, or artwork. Always translate the request into a detailed English `prompt` with the subject, scene, style and lighting. Pick `aspect` (square|landscape|wide|portrait) or pass `width`/`height`, and optionally a `style` or `negative` hint.',
    "parameters": {   'type': 'OBJECT',
        'properties': {   'prompt': {   'type': 'STRING',
                                        'description': 'Detailed English '
                                                       'description of the image '
                                                       'to generate'},
                          'aspect': {   'type': 'STRING',
                                        'description': 'square | landscape | wide '
                                                       '| portrait (default: '
                                                       'square)'},
                          'width': {   'type': 'INTEGER',
                                       'description': 'Explicit width in pixels '
                                                      '(overrides aspect)'},
                          'height': {   'type': 'INTEGER',
                                        'description': 'Explicit height in pixels '
                                                       '(overrides aspect)'},
                          'model': {   'type': 'STRING',
                                       'description': 'flux (default) | turbo | '
                                                      'gptimage | kontext'},
                          'seed': {   'type': 'INTEGER',
                                      'description': 'Optional seed to reproduce '
                                                     'the same image'},
                          'enhance': {   'type': 'BOOLEAN',
                                         'description': 'Run enhancement/upscaling '
                                                        '(default: false)'},
                          'style': {   'type': 'STRING',
                                       'description': 'Style hint, e.g. '
                                                      "'watercolor', 'cyberpunk', "
                                                      "'photorealistic'"},
                          'negative': {   'type': 'STRING',
                                          'description': 'What to avoid in the '
                                                         'image'},
                          'save_path': {   'type': 'STRING',
                                           'description': 'Optional full output '
                                                          'path (default: '
                                                          'Desktop/FoxAI/images)'},
                          'auto_open': {   'type': 'BOOLEAN',
                                           'description': 'Open the image after '
                                                          'creating it (default: '
                                                          'true)'}},
        'required': ['prompt']},
    "handler": image_generator,
}
