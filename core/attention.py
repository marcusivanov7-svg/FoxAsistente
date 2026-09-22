# -*- coding: utf-8 -*-
"""attention.py — Gestor de ATENCIÓN de FOX.

Antes FOX tenía dos estados y nada en medio: o el micrófono estaba abierto y
TODO lo que se oía en la habitación se enviaba a la nube, o estaba silenciado y
había que pulsar Ctrl+Espacio. El "modo suspensión" tampoco era gran cosa: se
dejaba de enviar audio pero el resto de la maquinaria seguía a pleno pulmón.

Este módulo introduce tres estados explícitos y las reglas para pasar de uno a
otro, todo decidido en local (sin gastar red ni cuota):

  ARMADO   — el estado de espera por defecto. El micrófono se escucha EN LOCAL
             con Vosk buscando "FOX". Nada sale del ordenador.
  ACTIVO   — hay una conversación en marcha: el audio se transmite a la nube.
             Es una VENTANA de tiempo, no un interruptor: si el usuario deja de
             hablarle, expira y se vuelve solo a ARMADO.
  DORMIDO  — reposo profundo, a petición del usuario. Se callan los avisos de
             fondo (guardián, sugerencias, informes) y se consume lo mínimo.
             OJO: sigue oyendo su nombre en local, igual que ARMADO. La
             diferencia con ARMADO no es el oído, es el silencio.

Salir del reposo es un caso aparte. Decir «FOX» para despertarlo no es una
pregunta, es abrirle la puerta: si ese audio se manda a la nube, el modelo lo
recibe como un turno y contesta "de acuerdo, señor", que sobra cuando ya suena
un pitido de confirmación. Por eso, al despertar del reposo, el audio se RETIENE
brevemente (GRACIA_DESPERTAR_SEG) en vez de enviarse: si el usuario no añade
nada, se descarta y FOX solo pita; si sigue hablando, se vuelca completo y la
orden llega entera. Despertar desde ARMADO no hace esto, porque ahí no hubo
orden de callarse.

Lo que este módulo NO gestiona es el micrófono APAGADO ("inactivo"). Ese estado
es del usuario y solo se alcanza con el atajo de teclado: cuando `ui.muted` está
activo el audio se descarta antes de llegar aquí. Es deliberado que ninguna orden
de voz pueda llevar a FOX ahí, porque entonces se quedaría sordo a su propio
nombre y solo se lo podría recuperar con el teclado.

La ventana de atención se renueva de dos maneras distintas a propósito:

  - Interacción real (un turno cerrado, FOX respondió) → ventana completa.
    Es una conversación; sería absurdo cortarla a los 10 segundos.
  - Solo voz detectada (VAD por energía) → prórroga corta con techo máximo.
    Así una frase larga no se corta a medias, pero una charla con otra persona
    en la misma habitación no mantiene el canal abierto para siempre.

El PREBUFFER es importante para que esto se sienta natural: se guardan los
últimos ~1,5 s de audio en un anillo, así al decir "Fox, ¿qué hora es?" la
pregunta entera llega a la nube y no solo lo que venga DESPUÉS del nombre.
"""
from __future__ import annotations

import collections
import json
import time
from typing import Callable, Iterable

# ── Estados ────────────────────────────────────────────────────────────────
DORMIDO = "ASLEEP"
ARMADO = "ARMED"
ACTIVO = "ACTIVE"

# ── Parámetros por defecto (todos configurables desde api_keys.json) ───────
VENTANA_SEG = 10.0        # duración de la ventana de atención
GRACIA_VOZ_SEG = 2.5      # prórroga mientras se detecta voz
TECHO_VOZ_SEG = 45.0      # máximo que la voz sola puede sostener la ventana
PREBUFFER_SEG = 1.5       # audio previo que se envía al activarse
VAD_UMBRAL = 0.012        # RMS normalizado a partir del cual se asume voz
# Cada cuánto audio se consulta al reconocedor. Se mide en SEGUNDOS DE AUDIO
# acumulado, no en tiempo de reloj: así ningún trozo se queda sin mirar aunque el
# micrófono entregue los bloques a un ritmo irregular.
VOSK_LOTE_ARMADO = 0.25
VOSK_LOTE_DORMIDO = 0.50
# Silencio tras el cual se da por cerrada la frase y se reinicia el reconocedor.
SILENCIO_CORTE = 0.9
# Al salir del reposo profundo con la palabra clave, el audio se retiene un
# instante en vez de mandarlo a la nube. Si el usuario solo dijo «FOX», ese
# audio se descarta y el modelo nunca lo oye: así FOX no contesta "de acuerdo,
# señor" a una simple llamada, basta el pitido de confirmación. Si en cambio la
# orden viene en la misma frase ("Fox, ¿qué hora es?"), al llenarse este plazo
# se vuelca lo retenido y no se pierde ni una sílaba.
#
# Igual que los lotes de Vosk, se mide en SEGUNDOS DE AUDIO retenido y no en
# tiempo de reloj: así la decisión depende de lo que se oyó, no del ritmo al que
# el micrófono entregue los bloques.
GRACIA_DESPERTAR_SEG = 1.2
# Voz acumulada que hay que oír durante esa gracia para dar por hecho que el
# usuario dijo algo más que el nombre. No vale un pico suelto: el pitido de
# confirmación puede volver por el micrófono y se colaría como si fuera habla.
VOZ_MINIMA_DESPERTAR_SEG = 0.35

