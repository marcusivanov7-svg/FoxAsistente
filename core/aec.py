# -*- coding: utf-8 -*-
"""core/aec.py — Cancelación de eco acústico (AEC) para FOX.

Envuelve `pywebrtc-audio` (WebRTC AEC3, el mismo que corre en Chrome) para
quitar del micrófono el eco de la propia voz de FOX antes de mandar el audio a
Gemini.

Por qué hace falta: mientras FOX habla por los parlantes, el micrófono captura
esa voz (eco) además de la del usuario. El VAD de Gemini busca «silencio → voz»
para detectar el barge-in, pero como el micrófono nunca está en silencio
mientras FOX habla, nunca salta y la interrupción no funciona. Limpiando el eco,
Gemini vuelve a escuchar solo al usuario y puede cortarse a mitad de frase
(y entonces «seguí con lo que estabas diciendo» retoma donde quedó).

Uso (dos hilos distintos, por eso es thread-safe por fuera):

  - El reproductor (`_play_audio`) llama `alimentar_referencia()` con lo que
    escribe al parlante, ya resampleado a la frecuencia del micrófono.
  - El callback del micrófono llama `procesar()` con el audio capturado y
    recibe el audio sin eco.

El objeto interno de WebRTC se crea y usa SOLO en el hilo que llama a
`procesar()` (el hilo del micrófono), cumpliendo el requisito de la librería
de no usarse desde varios hilos a la vez. Si la librería no está instalada, el
módulo degrada limpiamente: `disponible` es False y `procesar()` devuelve el
audio tal cual.
"""
from __future__ import annotations

import threading

import numpy as np


class EchoCanceller:
    def __init__(self, sample_rate: int = 16000, stream_delay_ms: int = 0):
        self.sample_rate = sample_rate
        self.stream_delay_ms = stream_delay_ms   # 0 → AEC3 estima el retardo solo
        self._mod = None
        self._proc = None
        self._ready = False
        self._error = None
        self._lock = threading.Lock()
        # Referencia (lo que suena por el parlante), ya a self.sample_rate.
        # Se consume en sincronía con cada bloque del micrófono en `procesar()`.
        self._far = bytearray()
        self._importar()

    def _importar(self):
        try:
            import pywebrtc_audio as _m
            self._mod = _m
            self._ready = True
        except Exception as e:            # no instalado / sin wheel para esta versión
            self._error = e
            self._ready = False

    @property
    def disponible(self) -> bool:
        return self._ready

    @property
    def error(self):
        return self._error

    def _asegurar_proc(self):
        """Crea el AudioProcessor de forma perezosa, en el hilo del micrófono."""
        if self._proc is None and self._mod is not None:
            self._proc = self._mod.AudioProcessor(
                sample_rate=self.sample_rate,
                num_channels=1,
                echo_cancellation=True,
                noise_suppression=True,      # limpia ruido de fondo (nivel 1, suave)
                high_pass_filter=True,
                auto_gain_control=False,     # no queremos cambiar el volumen de tu voz
                stream_delay_ms=self.stream_delay_ms,
            )

    def alimentar_referencia(self, pcm: bytes) -> None:
        """El reproductor llama esto con cada trozo que va a sonar por el
        parlante (int16, a self.sample_rate). Solo guarda historia reciente."""
        if not pcm or not self._ready:
            return
        with self._lock:
            self._far.extend(pcm)
            # Tope de ~3 s de historia: lo más viejo ya no produce eco relevante
            # y evita que el buffer crezca sin fin si el ritmo de los dos flujos
            # se desfasa momentáneamente.
            tope = self.sample_rate * 2 * 3
            if len(self._far) > tope:
                del self._far[: len(self._far) - tope]

    def reset(self) -> None:
        """Limpia el estado del AEC (tras una interrupción, p. ej.)."""
        try:
            if self._proc is not None:
                self._proc.reset()
        except Exception:
            pass
        with self._lock:
            self._far.clear()

    def procesar(self, near: bytes) -> bytes:
        """Limpia el eco de `near` (micrófono, int16). Devuelve el audio limpio
        del mismo largo. Si AEC no está disponible, devuelve `near` sin tocar."""
        if not self._ready or not near:
            return near

        n = len(near)
        with self._lock:
            if len(self._far) >= n:
                far = bytes(self._far[:n])
                del self._far[:n]
            else:
                # Todavía no hay suficiente referencia (recién empieza a sonar):
                # completar el faltante con silencio. AEC3 converge en los
                # primeros frames.
                faltan = n - len(self._far)
                far = bytes(self._far) + b"\x00\x00" * (faltan // 2)
                self._far.clear()

        try:
            self._asegurar_proc()
            near_arr = np.frombuffer(near, dtype=np.int16)
            far_arr = np.frombuffer(far, dtype=np.int16)
            limpio = self._proc.process(near_arr, far_arr)
            return np.ascontiguousarray(limpio, dtype=np.int16).tobytes()
        except Exception:
            # Nunca romper el audio del micrófono por un fallo del AEC.
            return near
