import platform as _platform
import subprocess as _subprocess

# ── OpenCV log silencing ─────────────────────────────────────────────────────
# NVIDIA Broadcast's virtual camera + OpenCV's DSHOW backend print noisy
# "[ WARN:0]" / videoio debug lines. Must run BEFORE cv2 is imported (which
# happens transitively via actions.screen_processor below).
import os as _os_env
_os_env.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
_os_env.environ.setdefault("OPENCV_VIDEOIO_DEBUG", "0")

# ── Console encoding ─────────────────────────────────────────────────────────
# Windows consoles default to a legacy codepage — cp1254 in Turkey, cp1251 in
# Russia, cp932 in Japan. Printing an emoji there raises UnicodeEncodeError, and
# several of these prints sit inside except handlers, so the handler itself dies
# and skips the recovery code after it. Reconfiguring to UTF-8 with a
# replacement fallback costs nothing and makes the app behave in every locale.
import sys as _sys

for _stream in ("stdout", "stderr"):
    try:
        _s = getattr(_sys, _stream, None)
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass          # pythonw / redirected pipes / anything exotic — never fatal

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **                       kw)

    _subprocess.Popen = _Popen

# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import re
import threading
import time
import json
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import sounddevice as sd
import numpy as np
from google import genai
from google.genai import types
from ui import FoxUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    save_session_summary, pop_last_session,
    search_memory, set_trim_notifier, set_token_saver,
)

from actions.file_processor import file_processor
from actions.flight_finder     import flight_finder
from actions.open_app          import open_app
from actions.weather_report    import weather_action
from actions.send_message      import send_message
from actions.reminder          import reminder
from actions.computer_settings import computer_settings
from actions.office_builder     import office_builder
from actions.image_generator    import image_generator
from actions.tuya_controller    import tuya_controller
from actions.screen_processor  import _capture_camera, _capture_camera_burst, _capture_screen, _LiveCapture
from actions.youtube_video     import youtube_video
from actions.desktop           import desktop_control
from actions.browser_control   import browser_control
from actions.file_controller   import file_controller
from actions.code_helper       import code_helper
from actions.dev_agent         import dev_agent
from actions.web_search        import web_search as web_search_action
from actions.computer_control  import computer_control
from actions.game_updater      import game_updater
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.proactive         import ProactiveEngine
from actions.background_monitor import (
    add_monitor, remove_monitor, list_monitors, check_all as monitor_check_all,
)
from actions.web_search        import _news as _fetch_news_sync
from actions.smart_memory      import smart_memory
from actions.rules_engine      import rules_engine, format_rules_for_prompt
from actions.desktop_memory    import desktop_memory
from actions.window_context    import window_context
from actions.project_navigator import project_navigator
from actions.user_profile       import user_profile
from actions.windows_settings   import windows_settings
from actions.productivity_inbox import productivity_inbox
from actions.amsy_superpowers   import amsy_superpowers
from actions.telegram_api       import telegram_api
from actions.spotify_control    import spotify_control
from memory.config_manager     import (
    get_brief_enabled, get_voice, get_input_device, get_output_device,
    get_model_for,
)
from core.plugin_loader        import discover_plugins
from core.action_loader         import discover_actions
from core                      import undo as undo_stack
from core                      import confirm as confirm_gate
from core                      import audio_devices
from core.attention             import AttentionManager
from core.echo                  import EchoGuard
from core.geolocation           import get_location

def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"


def _buscar_modelo_vosk():
    """Devuelve el directorio del modelo Vosk local, si existe."""
    candidatos = []
    try:
        candidatos.append(BASE_DIR / "config" / "vosk_model")
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        try:
            candidatos.append(Path(getattr(sys, "_MEIPASS", "")) / "config" / "vosk_model")
        except Exception:
            pass
    for p in candidatos:
        if p and (p / "am" / "final.mdl").exists():
            return p
    return None
LIVE_MODEL          = "models/gemini-3.1-flash-live-preview"

# FOX: pool de modelos con rotación automática ante fallos repetidos (1011, etc.)
# Solo modelos "live" / native-audio (los de texto no sirven para la sesión de voz).
# NOTA: `gemini-2.5-flash-native-audio-latest` fue deprecado por Google — NO rotar hacia él.
FALLBACK_MODEL_POOL = [
    "models/gemini-3.1-flash-live-preview",
]

# Una sesión se considera "sana" solo si sobrevivió al menos este tiempo. Si el
# modelo conecta y se cae a los pocos segundos (1011, etc.), eso CUENTA como fallo
# para la rotación, aunque técnicamente "conectó".
SESSION_HEALTHY_SEC = 60.0

def _get_model_pool() -> list[str]:
    pool = [get_model_for("conversation", LIVE_MODEL)] + list(FALLBACK_MODEL_POOL)
    try:
        _cfg = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
        _custom = _cfg.get("live_models")
        if isinstance(_custom, list) and any(str(m).strip() for m in _custom):
            pool = [str(m).strip() for m in _custom if str(m).strip()]
    except Exception:
        pass
    return pool


def _model_needs_realtime_text(model: str) -> bool:
    """True si el modelo es un Gemini 3.x "live" (A2A).

    Estos modelos rechazan `send_client_content` para texto en plena conversación
    con error 1007 ("invalid argument"); el texto en caliente debe ir por
    `send_realtime_input` en su lugar. Los Gemini 2.x (native audio) aceptan ambos
    y siguen usando `send_client_content` por su garantía de orden."""
    return "gemini-3" in (model or "").lower()


def _briefing_due_today() -> bool:
    """True si el briefing de arranque aún no se dio hoy (persistido en config)."""
    try:
        cfg = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
        return cfg.get("last_briefing_date", "") != datetime.now().strftime("%Y-%m-%d")
    except Exception:
        return True


def _mark_briefing_sent() -> None:
    """Persiste la fecha de hoy para que el briefing no se repita en el día."""
    try:
        cfg = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
        cfg["last_briefing_date"] = datetime.now().strftime("%Y-%m-%d")
        API_CONFIG_PATH.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"[Briefing] ⚠️ No se pudo guardar la fecha: {e}")


CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

# Margen extra sobre la latencia de salida antes de volver a confiar en el
# micrófono tras hablar: cubre el decaimiento de la sala y el eco del parlante.
_TAIL_MARGIN = 0.25

# RMS below which 16-bit PCM is treated as room silence; above _LEVEL_FULL it
# reads as a full-height waveform. Tuned so ordinary speech lands mid-range and
# the bars still move for a quiet talker — language- and device-independent.
_LEVEL_FLOOR = 60.0
_LEVEL_FULL  = 2600.0


def _pcm_level(samples) -> float:
    """Map a block of int16 PCM samples to a 0.0–1.0 loudness level for the HUD
    waveform. Returns 0.0 on empty/invalid input so it can never raise."""
    try:
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(x * x)))
    except Exception:
        return 0.0
    if rms <= _LEVEL_FLOOR:
        return 0.0
    return min(1.0, (rms - _LEVEL_FLOOR) / (_LEVEL_FULL - _LEVEL_FLOOR))


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _latency_log(msg: str) -> None:
    """Cronómetro discreto de TTFT: solo consola, sin archivo. Para verificar
    si la respuesta se degrada en sesiones largas. Borrar es seguro."""
    try:
        print(f"[CRONO] {time.monotonic():.3f}  {msg}", flush=True)
    except Exception:
        pass


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are Fox, a personal AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

CORE_TOOL_DECLARATIONS = [
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "manage_monitor",
        "description": (
            "Add, remove, or list background monitoring topics. "
            "FOX checks these topics once a day and alerts the user when there is a new development. "
            "Use 'add' when the user says 'monitor X', 'track X', 'follow X'. "
            "Use 'remove' when the user says 'stop monitoring X'. "
            "Use 'list' when the user asks what is being monitored. "
            "Do NOT add crypto, financial, or trading topics."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type":        "STRING",
                    "description": "add | remove | list",
                },
                "topic": {
                    "type":        "STRING",
                    "description": "Topic to monitor or stop monitoring (e.g. 'space exploration', 'AI news')",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "shutdown_fox",
        "description": (
            "Closes and exits this assistant application completely. "
            "Call ONLY this when the user wants to shut down the ASSISTANT ITSELF "
            "(not the computer). Trigger phrases include, in any language: "
            "'apagate', 'apagá vos', 'cerrate', 'cerrá el asistente', 'terminá la sesión', "
            "'chau, apagate', 'shut yourself down', 'close yourself', 'stop Fox', 'goodbye'. "
            "IMPORTANT: if the user says shut down / restart the COMPUTER, the PC, "
            "the machine or the equipment ('apagá la PC/computadora/equipo/máquina'), "
            "that is computer_settings with action=shutdown/restart — NEVER this tool. "
            "Distinguish the reflexive ('apagate' = yourself) from the hardware command."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Look up a fact you have stored about the user but which is NOT in "
            "the memory block of your system prompt. "
            "The prompt lists the keys it did not have room for under "
            "'[ALSO REMEMBERED]' — if the user asks about anything named there, "
            "call this FIRST. "
            "Also call it before saying you do not know something personal, and "
            "when the user asks what you remember about them (leave query empty "
            "for everything). "
            "This is a local file search: it is instant and costs nothing."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Keyword to search for — a name, a topic, a category "
                        "(e.g. 'ayse', 'coffee', 'projects'). "
                        "Leave empty to list everything stored."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "undo",
        "description": (
            "Reverse the last change YOU made to this computer — a file you "
            "moved, renamed, created or wrote, or a setting you changed such as "
            "volume, brightness, dark mode or WiFi. "
            "Call this whenever the user says undo, revert, take it back, put it "
            "back, cancel that, or tells you that you did the wrong thing, in ANY "
            "language. "
            "Use action='list' when they ask what can be undone. "
            "This only covers your own actions — it is not the Ctrl+Z of whatever "
            "application is on screen (that is computer_settings with action 'undo')."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "undo (default) — reverse the last change | list — show what can be undone",
                },
            },
            "required": [],
        },
    },
]

#  FASE 2: división de herramientas 
# Las herramientas LIVE viajan en la sesión de voz rápida.
# Las SPECIALIST se ejecutan en un agente de fondo cuando Live llama a delegate_task.
LIVE_TOOL_NAMES = {
    "open_app", "computer_settings", "computer_control", "windows_settings",
    "save_memory", "recall_memory", "undo",
    "weather_report", "system_status", "youtube_video", "send_message",
    "reminder", "spotify_control", "shutdown_fox", "web_search",
    # Rápidas y de uso frecuente: el modelo no estaba delegando bien estas,
    # así que vuelven a Live para que las ejecute directo.
    "productivity_inbox", "window_context", "rules_engine", "smart_memory",
    "desktop_memory",
}
SPECIALIST_READ_TOOLS = {"productivity_inbox", "recall_memory"}

DELEGATE_TASK_DECLARATION = {
    "name": "delegate_task",
    "description": (
        "Delegate heavy or complex tasks to a background specialist agent. "
        "Use ONLY for tasks that require: office_builder, image_generator, tuya_control, "
        "browser_control, file_controller, desktop_control, code_helper, dev_agent, "
        "game_updater, flight_finder, manage_monitor, file_processor, project_navigator, "
        "user_profile, amsy_superpowers, telegram_api. "
        "Do NOT delegate simple web searches or quick memory/inbox/context/rules tasks: "
        "those tools are already available to you live. "
        "Give the specialist a complete natural-language task description."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "task_description": {
                "type": "STRING",
                "description": "Exact description of what the user wants to do"
            }
        },
        "required": ["task_description"]
    }
}

_AGENT_SYSTEM_PROMPT = (
    "Eres Fox, un asistente agente con herramientas del sistema (archivos, web, "
    "aplicaciones, memoria, automatización del PC, etc.). "
    "Usá las herramientas cuando haga falta para completar la tarea y, tras ver los "
    "resultados, escribí la respuesta final en el idioma en que te habla el usuario. "
    "Sé conciso y no repitas el trabajo ya hecho."
)

