# -*- coding: utf-8 -*-
"""vosk_lite.py  Wrapper ctypes mínimo para libvosk.dll.

Usa las DLLs de Vosk incluidas en core/vosk_dll/. No requiere pip install vosk.
La API imita a la del paquete oficial `vosk` para que core/attention.py funcione
igual con o sin el paquete instalado.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path

_LOGGER_LIB = None  # para SetLogLevel


def _dll_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "core" / "vosk_dll"
    return Path(__file__).resolve().parent / "vosk_dll"


def _lib():
    global _LOGGER_LIB
    if _LOGGER_LIB is not None:
        return _LOGGER_LIB
    dll_dir = _dll_dir()
    if not (dll_dir / "libvosk.dll").exists():
        raise RuntimeError(f"libvosk.dll no encontrado en {dll_dir}")
    # Asegura que Windows encuentre las DLLs dependientes (libstdc++-6.dll, etc.)
    if hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(str(dll_dir))
        except Exception:
            pass
    lib = ctypes.CDLL(str(dll_dir / "libvosk.dll"))

    lib.vosk_model_new.argtypes = [ctypes.c_char_p]
    lib.vosk_model_new.restype = ctypes.c_void_p

    lib.vosk_model_free.argtypes = [ctypes.c_void_p]
    lib.vosk_model_free.restype = None

    lib.vosk_recognizer_new.argtypes = [ctypes.c_void_p, ctypes.c_float]
    lib.vosk_recognizer_new.restype = ctypes.c_void_p

    lib.vosk_recognizer_new_grm.argtypes = [ctypes.c_void_p, ctypes.c_float, ctypes.c_char_p]
    lib.vosk_recognizer_new_grm.restype = ctypes.c_void_p

    lib.vosk_recognizer_accept_waveform.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.vosk_recognizer_accept_waveform.restype = ctypes.c_int

    lib.vosk_recognizer_result.argtypes = [ctypes.c_void_p]
    lib.vosk_recognizer_result.restype = ctypes.c_char_p

    lib.vosk_recognizer_partial_result.argtypes = [ctypes.c_void_p]
    lib.vosk_recognizer_partial_result.restype = ctypes.c_char_p

    lib.vosk_recognizer_reset.argtypes = [ctypes.c_void_p]
    lib.vosk_recognizer_reset.restype = None

    lib.vosk_recognizer_free.argtypes = [ctypes.c_void_p]
    lib.vosk_recognizer_free.restype = None

    lib.vosk_set_log_level.argtypes = [ctypes.c_int]
    lib.vosk_set_log_level.restype = None

    _LOGGER_LIB = lib
    return lib


def SetLogLevel(level: int = -1) -> None:
    try:
        _lib().vosk_set_log_level(int(level))
    except Exception:
        pass


class Model:
    """Modelo Vosk cargado desde un directorio descomprimido."""

    def __init__(self, model_path: str):
        self._lib = _lib()
        self._handle = self._lib.vosk_model_new(str(model_path).encode("utf-8"))
        if not self._handle:
            raise RuntimeError(f"Vosk no pudo cargar el modelo en {model_path}")

    def __del__(self):
        try:
            if getattr(self, "_handle", None):
                self._lib.vosk_model_free(self._handle)
        except Exception:
            pass


class KaldiRecognizer:
    """Reconocedor Vosk a 16 kHz, con o sin gramática restringida."""

    def __init__(self, model: Model, sample_rate: float, grammar: str | None = None):
        self._lib = model._lib
        self._model = model
        self._handle = None
        if grammar:
            self._handle = self._lib.vosk_recognizer_new_grm(
                model._handle, float(sample_rate), grammar.encode("utf-8")
            )
        else:
            self._handle = self._lib.vosk_recognizer_new(model._handle, float(sample_rate))
        if not self._handle:
            raise RuntimeError("Vosk no pudo crear el reconocedor")

    def AcceptWaveform(self, data: bytes) -> bool:
        return bool(self._lib.vosk_recognizer_accept_waveform(self._handle, bytes(data), len(data)))

    def Result(self) -> str:
        p = self._lib.vosk_recognizer_result(self._handle)
        if not p:
            return ""
        return p.decode("utf-8", errors="replace")

    def PartialResult(self) -> str:
        p = self._lib.vosk_recognizer_partial_result(self._handle)
        if not p:
            return ""
        return p.decode("utf-8", errors="replace")

    def Reset(self) -> None:
        self._lib.vosk_recognizer_reset(self._handle)

    def __del__(self):
        try:
            if getattr(self, "_handle", None):
                self._lib.vosk_recognizer_free(self._handle)
        except Exception:
            pass
