"""windows_settings — open any Windows settings panel (ported from AMSY).

Maps natural-language names to ms-settings: URIs so "abrí la configuración de
WiFi / pantalla / sonido" opens the right panel, without guessing.
"""
from __future__ import annotations

import subprocess
import sys

_MS_SETTINGS = {
    "display": "ms-settings:display", "pantalla": "ms-settings:display", "screen": "ms-settings:display",
    "wifi": "ms-settings:network-wifi", "network": "ms-settings:network", "red": "ms-settings:network",
    "bluetooth": "ms-settings:bluetooth",
    "sound": "ms-settings:sound", "audio": "ms-settings:sound", "sonido": "ms-settings:sound", "volume": "ms-settings:sound",
    "notifications": "ms-settings:notifications", "notificaciones": "ms-settings:notifications",
    "power": "ms-settings:powersleep", "batería": "ms-settings:powersleep", "bateria": "ms-settings:powersleep", "energía": "ms-settings:powersleep",
    "personalization": "ms-settings:personalization", "personalización": "ms-settings:personalization",
    "themes": "ms-settings:themes", "temas": "ms-settings:themes",
    "colors": "ms-settings:colors", "colores": "ms-settings:colors",
    "background": "ms-settings:personalization-background", "fondo": "ms-settings:personalization-background",
    "lock": "ms-settings:lockscreen", "bloqueo": "ms-settings:lockscreen",
    "about": "ms-settings:about", "acerca": "ms-settings:about",
    "apps": "ms-settings:appsfeatures", "aplicaciones": "ms-settings:appsfeatures",
    "privacy": "ms-settings:privacy", "privacidad": "ms-settings:privacy",
    "update": "ms-settings:windowsupdate", "updates": "ms-settings:windowsupdate", "actualizaciones": "ms-settings:windowsupdate",
    "storage": "ms-settings:storagesense", "almacenamiento": "ms-settings:storagesense",
    "mouse": "ms-settings:mousetouchpad", "touchpad": "ms-settings:devices-touchpad",
    "keyboard": "ms-settings:keyboard", "teclado": "ms-settings:keyboard",
    "accessibility": "ms-settings:easeofaccess", "accesibilidad": "ms-settings:easeofaccess",
    "date": "ms-settings:dateandtime", "time": "ms-settings:dateandtime", "fecha": "ms-settings:dateandtime", "hora": "ms-settings:dateandtime",
    "language": "ms-settings:regionlanguage", "idioma": "ms-settings:regionlanguage",
    "default apps": "ms-settings:defaultapps", "aplicaciones predeterminadas": "ms-settings:defaultapps",
    "taskbar": "ms-settings:taskbar", "barra de tareas": "ms-settings:taskbar",
    "multitasking": "ms-settings:multitasking", "multitarea": "ms-settings:multitasking",
    "recovery": "ms-settings:recovery", "recuperación": "ms-settings:recovery",
    "printers": "ms-settings:printers", "impresoras": "ms-settings:printers",
    "vpn": "ms-settings:network-vpn",
    "proxy": "ms-settings:network-proxy",
    "accounts": "ms-settings:yourinfo", "cuentas": "ms-settings:yourinfo",
    "camera": "ms-settings:privacy-webcam", "cámara": "ms-settings:privacy-webcam",
    "microphone": "ms-settings:privacy-microphone", "micrófono": "ms-settings:privacy-microphone", "mic": "ms-settings:privacy-microphone",
}


def _fuzzy(key: str):
    key = (key or "").strip().lower()
    if not key:
        return None
    if key in _MS_SETTINGS:
        return _MS_SETTINGS[key]
    for k, uri in _MS_SETTINGS.items():
        if k in key or key in k:
            return uri
    return None


def windows_settings(parameters=None, player=None, **kwargs) -> str:
    p = parameters or {}
    action = (p.get("action") or "open").strip().lower()
    what = (p.get("what") or p.get("setting") or p.get("query") or "").strip()

    if action in ("list", "help"):
        return "Puedo abrir: " + ", ".join(sorted(set(_MS_SETTINGS.keys())))

    if sys.platform != "win32":
        return "windows_settings solo funciona en Windows."

    uri = _fuzzy(what)
    if not uri:
        return f"No conozco esa configuración ('{what}'). Pedime action='list' para ver las disponibles."
    try:
        subprocess.Popen(
            ["start", uri],
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return f"Abierta la configuración de {what or 'Windows'}."
    except Exception as e:
        return f"No pude abrir {what}: {e}"

TOOL = {
    "name": 'windows_settings',
    "description": "Abre cualquier panel de configuración de Windows (WiFi, sonido, pantalla, Bluetooth, actualizaciones, accesibilidad, etc.). Usalo para CUALQUIER configuración del sistema. action='list' para ver las disponibles.",
    "parameters": {   'type': 'OBJECT',
        'properties': {   'action': {   'type': 'STRING',
                                        'description': 'open | list'},
                          'what': {   'type': 'STRING',
                                      'description': "Qué configurar (ej: 'wifi', "
                                                     "'sonido', 'pantalla')"}},
        'required': []},
    "handler": windows_settings,
}