class _ReconnectSignal(Exception):
    """Raised inside the session TaskGroup to force a clean, voluntary reconnect
    (e.g. the user picked a new voice — the voice is fixed at connect time, so
    the session must be rebuilt).

    Carries `keep_context`: True for an ordinary rebuild, where the stored
    resumption handle is replayed and the conversation continues; False when the
    new session must genuinely start clean (see the voice-change note in
    _on_voice_change)."""

    def __init__(self, keep_context: bool = True):
        super().__init__()
        self.keep_context = keep_context


def _is_reconnect_signal(exc: BaseException) -> bool:
    """True if `exc` is a _ReconnectSignal, or a(n) (Base)ExceptionGroup that
    wraps one — TaskGroup bundles child exceptions into a group."""
    if isinstance(exc, _ReconnectSignal):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_reconnect_signal(sub) for sub in exc.exceptions)
    return False


def _keep_context_of(exc: BaseException) -> bool:
    """Read `keep_context` off a reconnect signal, unwrapping the group the
    TaskGroup put it in. Defaults to True: an unexpected shape must not silently
    wipe the conversation."""
    if isinstance(exc, _ReconnectSignal):
        return getattr(exc, "keep_context", True)
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            if _is_reconnect_signal(sub):
                return _keep_context_of(sub)
    return True


