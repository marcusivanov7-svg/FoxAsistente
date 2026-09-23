# -*- coding: utf-8 -*-
"""logging_config.py - Configuración de logging estructurado para Fox.

Reemplaza los print() estándar por un sistema de logs rotativos
que guarda el historial en /logs/fox.log para fácil debugging post-mortem.
"""

import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from pathlib import Path

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

# Crear directorio de logs
LOGS_DIR = get_base_dir() / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOGS_DIR / "fox.log"

def setup_logger(name: str = "Fox") -> logging.Logger:
    """
    Configura y devuelve un logger estructurado con rotación de archivos.
    Conserva hasta 5 archivos de 10 MB cada uno.
    """
    logger = logging.getLogger(name)
    
    # Si ya tiene handlers, no duplicarlos
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # Formato del log: Fecha Hora | Nivel | Archivo:Línea | Mensaje
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(module)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Handler para escribir en archivo rotativo (Máx 10 MB, 5 backups)
    file_handler = RotatingFileHandler(
        filename=LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    # Handler para mostrar en la consola
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)  # En consola solo mostramos INFO o superior
    console_handler.setFormatter(formatter)

    # Añadir handlers al logger
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger

# Instancia global por defecto
logger = setup_logger()
