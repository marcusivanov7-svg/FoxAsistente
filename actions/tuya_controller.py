# actions/tuya_controller.py — Domótica Tuya Smart para Fox (v2)
"""Control de dispositivos Tuya/Smart Life (luces, enchufes, AC, etc.).

Portado de la v1 y mejorado para la v2:
- Firma de tool v2: `tuya_controller(parameters, player, speak)`.
- Caché de dispositivos (5 min) para no golpear la API en cada orden.
- `action='list'` y `action='status'` para ver dispositivos y estado en vivo.
- Mapeo de acciones a códigos Tuya con candidatos y reintento (cada dispositivo
  usa un código distinto: switch/power/... según su categoría).
- Soporte de brillo (0-100% → 10-1000), temperatura de color y modos AC.

Credenciales en config/api_keys.json:
  tuya_client_id, tuya_secret, tuya_region (us|eu|cn), tuya_device_id (opcional).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

try:
    import tinytuya
    _TUYA_OK = True
except ImportError:
    tinytuya = None
    _TUYA_OK = False


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = _get_base_dir() / "config" / "api_keys.json"

_cloud = None
_devices_cache = None
_devices_cache_t = 0.0


def _load_config() -> dict:
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _get_cloud():
    global _cloud
    if not _TUYA_OK:
        raise RuntimeError("tinytuya no está instalado. Ejecutá: pip install tinytuya")
    if _cloud is None:
        cfg = _load_config()
        api_key = (cfg.get("tuya_client_id") or "").strip()
        api_secret = (cfg.get("tuya_secret") or "").strip()
        region = (cfg.get("tuya_region") or "us").strip() or "us"
        if not api_key or not api_secret:
            raise RuntimeError(
                "Faltan las claves de Tuya en config/api_keys.json "
                "(tuya_client_id / tuya_secret)."
            )
        _cloud = tinytuya.Cloud(apiKey=api_key, apiSecret=api_secret, region=region)
    return _cloud


def _get_devices(force: bool = False):
    global _devices_cache, _devices_cache_t
    now = time.time()
    if not force and _devices_cache is not None and now - _devices_cache_t < 300:
        return _devices_cache
    cloud = _get_cloud()
    data = cloud.getdevices()
    devices = (data or {}).get("result", []) or []
    _devices_cache = devices
    _devices_cache_t = now
    return devices


def _find_device(devices, nombre):
    nombre = (nombre or "").lower().strip()
    cfg = _load_config()
    fixed = (cfg.get("tuya_device_id") or "").strip()

    if nombre:
        for d in devices:
            n = (d.get("name") or "").lower()
            if nombre in n or n in nombre or nombre == d.get("id"):
                return d
    if fixed:
        for d in devices:
            if d.get("id") == fixed:
                return d
    return devices[0] if devices else None


# Candidatos de códigos Tuya por acción (se prueban hasta que uno funcione).
_CANDIDATOS = {
    "on":        [("switch", True), ("power", True)],
    "turn_on":   [("switch", True), ("power", True)],
    "off":       [("switch", False), ("power", False)],
    "turn_off":  [("switch", False), ("power", False)],
    "brightness": [("bright_value", None), ("brightness", None)],
    "color_temp": [("temp_set", None), ("color_temp", None), ("temperature", None)],
    "color":     [("colour_data", None), ("color", None), ("colour_data_v2", None)],
    "mode":      [("mode", None)],
}


def _scale_brightness(value) -> int:
    """Acepta 0-100 (%) o 10-1000 (rango nativo Tuya) y normaliza a 10-1000."""
    v = int(value)
    if v <= 100:                       # vino en porcentaje
        v = round(v / 100 * 990) + 10
    return max(10, min(1000, v))


def _parse_color(value: str):
    """'rojo'/'red'/'#FF0000'/'255,0,0' → HSV dict de Tuya."""
    import colorsys
    named = {
        "rojo": "red", "red": "red", "verde": "green", "green": "green",
        "azul": "blue", "blue": "blue", "amarillo": "yellow", "yellow": "yellow",
        "naranja": "orange", "orange": "orange", "rosa": "pink", "pink": "pink",
        "violeta": "purple", "morado": "purple", "purple": "purple",
        "celeste": "cyan", "cyan": "cyan", "blanco": "white", "white": "white",
    }
    raw = (value or "").strip().lower()
    key = named.get(raw, raw)
    hex_map = {
        "red": "#FF0000", "green": "#00FF00", "blue": "#0000FF",
        "yellow": "#FFFF00", "orange": "#FFA500", "pink": "#FFC0CB",
        "purple": "#800080", "cyan": "#00FFFF", "white": "#FFFFFF",
    }
    hexval = hex_map.get(key, key if key.startswith("#") else "")
    if not hexval.startswith("#"):
        try:
            parts = [int(x) for x in re_split(value)]
            if len(parts) == 3:
                hexval = "#%02X%02X%02X" % tuple(parts)
            else:
                hexval = "#FFFFFF"
        except Exception:
            hexval = "#FFFFFF"
    hexval = hexval.lstrip("#")
    r = int(hexval[0:2], 16); g = int(hexval[2:4], 16); b = int(hexval[4:6], 16)
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    # Tuya colour_data: {"h":0-360,"s":0-1000,"v":0-1000}
    return {"h": int(h * 360), "s": int(s * 1000), "v": int(v * 1000)}


def re_split(value: str):
    import re
    return re.split(r"[,;\s]+", value.strip())


def _send(cloud, device_id, code, value):
    try:
        res = cloud.sendcommand(device_id, {"commands": [{"code": code, "value": value}]})
        if isinstance(res, dict) and res.get("error"):
            return False, str(res.get("error"))
        return True, res
    except Exception as e:
        return False, str(e)


def _value_for(code: str, value, action: str):
    v = value
    if v is None or str(v).strip() == "":
        if code in ("bright_value", "brightness"):
            return 500
        if code in ("temp_set", "color_temp", "temperature"):
            return 4000 if code == "temp_set" else 300
        if code == "mode":
            return "auto"
        if code.startswith("colour"):
            return _parse_color("white")
        return True
    if code in ("bright_value", "brightness"):
        return _scale_brightness(v)
    if code in ("temp_set", "color_temp", "temperature"):
        try:
            return int(str(v).strip())
        except Exception:
            return 4000
    if code == "mode":
        return {"frio": "cool", "calor": "heat", "cool": "cool", "heat": "heat",
                "auto": "auto", "fan": "fan", "dry": "dry"}.get(
                    str(v).lower(), str(v) or "auto")
    if code.startswith("colour"):
        return _parse_color(str(v))
    if isinstance(v, str) and v.lower() in ("true", "false"):
        return v.lower() == "true"
    return v


def _status_text(cloud, device) -> str:
    dev_id = device.get("id")
    name = device.get("name", "dispositivo")
    online = "en línea" if device.get("online") else "fuera de línea"
    try:
        st = cloud.getstatus(dev_id)
        dps = (st or {}).get("result") or st or {}
    except Exception as e:
        return f"{name} ({online}) — no pude leer su estado: {e}"
    flat = {}
    if isinstance(dps, dict):
        flat = dps
    elif isinstance(dps, list):
        for i, item in enumerate(dps):
            if isinstance(item, dict):
                flat.update({f"dps[{i}].{k}": v for k, v in item.items()})
            else:
                flat[f"dps[{i}]"] = item
    if not flat:
        return f"{name} ({online}) — sin estado disponible."
    resumen = ", ".join(f"{k}={v}" for k, v in list(flat.items())[:10])
    return f"{name} ({online}): {resumen}"


def tuya_controller(parameters=None, player=None, speak=None, **kwargs) -> str:
    p = parameters or {}
    device = str(p.get("device") or p.get("name") or "").strip()
    action = str(p.get("action") or "").lower().strip()
    value = p.get("value", "")

    if player:
        player.write_log(f"SYS: 🏠 Tuya: {action} → {device or 'dispositivo por defecto'}")

    try:
        cloud = _get_cloud()

        if action in ("list", "devices", "listar"):
            devices = _get_devices(force=True)
            if not devices:
                return "No encontré dispositivos Tuya vinculados a tu cuenta."
            lines = []
            for d in devices:
                st = "online" if d.get("online") else "offline"
                lines.append(f"- {d.get('name', '(sin nombre)')} [{st}] (id: {d.get('id')})")
            return "Dispositivos Tuya:\n" + "\n".join(lines)

        devices = _get_devices()
        if not devices:
            return "No encontré dispositivos Tuya vinculados a tu cuenta."
        dev = _find_device(devices, device)
        if not dev:
            return "No encontré ese dispositivo en Tuya. Usá action='list' para verlos."
        dev_id = dev.get("id")
        dev_name = dev.get("name", "dispositivo")

        if action in ("status", "estado", "query"):
            return _status_text(cloud, dev)

        cmds = _CANDIDATOS.get(action)
        if not cmds:
            return f"Acción de domótica desconocida: '{action}'. Uso: on/off/brightness/color/color_temp/mode/status/list."

        ultimo_error = ""
        for code, default in cmds:
            v = _value_for(code, value, action)
            ok, res = _send(cloud, dev_id, code, v)
            if ok:
                if speak:
                    speak(f"{dev_name}: listo.")
                return f"Comando '{action}' aplicado a {dev_name}."
            ultimo_error = str(res)
        return f"No pude aplicar '{action}' a {dev_name}. Detalle: {ultimo_error[:200]}"
    except Exception as e:
        return f"Error de domótica Tuya: {e}"

TOOL = {
    "name": 'tuya_control',
    "description": "Controls Tuya / Smart Life smart-home devices (lights, plugs, air conditioners, fans, etc.). Requires tuya_client_id and tuya_secret in config. Use for any request about smart devices: turn on/off, dim, change color, set temperature, or check status. action='list' lists all devices; action='status' reads a device's live state; on/off, brightness (0-100), color (name or hex), color_temp, mode.",
    "parameters": {   'type': 'OBJECT',
        'properties': {   'action': {   'type': 'STRING',
                                        'description': 'on | off | brightness | '
                                                       'color | color_temp | mode '
                                                       '| status | list'},
                          'device': {   'type': 'STRING',
                                        'description': 'Device name (or id) — e.g. '
                                                       "'living room lamp'. Empty "
                                                       'uses the default/first '
                                                       'device.'},
                          'value': {   'type': 'STRING',
                                       'description': 'Value for the action: '
                                                      'brightness 0-100, color '
                                                      'name/hex, temperature, '
                                                      'mode'}},
        'required': ['action']},
    "handler": tuya_controller,
}