class FoxLive:

    def __init__(self, ui: FoxUI):
        self.ui             = ui
        self._asst_name     = "FOX"   # updated each session from config
        self._current_model = ""         # set on each connect/rotation (for 3.x text path)
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._camera_active        = False   # botón 📷 — cámara transmitiendo
        self._screen_active        = False   # botón 🖥️ — pantalla compartida
        self._last_audio_out       = 0.0     # monotonic time of last audio chunk sent to Gemini
        self._vision_source        = None    # "camera" | "screen" | None (streaming en vivo)
        self._vision_stop          = threading.Event()
        self._vision_thread        = None
        self._interrupted          = False   # True while draining audio after user interrupt
        self._echo                 = EchoGuard()  # clasifica "voz del usuario" vs "nuestro eco" (liviano)
        self._out_latency          = 0.20    # segundos, reemplazado con el valor real del dispositivo
        self._tail_until           = 0.0     # monotonic time que expira el eco tras hablar
        self._barge_blocks         = 0       # bloques consecutivos de voz del usuario (barge-in)
        # Pool de hilos DEDICADO para herramientas. El audio usa el pool por
        # defecto (asyncio.to_thread); si las herramientas (web_search, etc.)
        # compartieran ese mismo pool, competirían con la escritura de audio y
        # causarían los microcortes al buscar en internet.
        self._tool_executor        = ThreadPoolExecutor(max_workers=6, thread_name_prefix="fox-tool")
        self._last_barge_interrupt = 0.0     # debounce para el barge-in automático por voz
        try:
            _cfg_bt = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
            self._barge_threshold = float(_cfg_bt.get("barge_in_threshold", 0.20) or 0.20)
            self._barge_threshold = max(0.05, min(0.80, self._barge_threshold))
        except Exception:
            pass
        self._speaker_muted        = False   # FOX: True → se descarta el audio de salida
        self._voice_speed_val      = 1.0     # FOX: velocidad de voz (resampleo client-side)
        self._in_specialist        = False   # True mientras ejecuta herramientas del especialista
        self._last_delegate_task   = None    # última tarea delegada (para evitar bucles)
        self._last_delegate_time   = 0.0
        self._specialist_lock      = None    # asyncio.Lock: una tarea pesada a la vez
        self._specialist_tasks     = set()   # tareas de especialista en segundo plano
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self.ui.on_voice_change   = self._on_voice_change     # voice picker → rebuild session
        self.ui.on_audio_device_change = self._on_audio_device_change
        self.ui.on_config_saved   = self._on_config_saved     # guardar config → reconectar sesión
        self.ui.on_speaker_toggle = self._on_speaker_toggle   # FOX: botón 🔊 de la barra inferior
        self.ui.on_camera_toggle = self._on_camera_toggle     # FOX: botón 📷 de la barra inferior
        self.ui.on_screen_toggle = self._on_screen_toggle     # FOX: botón 🖥️ de la barra inferior

        # ── Modo agente (chat estilo DeepSeek Harness) ────────────────────
        from core.agent import AgentEngine
        from core.permissions import Sandbox
        self._agent = AgentEngine(
            tool_executor=self._agent_tool_executor,
            tools=[],
            system_prompt=_AGENT_SYSTEM_PROMPT,
        )
        self._agent_busy = False
        self._sandbox = Sandbox()
        self.ui.on_agent_submit = self._on_agent_submit
        self.ui.on_agent_clear  = self._on_agent_clear

        self._reconnect_event: asyncio.Event | None = None
        self._reconnect_keep = True   # False → next rebuild drops the resumption handle
        self._recent_reconnects: list[float] = []   # timestamps para el guard anti-churn

        # ── Session resumption ─────────────────────────────────────────
        # The server issues a resumption handle every few seconds and reissues
        # it as the conversation moves on. Before this, session_resumption was
        # switched ON in the config and the update was never read, so the handle
        # was thrown away and EVERY reconnect — a dropped packet, a voice change,
        # switching microphone — started an empty session. "Unlimited sessions"
        # leaked through exactly this hole.
        #
        # Deliberately in RAM only, never written to disk. Persisting it would
        # make a fresh launch continue yesterday's conversation, which sounds
        # appealing but breaks the session-summary flow: _save_session_summary
        # runs at shutdown and the morning briefing pops it the next day. A
        # conversation that never ends never produces a summary, and the
        # "yesterday we talked about…" line silently disappears.
        self._resume_handle: str | None = None
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._telegram_reply   = False          # True cuando esperamos mandar la respuesta a Telegram
        self._telegram_audio_buf = bytearray()  # audio de la respuesta para mandar como voz por Telegram
        self._telegram_text_buf  = []           # textos de los turnos de la respuesta (para la final)
        self._telegram_debounce  = None         # task asyncio de debounce
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._session_log: list[str] = []          # conversation turns for end-of-session summary

        # FOX: por defecto usamos v1beta (menor latencia y más estable).
        # Si querés el modo afectivo/proactivo, poné "enhanced_live": true en config/api_keys.json
        # y se activará v1alpha con affective_dialog + proactive_audio.
        self._enhanced_live = False
        try:
            _cfg_enh = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
            #if _cfg_enh.get("enhanced_live") is True:
                #self._enhanced_live = True
        except Exception:
            pass

        # ── FOX: Google Search nativo (grounding) en la sesión Live ─────────
        # Busca en Google mientras habla (como AI Studio). Se apaga con
        # "grounding_enabled": false en api_keys.json; se auto-desactiva si el
        # servidor lo rechaza o si quedan 2 sesiones mudas seguidas.
        # ── FOX: wake word local (Vosk + prebuffer) ───────────────
        # Fox escucha en local la palabra clave; solo transmite a la nube
        # cuando está ACTIVO. Si Vosk no está disponible, se mantiene
        # el comportamiento anterior (micrófono abierto).
        self.atencion = None
        try:
            _cfg_wk = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            _cfg_wk = {}
        self._wake_enabled = bool(_cfg_wk.get("wake_word_enabled", True))
        self._wake_word = str(_cfg_wk.get("wake_word", "fox") or "fox").strip().lower()
        if self._wake_enabled:
            try:
                _modelo_vosk = _buscar_modelo_vosk()
                _ventana = int(_cfg_wk.get("attention_window_sec", 40))
                self.atencion = AttentionManager(
                    vosk_model_dir=_modelo_vosk,
                    sample_rate=SEND_SAMPLE_RATE,
                    on_estado=self._on_estado_atencion,
                    on_wake=self._on_wake_atencion,
                    ocupado=lambda: self._is_speaking or self._waiting_for_reply or self._phone_active,
                    log=lambda m: print(f"[ATENCION] {m}"),
                    config={
                        "attention_window_sec": _ventana,
                        "attention_voice_grace_sec": float(_cfg_wk.get("attention_voice_grace_sec", 2.5)),
                        "attention_voice_cap_sec": float(_cfg_wk.get("attention_voice_cap_sec", 45)),
                        "attention_vad_threshold": float(_cfg_wk.get("attention_vad_threshold", 0.012)),
                        "continuous_listening": True,
                    },
                    palabras_clave=(self._wake_word,),
                )
            except Exception as e:
                print(f"[ATENCION] Wake word no disponible: {e}")
                self.atencion = None

        self._use_grounding = True
        try:
            _cfg_g = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
            if _cfg_g.get("grounding_enabled") is False:
                self._use_grounding = False
        except Exception:
            pass

        # ── FOX: watchdog de servidor mudo ──────────────────────────────────
        self._waiting_for_reply = False
        self._voice_sent_at = 0.0
        self._no_response_reconnects = 0
        # Mientras un tool se está ejecutando, el servidor no manda audio: el
        # watchdog no debe castigar eso como "servidor mudo".
        self._executing_tool = False
        # Última vez que el servidor mandó ALGO (audio, transcripción, tool call).
        # Un modelo que "piensa" tras una búsqueda puede pasar rato sin audio pero
        # sigue vivo; solo reconectamos si pasa MUCHO tiempo sin NADA.
        self._last_server_event = 0.0
        _core_names = {t["name"] for t in CORE_TOOL_DECLARATIONS}
        self._action_registry = discover_actions(
            actions_dir=Path(__file__).resolve().parent / "actions",
            reserved_names=_core_names,
            logger=lambda msg: (print(f"[Actions] {msg}"), self.ui.write_log(f"SYS: {msg}")),
        )
        _core_names |= self._action_registry.names()
        self._plugin_registry = discover_plugins(
            plugins_dir=Path(__file__).resolve().parent / "plugins",
            core_tool_names=_core_names,
            logger=lambda msg: (print(f"[Plugins] {msg}"), self.ui.write_log(f"SYS: {msg}")),
        )
        _action_decls = self._action_registry.get_tool_declarations()
        self._live_tool_declarations = (
            [t for t in CORE_TOOL_DECLARATIONS if t["name"] in LIVE_TOOL_NAMES]
            + [t for t in _action_decls if t["name"] in LIVE_TOOL_NAMES]
        )
        self._specialist_tool_declarations = (
            [t for t in CORE_TOOL_DECLARATIONS
             if t["name"] not in LIVE_TOOL_NAMES or t["name"] in SPECIALIST_READ_TOOLS]
            + [t for t in _action_decls
               if t["name"] not in LIVE_TOOL_NAMES or t["name"] in SPECIALIST_READ_TOOLS]
        )
        self._agent_tools = CORE_TOOL_DECLARATIONS + _action_decls
        self._agent.set_tools(self._agent_tools)
        self.ui.get_plugins = self._plugin_registry.list_for_ui
        self.ui.request_say = self.plugin_say   # plugins: mid-task speech channel
        self.ui.on_add_skill = self.add_plugin_from_ui   # pestaña Skills

    def plugin_say(self, instruction: str) -> None:
        """
        Thread-safe speech channel for plugins: lets a plugin ask FOX to
        say something short WHILE its run() is still executing (plugins block
        their executor thread, so they can't speak through the tool response
        until they finish). The instruction is injected into the Live session
        exactly like a proactive check-in; Gemini phrases it naturally in the
        user's language. Silently a no-op when no session is connected.
        """
        loop = getattr(self, "_loop", None)
        if not loop or not self.session:
            return

        async def _say():
            try:
                await self._send_text(instruction)
            except Exception as e:
                print(f"[PluginSay] {e}")

        try:
            asyncio.run_coroutine_threadsafe(_say(), loop)
        except Exception as e:
            print(f"[PluginSay] {e}")

    def add_plugin_from_ui(self, filename: str, code: str) -> tuple[bool, str]:
        """Pestaña Skills: escribe un plugin en plugins/, re-descubre y reconecta
        para que la nueva skill quede disponible sin reiniciar Fox. Se llama desde
        el hilo Qt (click del botón), así que solo usa `request_reconnect` (thread-safe)."""
        plugins_dir = Path(__file__).resolve().parent / "plugins"
        nombre = (filename or "").strip()
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$", nombre):
            return False, "Nombre inválido: usá letras/números/guiones bajos, sin espacios."
        if not (code or "").strip():
            return False, "El código está vacío."

        try:
            (plugins_dir / f"{nombre}.py").write_text(code, encoding="utf-8")
        except Exception as e:
            return False, f"No se pudo guardar {nombre}.py: {e}"

        # Re-descubrir: importa el archivo nuevo y revalida los existentes.
        try:
            _core_names = ({t["name"] for t in CORE_TOOL_DECLARATIONS}
                           | self._action_registry.names())
            self._plugin_registry = discover_plugins(
                plugins_dir=plugins_dir,
                core_tool_names=_core_names,
                logger=lambda msg: (print(f"[Plugins] {msg}"), self.ui.write_log(f"SYS: {msg}")),
            )
            self.ui.get_plugins = self._plugin_registry.list_for_ui
        except Exception as e:
            return False, f"Guardado, pero falló la recarga: {e}"

        # Reconectar para que la nueva skill entre en la sesión Live.
        self.request_reconnect(keep_context=True, reason="nueva skill")
        return True, f"Skill '{nombre}' guardada y cargada. Reconectando sesión..."

    def _on_estado_atencion(self, estado: str, motivo: str):
        print(f"[ATENCION] {motivo} -> {estado}")
        if estado == "ACTIVE" and not self.ui.muted:
            self.ui.set_state("LISTENING")
        elif estado in ("ARMED", "ASLEEP"):
            # Sin transmisión a la nube: el watchdog no debe esperar respuesta.
            self._waiting_for_reply = False
            self.ui.set_state("SLEEPING")

    def _on_wake_atencion(self, desde_reposo: bool):
        self.ui.write_log("SYS: Wake word detectada — escuchando...")

    async def _keepalive(self):
        """Manda un silencio mínimo cada ~30s mientras NO hay conversación activa,
        para que la sesión Live no se duerma (idle timeout) en modo espera/reposo."""
        while True:
            await asyncio.sleep(30)
            try:
                if self.session is None:
                    continue
                _at = getattr(self, "atencion", None)
                if _at is not None and _at.wake_disponible and not _at.activo:
                    _silence = b"\x00\x00" * (SEND_SAMPLE_RATE // 10)  # 100 ms de silencio
                    await self.session.send_realtime_input(
                        audio=types.Blob(data=_silence, mime_type=f"audio/pcm;rate={SEND_SAMPLE_RATE}")
                    )
            except Exception:
                pass

    async def _watch_attention_config(self):
        """Aplica en caliente la ventana de atención y el umbral de barge-in."""
        while True:
            await asyncio.sleep(5)
            try:
                _cfg = json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
                # Hot-reload del umbral de interrupción por voz (slider).
                try:
                    _bt = float(_cfg.get("barge_in_threshold", 0.20) or 0.20)
                    self._barge_threshold = max(0.05, min(0.80, _bt))
                except Exception:
                    pass
                if self.atencion is not None:
                    self.atencion.aplicar_config({
                        "attention_window_sec": int(_cfg.get("attention_window_sec", 40)),
                        "attention_voice_grace_sec": float(_cfg.get("attention_voice_grace_sec", 2.5)),
                        "attention_voice_cap_sec": float(_cfg.get("attention_voice_cap_sec", 45)),
                        "attention_vad_threshold": float(_cfg.get("attention_vad_threshold", 0.012)),
                        "continuous_listening": bool(_cfg.get("wake_word_enabled", True)),
                    })
            except Exception:
                pass

    def request_reconnect(self, keep_context: bool = True, reason: str = ""):
        """Thread-safe: ask the run loop to tear down and rebuild the Live
        session. Called from the Qt thread. No-op until the async loop and
        reconnect event exist.

        `keep_context=False` drops the resumption handle so the new session
        starts empty — only for changes the server cannot apply to a resumed
        session."""
        loop = getattr(self, "_loop", None)
        ev   = self._reconnect_event
        self._reconnect_keep   = keep_context
        self._reconnect_reason = reason
        if loop and ev is not None:
            loop.call_soon_threadsafe(ev.set)

    def _on_voice_change(self):
        """Voice picker applied.

        The voice is baked into the session at connect time, so a rebuild is
        required. It is rebuilt WITHOUT the resumption handle on purpose:
        resuming restores the server's own session state, and the safe reading
        is that it restores the voice with it — which would make the picker
        appear to do nothing. Losing context here is acceptable because changing
        voice is a deliberate, rare act; losing it on a dropped packet was not."""
        self.request_reconnect(keep_context=False, reason="new voice")

    def _on_audio_device_change(self):
        """Microphone or speaker changed. Both streams are opened inside the
        session TaskGroup, so they can only be re-opened by rebuilding it —
        but the conversation is kept, which is the whole reason resumption
        landed before this feature did."""
        self.request_reconnect(keep_context=True, reason="audio device")

    def _on_config_saved(self):
        """Configuración general guardada (temperatura, sarcasmo, silencio,
        wake word, modelo, barge-in…). Se reconecta la sesión Live manteniendo
        el contexto para que los cambios carguen de inmediato."""
        self.request_reconnect(keep_context=True, reason="config saved")

    def _on_speaker_toggle(self, active: bool):
        """FOX: botón 🔊 de la barra inferior — silencia solo la salida de voz."""
        self._speaker_muted = not active
        self.ui.write_log(f"SYS: Altavoz {'activado' if active else 'silenciado'}.")

    # ── Vision en vivo (botones 📷 / 🖥️) ───────────────────────────────────────
    # La cámara/pantalla se muestra SIEMPRE en el HUD, pero los frames solo se
    # mandan a Gemini mientras el usuario está hablando (viajan junto con la voz),
    # replicando el comportamiento de Google AI Studio Live sin gastar de más.

    def _on_camera_toggle(self, active: bool):
        if active:
            self._screen_active = False
            self._camera_active = True
            self._start_vision("camera")
        else:
            self._camera_active = False
            if self._vision_source == "camera":
                self._stop_vision()

    def _on_screen_toggle(self, active: bool):
        if active:
            self._camera_active = False
            self._screen_active = True
            self._start_vision("screen")
        else:
            self._screen_active = False
            if self._vision_source == "screen":
                self._stop_vision()

    def _start_vision(self, source: str):
        if self._vision_source == source:
            return
        self._stop_vision()
        self._vision_source = source
        self._vision_stop.clear()
        self._vision_thread = threading.Thread(
            target=self._vision_capture_loop, args=(source,),
            daemon=True, name=f"vision-{source}",
        )
        self._vision_thread.start()
        self.ui.set_live_view(True)
        self.ui.write_log(f"SYS: {'Cámara' if source == 'camera' else 'Pantalla'} en vivo.")

    def _stop_vision(self):
        self._vision_stop.set()
        self._vision_source = None
        self._vision_thread = None
        self.ui.set_live_view(False)

    def _vision_capture_loop(self, source: str):
        holder = None
        try:
            holder = _LiveCapture(source)
        except Exception as e:
            print(f"[Vision] ⚠️ No se pudo iniciar {source}: {e}")
            self.ui.write_log(f"SYS: No se pudo iniciar {source}.")
            self._camera_active = self._screen_active = False
            self.ui.set_live_view(False)
            return
        # fps del preview (HUD, local y gratis): cámara ~15, pantalla ~10.
        _interval = 0.066 if source == "camera" else 0.1
        # Envío a Gemini: pocos fps para no fundir la cuota (pantalla pesa más).
        _send_interval = 0.4 if source == "camera" else 1.0
        _last_sent = 0.0
        try:
            while not self._vision_stop.is_set() and self._vision_source == source:
                data = holder.grab()
                if data:
                    # 1) Siempre en el HUD (local, sin costo, fluido).
                    self.ui.show_live_frame(data)
                    # 2) A Gemini SOLO durante la voz reciente, throttleado.
                    _now = time.monotonic()
                    if _now - self._last_audio_out < 0.6 and _now - _last_sent > _send_interval:
                        _loop = getattr(self, "_loop", None)
                        if _loop is not None and self.session is not None:
                            try:
                                asyncio.run_coroutine_threadsafe(
                                    self.session.send_realtime_input(
                                        video=types.Blob(data=data, mime_type="image/jpeg")
                                    ),
                                    _loop,
                                )
                                _last_sent = _now
                            except Exception:
                                pass
                self._vision_stop.wait(_interval)
        except Exception as e:
            print(f"[Vision] ❌ stream error: {e}")
        finally:
            try:
                holder.close()
            except Exception:
                pass
            if self._vision_source == source:
                self._vision_source = None
                self.ui.set_live_view(False)

    # ── Telegram (reemplaza el control remoto web) ────────────────────────────
    def _send_telegram_voice(self, text: str, audio_bytes: bytes):
        """Envía la respuesta a Telegram: voz si hay audio, si no, texto."""
        from actions.telegram_api import (
            get_telegram_config, send_message, send_voice, pcm_to_ogg,
        )
        token, chat = get_telegram_config()
        if not token or not chat:
            return
        try:
            if audio_bytes:
                ogg = pcm_to_ogg(audio_bytes, RECEIVE_SAMPLE_RATE)
                if ogg and send_voice(token, chat, ogg):
                    return
            if text:
                send_message(token, chat, text)
        except Exception as e:
            print(f"[Telegram] ⚠️ No pude responder con voz: {e}")

    def _telegram_new_command(self):
        self._telegram_reply = True
        self._telegram_audio_buf.clear()
        self._telegram_text_buf.clear()
        if self._telegram_debounce:
            self._telegram_debounce.cancel()
            self._telegram_debounce = None

    async def _telegram_flush(self):
        """Debounce: manda la respuesta completa tras 5s sin nuevos turnos."""
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            return
        self._telegram_reply = False
        text = " ".join(self._telegram_text_buf).strip()
        audio = bytes(self._telegram_audio_buf)
        self._telegram_text_buf.clear()
        self._telegram_audio_buf.clear()
        if text or audio:
            await asyncio.to_thread(self._send_telegram_voice, text, audio)

    async def _telegram_listener(self):
        """Poll de getUpdates: reenvía los mensajes (texto o voz) a Fox."""
        from actions.telegram_api import (
            get_telegram_config, poll_telegram, download_file, ogg_to_pcm16k,
        )
        token, chat = get_telegram_config()
        if not token or not chat:
            print("[Telegram] ⚠️ No configurado (faltan telegram_token / telegram_chat_id).")
            return
        offset = 0
        print("[Telegram] 👂 Escuchando mensajes...")
        while True:
            try:
                msgs, offset = await asyncio.to_thread(poll_telegram, token, chat, offset)
                for item in msgs:
                    if item.get("type") == "voice":
                        self.ui.write_log("[Telegram]: (mensaje de voz)")
                        ogg = await asyncio.to_thread(download_file, token, item.get("file_id", ""))
                        pcm = await asyncio.to_thread(ogg_to_pcm16k, ogg) if ogg else None
                        if pcm and self.session:
                            self._telegram_new_command()
                            _CH = 3200  # ~200 ms @ 16 kHz
                            for _i in range(0, len(pcm), _CH):
                                await self.session.send_realtime_input(
                                    audio=types.Blob(
                                        data=pcm[_i:_i + _CH],
                                        mime_type=f"audio/pcm;rate={SEND_SAMPLE_RATE}",
                                    )
                                )
                    else:
                        text = (item.get("text") or "").strip()
                        if not text:
                            continue
                        self.ui.write_log(f"[Telegram]: {text}")
                        self._telegram_new_command()
                        await self._send_text(text)
            except Exception as e:
                print(f"[Telegram] ⚠️ {e}")
            await asyncio.sleep(2.5)

    async def _watch_reconnect(self):
        """Session-scoped task: when a voluntary reconnect is requested, raise a
        signal that unwinds the TaskGroup so the run loop rebuilds the session."""
        assert self._reconnect_event is not None
        await self._reconnect_event.wait()
        self._reconnect_event.clear()
        keep   = self._reconnect_keep
        reason = getattr(self, "_reconnect_reason", "") or "settings"
        self.ui.write_log(
            f"SYS: Applying {reason} — reconnecting"
            + ("..." if keep else " (starting a fresh conversation)...")
        )
        raise _ReconnectSignal(keep_context=keep)

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return
        if self.atencion is not None:
            self.atencion.activar("texto")
        # FOX: marcar espera de respuesta — el watchdog detecta servidores mudos
        self._waiting_for_reply = True
        self._voice_sent_at = time.monotonic()
        self._last_server_event = time.monotonic()
        self._send_text_threadsafe(text)

    def _tail_active(self) -> bool:
        """True mientras el parlante todavía puede estar terminando nuestra frase."""
        return time.monotonic() < self._tail_until

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self._tail_until = 0.0
            self.ui.set_state("SPEAKING")
        else:
            # Mantener el guard de eco abierto durante la latencia del dispositivo
            # + margen. El mic NO se silencia: el guard deja pasar una respuesta
            # real, solo descarta nuestro propio eco.
            self._tail_until = time.monotonic() + self._out_latency + _TAIL_MARGIN
            if not self.ui.muted:
                self.ui.set_state("LISTENING")

    def interrupt(self) -> None:
        """Stop FOX mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        self._echo.reset()
        if self.atencion is not None:
            self.atencion.activar("interrupcion")
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[FOX] ✋ Interrupted — {drained} audio chunks discarded")
            else:
                print("[FOX] ✋ Interrupted — (buffer ya vacío)")
        else:
            print("[FOX] ✋ Interrupted — (cola nula)")
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    async def _send_text(self, text: str) -> None:
        """Envía un turno de texto al modelo, eligiendo el método que acepta.

        Gemini 3.x "live" (A2A) rechaza `send_client_content` con texto a mitad de
        conversación (1007) y exige `send_realtime_input`; Gemini 2.x acepta ambos
        y mantiene el camino turn-based por su garantía de orden."""
        if not self.session or not text:
            return
        if _model_needs_realtime_text(self._current_model):
            await self.session.send_realtime_input(text=text)
        else:
            await self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True,
            )

    def _send_text_threadsafe(self, text: str) -> None:
        """Versión thread-safe de `_send_text` para llamadas desde el hilo Qt."""
        loop = getattr(self, "_loop", None)
        if not loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(self._send_text(text), loop)

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        self._send_text_threadsafe(text)

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    # ── Modo agente (chat) ───────────────────────────────────────────────────
    async def _agent_tool_executor(self, name: str, args: dict) -> str:
        """Ejecuta una herramienta desde el motor agente, respetando el sandbox."""
        ok, reason = self._sandbox.check(name, args)
        if not ok:
            return f"[BLOQUEADO] {reason}"

        class _FC:
            pass
        fc = _FC()
        fc.name = name
        fc.args = args or {}
        fc.id = name

        self._in_specialist = True
        try:
            fr = await self._execute_tool(fc)
        finally:
            self._in_specialist = False

        if fr is None:
            return ""
        if getattr(fr, "response", None):
            return str(fr.response.get("result", fr.response))
        return str(fr)

    def _on_agent_submit(self, request: dict) -> None:
        """Recibe un envío del panel de chat (hilo Qt) y lo agenda en el loop."""
        loop = getattr(self, "_loop", None)
        if not loop:
            return
        asyncio.run_coroutine_threadsafe(self._run_agent(request), loop)

    def _on_agent_clear(self) -> None:
        """Limpia el historial del agente (hilo Qt → loop asyncio)."""
        loop = getattr(self, "_loop", None)
        if not loop:
            return

        async def _clear():
            self._agent.clear_history()

        asyncio.run_coroutine_threadsafe(_clear(), loop)

    async def _run_agent(self, request: dict) -> None:
        """Corre el loop del agente para un turno del usuario (modo chat)."""
        if self._agent_busy:
            self.ui.emit_agent_event({
                "type": "error",
                "error": "Ya hay una tarea en curso. Esperá a que termine.",
            })
            return
        self._agent_busy = True
        try:
            text = (request.get("text") or "").strip()
            if not text:
                return
            provider = request.get("provider") or None
            model = request.get("model") or None
            effort = request.get("effort") or "off"
            self._sandbox.set_mode(request.get("sandbox") or "full")
            self._sandbox.set_roots(request.get("folders") or [])
            async for ev in self._agent.run(text, provider=provider, model=model, effort=effort):
                self.ui.emit_agent_event(ev)
        except Exception as e:
            self.ui.emit_agent_event({"type": "error", "error": str(e)})
        finally:
            self._agent_busy = False

    async def _start_agent_web_server(self):
        """Arranca el servidor web del Modo Agente (DeepSeek Harness) en segundo plano.

        Reutiliza el AgentEngine y el Sandbox ya creados, sin duplicar código.
        La UI web se abre en el navegador (no embebida en Qt).
        """
        from agent_web.server import AgentWebServer, run_server
        from core.agent import AgentEngine

        async def _web_tool_executor(name: str, args: dict) -> str:
            ok, reason = self._sandbox.check(name, args)
            if not ok:
                return f"[BLOQUEADO] {reason}"
            
            loop = asyncio.get_event_loop()
            _ctx = {"ui": None, "response": None, "session_memory": None}
            try:
                r = await loop.run_in_executor(self._tool_executor, lambda: self._action_registry.run(name, args, _ctx))
                return str(r.response.get("result", "")) if hasattr(r, "response") and isinstance(r.response, dict) else str(r)
            except Exception as e:
                return str(e)

        try:
            web_agent = AgentEngine(
                tool_executor=_web_tool_executor,
                tools=self._agent.tools,
                system_prompt=self._agent.system_prompt
            )
            server = AgentWebServer(
                agent_engine=web_agent,
                sandbox=self._sandbox,
                lock=asyncio.Lock(),
            )
            await run_server(server, host="127.0.0.1", port=8765)
        except OSError as e:
            print(f"[AgentWeb] No se pudo arrancar (¿puerto 8765 ocupado?): {e}")
        except Exception as e:
            print(f"[AgentWeb] Error inesperado: {e}")

    def _build_tools(self):
        """Herramientas de función + Google Search nativo (grounding) opcional.
        El grounding hace que el modelo busque en Google EN SEGUNDO PLANO
        mientras genera la respuesta de voz, sin pausas por herramientas."""
        tools = [
            {
                "function_declarations": self._live_tool_declarations
                + [DELEGATE_TASK_DECLARATION]
                + self._plugin_registry.get_tool_declarations()
            }
        ]
        if getattr(self, "_use_grounding", True):
            tools.append({"google_search": {}})
        return tools

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        try:
            _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "FOX").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "FOX"
            _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Identity injection — overrides any hardcoded name in prompt.txt
        _addr = (f"ADDRESS: Always call the user '{_user_name}'."
                 if _user_name
                 else "ADDRESS: Address the user with the ordinary respectful form "
                      "for a superior in the language you are currently speaking — "
                      "\"sir\" in English, its everyday equivalent in any other "
                      "language. Never an archaic or aristocratic form, and never "
                      "the form from a different language than the one you are "
                      "speaking in this sentence.")
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n\n"
        )

        parts = [time_ctx, identity_ctx]
        if mem_str:
            parts.append(mem_str)
        parts.append(sys_prompt)

        # ── FOX/AMSY: automatizaciones activas en el prompt ─────────────────
        _rules_str = format_rules_for_prompt()
        if _rules_str:
            parts.append(_rules_str)

        # ── FOX: sarcasmo desde config ──────────────────────────────────────
        try:
            _sarc = int(_cfg.get("sarcasm_level", 5))
        except Exception:
            _sarc = 5
        if _sarc <= 3:
            _tono = "formal y serio"
        elif _sarc <= 7:
            _tono = "casual y amigable"
        else:
            _tono = "sarcástico y con humor"
        parts.append(f"[PERSONALIDAD] Nivel de sarcasmo: {_sarc}/10. Tu tono debe ser {_tono}.")

        if getattr(self, "_wake_enabled", False) and getattr(self, "atencion", None) is not None and self.atencion.wake_disponible:
            parts.append(
                f"[ATENCION - MODO ESPERA]\n"
                f"Tu palabra clave es \"{self._wake_word}\". Cuando la escuches sola, no contestes; "
                f"espera la orden del usuario. Si el usuario deja de hablarte, la ventana de atencion "
                f"expira sola y volves a modo espera sin avisar."
            )

        # ── FOX: velocidad de voz desde config ──────────────────────────────
        # Este SDK no soporta speaking_rate en SpeechConfig, así que la
        # velocidad se aplica del lado del cliente (resampleo en _play_audio).
        try:
            _vsp_raw = int(_cfg.get("voice_speed", 10) or 10)
        except Exception:
            _vsp_raw = 10
        self._voice_speed_val = max(0.5, min(2.0, _vsp_raw / 10.0))

        # ── FOX/AMSY: token-saver mode ───────────────────────────────────────
        # Prompt más corto → primer token más rápido. La memoria completa queda
        # en disco y se recupera con recall_memory (local, instantáneo).
        try:
            set_token_saver(bool(_cfg.get("token_saver_mode", True)))
        except Exception:
            set_token_saver(True)

        cfg = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=self._build_tools(),
            # Hand back the handle captured from the last session_resumption
            # update. `handle=None` is exactly the old behaviour (ask for
            # handles, start fresh), so the first connect of a run is unchanged.
            session_resumption=types.SessionResumptionConfig(
                handle=self._resume_handle
            ),
            # Sliding-window compression: session never dies from a full context
            # window — FOX can stay in one conversation for hours
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=get_voice()
                    )
                )
            ),
        )

        # Thinking mínimo → primer audio más rápido (solo Gemini 3.x; el 2.5 no responde con esto).
        if _model_needs_realtime_text(self._current_model):
            cfg["thinking_config"] = types.ThinkingConfig(
                include_thoughts=False,
                thinking_level="MINIMAL",
            )

        # ── FOX/AMSY: velocidad desde config ────────────────────────────────
        # temperature=0.2 → respuesta más estable/determinista (menos divagación).
        try:
            _temp = float(_cfg.get("ia_temperature", 0.2) or 0.2)
        except Exception:
            _temp = 0.2
        cfg["temperature"] = max(0.0, min(2.0, _temp))

        # ── Barge-in REAL ────────────────────────────────────────────────────
        # barge_in_sensitivity (0.0-1.0) controla cuán fácil es INTERRUMPIR a Fox
        # mientras habla → mapea a start_of_speech_sensitivity (LOW/MEDIUM/HIGH).
        try:
            _barge = float(_cfg.get("barge_in_sensitivity", 0.69) or 0.69)
        except Exception:
            _barge = 0.69
        _barge = max(0.0, min(1.0, _barge))
        # El API solo acepta LOW/HIGH (no existe MEDIUM): el SDK valida el valor
        # y rechaza la conexión con error 1007 si le pasás uno inválido.
        if _barge >= 0.5:
            _start_sens = "START_SENSITIVITY_HIGH"
        else:
            _start_sens = "START_SENSITIVITY_LOW"

        # ── Silencio para responder ──────────────────────────────────────────
        # response_silence_ms (250-900) = cuánto silencio espera Fox tras tu frase
        # antes de empezar a responder (menos = responde antes).
        try:
            _silence_ms = int(float(_cfg.get("response_silence_ms", 500) or 500))
        except Exception:
            _silence_ms = 500
        _silence_ms = max(250, min(900, _silence_ms))

        #  FOX: VAD rápido (probado en FOX-HRZ)
        # prefix_padding_ms=100 conserva el comienzo de la frase.
        try:
            cfg["realtime_input_config"] = types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=_start_sens,
                    end_of_speech_sensitivity="END_SENSITIVITY_HIGH",
                    prefix_padding_ms=100,
                    silence_duration_ms=_silence_ms,
                )
            )
            print(f"[FOX] VAD aplicado (typed) — barge-in={_start_sens}, silence={_silence_ms}ms")
        except Exception:
            try:
                cfg["realtime_input_config"] = {
                    "automatic_activity_detection": {
                        "start_of_speech_sensitivity": _start_sens,
                        "end_of_speech_sensitivity": "END_SENSITIVITY_HIGH",
                        "prefix_padding_ms": 100,
                        "silence_duration_ms": _silence_ms,
                    }
                }
                print(f"[FOX] VAD aplicado (dict) — barge-in={_start_sens}, silence={_silence_ms}ms")
            except Exception:
                print("[FOX] VAD config no aplicado")
        if self._enhanced_live:
            # Affective dialog: FOX hears tone/emotion and adapts its voice.
            # Proactive audio: FOX stays silent when speech isn't addressed
            # to it (background chatter, talking to someone else in the room).
            cfg["enable_affective_dialog"] = True
            cfg["proactivity"] = types.ProactivityConfig(proactive_audio=True)
        return types.LiveConnectConfig(**cfg)

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[FOX] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."
        # En modo especialista, las herramientas no deben hablar por su cuenta:
        # devuelven texto y el especialista decide el mensaje final.
        _tool_speak = None if getattr(self, "_in_specialist", False) else self.speak

        try:
            if name == "recall_memory":
                # Local file search: no network, no second model. Kept out of
                # the executor deliberately — it is a dictionary scan over a few
                # hundred short strings, and a thread hop would cost more than
                # the work itself.
                result = search_memory(args.get("query", ""), limit=8)

            elif name == "undo":
                if str(args.get("action", "")).lower().strip() == "list":
                    items = undo_stack.history()
                    result = ("Things I can undo, most recent first:\n"
                              + "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
                              ) if items else "I have not changed anything I can undo yet."
                else:
                    result = await loop.run_in_executor(self._tool_executor, undo_stack.undo_last)

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if getattr(self, "_vision_source", None) is not None:
                    print("[Vision] ⏳ Ya hay transmisión en vivo activa — ignorando screen_process")
                    result = "Ya hay una transmisión en vivo activa. No necesito capturar de nuevo."
                elif getattr(self, "_vision_cam_active", False):
                    print("[Vision] Camera already active  ignoring duplicate camera request")
                    result = "La cámara ya está activa. Decime 'cerrar cámara' si querés apagarla."
                elif self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        try:
                            frames, mime_t = await loop.run_in_executor(self._tool_executor, _capture_camera_burst)
                        except Exception as _cam_err:
                            self._vision_busy = False
                            print(f"[Vision] ⚠️ Camera unavailable: {_cam_err}")
                            if not self.ui.muted:
                                self.ui.set_state("LISTENING")
                            result = (
                                "No usable webcam was found. Tell the user, in their "
                                "own language, that no camera is connected and suggest "
                                "using the screen instead."
                            )
                            return types.FunctionResponse(
                                id=fc.id, name=name,
                                response={"result": result}
                            )
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(frames)} frames ({sum(len(f) for f in frames):,} bytes)")
                        _stall = "camera"
                    else:
                        img_b, mime_t = await loop.run_in_executor(self._tool_executor, _capture_screen)
                        print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        frames = [img_b]
                        _stall = "screen"
                    self._pending_vision = (frames, mime_t, user_text, angle)
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured. "
                        f"Immediately say ONE short natural sentence in the user's own language, "
                        f"telling them you are looking at their {_stall} right now. "
                        f"Do NOT describe or guess content — the actual image arrives in the NEXT message."
                    )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "system_status":
                r = await loop.run_in_executor(self._tool_executor, get_system_status)
                result = str(r)

            elif name == "manage_monitor":
                action = args.get("action", "").lower().strip()
                topic  = args.get("topic", "").strip()
                if action == "add" and topic:
                    result = await asyncio.to_thread(add_monitor, topic)
                elif action == "remove" and topic:
                    result = await asyncio.to_thread(remove_monitor, topic)
                elif action == "list":
                    topics = await asyncio.to_thread(list_monitors)
                    result = ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
                else:
                    result = "Specify action (add/remove/list) and a topic."

            elif name == "shutdown_fox":
                self.ui.write_log("SYS: Shutdown requested.")
                async def _do_shutdown():
                    await self._save_session_summary()
                    if self.session:
                        try:
                            await self._send_text("Say a brief natural goodbye to the user.")
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
                    import os as _os
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())

            elif name == "delegate_task":
                _tarea = str(args.get("task_description", "") or "").strip()
                if not _tarea:
                    result = "No task description provided."
                else:
                    _ahora = time.monotonic()
                    if (_tarea == self._last_delegate_task
                            and (_ahora - self._last_delegate_time) < 20.0):
                        print("[Specialist] Tarea duplicada detectada  se ignora para evitar bucle.")
                        result = "Ya estoy en eso; te aviso cuando termine."
                    elif getattr(self, "_specialist_lock", None) is not None and self._specialist_lock.locked():
                        print("[Specialist] Ya hay una tarea pesada en curso  se ignora.")
                        result = "Ya estoy con una tarea pesada; te aviso al terminar."
                    else:
                        self._last_delegate_task = _tarea
                        self._last_delegate_time = _ahora
                        print(f"[Specialist]  {_tarea}")
                        # Camino corto: si termina en menos de ~8 s, el
                        # resultado vuelve como respuesta de esta herramienta.
                        _task = asyncio.create_task(self._specialist_worker(_tarea))
                        self._specialist_tasks.add(_task)
                        try:
                            result = await asyncio.wait_for(asyncio.shield(_task), timeout=8.0)
                        except asyncio.TimeoutError:
                            # Camino largo: se sigue procesando en segundo
                            # plano y Fox avisa por voz cuando termine.
                            asyncio.create_task(self._speak_when_specialist_done(_task))
                            result = "Dale, lo estoy procesando en segundo plano; te aviso cuando termine."
                        except Exception as e:
                            result = f"La tarea pesada falló: {e}"
                        # FOX: resultados largos del especialista también al panel
                        if len(result) > 80:
                            self.ui.show_content("Resultado", result)

            elif self._action_registry.has(name):
                # file_processor: cae al archivo subido actual si no se indica uno
                if name == "file_processor" and not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                _ctx = {
                    "player": self.ui,
                    "speak": _tool_speak,
                    "response": None,
                    "session_memory": None,
                }
                r = await loop.run_in_executor(self._tool_executor, lambda: self._action_registry.run(name, args, _ctx))
                result = r or "Done."
                # web_search: reflejar resultados en el panel de contenido
                if (name == "web_search" and r
                        and not r.startswith("No results")
                        and not r.startswith("Search failed")):
                    _mode  = args.get("mode", "search")
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)

            else:
                if self._plugin_registry.has(name):
                    r = await loop.run_in_executor(
                        self._tool_executor,
                        lambda: self._plugin_registry.run(name, args, player=self.ui, session_memory=None)
                    )
                    result = r or "Done."
                else:
                    result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            if getattr(self, "_in_specialist", False):
                print(f"[Specialist] Tool '{name}' failed: {e}")
            else:
                self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[FOX] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    async def _specialist_worker(self, task_description: str) -> str:
        """Agente de fondo con el set SPECIALIST de herramientas.

        Usa el proveedor configurado en la interfaz (Gemini, OpenRouter,
        DeepSeek, Groq, etc.) y un loop manual de hasta 5 pasos.
        Devuelve el texto final que Fox leerá en voz alta.
        """
        from core import specialist_llm

        if self._specialist_lock is None:
            self._specialist_lock = asyncio.Lock()

        system_prompt = (
            "You are Fox's background specialist. You have tools for heavy tasks. "
            "Use the tools to complete the user's request. After tool results, write the final answer "
            "in the same language the user speaks."
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task_description},
        ]

        async with self._specialist_lock:
            try:
                async with asyncio.timeout(120):
                    called_tools: set[str] = set()
                    for _step in range(5):
                        resp = await asyncio.to_thread(
                            specialist_llm.call_specialist_llm,
                            messages=messages,
                            tools=self._specialist_tool_declarations,
                        )
                        content = (resp.get("content") or "").strip()
                        calls = resp.get("tool_calls") or []

                        if not calls:
                            return content or "Listo."

                        # Ejecutar herramientas solicitadas
                        fn_responses = []
                        for call in calls:
                            fn_name = call.get("function", {}).get("name", "")
                            fn_args = call.get("function", {}).get("arguments", {})
                            fc_id = call.get("id", fn_name)

                            # Evitar repetir la misma herramienta en un mismo task
                            # (el modelo tiende a re-llamar image_generator, etc.)
                            if fn_name in called_tools:
                                fn_responses.append(types.FunctionResponse(
                                    id=fc_id, name=fn_name,
                                    response={"result": f"{fn_name} ya fue ejecutada antes en esta tarea."},
                                ))
                                continue
                            called_tools.add(fn_name)

                            class _FakeFC:
                                name = fn_name
                                args = fn_args
                                id = fc_id

                            try:
                                fr = await self._execute_tool(_FakeFC())
                                fn_responses.append(fr)
                            except Exception as e:
                                fn_responses.append(types.FunctionResponse(
                                    id=fc_id,
                                    name=fn_name,
                                    response={"error": str(e)},
                                ))

                        # Agregar al historial
                        assistant_msg = {"role": "assistant", "content": content or ""}
                        if calls:
                            # Incluir tool_calls para que el proveedor sepa qué se ejecutó
                            assistant_msg["tool_calls"] = [
                                {
                                    "id": c.get("id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": c.get("function", {}).get("name", ""),
                                        "arguments": json.dumps(c.get("function", {}).get("arguments", {}), ensure_ascii=False),
                                    },
                                }
                                for c in calls
                            ]
                        messages.append(assistant_msg)

                        for fr in fn_responses:
                            resultado = fr.response.get("result", str(fr.response)) if fr.response else ""
                            messages.append({
                                "role": "tool",
                                "content": str(resultado),
                                "tool_call_id": fr.id,
                                "name": fr.name,
                            })

                return "Llegué al límite de pasos del especialista."
            except asyncio.TimeoutError:
                return "La tarea tardó demasiado (más de 120 segundos)."
            except Exception as e:
                return f"La tarea pesada falló: {e}"
            finally:
                self._in_specialist = False

    async def _speak_when_specialist_done(self, task):
        """Avisa por voz cuando termina una tarea pesada en segundo plano."""
        try:
            final = await task
        except Exception as e:
            final = f"La tarea pesada falló: {e}"
        finally:
            self._specialist_tasks.discard(task)
        if final:
            if len(final) > 80:
                self.ui.show_content("Resultado", final)
            self.speak(final)

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            # Agrupa todos los chunks PCM disponibles en un solo envío
            # (hasta ~200 ms) para reducir llamadas a la API y carga en asyncio.
            data = bytearray(msg.get("data", b""))
            mime_type = msg.get("mime_type", "audio/pcm")
            try:
                while len(data) < 6400:
                    extra = self.out_queue.get_nowait()
                    if extra.get("mime_type", "audio/pcm") == mime_type:
                        data.extend(extra.get("data", b""))
            except asyncio.QueueEmpty:
                pass
            await self.session.send_realtime_input(
            audio=types.Blob(data=bytes(data), mime_type=mime_type)
           )
            self._last_audio_out = time.monotonic()

    async def _listen_audio(self):
        print("[FOX] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def _encolar(data: bytes):
            loop.call_soon_threadsafe(
                self.out_queue.put_nowait,
                {"data": data, "mime_type": f"audio/pcm;rate={SEND_SAMPLE_RATE}"}
            )

        def callback(indata, frames, time_info, status):
            if self.ui.muted or self._phone_active:
                return
            with self._speaking_lock:
                fox_speaking = self._is_speaking
            rms = _pcm_level(indata)

            # ── Barge-in por voz (EchoGuard, liviano) ────────────────────────
            # Mientras Fox habla el mic NO se transmite a Gemini. EchoGuard
            # clasifica cada bloque: ¿voz del usuario o nuestro propio eco?
            # Tras varios bloques consecutivos de voz real, se corta a Fox.
            if fox_speaking:
                try:
                    if self._echo.is_user_speech(indata, SEND_SAMPLE_RATE, rms):
                        self._barge_blocks += 1
                        if self._barge_blocks >= self._echo.required_blocks:
                            print("[FOX] 🎙 Barge-in detectado (EchoGuard)")
                            self._barge_blocks = 0
                            loop.call_soon_threadsafe(self.interrupt)
                    else:
                        self._barge_blocks = 0
                except Exception:
                    pass
                return

            # ── Cola de eco ───────────────────────────────────────────────────
            # El flag de "hablando" ya cayó pero el parlante todavía suena. El
            # guard deja pasar una respuesta real y descarta solo nuestro eco.
            if self._tail_active():
                try:
                    if not self._echo.is_user_speech(indata, SEND_SAMPLE_RATE, rms):
                        return
                    self._tail_until = 0.0   # una voz real termina la cola antes
                except Exception:
                    return
            elif self._echo._hist:
                self._echo.reset()

            data = indata.tobytes()

            atencion = getattr(self, "atencion", None)
            if atencion is not None and atencion.wake_disponible:
                # Fox en modo wake word: el audio solo sale a la nube cuando
                # el gestor de atención lo autoriza.
                estaba_activo = atencion.activo
                enviar = atencion.procesar(data, rms)
                for bloque in atencion.tomar_prebuffer():
                    if bloque:
                        _encolar(bloque)
                if enviar:
                    _encolar(data)
                # Solo se marca "esperando respuesta" si hay voz real y no es
                # el bloque exacto que detectó la palabra clave (puede ser un
                # "fox" a secas, que no debe disparar al watchdog).
                if atencion.activo and rms > 0.03 and not (enviar and not estaba_activo):
                    self._waiting_for_reply = True
                    self._voice_sent_at = time.monotonic()
                    self._last_server_event = time.monotonic()
                elif not atencion.activo:
                    self._waiting_for_reply = False
            else:
                # Fallback: sin Vosk, micrófono abierto (comportamiento clásico).
                _encolar(data)
                if not fox_speaking and rms > 0.03:
                    self._waiting_for_reply = True
                    self._voice_sent_at = time.monotonic()
                    self._last_server_event = time.monotonic()

            # Feed the live mic level to the HUD so the waveform reacts to
            # the user's actual voice. Purely cosmetic  any failure here must
            # never disturb the mic.
            try:
                self.ui.set_audio_level(rms)
            except Exception:
                pass

        try:
            def _open_mic(dev):
                return sd.InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    device=dev,
                    callback=callback,
                )

            # Which microphone. resolve() returns None for "system default" and
            # for a saved device that is no longer present — so a headset
            # unplugged since the last run falls back to the built-in mic
            # instead of raising on startup and taking the session with it.
            _mic_name = get_input_device()
            _mic_dev  = audio_devices.resolve(_mic_name, "input")
            if _mic_dev is not None:
                print(f"[FOX] 🎤 Input device: {_mic_name}")
            try:
                _mic_stream = _open_mic(_mic_dev)
            except Exception as _e:
                # A device the picker listed but the driver will not open right
                # now — exclusive mode, a webcam already in use, a virtual mic
                # whose source went away. Chosen hardware failing must never
                # mean the assistant cannot hear at all.
                if _mic_dev is None:
                    raise
                print(f"[FOX] ⚠️  Mic '{_mic_name}' failed: {_e} — using default")
                self.ui.write_log(
                    f"SYS: Microphone '{_mic_name}' unavailable — using system default."
                )
                _mic_stream = _open_mic(None)

            with _mic_stream:
                print("[FOX] 🎤 Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[FOX] ❌ Mic: {e}")
            raise

    async def _receive_audio(self):
        print("[FOX] 👂 Recv started")
        out_buf, in_buf = [], []
        self._latency_first_audio = True

        try:
            while True:
                async for response in self.session.receive():

                    # ── Session resumption ───────────────────────────────────
                    # The server sends this periodically. `resumable` goes false
                    # while a turn is mid-flight — replaying a handle from that
                    # moment is what the flag exists to prevent — so only
                    # resumable handles are kept. This is three lines and it is
                    # the entire fix for "every reconnect forgets everything".
                    _sru = getattr(response, "session_resumption_update", None)
                    if _sru is not None:
                        if getattr(_sru, "resumable", False) and getattr(_sru, "new_handle", None):
                            if self._resume_handle is None:
                                print("[FOX] 🔗 Session resumption armed")
                            self._resume_handle = _sru.new_handle

                    if response.data:
                        self._last_server_event = time.monotonic()  # servidor vivo
                        self._waiting_for_reply = False  # FOX: hay audio del servidor → vivo
                        if getattr(self, "_latency_first_audio", True):
                            self._latency_first_audio = False
                            _last_mic = getattr(self, "_last_audio_out", 0.0)
                            if _last_mic > 0:
                                _ttft = (time.monotonic() - _last_mic) * 1000
                                _latency_log(f"TTFT {_ttft:.0f}ms  model={self._current_model}")
                        if self._interrupted:
                            pass  # discard: interrupted
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            if self._telegram_reply:
                                self._telegram_audio_buf.extend(_audio_data)
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        self._last_server_event = time.monotonic()  # transcripción / turno → vivo
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt and txt != (out_buf[-1] if out_buf else ""):
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            # FOX: el servidor respondió → sesión sana
                            self._waiting_for_reply = False
                            self._no_response_reconnects = 0
                            self._latency_first_audio = True
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_buf = []
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if full_out:
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                if self._telegram_reply:
                                    self._telegram_text_buf.append(full_out)
                                    if self._telegram_debounce:
                                        self._telegram_debounce.cancel()
                                    self._telegram_debounce = asyncio.create_task(self._telegram_flush())
                            if self.atencion is not None and (full_in or full_out):
                                self.atencion.renovar("turno completo")
                            out_buf = []

                            # Vision injection: model finished tool-response turn → now send the image
                            if self._pending_vision and self.session:
                                import base64 as _b64
                                frames, mime_t, question, angle = self._pending_vision
                                self._pending_vision = None
                                _bytes_total = sum(len(f) for f in frames)
                                print(f"[Vision] 📤 {len(frames)} frames ({_bytes_total:,} bytes, angle={angle}) → main session")
                                if _model_needs_realtime_text(self._current_model):
                                    # Ráfaga de frames (mini-video) — igual que AI Studio Live:
                                    # el modelo "live" necesita movimiento para leer bien la escena.
                                    for _f in frames:
                                        await self.session.send_realtime_input(
                                            video=types.Blob(data=_f, mime_type=mime_t)
                                        )
                                    await self.session.send_realtime_input(text=question)
                                else:
                                    b64 = _b64.b64encode(frames[0]).decode("ascii")
                                    await self.session.send_client_content(
                                        turns={"parts": [
                                            {"inline_data": {"mime_type": mime_t, "data": b64}},
                                            {"text": question},
                                        ]},
                                        turn_complete=True,
                                    )
                                # Mark next turn_complete behaviour depending on angle
                                if self._vision_cam_active:
                                    # Camera: keep busy until FOX finishes speaking the answer
                                    self._vision_cam_active    = False
                                    self._vision_close_pending = True
                                else:
                                    # Screen-only: no camera to close; release busy flag now
                                    self._vision_busy = False
                            elif self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        self._last_server_event = time.monotonic()  # pidió un tool → vivo
                        fn_responses = []
                        self._executing_tool = True
                        try:
                            for fc in response.tool_call.function_calls:
                                print(f"[FOX] 📞 {fc.name}")
                                fr = await self._execute_tool(fc)
                                fn_responses.append(fr)
                            await self.session.send_tool_response(
                                function_responses=fn_responses
                            )
                        finally:
                            self._executing_tool = False
        except Exception as e:
            _err = str(e).lower()
            if not ("1008" in _err or "goaway" in _err
                    or "operation was aborted" in _err
                    or "policy violation" in _err
                    or "session duration" in _err):
                print(f"[FOX] ❌ Recv: {e}")
                traceback.print_exc()
            raise

    def _resample_pcm(self, data: bytes, speed: float) -> bytes:
        """Cambia la velocidad del audio PCM sin librerías externas
        (interpolación lineal) — port del Fox original."""
        try:
            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
            n_out = int(len(arr) / speed)
            if n_out < 2:
                return data
            idx = np.linspace(0, len(arr) - 1, n_out)
            i0 = idx.astype(np.int64)
            i1 = np.minimum(i0 + 1, len(arr) - 1)
            frac = (idx - i0).astype(np.float32)
            res = arr[i0] * (1 - frac) + arr[i1] * frac
            return res.astype(np.int16).tobytes()
        except Exception:
            return data

    async def _play_audio(self):
        print("[FOX] 🔊 Play started")

        _spk_name = get_output_device()
        _spk_dev  = audio_devices.resolve(_spk_name, "output")
        if _spk_dev is not None:
            print(f"[FOX] 🔊 Output device: {_spk_name}")

        def _open_spk(dev):
            st = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
            )
            st.start()
            return st

        try:
            stream = _open_spk(_spk_dev)
        except Exception as _e:
            # A chosen output that the host API accepts by name but refuses to
            # open (exclusive mode, wrong sample rate, device asleep) must not
            # cost the user their voice. Fall back to the default and say so.
            if _spk_dev is None:
                raise
            print(f"[FOX] ⚠️  Output device '{_spk_name}' failed: {_e} — using default")
            self.ui.write_log(f"SYS: Speaker '{_spk_name}' unavailable — using system default.")
            stream = _open_spk(None)

        # Preguntar al dispositivo cuán atrás van los parlantes (en vez de
        # asumirlo). De eso se dimensiona la cola de eco.
        try:
            lat = float(getattr(stream, "latency", 0.0) or 0.0)
            if 0.0 < lat < 1.0:
                self._out_latency = lat
            print(f"[FOX] 🔊 Output latency {self._out_latency*1000:.0f} ms "
                  f"→ echo tail {(self._out_latency + _TAIL_MARGIN)*1000:.0f} ms")
        except Exception:
            pass

        try:
            # ── Playout (agrupado, anti-hipo) ─────────────────────────────────
            # En vez de escribir cortes fijos de ~50 ms (un round-trip al pool
            # de hilos por cada uno), agrupamos TODO el audio disponible en UNA
            # sola escritura de hasta ~200 ms. Menos escrituras = menos contención
            # con la ejecución de herramientas (web_search usa el mismo pool),
            # que es justo lo que causaba los hipos al buscar en internet.
            _MAX_BATCH = 9600       # ~200 ms (24 kHz · 16-bit · mono)
            _buf = bytearray()
            _started = False

            while True:
                # Interrupción → descartar también lo que ya quedó buffereado.
                if getattr(self, "_interrupted", False):
                    _buf.clear()
                    _started = False
                    await asyncio.sleep(0.01)
                    continue

                # Drenar todo lo ya encolado hacia el buffer de playout.
                try:
                    while True:
                        _buf.extend(self.audio_in_queue.get_nowait())
                except asyncio.QueueEmpty:
                    pass

                turn_done = bool(
                    self._turn_done_event
                    and self._turn_done_event.is_set()
                    and self.audio_in_queue.empty()
                )

                # FOX: altavoz silenciado → descartar el audio en vez de reproducirlo
                if getattr(self, "_speaker_muted", False):
                    _buf.clear()
                    if turn_done:
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    await asyncio.sleep(0.01)
                    continue

                # Colchón de precarga: esperar a tener ~100 ms de audio antes de
                # escribir, para puentear los huecos de red en respuestas largas.
                # Al terminar el turno se vuelca lo que quede.
                if len(_buf) < 4800 and not turn_done:
                    try:
                        _buf.extend(
                            await asyncio.wait_for(
                                self.audio_in_queue.get(), timeout=0.1
                            )
                        )
                    except asyncio.TimeoutError:
                        pass
                    continue

                if not _buf:
                    if turn_done:
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                        _started = False
                    await asyncio.sleep(0.01)
                    continue

                _started = True
                self.set_speaking(True)

                # Agrupar TODO lo disponible en UNA sola escritura (hasta ~200 ms).
                # Menos round-trips al pool de hilos = menos contención con las
                # herramientas (web_search), que es lo que quitaba los hipos.
                batch = bytes(_buf[:_MAX_BATCH])
                del _buf[:_MAX_BATCH]

                # FOX: velocidad de voz (resampleo client-side — este SDK no
                # soporta speaking_rate en SpeechConfig)
                _vsp = getattr(self, "_voice_speed_val", 1.0)
                if _vsp != 1.0:
                    batch = self._resample_pcm(batch, _vsp)

                # Drive the HUD waveform from the assistant's own voice.
                try:
                    _pcm = np.frombuffer(batch, dtype=np.int16)
                    _lvl = _pcm_level(_pcm)
                    self.ui.set_audio_level(_lvl)
                    # Registrar lo que suena para que EchoGuard distinga el eco.
                    self._echo.note_output(_pcm, RECEIVE_SAMPLE_RATE, _lvl)
                except Exception:
                    pass

                try:
                    await asyncio.to_thread(stream.write, batch)
                except (RuntimeError, asyncio.CancelledError):
                    break   # executor shutting down — exit cleanly
        except Exception as e:
            print(f"[FOX] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    # ── Morning briefing ────────────────────────────────────────────────────────

    def _remember_location(self, loc: dict) -> None:
        """Guarda ciudad/país aproximados en memoria (identity), solo si cambian."""
        try:
            upd: dict = {}
            if (loc or {}).get("city"):
                upd["city"] = {"value": loc["city"]}
            if (loc or {}).get("country"):
                upd["country"] = {"value": loc["country"]}
            if upd:
                update_memory({"identity": upd})
        except Exception as e:
            print(f"[Briefing] No pude recordar la ubicación: {e}")

    @staticmethod
    def _localised_news_query(country: str, lang: str) -> str:
        """Consulta de noticias centrada en el país del usuario (o mundo si no hay)."""
        if not country:
            return "top world news today"
        lang_l = (lang or "").lower()
        if any(w in lang_l for w in ("spanish", "español", "espanol", "castellano")):
            return f"noticias de hoy {country}"
        return f"top news in {country}"

    async def _send_startup_briefing(self) -> None:
        """
        Two-phase briefing optimized for speed:
          Phase 1 — instant greeting (no tools) → speech starts in <1s
          Phase 2 — news pre-fetched in a background thread while Phase 1 plays,
                    delivered as ready text (no Gemini tool-call round-trip) and
                    shown on the UI content panel. Waits for turn_complete event
                    instead of a fixed sleep so there is no unnecessary gap.
        """
        memory   = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")
        time_str = datetime.now().strftime("%H:%M")

        # ── Ubicación aproximada (país) para localizar las noticias ──────────
        # Se deduce de la IP (sin GPS, sin permisos) y se cachea 7 días. Si falla
        # (sin internet o VPN que la oculte), cae a noticias del mundo como antes.
        loc = None
        try:
            loc = await asyncio.wait_for(asyncio.to_thread(get_location), timeout=8.0)
        except Exception as e:
            print(f"[Briefing] Ubicación no disponible: {e}")
        country = (loc or {}).get("country") or ""
        if loc:
            # Recordá ciudad/país en memoria (identity) para que Fox lo conozca
            # en toda la sesión sin esperar a que el usuario lo diga.
            await asyncio.to_thread(self._remember_location, loc)

        # Start fetching news immediately — runs in parallel while phase 1 plays
        loop = asyncio.get_event_loop()
        news_query = self._localised_news_query(country, lang)
        news_future = loop.run_in_executor(self._tool_executor, _fetch_news_sync, news_query)

        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── Phase 1: instant greeting ─────────────────────────────────────────
        # The briefing fires before the user has said anything, so the
        # remembered language is the only signal there is. It is a starting
        # point, not a setting: the moment they reply, their language wins.
        lang_clause = (f" Da este saludo en {lang} y después seguí el idioma del "
                       f"usuario desde su primera respuesta."
                       if lang else "")
        name_clause = f" Tratá al usuario por su nombre ({name})." if name else ""

        # Inject last session context if available — pop removes it so it's never repeated
        last = await asyncio.to_thread(pop_last_session)
        session_clause = ""
        if last:
            try:
                _delta = (datetime.now() - datetime.strptime(last["date"], "%Y-%m-%d")).days
                _when  = ("hoy más temprano" if _delta == 0
                          else ("ayer" if _delta == 1 else f"hace {_delta} días"))
            except Exception:
                _when = "la última vez"
            session_clause = (
                f" Además, mencioná brevemente y con naturalidad que {_when}: {last['summary']}"
            )

        p1 = (
            f"Saludá al usuario con calidez, mencioná que son las {time_str} y que ya "
            f"estás buscando las noticias de hoy.{session_clause} "
            f"Máximo 2 frases cortas. No llames herramientas.{lang_clause}{name_clause}"
        )

        # Clear the turn-done event so we can wait for Phase 1 to finish
        if self._turn_done_event:
            self._turn_done_event.clear()

        await self._send_text(p1)
        self.ui.write_log("SYS: Briefing fase 1 (saludo) enviado.")

        # ── Phase 2: fire as soon as Phase 1 audio is done ───────────────────
        async def _deliver_news():
            try:
                lang_str = (f" Hablá en {lang}, salvo que el usuario ya haya hablado "
                            f"en otro idioma; en ese caso usá el suyo."
                            if lang else "")

                # Wait for news fetch (already running) and Phase 1 turn-complete
                # in parallel — whichever takes longer determines the wait time
                news_done   = asyncio.wrap_future(news_future)
                turn_waited = False
                if self._turn_done_event:
                    try:
                        await asyncio.wait_for(self._turn_done_event.wait(), timeout=6.0)
                        turn_waited = True
                    except asyncio.TimeoutError:
                        pass

                # Extra buffer: turn_complete fires when Gemini finishes *generating*
                # Phase 1, but audio may still be playing.  Waiting a beat here
                # prevents Phase 2 audio from arriving while Phase 1 is mid-sentence
                # (which sounds like a "repeated first response" to the user).
                if turn_waited:
                    await asyncio.sleep(0.8)
                else:
                    await asyncio.sleep(1.0)

                try:
                    news_text = await asyncio.wait_for(news_done, timeout=8.0)
                except Exception as e:
                    self.ui.write_log(f"SYS: Falló la búsqueda de noticias: {e!r}")
                    news_text = ""

                if not self.session:
                    return

                failed = (not news_text) or news_text.startswith(
                    ("No news found", "Search failed", "Please provide", "No se encontraron")
                )
                if not failed:
                    # Show on UI content panel immediately
                    self.ui.show_content(
                        f"NOTICIAS — {country}" if country else "NOTICIAS — lo más importante de hoy",
                        news_text,
                    )

                    _donde = f" de {country}" if country else ""
                    p2 = (
                        f"[BRIEFING] Estas son las noticias más importantes de hoy{_donde}:\n{news_text}\n\n"
                        "Elegí UNA noticia, resumila en una frase, y después decí que la lista "
                        f"completa está en pantalla. No llames herramientas.{lang_str}"
                    )
                else:
                    self.ui.write_log(
                        f"SYS: Noticias no disponibles — el backend devolvió: {news_text[:120]!r}"
                    )
                    p2 = (
                        "No pude conseguir las noticias en este momento. "
                        f"Avisale al usuario brevemente.{lang_str}"
                    )

                await self._send_text(p2)
                self.ui.write_log("SYS: Briefing fase 2 (noticias) enviado.")
            except Exception as e:
                print(f"[Briefing] Error en fase 2: {e}")
                self.ui.write_log(f"SYS: Falló la fase 2 del briefing: {e}")

        asyncio.create_task(_deliver_news())

    # ── Session memory ──────────────────────────────────────────────────────────

    async def _save_session_summary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to long_term.json."""
        log = self._session_log
        if len(log) < 3:          # need at least one exchange to be worth saving
            return
        self._session_log = []    # reset immediately so the next session starts clean

        memory = load_memory()
        lang_entry = memory.get("identity", {}).get("language", {})
        lang = (lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)).strip()
        lang = lang or "English"

        convo = "\n".join(log[-40:])   # cap at last 40 turns to stay within token budget
        prompt = (
            f"Summarize this conversation in 1-2 sentences in {lang}. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        # FOX: reintento breve (el 503 de Gemini es temporal) y error silencioso
        for _intento in range(2):
            try:
                from google import genai as _genai
                client = _genai.Client(api_key=_get_api_key())
                resp   = await asyncio.to_thread(
                    client.models.generate_content,
                    model="gemini-flash-latest",
                    contents=prompt,
                )
                summary = (resp.text or "").strip()
                if summary:
                    save_session_summary(summary, lang)
                break
            except Exception:
                if _intento == 0:
                    await asyncio.sleep(2.0)
                    continue
                print("[Memory] ⚠️ Resumen de sesión no guardado (servidor de Google ocupado).")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _watchdog(self):
        """FOX: si el servidor queda MUCHO rato sin mandar nada tras hablar,
        fuerza una reconexión limpia. Si pasa 2 veces seguidas con grounding
        activo, lo desactiva solo (algunos modelos no lo toleran).

        Cambio clave vs. la versión que desconectaba cada ~30 s: antes se medía
        desde la ÚLTIMA VOZ del usuario; ahora se mide desde el ÚLTIMO BYTE del
        servidor (`_last_server_event`). Un modelo "thinking" que llama a web_search
        y después razona en silencio 20-40 s NO es un cuelgue: es trabajo normal.
        Recién se reconecta si el servidor no emite NADA durante MUTE_TIMEOUT_SEC,
        y nunca mientras un tool se está ejecutando."""
        MUTE_TIMEOUT_SEC = 60.0   # silencio real tolerado antes de reconectar
        while True:
            await asyncio.sleep(5)
            if not getattr(self, "_waiting_for_reply", False) or not self.session:
                continue
            if getattr(self, "atencion", None) is not None and self.atencion.wake_disponible:
                if not self.atencion.activo:
                    continue
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue
            # Un tool en ejecución (ofimática, búsqueda, generación) bloquea el
            # audio del servidor de forma legítima: nunca castigarlo.
            if getattr(self, "_executing_tool", False):
                continue
            espera   = time.monotonic() - getattr(self, "_voice_sent_at", 0.0)
            silencio = time.monotonic() - getattr(self, "_last_server_event", 0.0)
            if espera > 20.0 and silencio > MUTE_TIMEOUT_SEC:
                self._no_response_reconnects += 1
                print(f"[FOX] Servidor sin respuesta ({silencio:.0f}s de silencio) — reconectando limpiamente.")
                self.ui.write_log("SYS: Servidor sin respuesta — reconectando...")
                self._waiting_for_reply = False
                if self._no_response_reconnects >= 2 and getattr(self, "_use_grounding", False):
                    self._use_grounding = False
                    self._no_response_reconnects = 0
                    print("[FOX] 2 sesiones mudas seguidas — desactivando grounding.")
                    self.ui.write_log("SYS: Grounding desactivado por falta de respuesta del servidor.")
                raise RuntimeError("watchdog: servidor mudo")

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            try:
                alert = await asyncio.to_thread(self._sys_monitor.check)
            except (RuntimeError, asyncio.CancelledError):
                return
            if not alert or not self.session:
                continue
            # Don't interrupt an active conversation
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self._send_text(alert)
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    # ── Background monitor ──────────────────────────────────────────────────────

    async def _run_background_monitor(self) -> None:
        """Check user-configured topics once per day; speak alerts when new headlines appear."""
        await asyncio.sleep(300)          # wait 5 min after startup before first check
        while True:
            if self.session:
                # Don't interrupt if user spoke recently or FOX is mid-sentence
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if not speaking and not recent_speech:
                    try:
                        alerts = await asyncio.to_thread(monitor_check_all)
                        memory = load_memory()
                        lang_e = memory.get("identity", {}).get("language", {})
                        lang   = (lang_e.get("value", "") if isinstance(lang_e, dict) else str(lang_e)).strip() or "English"
                        for alert in alerts:
                            msg = (
                                f"{alert}\n\n"
                                f"Inform the user about this development naturally in {lang}. "
                                "One brief sentence only."
                            )
                            await self._send_text(msg)
                            self.ui.write_log(f"SYS: Monitor alert sent.")
                            await asyncio.sleep(6)   # gap between consecutive alerts
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Background check error: {e}")
            await asyncio.sleep(1800)     # check every 30 minutes

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory       = await asyncio.to_thread(load_memory)
                monitors     = await asyncio.to_thread(list_monitors)
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.build_prompt(
                    memory       = memory,
                    monitors     = monitors or None,
                    recent_turns = recent_turns or None,
                )
                await self._send_text(prompt)
                self.ui.write_log("SYS: Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    await self._send_text(text)
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()
        self._reconnect_event = asyncio.Event()

        # ── Modo Agente web ────────────────────────────────────────────────
        # Arranca el servidor FastAPI+WebSocket del agente en segundo plano.
        # Se abre en el navegador con el botón "🤖 AGENTE" de la interfaz.
        asyncio.create_task(self._start_agent_web_server())

        # ── Wire the shared core services to the interface ───────────────────
        # The confirmation gate is useless without a way to ask, and a memory
        # trim is invisible without a way to say so. Both are bound once here
        # rather than passed down through every action signature.
        confirm_gate.bind(
            show = self.ui.show_confirm,
            hide = self.ui.hide_confirm,
            log  = self.ui.write_log,
        )
        set_trim_notifier(self.ui.write_log)

        # Tell the device picker the exact rates the streams open at, from the
        # constants that actually open them — so it can never list a device that
        # cannot be opened at them.
        audio_devices.configure(SEND_SAMPLE_RATE, RECEIVE_SAMPLE_RATE)

        # Enumerate audio devices off-thread. The settings drawer must never pay
        # for host-API enumeration on the Qt thread.
        audio_devices.prefetch()

        # Dashboard web remoto DESACTIVADO — el control remoto web se eliminó.
        # (Se usa Telegram en su lugar para el acceso remoto.)
        self._dashboard = None

        # Telegram bot: escucha mensajes entrantes y los reenvía a Fox.
        asyncio.create_task(self._telegram_listener())

        # FOX: pool de modelos con rotación ante fallos repetidos
        _pool = _get_model_pool()
        _pool_idx = 0
        _modelo = _pool[0]
        self._current_model = _modelo
        _fail_streak = 0
        _latency_log(f"SESION model={_modelo}")

        while True:
            try:
                print(f"[FOX] Connecting ({_modelo})...")
                self.ui.set_state("THINKING")
                # 0 = aún no conectó. Se actualiza en "Connected." para poder
                # distinguir un fallo de conexión de un fallo tras conectar.
                self._session_started_at = 0.0
                _resumed_with = self._resume_handle is not None
                config = self._build_config()

                # Fresh client on every reconnect — avoids stale HTTP session state
                # v1alpha carries the enhanced audio features (affective dialog,
                # proactive audio); if they get rejected we fall back to v1beta.
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={"api_version": "v1alpha" if self._enhanced_live else "v1beta"}
                )

                async with (
                    client.aio.live.connect(model=_modelo, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False

                    print("[FOX] Connected.")
                    # No se resetea _fail_streak acá: conectar no es sinónimo de
                    # "sano". El reset se decide en el handler según cuánto duró
                    # la sesión (ver SESSION_HEALTHY_SEC más abajo).
                    self._session_started_at = time.monotonic()
                    if _resumed_with:
                        # Say it plainly: the difference between "it reconnected"
                        # and "it reconnected and still knows what we were doing"
                        # is the whole point, and it is invisible otherwise.
                        self.ui.write_log("SYS: Reconnected — conversation restored.")
                    self.ui.set_state("LISTENING")
                    self.ui.write_log("SYS: Fox online.")

                    # FOX: primer arranque → activo (para hablar de una). En una
                    # RECONEXIÓN (por idle/cierre) → espera, para no transmitir
                    # en vano y no repetir el ciclo idle → abort → reconexión.
                    if getattr(self, "atencion", None) is not None and self.atencion.wake_disponible:
                        if getattr(self, "_first_connect_done", False):
                            self.atencion.armar("reconexión")
                        else:
                            self.atencion.activar("inicio")
                            self._first_connect_done = True

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    self._reconnect_event.clear()  # ignore requests from before this session
                    tg.create_task(self._watch_reconnect())
                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._watchdog())          # FOX: servidor mudo → reconexión
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_background_monitor())
                    tg.create_task(self._run_proactive_mode())
                    tg.create_task(self._watch_attention_config())
                    tg.create_task(self._keepalive())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Morning briefing — once per day (first launch of the day)
                    if (not self._briefing_sent and get_brief_enabled()
                            and _briefing_due_today()):
                        self._briefing_sent = True
                        _mark_briefing_sent()
                        tg.create_task(self._send_startup_briefing())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                # Voluntary reconnect (voice change) — not an error. Rebuild the
                # session immediately with no backoff and no scary logs.
                if _is_reconnect_signal(e):
                    print("[FOX] Voluntary reconnect requested.")
                    if not _keep_context_of(e):
                        # A deliberate clean slate (voice change) — drop the
                        # handle so the next connect really does start empty.
                        self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                # A resumption handle the server will not accept — expired, or
                # belonging to a session it has since dropped. Without this, the
                # same dead handle would be replayed on every retry and the
                # assistant would never come back at all: the feature meant to
                # survive a reconnect would be the thing preventing one. Drop it
                # once and let the next attempt start clean.
                if _resumed_with and (
                    "resum" in str(e).lower()
                    or "handle" in str(e).lower()
                    or "INVALID_ARGUMENT" in str(e)
                    or "NOT_FOUND" in str(e)
                ):
                    print("[FOX] 🔗 Resumption handle rejected — starting a fresh session")
                    self.ui.write_log("SYS: Could not restore the conversation — starting fresh.")
                    self._resume_handle = None
                    self._conn_backoff = 0
                    continue

                err_str = str(e)
                _sub_msgs: list[str] = []
                if isinstance(e, BaseExceptionGroup):
                    _sub_msgs = [str(x) for x in e.exceptions]

                # FOX: watchdog de servidor mudo — reconexión rápida y silenciosa
                if ("watchdog: servidor mudo" in err_str
                        or any("watchdog: servidor mudo" in m for m in _sub_msgs)):
                    self._conn_backoff = 1
                    continue

                # FOX: cierre de sesión iniciado por el SERVIDOR (GoAway, 1008,
                # "operation was aborted", límite de duración, policy violation).
                # No es un fallo real: solo hay que reconectar limpio, sin traceback.
                _err_low = err_str.lower()
                _server_close = (
                    "goaway" in _err_low
                    or "session duration" in _err_low
                    or "operation was aborted" in _err_low
                    or "1008" in _err_low
                    or "policy violation" in _err_low
                )
                if (_server_close
                        or any("goaway" in m.lower()
                               or "session duration" in m.lower()
                               or "operation was aborted" in m.lower()
                               or "1008" in m
                               or "policy violation" in m.lower()
                               for m in _sub_msgs)):
                    print("[FOX] El servidor cerró la sesión — reconectando.")
                    self.ui.write_log("SYS: Sesión reiniciada.")
                    # El handle de reanudación apunta a la sesión muerta: soltarlo
                    # evita el doble 1008 ("resumption handle rejected").
                    self._resume_handle = None
                    _fail_streak = 0
                    self._conn_backoff = 1
                    continue

                # FOX: cuota de Gemini agotada (1011 "exceeded your current quota").
                # Reintentar cada 3s no sirve de nada: espera larga + aviso claro.
                if ("quota" in _err_low or "billing" in _err_low
                        or any("quota" in m.lower() or "billing" in m.lower()
                               for m in _sub_msgs)):
                    print("[FOX] ⚠️ Cuota de Gemini agotada — esperando 5 min antes de reintentar.")
                    self.ui.write_log(
                        "ERR: Cuota de Gemini agotada — revisá tu plan/billing en Google AI Studio."
                    )
                    self.ui.set_state("SLEEPING")
                    await asyncio.sleep(300)
                    continue

                print(f"[FOX] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                # FOX: si el servidor rechaza el grounding, lo desactivamos y
                # reintentamos ya (fallback automático).
                if getattr(self, "_use_grounding", False) and any(
                    k in err_str.lower()
                    for k in ("google_search", "grounding", "invalid tool",
                              "unsupported tool", "tool_config", "tools")
                ):
                    self._use_grounding = False
                    self.ui.write_log("SYS: Grounding no soportado — reconectando sin él.")
                    self._conn_backoff = 1
                    continue

                # Enhanced audio features rejected by the server (preview API
                # drift) — drop them and reconnect with the plain config.
                if self._enhanced_live and (
                    "INVALID_ARGUMENT" in err_str
                    or "affective" in err_str.lower()
                    or "proactiv" in err_str.lower()
                    or "Unknown name" in err_str
                    or "unexpected keyword" in err_str
                ):
                    self._enhanced_live = False
                    self.ui.write_log(
                        "SYS: Advanced audio features unavailable — reconnecting without them."
                    )
                    continue

                # Invalid API key — stop hammering the API, prompt re-configuration.
                # (NO usar "1007": es un error genérico de payload inválido, no de
                # clave — disparaba una reconfiguración que reseteaba api_keys.json.)
                if "API key not valid" in err_str or "API_KEY_INVALID" in err_str:
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[FOX] New API key saved — reconnecting...")
                    _conn_backoff = 3
                    continue

                # FOX: rotación de modelo ante fallos.
                # Una sesión que vivió SESSION_HEALTHY_SEC se considera sana: un
                # error aislado no acumula. Si conectó y murió rápido (1011, etc.)
                # —o ni llegó a conectar—, cuenta como fallo; a los 3, rota.
                _started = getattr(self, "_session_started_at", 0.0)
                if _started and (time.monotonic() - _started) >= SESSION_HEALTHY_SEC:
                    _fail_streak = 0
                else:
                    _fail_streak += 1
                if _fail_streak >= 3 and len(_pool) > 1:
                    _fail_streak = 0
                    _pool_idx = (_pool_idx + 1) % len(_pool)
                    _modelo = _pool[_pool_idx]
                    self._current_model = _modelo
                    self.ui.write_log(f"SYS: Cambiando a modelo alternativo ({_modelo})...")

                # Network / timeout errors — log clearly and back off
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: No se pudo conectar — reintentando en {_conn_backoff}s. "
                        "(Si estás fuera de una región soportada, probá con una VPN)"
                    )
                else:
                    self._conn_backoff = 3
            finally:
                self.session = None
                # Only save if there was a real conversation (≥3 turns)
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._save_session_summary())

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            # Guard anti-churn: si hubo muchas reconexiones en el último minuto,
            # el backoff sube exponencialmente para no quemar la cuota (RPM/RPD)
            # ni saturar la API con un bucle de reconexiones.
            _now = time.monotonic()
            _recent = [t for t in self._recent_reconnects if _now - t < 60.0]
            if len(_recent) >= 3:
                delay = max(delay, min(60, 2 ** len(_recent)))   # 8, 16, 32, 60...
            self._recent_reconnects = _recent + [_now]
            print(f"[FOX] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

def main():
    ui = FoxUI(str(Path(__file__).resolve().parent / "config" / "fox_face.png"))

    def runner():
        ui.wait_for_api_key()
        fox = FoxLive(ui)
        try:
            asyncio.run(fox.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()