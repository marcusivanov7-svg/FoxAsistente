"""amsy_superpowers — focus, ambient sounds, voice notes, multi-monitor (ported from AMSY).

feature=focus    → focus timer (start/stop/status)
feature=ambient  → rain / cafe / forest / ocean / white / pink / brown noise
feature=note     → save categorized voice/text notes
feature=monitor  → move the active window to another monitor
"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR   = get_base_dir()
NOTES_PATH = BASE_DIR / "memory" / "voice_notes.json"

# ── Focus timer ───────────────────────────────────────────────────────────────
_focus_start: float | None = None
_focus_minutes: int = 0


def _focus_begin(minutes: int) -> str:
    global _focus_start, _focus_minutes
    _focus_start = time.monotonic()
    _focus_minutes = max(1, min(180, int(minutes or 25)))
    return f"Focus mode activado: {_focus_minutes} minutos. Sin distracciones."


def _focus_stop() -> str:
    global _focus_start, _focus_minutes
    if _focus_start is None:
        return "No hay focus mode activo."
    _focus_start = None
    _focus_minutes = 0
    return "Focus mode desactivado."


def _focus_status() -> str:
    if _focus_start is None:
        return "No hay focus mode activo."
    elapsed = (time.monotonic() - _focus_start) / 60
    remaining = _focus_minutes - elapsed
    if remaining <= 0:
        return "El focus mode terminó. ¡Buen trabajo!"
    return f"Focus mode activo: quedan {int(remaining)} min {int((remaining % 1) * 60)} s."


# ── Ambient sounds (background noise generator, no audio files) ───────────────
_ambient_thread: threading.Thread | None = None
_ambient_stop = threading.Event()


def _noise_block(kind: str, n: int):
    try:
        import numpy as np
    except Exception:
        return None
    if kind in ("white", "lluvia", "rain"):
        b = np.random.randn(n)
    elif kind in ("brown", "cafe", "café", "coffee"):
        b = np.cumsum(np.random.randn(n))
    elif kind in ("pink", "bosque", "forest"):
        white = np.random.randn(n)
        b = np.convolve(white, [0.05, 0.1, 0.2, 0.3, 0.2, 0.1, 0.05], mode="same")
    else:  # ocean / default
        white = np.random.randn(n)
        b = np.convolve(white, [0.2, 0.5, 0.3], mode="same")
    b = b.astype(np.float32)
    peak = float(np.max(np.abs(b))) + 1e-6
    return b / peak * 0.08


def _ambient_loop(kind: str, seconds: int) -> None:
    try:
        import sounddevice as sd
    except Exception as e:
        print(f"[superpowers] sounddevice no disponible: {e}")
        return
    sr = 16000
    block = int(sr * 1.0)
    deadline = time.monotonic() + seconds
    try:
        while not _ambient_stop.is_set() and time.monotonic() < deadline:
            b = _noise_block(kind, block)
            if b is None:
                return
            sd.play(b, sr)
            sd.wait()
    finally:
        try:
            sd.stop()
        except Exception:
            pass


def _ambient_start(kind: str, minutes: int) -> str:
    global _ambient_thread
    kind = (kind or "ocean").lower()
    if _ambient_thread and _ambient_thread.is_alive():
        _ambient_stop.set()
        _ambient_thread.join(timeout=2)
    _ambient_stop.clear()
    _ambient_thread = threading.Thread(
        target=_ambient_loop,
        args=(kind, max(1, min(120, int(minutes or 10))) * 60),
        daemon=True,
    )
    _ambient_thread.start()
    return f"Sonido ambiental '{kind}' reproduciendo."


def _ambient_stop_now() -> str:
    global _ambient_thread
    _ambient_stop.set()
    if _ambient_thread:
        _ambient_thread.join(timeout=2)
        _ambient_thread = None
    return "Sonido ambiental detenido."


# ── Voice notes ───────────────────────────────────────────────────────────────
def _notes_load() -> list:
    if not NOTES_PATH.exists():
        return []
    try:
        d = json.loads(NOTES_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _note_add(category: str, text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "Falta el texto de la nota."
    notes = _notes_load()
    notes.append({
        "category": (category or "general").strip().lower(),
        "text":     text[:600],
        "created":  datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTES_PATH.write_text(json.dumps(notes, indent=2, ensure_ascii=False), encoding="utf-8")
    return f"Nota guardada ({category or 'general'}): {text[:60]}"


def _note_list() -> str:
    notes = _notes_load()
    if not notes:
        return "No tenés notas guardadas."
    return "Notas:\n" + "\n".join(f"- [{n['category']}] {n['text'][:120]}" for n in notes[-10:])


# ── Multi-monitor ─────────────────────────────────────────────────────────────
def _move_to_monitor(index: int) -> str:
    try:
        import mss
        import pygetwindow as gw
        monitors = mss.mss().monitors  # [0]=all, [1..]=each physical monitor
        if len(monitors) <= 2:
            return "Solo hay un monitor detectado."
        if not (1 <= index < len(monitors)):
            index = 1
        w = gw.getActiveWindow()
        if not w:
            return "No pude detectar la ventana activa."
        target = monitors[index]
        w.moveTo(target["left"], target["top"])
        return f"Ventana activa movida al monitor {index}."
    except Exception as e:
        return f"No pude mover la ventana: {e}"


def amsy_superpowers(parameters=None, player=None, **kwargs) -> str:
    p = parameters or {}
    feature = (p.get("feature") or "").strip().lower()
    action = (p.get("action") or "start").strip().lower()

    if feature in ("focus", "foco", "concentración", "concentracion"):
        if action in ("stop", "off"):
            return _focus_stop()
        if action in ("status", "state"):
            return _focus_status()
        return _focus_begin(int(p.get("minutes") or 25))

    if feature in ("ambient", "ambiente", "sound", "sonido"):
        if action in ("stop", "off"):
            return _ambient_stop_now()
        return _ambient_start(p.get("kind") or "ocean", int(p.get("minutes") or 10))

    if feature in ("note", "nota", "voice_note", "voice"):
        if action in ("list", "ls"):
            return _note_list()
        return _note_add(p.get("category") or "general", p.get("text"))

    if feature in ("monitor", "multi_monitor", "multimonitor"):
        return _move_to_monitor(int(p.get("index") or 1))

    return ("amsy_superpowers: feature debe ser focus | ambient | note | monitor. "
            "Ej: feature=focus, action=start, minutes=25.")

TOOL = {
    "name": 'amsy_superpowers',
    "description": 'Funciones exclusivas: focus mode (timer de concentración), sonidos ambientales (lluvia, café, bosque, océano, ruido blanco/rosa/marrón), notas de voz/texto por categorías, y mover la ventana activa entre monitores. feature=focus|ambient|note|monitor, action=start|stop|status.',
    "parameters": {   'type': 'OBJECT',
        'properties': {   'feature': {   'type': 'STRING',
                                         'description': 'focus | ambient | note | '
                                                        'monitor'},
                          'action': {   'type': 'STRING',
                                        'description': 'start | stop | status | '
                                                       'list'},
                          'minutes': {   'type': 'INTEGER',
                                         'description': 'Duración en minutos'},
                          'kind': {   'type': 'STRING',
                                      'description': 'Tipo de sonido: rain | cafe '
                                                     '| forest | ocean | white | '
                                                     'pink | brown'},
                          'category': {   'type': 'STRING',
                                          'description': 'Categoría de nota'},
                          'text': {   'type': 'STRING',
                                      'description': 'Texto de la nota'},
                          'index': {   'type': 'INTEGER',
                                       'description': 'Monitor destino (1, 2, '
                                                      '...)'}},
        'required': []},
    "handler": amsy_superpowers,
}