# Palabras que cuentan como llamada.
PALABRAS_CLAVE = ("fox",)

# La gramática de Vosk se restringe a un puñado de palabras: el reconocimiento se
# vuelve muchísimo más rápido y ligero que transcribir todo el español.
#
# El detalle importante son los DISTRACTORES. Con una gramática de una sola
# palabra, Vosk fuerza cualquier ruido hacia ella y salta a la mínima. Dándole
# alternativas plausibles (palabras cortas y frecuentes, y algunas que suenan
# parecido a "fox") tiene dónde encajar lo que no es una llamada, y los falsos
# positivos caen en picado. Todas están en el vocabulario del modelo pequeño de
# español; las que no lo estén, Vosk las ignora sin avisar.
_DISTRACTORES = (
    "oye", "hey", "che", "escucha", "ya", "para", "gracias", "bueno",
    "claro", "vamos", "dale", "servicio", "javier", "jarra", "harris",
)
_GRAMATICA = json.dumps(list(PALABRAS_CLAVE) + list(_DISTRACTORES) + ["[unk]"])


def _construir_gramatica(palabras):
    return json.dumps(list(palabras) + list(_DISTRACTORES) + ["[unk]"])


class AttentionManager:
    """Decide, muestra a muestra, si el audio del micrófono debe salir a la nube.

    No toca la red ni la interfaz: solo mantiene el estado y avisa mediante
    callbacks. Quien lo usa (FoxLive) decide qué hacer con esos avisos.
    """

    def __init__(
        self,
        vosk_model_dir=None,
        *,
        sample_rate: int = 16000,
        on_estado: Callable[[str, str], None] | None = None,
        # Recibe True si la llamada sacó a FOX del reposo profundo.
        on_wake: Callable[[bool], None] | None = None,
        ocupado: Callable[[], bool] | None = None,
        log: Callable[[str], None] | None = None,
        config: dict | None = None,
        palabras_clave: tuple[str, ...] | None = None,
    ):
        cfg = config or {}
        self.sample_rate = sample_rate
        self.ventana_seg = float(cfg.get("attention_window_sec", VENTANA_SEG))
        self.gracia_voz = float(cfg.get("attention_voice_grace_sec", GRACIA_VOZ_SEG))
        self.techo_voz = float(cfg.get("attention_voice_cap_sec", TECHO_VOZ_SEG))
        self.vad_umbral = float(cfg.get("attention_vad_threshold", VAD_UMBRAL))
        # Escucha continua activada: si se desactiva, FOX se comporta como
        # antes (micrófono abierto de par en par mientras no esté silenciado).
        self.continua = bool(cfg.get("continuous_listening", True))

        self._on_estado = on_estado
        self._on_wake = on_wake
        # ¿Está FOX a media faena? Mientras devuelva True, la ventana NO expira.
        # Sin esto, el reloj corría en paralelo a lo que FOX estuviera haciendo:
        # si tardaba más que la ventana en hablar o en terminar una acción larga
        # (teclear código, generar una presentación), pasaba a "Di FOX" en
        # medio del trabajo y volvía a activarse al hablar. Nada dejaba de
        # funcionar, pero el estado que se veía era falso y desconcertante.
        self._ocupado = ocupado
        self._log = log or (lambda _m: None)

        self._estado = ARMADO if self.continua else ACTIVO
        self._hasta = 0.0          # instante en que expira la ventana
        self._techo = 0.0          # tope absoluto que la voz puede sostener
        self._ultima_voz = 0.0
        self._motivo = "inicio"

        # Anillo de prebuffer: bytes de audio crudo de los últimos PREBUFFER_SEG.
        bytes_por_seg = sample_rate * 2  # int16 mono
        self._prebuffer: collections.deque = collections.deque(
            maxlen=max(1, int(bytes_por_seg * PREBUFFER_SEG / 512))
        )
        self._volcar_prebuffer = False

        # Retención al salir del reposo: mientras esté activa, el audio no sale
        # a la nube y se acumula aquí. Si el usuario solo llamó a FOX y se
        # calló, se tira; si siguió hablando, se vuelca entero.
        self._reteniendo = False
        self._retenido: list[bytes] = []
        self._audio_retenido = 0.0       # segundos de audio acumulados
        self._voz_tras_despertar = 0.0   # de esos, cuántos llevan voz

        # Vosk: reconocedor local para la palabra clave.
        self._vosk = None
        self._buf_vosk = bytearray()
        self._ultimo_vosk = 0.0
        self._reconocedor_limpio = True
        self.palabras_clave = tuple(palabras_clave or PALABRAS_CLAVE)
        self._gramatica = _construir_gramatica(self.palabras_clave)
        self._cargar_vosk(vosk_model_dir)

    # ── Vosk ───────────────────────────────────────────────────────────────
    def _cargar_vosk(self, model_dir):
        if not model_dir:
            return
        try:
            import os

            if not os.path.isdir(str(model_dir)):
                self._log(f"[ATENCIÓN] Modelo Vosk no encontrado en {model_dir}")
                return
            try:
                import vosk
            except Exception:
                try:
                    from core import vosk_lite as vosk
                except Exception:
                    vosk = None
            if vosk is None:
                self._log("[ATENCIÓN] Vosk no disponible: instalá `pip install vosk` o revisá core/vosk_dll")
                return

            vosk.SetLogLevel(-1)  # sin ruido en el log
            modelo = vosk.Model(str(model_dir))
            try:
                # Gramática restringida: solo interesa la palabra clave.
                self._vosk = vosk.KaldiRecognizer(modelo, self.sample_rate, self._gramatica)
            except Exception:
                self._vosk = vosk.KaldiRecognizer(modelo, self.sample_rate)
            self._log("[ATENCIÓN] Palabra clave local lista (Vosk).")
        except Exception as e:
            self._log(f"[ATENCIÓN] Vosk no disponible: {e}")
            self._vosk = None

    @property
    def wake_disponible(self) -> bool:
        return self._vosk is not None

    def cargar_modelo(self, model_dir) -> bool:
        """Carga el modelo a posteriori (cuando la descarga inicial termina).

        La primera vez que se usa FOX el modelo de voz local puede no estar
        descargado todavía. En vez de dejar la escucha continua inutilizable
        hasta el siguiente arranque, se conecta en caliente en cuanto está.
        """
        if self._vosk is not None:
            return True
        self._cargar_vosk(model_dir)
        if self._vosk is None:
            return False
        self.continua = True
        self.armar("modelo de voz listo")
        return True

    # ── Estado ─────────────────────────────────────────────────────────────
    @property
    def estado(self) -> str:
        return self._estado

    @property
    def activo(self) -> bool:
        return self._estado == ACTIVO

    @property
    def dormido(self) -> bool:
        return self._estado == DORMIDO

    @property
    def restante(self) -> float:
        """Segundos que le quedan a la ventana de atención (0 si no está activa)."""
        if self._estado != ACTIVO:
            return 0.0
        return max(0.0, self._hasta - time.monotonic())

    def _esta_ocupado(self) -> bool:
        """¿FOX está a media faena? Nunca lanza: si falla, se asume que no."""
        if self._ocupado is None:
            return False
        try:
            return bool(self._ocupado())
        except Exception:
            return False

    def _cambiar(self, nuevo: str, motivo: str):
        if nuevo == self._estado:
            return
        anterior, self._estado = self._estado, nuevo
        self._motivo = motivo
        if self._vosk is not None:
            # Se limpia el reconocedor en cada cambio de estado para no arrastrar
            # audio viejo y volver a dispararse con un "fox" ya atendido.
            try:
                self._vosk.Reset()
            except Exception:
                pass
            self._reconocedor_limpio = True
        self._buf_vosk.clear()
        if self._on_estado:
            try:
                self._on_estado(nuevo, motivo)
            except Exception:
                pass

    # ── Transiciones públicas ──────────────────────────────────────────────
    def activar(self, motivo: str = "manual", *, con_prebuffer: bool = False,
                retener: bool = False):
        """Abre la ventana de atención (equivale a haber dicho «FOX»).

        Con `retener`, el audio no sale a la nube de inmediato: se guarda hasta
        confirmar que el usuario dijo algo más que el nombre. Se usa al salir del
        reposo, para que una llamada a secas no genere una respuesta hablada.
        """
        ahora = time.monotonic()
        self._hasta = ahora + self.ventana_seg
        self._techo = ahora + max(self.ventana_seg, self.techo_voz)
        if con_prebuffer:
            self._volcar_prebuffer = True
        if retener:
            self._reteniendo = True
            self._retenido = []
            self._audio_retenido = 0.0
            self._voz_tras_despertar = 0.0
        self._cambiar(ACTIVO, motivo)

    def armar(self, motivo: str = "expiró"):
        """Vuelve a la espera de la palabra clave."""
        self._hasta = 0.0
        self._techo = 0.0
        self._prebuffer.clear()
        self._volcar_prebuffer = False
        self._descartar_retenido()
        self._cambiar(ARMADO, motivo)

    def dormir(self, motivo: str = "orden"):
        """Reposo profundo: solo la palabra clave lo saca de aquí."""
        self._hasta = 0.0
        self._techo = 0.0
        self._prebuffer.clear()
        self._volcar_prebuffer = False
        self._descartar_retenido()
        self._cambiar(DORMIDO, motivo)

    def _descartar_retenido(self):
        """Tira el audio retenido y cierra la ventana de gracia del despertar."""
        self._reteniendo = False
        self._retenido = []
        self._audio_retenido = 0.0
        self._voz_tras_despertar = 0.0

    def renovar(self, motivo: str = "interacción"):
        """Interacción real: la ventana se reinicia entera.

        Se llama al cerrarse un turno o cuando FOX habla. Estando dormido no
        hace nada: de eso se trata el reposo.
        """
        if self._estado == DORMIDO:
            return
        ahora = time.monotonic()
        self._hasta = ahora + self.ventana_seg
        self._techo = ahora + max(self.ventana_seg, self.techo_voz)
        if self._estado != ACTIVO:
            self._cambiar(ACTIVO, motivo)

    # ── Bucle de audio ─────────────────────────────────────────────────────
    def procesar(self, pcm: bytes, rms: float) -> bool:
        """Procesa un bloque de audio del micrófono.

        Devuelve True si ese bloque debe transmitirse a la nube. `rms` es el
        nivel normalizado (0..1) que ya calcula el callback del micrófono, así
        se evita recalcularlo aquí.
        """
        ahora = time.monotonic()

        if not self.continua:
            return self._estado != DORMIDO

        hay_voz = rms >= self.vad_umbral
        if hay_voz:
            self._ultima_voz = ahora

        if self._estado == ACTIVO:
            # Gracia tras despertar del reposo: el audio se guarda en vez de
            # enviarse, hasta saber si el usuario dijo algo más que «FOX».
            if self._reteniendo:
                # Duración real del bloque: no todos miden lo mismo.
                dur = len(pcm) / (self.sample_rate * 2)
                self._audio_retenido += dur
                if hay_voz:
                    self._voz_tras_despertar += dur
                if self._audio_retenido < GRACIA_DESPERTAR_SEG:
                    self._retenido.append(pcm)
                    # Prórroga normal de la ventana mientras se retiene, para no
                    # perder el turno por estar esperando.
                    if hay_voz and self._hasta < self._techo:
                        self._hasta = min(
                            self._techo, max(self._hasta, ahora + self.gracia_voz)
                        )
                    return False
                # Se llenó la gracia: si se oyó habla de verdad, se vuelca todo
                # lo retenido; si no, se descarta y FOX se queda callado tras
                # el pitido, que es toda la confirmación que hace falta.
                self._reteniendo = False
                if self._voz_tras_despertar < VOZ_MINIMA_DESPERTAR_SEG:
                    # Solo se dijo el nombre: se tira ese audio para que el
                    # modelo no lo reciba como turno. La ventana SIGUE abierta,
                    # así el usuario da la orden a continuación sin repetir
                    # «FOX»: es justo para eso que lo llamó.
                    self._retenido = []
                    self._volcar_prebuffer = False
                    self._prebuffer.clear()

            # Prórroga corta mientras se oiga voz, con techo para que una
            # conversación ajena no mantenga el canal abierto indefinidamente.
            if hay_voz and self._hasta < self._techo:
                self._hasta = min(self._techo, max(self._hasta, ahora + self.gracia_voz))
            if ahora >= self._hasta:
                # Mientras FOX esté ocupado (hablando, pensando o ejecutando
                # una acción) la ventana no expira: la conversación sigue viva,
                # aunque el usuario esté callado esperando la respuesta. El techo
                # de voz NO se aplica aquí a propósito, porque no es el ruido de
                # la habitación lo que sostiene la ventana, es el propio trabajo
                # de FOX, que termina solo.
                if self._esta_ocupado():
                    self._hasta = ahora + self.gracia_voz
                    return True
                self.armar("silencio")
                return False
            self._prebuffer.clear()
            return True

        # ARMADO o DORMIDO: nada sale del equipo. Solo se busca la palabra clave.
        if self._estado == ARMADO:
            self._prebuffer.append(pcm)

        # Puerta de energía: en silencio prolongado no se molesta al reconocedor.
        # Es lo que hace que el reposo sea de verdad barato en CPU. El margen tras
        # la última voz evita cortar la frase en las pausas naturales del habla.
        en_habla = hay_voz or (ahora - self._ultima_voz) < SILENCIO_CORTE
        if not en_habla:
            if self._buf_vosk:
                self._buf_vosk.clear()
            if self._vosk is not None and not self._reconocedor_limpio:
                # Frase terminada sin llamada: se reinicia para que el audio viejo
                # no se mezcle con la frase siguiente.
                try:
                    self._vosk.Reset()
                except Exception:
                    pass
                self._reconocedor_limpio = True
            return False

        self._buf_vosk.extend(pcm)
        self._reconocedor_limpio = False

        lote = VOSK_LOTE_DORMIDO if self._estado == DORMIDO else VOSK_LOTE_ARMADO
        minimo = int(self.sample_rate * 2 * lote)   # int16 mono
        if len(self._buf_vosk) < minimo:
            return False

        audio = bytes(self._buf_vosk)
        self._buf_vosk.clear()
        self._ultimo_vosk = ahora
        if self._detectar_clave(audio):
            # Venía del reposo: se retiene el audio para no contestarle a una
            # llamada a secas. Desde ARMADO se transmite ya, como siempre.
            venia_dormido = self._estado == DORMIDO
            self.activar(
                "palabra clave", con_prebuffer=True, retener=venia_dormido
            )
            if self._on_wake:
                try:
                    self._on_wake(venia_dormido)
                except Exception:
                    pass
            # Al retener, este bloque tampoco sale: entra en la cola de espera.
            if venia_dormido:
                self._retenido.append(pcm)
                return False
            return True
        return False

    def _detectar_clave(self, audio: bytes) -> bool:
        if self._vosk is None or not audio:
            return False
        try:
            if self._vosk.AcceptWaveform(audio):
                texto = json.loads(self._vosk.Result()).get("text", "")
            else:
                texto = json.loads(self._vosk.PartialResult()).get("partial", "")
        except Exception:
            return False
        return _contiene_clave(texto, self.palabras_clave)

    def tomar_prebuffer(self) -> Iterable[bytes]:
        """Devuelve (una sola vez) el audio previo a la palabra clave.

        Sin esto se perdería el arranque de la frase y FOX oiría preguntas a
        medias, que es justo lo que hace que un asistente parezca torpe.

        Incluye también el audio retenido durante la gracia del despertar, en
        orden: primero lo anterior al nombre, después lo que se dijo mientras se
        esperaba. Si la llamada fue a secas, ese retenido ya se descartó.
        """
        bloques: list[bytes] = []
        if self._volcar_prebuffer:
            self._volcar_prebuffer = False
            bloques.extend(self._prebuffer)
            self._prebuffer.clear()
        if self._retenido and not self._reteniendo:
            bloques.extend(self._retenido)
            self._retenido = []
        return bloques

    def aplicar_config(self, cfg: dict):
        """Recarga los parámetros cuando el usuario guarda los ajustes."""
        cfg = cfg or {}
        self.ventana_seg = float(cfg.get("attention_window_sec", self.ventana_seg))
        self.gracia_voz = float(cfg.get("attention_voice_grace_sec", self.gracia_voz))
        self.techo_voz = float(cfg.get("attention_voice_cap_sec", self.techo_voz))
        self.vad_umbral = float(cfg.get("attention_vad_threshold", self.vad_umbral))
        nueva = bool(cfg.get("continuous_listening", self.continua))
        if nueva != self.continua:
            self.continua = nueva
            if nueva:
                self.armar("ajustes")
            else:
                self.activar("ajustes")


def _contiene_clave(texto: str, palabras_clave=PALABRAS_CLAVE) -> bool:
    t = (texto or "").lower()
    return any(p in t for p in palabras_clave) if t else False
