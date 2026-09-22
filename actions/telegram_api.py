"""telegram_api — send messages via the Telegram Bot API (ported from AMSY).

Faster and headless vs the desktop-automation path in send_message: works with
no app open, using the bot token + chat id from config/api_keys.json.
No-ops gracefully when the token is not configured.
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


def _cfg() -> dict:
    try:
        return json.loads(
            (BASE_DIR / "config" / "api_keys.json").read_text(encoding="utf-8")
        )
    except Exception:
        return {}


def _send(token: str, chat_id: str, text: str) -> bool:
    import requests
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)
    return bool(resp.json().get("ok", False))


def get_telegram_config() -> tuple[str, str]:
    """(token, chat_id) desde config; devuelve ("", "") si no está configurado."""
    cfg = _cfg()
    token = (cfg.get("telegram_token") or "").strip()
    chat = (cfg.get("telegram_chat_id") or "").strip()
    return token, chat


def poll_telegram(token: str, chat_id: str, offset: int) -> tuple[list[dict], int]:
    """Long-poll getUpdates; devuelve ([{type: text|voice, ...}, ...], nuevo_offset)."""
    import requests
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/getUpdates",
            json={"offset": offset, "timeout": 20, "allowed_updates": ["message"]},
            timeout=30,
        )
        data = resp.json()
    except Exception:
        return [], offset
    if not data.get("ok"):
        return [], offset
    msgs: list[dict] = []
    new_offset = offset
    for upd in data.get("result", []):
        new_offset = max(new_offset, upd.get("update_id", 0) + 1)
        msg = upd.get("message")
        if not msg:
            continue
        if str(msg.get("chat", {}).get("id", "")) != str(chat_id):
            continue
        if msg.get("text"):
            msgs.append({"type": "text", "text": msg["text"].strip()})
        elif msg.get("voice"):
            msgs.append({"type": "voice", "file_id": msg["voice"].get("file_id", "")})
    return msgs, new_offset


def send_message(token: str, chat_id: str, text: str) -> bool:
    """Enviar un mensaje de texto al chat de Telegram."""
    return _send(token, chat_id, text)


def _run_ffmpeg(in_bytes: bytes, out_suffix: str, input_args: list[str], output_args: list[str]) -> bytes | None:
    """Ejecuta ffmpeg con bytes de entrada; `input_args` van ANTES de -i (formato
    de entrada), `output_args` van DESPUÉS de -i (formato de salida)."""
    import subprocess, tempfile, os
    in_path = out_path = None
    try:
        fd, in_path = tempfile.mkstemp(suffix=".in")
        os.close(fd)
        with open(in_path, "wb") as f:
            f.write(in_bytes)
        out_path = in_path + out_suffix
        subprocess.run(
            ["ffmpeg", "-y", *input_args, "-i", in_path, *output_args, out_path],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        with open(out_path, "rb") as f:
            return f.read()
    except Exception:
        return None
    finally:
        for p in (in_path, out_path):
            if p:
                try:
                    os.unlink(p)
                except Exception:
                    pass


def ogg_to_pcm16k(ogg_bytes: bytes) -> bytes | None:
    """Convierte un audio OGG (voz de Telegram) a PCM 16 kHz mono s16le."""
    return _run_ffmpeg(ogg_bytes, ".pcm", [], ["-ar", "16000", "-ac", "1", "-f", "s16le"])


def pcm_to_ogg(pcm_bytes: bytes, sample_rate: int) -> bytes | None:
    """Convierte PCM s16le a OGG/Opus para mandar como nota de voz."""
    return _run_ffmpeg(
        pcm_bytes, ".ogg",
        ["-f", "s16le", "-ar", str(sample_rate), "-ac", "1"],
        ["-c:a", "libopus"],
    )


def download_file(token: str, file_id: str) -> bytes | None:
    """Descarga un archivo (voz) de Telegram vía getFile."""
    import requests
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/getFile",
            json={"file_id": file_id}, timeout=15,
        )
        data = r.json()
        fp = (data.get("result") or {}).get("file_path")
        if not data.get("ok") or not fp:
            return None
        d = requests.get(f"https://api.telegram.org/file/bot{token}/{fp}", timeout=60)
        return d.content if d.status_code == 200 else None
    except Exception:
        return None


def send_voice(token: str, chat_id: str, ogg_bytes: bytes) -> bool:
    """Envía una nota de voz (OGG/Opus) a Telegram."""
    import requests, io
    try:
        url = f"https://api.telegram.org/bot{token}/sendVoice"
        files = {"voice": ("voice.ogg", io.BytesIO(ogg_bytes), "audio/ogg")}
        resp = requests.post(url, data={"chat_id": chat_id}, files=files, timeout=60)
        return bool(resp.json().get("ok", False))
    except Exception:
        return False


def telegram_api(parameters=None, player=None, **kwargs) -> str:
    p = parameters or {}
    cfg = _cfg()
    token = (p.get("token") or cfg.get("telegram_token") or "").strip()
    chat = (p.get("chat_id") or cfg.get("telegram_chat_id") or "").strip()
    text = (p.get("text") or p.get("message") or "").strip()

    if not token:
        return "Telegram no está configurado: falta telegram_token en config/api_keys.json."
    if not chat:
        return "Telegram no está configurado: falta telegram_chat_id en config/api_keys.json."
    if not text:
        return "Falta el texto del mensaje."

    try:
        if _send(token, chat, text):
            return f"Enviado por Telegram: {text[:60]}"
        return "Telegram devolvió un error al enviar."
    except Exception as e:
        return f"No pude enviar por Telegram: {e}"

TOOL = {
    "name": 'telegram_api',
    "description": 'Envía mensajes por Telegram usando la Bot API (rápido y sin abrir la app). Requiere telegram_token y telegram_chat_id en config. Usalo para mandar mensajes a tu propio chat de Telegram o un canal.',
    "parameters": {   'type': 'OBJECT',
        'properties': {   'text': {   'type': 'STRING',
                                      'description': 'Texto del mensaje'},
                          'chat_id': {   'type': 'STRING',
                                         'description': 'Chat id destino '
                                                        '(opcional, usa el de '
                                                        'config)'}},
        'required': []},
    "handler": telegram_api,
}
