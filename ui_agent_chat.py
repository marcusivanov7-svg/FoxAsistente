# -*- coding: utf-8 -*-
"""ui_agent_chat.py — Panel de chat estilo DeepSeek Harness para Fox.

Widget autocontenido (no importa `ui.py` para evitar importes circulares).
Muestra:

    - Selector de proveedor / modelo / capacidad de pensamiento / permisos.
    - Botón "Agregar carpeta" + chips de carpetas permitidas.
    - Barra circular de contexto/memoria (tokens usados vs límite).
    - Burbujas de chat con streaming, chips de estado (pensando/ejecutando)
      y bloques de razonamiento colapsados.

Se comunica con el resto de la app mediante señales Qt:

    submit(dict)   → el usuario envía un mensaje con sus parámetros.
    back()         → volver al modo VIVO (el orbe).
    folders_changed(list) → cambió la lista de carpetas permitidas.

Recibe los eventos del motor agente a través de ``on_event(ev)`` (debe llamarse
en el hilo principal de Qt; la app lo puentea con una señal).
"""
from __future__ import annotations

import math
from typing import Any

from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core import providers
from core.permissions import MODE_LABELS, MODES

# Paleta (coincide con el tema por defecto de Fox).
_C = {
    "BG": "#00060a", "PANEL": "#010d14", "PANEL2": "#010f18",
    "BORDER": "#0d3347", "BORDER_B": "#1a5c7a", "PRI": "#00d4ff",
    "PRI_DIM": "#007a99", "TEXT": "#8ffcff", "TEXT_DIM": "#3a8a9a",
    "TEXT_MED": "#5ab8cc", "WHITE": "#d8f8ff", "DARK": "#000d14",
    "GREEN": "#00ff88", "ACC": "#ff6b00", "RED": "#ff3355",
}


def _qss(sel: str, body: str) -> str:
    return f"{sel} {{ {body} }}"


# Límites de contexto aproximados por familia de modelo (para la barra circular).
_CONTEXT_LIMITS: dict[str, int] = {
    "gemini-2.5-pro": 1048576,
    "gemini-2.5": 1048576,
    "gemini-2.0": 1048576,
    "gemini-1.5": 2097152,
    "claude-3-5": 200000,
    "claude-3-7": 200000,
    "claude-sonnet-4": 200000,
    "claude": 200000,
    "gpt-4o": 128000,
    "gpt-4.1": 1047576,
    "o3": 200000,
    "gpt-": 128000,
    "deepseek": 65536,
    "llama-3.3": 128000,
    "llama-3.1": 128000,
    "llama-3.2": 128000,
    "mistral": 128000,
    "qwen": 128000,
    "mixtral": 32768,
    "gemma": 8192,
    "ag-": 128000,
}
_DEFAULT_CONTEXT_LIMIT = 128000


def _context_limit_for(model: str) -> int:
    m = (model or "").lower()
    for key, limit in _CONTEXT_LIMITS.items():
        if key in m:
            return limit
    return _DEFAULT_CONTEXT_LIMIT


class ContextBar(QWidget):
    """Barra circular que muestra cuánto del contexto/memoria se está usando."""

    def __init__(self, parent=None, size: int = 46) -> None:
        super().__init__(parent)
        self._size = size
        self._pct = 0.0
        self._used = 0
        self._limit = 0
        self.setFixedSize(size, size)

    def set_usage(self, used: int | None, limit: int | None) -> None:
        self._used = used or 0
        self._limit = limit or 0
        if self._limit > 0 and self._used is not None:
            self._pct = max(0.0, min(1.0, self._used / self._limit))
        else:
            self._pct = 0.0
        self.update()

    def paintEvent(self, _ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        m = 3
        rect = self.rect().adjusted(m, m, -m, -m)

        # fondo del anillo
        p.setPen(QPen(QColor(_C["BORDER_B"]), 4))
        p.drawArc(rect, 0, 360 * 16)

        # progreso
        if self._pct > 0:
            span = int(-360 * 16 * self._pct)
            p.setPen(QPen(QColor(_C["PRI"]), 4))
            p.drawArc(rect, 90 * 16, span)

        # porcentaje central
        p.setPen(QColor(_C["WHITE"]))
        f = QFont("Courier New", 8, QFont.Weight.Bold)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, f"{int(self._pct * 100)}%")
        p.end()


class _Bubble(QFrame):
    def __init__(self, role: str, parent=None) -> None:
        super().__init__(parent)
        self._role = role
        self.setObjectName("bubble")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(2)
        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._label.setFont(QFont("Segoe UI", 10))
        self._label.setStyleSheet("background: transparent; border: none;")
        lay.addWidget(self._label)
        self._apply_style()

    def _apply_style(self) -> None:
        if self._role == "user":
            self.setStyleSheet(
                f"QFrame#bubble {{ background: {_C['PRI_DIM']}; border: 1px solid {_C['BORDER_B']};"
                f" border-radius: 10px; }}"
                f" QLabel {{ color: {_C['WHITE']}; }}"
            )
        elif self._role == "assistant":
            self.setStyleSheet(
                f"QFrame#bubble {{ background: {_C['PANEL2']}; border: 1px solid {_C['BORDER']};"
                f" border-radius: 10px; }}"
                f" QLabel {{ color: {_C['TEXT']}; }}"
            )
        elif self._role == "status":
            self.setStyleSheet(
                f"QFrame#bubble {{ background: {_C['DARK']}; border: 1px dashed {_C['BORDER_B']};"
                f" border-radius: 8px; }}"
                f" QLabel {{ color: {_C['TEXT_MED']}; }}"
            )
        elif self._role == "tool":
            self.setStyleSheet(
                f"QFrame#bubble {{ background: {_C['DARK']}; border: 1px solid {_C['BORDER']};"
                f" border-radius: 6px; }}"
                f" QLabel {{ color: {_C['TEXT_DIM']}; }}"
            )
        else:  # error
            self.setStyleSheet(
                f"QFrame#bubble {{ background: {_C['DARK']}; border: 1px solid {_C['RED']};"
                f" border-radius: 10px; }}"
                f" QLabel {{ color: {_C['RED']}; }}"
            )

    def set_text(self, text: str) -> None:
        self._label.setText(text)

    def append_text(self, text: str) -> None:
        self._label.setText(self._label.text() + text)


class ChatPanel(QWidget):
    submit = pyqtSignal(dict)
    back = pyqtSignal()
    folders_changed = pyqtSignal(list)
    clear_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background: {_C['BG']};")
        self._folders: list[str] = []
        self._reasoning_buf: list[str] = []
        self._current_reasoning: _Bubble | None = None
        self._current_assistant: _Bubble | None = None
        self._status_bubble: _Bubble | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(8)

        root.addWidget(self._build_header())
        root.addWidget(self._build_controls())
        root.addWidget(self._build_folder_bar())
        root.addWidget(self._build_context_row())
        root.addWidget(self._build_messages(), stretch=1)
        root.addWidget(self._build_input())

    # ── Construcción de sub-bloques ──────────────────────────────────────────
    def _build_header(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        back_btn = QPushButton("◀  VIVO")
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.setStyleSheet(
            _qss("QPushButton", f"color: {_C['PRI']}; background: {_C['PANEL']};"
                 f" border: 1px solid {_C['BORDER_B']}; border-radius: 6px; padding: 6px 10px;")
            + "QPushButton:hover { border-color: #00d4ff; }"
        )
        back_btn.clicked.connect(self.back.emit)
        lay.addWidget(back_btn)

        title = QLabel("MODO AGENTE")
        title.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {_C['PRI']}; background: transparent;")
        lay.addWidget(title)
        lay.addStretch()

        self._status_label = QLabel("")
        self._status_label.setFont(QFont("Courier New", 8))
        self._status_label.setStyleSheet(f"color: {_C['TEXT_DIM']}; background: transparent;")
        lay.addWidget(self._status_label)
        return w

    def _build_controls(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background: {_C['PANEL']}; border: 1px solid {_C['BORDER']}; border-radius: 10px;")
        grid = QHBoxLayout(w)
        grid.setContentsMargins(10, 8, 10, 8)
        grid.setSpacing(8)

        def _lbl(txt):
            l = QLabel(txt)
            l.setFont(QFont("Segoe UI", 8))
            l.setStyleSheet(f"color: {_C['TEXT_DIM']}; background: transparent;")
            return l

        grid.addWidget(_lbl("Proveedor"))
        self._provider = QComboBox()
        for p in providers.list_providers():
            self._provider.addItem(p["name"], p["id"])
        self._provider.currentIndexChanged.connect(self._on_provider_changed)
        grid.addWidget(self._provider)

        grid.addWidget(_lbl("Modelo"))
        self._model = QComboBox()
        self._model.setEditable(True)
        grid.addWidget(self._model, stretch=2)

        grid.addWidget(_lbl("Pensamiento"))
        self._effort = QComboBox()
        for lv in ["off", "low", "medium", "high", "max"]:
            self._effort.addItem(lv, lv)
        grid.addWidget(self._effort)

        grid.addWidget(_lbl("Permisos"))
        self._sandbox = QComboBox()
        for m in MODES:
            self._sandbox.addItem(MODE_LABELS[m], m)
        grid.addWidget(self._sandbox)

        self._apply_combo_style(self._provider)
        self._apply_combo_style(self._model)
        self._apply_combo_style(self._effort)
        self._apply_combo_style(self._sandbox)

        # Valores por defecto desde la config
        self._set_active_defaults()
        return w

    def _build_folder_bar(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        add_btn = QPushButton("📁  Agregar carpeta")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(
            _qss("QPushButton", f"color: {_C['PRI']}; background: {_C['PANEL']};"
                 f" border: 1px solid {_C['BORDER_B']}; border-radius: 6px; padding: 5px 9px;")
            + "QPushButton:hover { border-color: #00d4ff; }"
        )
        add_btn.clicked.connect(self._add_folder)
        lay.addWidget(add_btn)

        self._chips = QHBoxLayout()
        self._chips.setSpacing(5)
        lay.addLayout(self._chips)
        lay.addStretch()

        hint = QLabel("Estas carpetas son las únicas accesibles en modo workspace/lectura.")
        hint.setFont(QFont("Segoe UI", 8))
        hint.setStyleSheet(f"color: {_C['TEXT_DIM']}; background: transparent;")
        lay.addWidget(hint)
        return w

    def _build_context_row(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        self._ctx_bar = ContextBar(size=44)
        lay.addWidget(self._ctx_bar)
        self._ctx_text = QLabel("Contexto: —")
        self._ctx_text.setFont(QFont("Segoe UI", 9))
        self._ctx_text.setStyleSheet(f"color: {_C['TEXT_MED']}; background: transparent;")
        lay.addWidget(self._ctx_text)
        lay.addStretch()

        clear_btn = QPushButton("Limpiar historial")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.setStyleSheet(
            _qss("QPushButton", f"color: {_C['TEXT_DIM']}; background: transparent; border: none;")
            + "QPushButton:hover { color: #00d4ff; }"
        )
        clear_btn.clicked.connect(self._clear)
        lay.addWidget(clear_btn)
        return w

    def _build_messages(self) -> QWidget:
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: {_C['PANEL']}; border: 1px solid {_C['BORDER']};"
            f" border-radius: 10px; }}"
            f" QScrollBar:vertical {{ background: {_C['DARK']}; width: 8px; }}"
            f" QScrollBar::handle:vertical {{ background: {_C['BORDER_B']}; border-radius: 4px; }}"
        )
        self._msg_container = QWidget()
        self._msg_container.setStyleSheet("background: transparent;")
        self._msg_layout = QVBoxLayout(self._msg_container)
        self._msg_layout.setContentsMargins(10, 10, 10, 10)
        self._msg_layout.setSpacing(7)
        self._msg_layout.addStretch()
        self._scroll.setWidget(self._msg_container)
        return self._scroll

    def _build_input(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Escribí tu mensaje… (Enter para enviar)")
        self._input.setStyleSheet(
            f"QLineEdit {{ background: {_C['PANEL']}; border: 1px solid {_C['BORDER_B']};"
            f" border-radius: 8px; color: {_C['WHITE']}; padding: 9px 12px; font-size: 11px; }}"
            "QLineEdit:focus { border-color: #00d4ff; }"
        )
        self._input.returnPressed.connect(self._send)
        lay.addWidget(self._input, stretch=1)

        send_btn = QPushButton("Enviar")
        send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        send_btn.setStyleSheet(
            _qss("QPushButton", f"color: {_C['DARK']}; background: {_C['PRI']};"
                 f" border: none; border-radius: 8px; padding: 9px 16px; font-weight: bold;")
            + "QPushButton:hover { background: #4fdcff; }"
        )
        send_btn.clicked.connect(self._send)
        lay.addWidget(send_btn)
        return w

    def _apply_combo_style(self, combo: QComboBox) -> None:
        combo.setStyleSheet(
            f"QComboBox {{ background: {_C['PANEL2']}; border: 1px solid {_C['BORDER_B']};"
            f" border-radius: 6px; color: {_C['WHITE']}; padding: 5px 8px; font-size: 10px; }}"
            "QComboBox:hover { border-color: #00d4ff; }"
            f"QComboBox QAbstractItemView {{ background: {_C['PANEL2']}; color: {_C['WHITE']};"
            f" border: 1px solid {_C['BORDER_B']}; selection-background-color: {_C['PRI_DIM']}; }}"
        )

    # ── Estado por defecto ───────────────────────────────────────────────────
    def _set_active_defaults(self) -> None:
        try:
            prov = providers.get_active_provider()
            model = providers.get_active_model()
            idx = self._provider.findData(prov)
            if idx >= 0:
                self._provider.setCurrentIndex(idx)
            self._refresh_models(prov, model)
            self._refresh_effort(prov)
        except Exception:
            self._refresh_models(self._provider.currentData() or "deepseek", "")

    def _on_provider_changed(self, _idx) -> None:
        prov = self._provider.currentData() or "deepseek"
        self._refresh_models(prov, "")
        self._refresh_effort(prov)

    def _refresh_models(self, prov: str, current: str) -> None:
        self._model.clear()
        models = providers.PROVIDERS.get(prov, {}).get("models", [])
        if prov == "ollama":
            local = providers.list_local_models()
            models = local or models
        for m in models:
            self._model.addItem(m, m)
        if current:
            i = self._model.findText(current)
            if i >= 0:
                self._model.setCurrentIndex(i)
            else:
                self._model.setEditText(current)

    def _refresh_effort(self, prov: str) -> None:
        self._effort.clear()
        for lv in providers.reasoning_levels(prov):
            self._effort.addItem(lv, lv)

    # ── Carpetas ─────────────────────────────────────────────────────────────
    def _add_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Agregar carpeta al workspace")
        if path and path not in self._folders:
            self._folders.append(path)
            self._rebuild_chips()
            self.folders_changed.emit(list(self._folders))

    def _remove_folder(self, path: str) -> None:
        if path in self._folders:
            self._folders.remove(path)
            self._rebuild_chips()
            self.folders_changed.emit(list(self._folders))

    def _rebuild_chips(self) -> None:
        while self._chips.count():
            item = self._chips.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for path in self._folders:
            chip = QFrame()
            chip.setStyleSheet(
                f"background: {_C['PANEL2']}; border: 1px solid {_C['BORDER_B']}; border-radius: 6px;"
            )
            cl = QHBoxLayout(chip)
            cl.setContentsMargins(6, 3, 6, 3)
            name = path.split("\\")[-1] if "\\" in path else path.split("/")[-1]
            lab = QLabel(name)
            lab.setFont(QFont("Segoe UI", 8))
            lab.setStyleSheet(f"color: {_C['TEXT_MED']}; background: transparent; border: none;")
            lab.setToolTip(path)
            cl.addWidget(lab)
            x = QPushButton("✕")
            x.setFixedSize(16, 16)
            x.setCursor(Qt.CursorShape.PointingHandCursor)
            x.setStyleSheet(
                f"QPushButton {{ color: {_C['TEXT_DIM']}; background: transparent; border: none; }}"
                "QPushButton:hover { color: #ff3355; }"
            )
            x.clicked.connect(lambda _=False, p=path: self._remove_folder(p))
            cl.addWidget(x)
            self._chips.addWidget(chip)

    # ── Envío ────────────────────────────────────────────────────────────────
    def _send(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        self._add_bubble("user", text)
        self.submit.emit({
            "text": text,
            "provider": self._provider.currentData() or "deepseek",
            "model": self._model.currentText().strip(),
            "effort": self._effort.currentData() or "off",
            "sandbox": self._sandbox.currentData() or "full",
            "folders": list(self._folders),
        })

    def _clear(self) -> None:
        self.clear_history()
        self.clear_requested.emit()

    # ── Render de eventos ────────────────────────────────────────────────────
    def clear_history(self) -> None:
        while self._msg_layout.count() > 1:  # el stretch va al final
            item = self._msg_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._current_assistant = None
        self._current_reasoning = None
        self._reasoning_buf = []
        self._clear_status()
        self.set_context(None, None)

    def _add_bubble(self, role: str, text: str = "") -> _Bubble:
        b = _Bubble(role)
        if text:
            b.set_text(text)
        # insertar antes del stretch
        self._msg_layout.insertWidget(self._msg_layout.count() - 1, b)
        self._scroll_to_bottom()
        return b

    def _scroll_to_bottom(self) -> None:
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, lambda: self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum()))

    def _clear_status(self) -> None:
        if self._status_bubble is not None:
            self._status_bubble.setParent(None)
            self._status_bubble.deleteLater()
            self._status_bubble = None
        self._status_label.setText("")

    def _set_status(self, text: str) -> None:
        self._clear_status()
        self._status_bubble = self._add_bubble("status", text)
        self._status_label.setText(text)

    def set_context(self, used: int | None, limit: int | None) -> None:
        if limit is None:
            limit = _context_limit_for(self._model.currentText())
        self._ctx_bar.set_usage(used, limit)
        if used is not None:
            self._ctx_text.setText(f"Contexto: {used:,} / {limit:,} tokens")
        else:
            self._ctx_text.setText("Contexto: —")

    def on_event(self, ev: dict[str, Any]) -> None:
        et = ev.get("type", "")
        if et == "status":
            self._set_status(ev.get("label", ""))
        elif et == "delta":
            if self._current_assistant is None:
                self._clear_status()
                self._current_assistant = self._add_bubble("assistant")
            self._current_assistant.append_text(ev.get("text", ""))
        elif et == "thinking":
            if self._current_reasoning is None:
                self._current_reasoning = self._add_bubble("tool", "🧠 " + ev.get("text", ""))
            else:
                self._current_reasoning.append_text(ev.get("text", ""))
        elif et == "tool_start":
            # Separa el texto previo del texto posterior a la herramienta.
            # El chip de estado (leyendo/escribiendo/ejecutando) lo pone el
            # evento "status" que llega justo después.
            self._current_assistant = None
            self._current_reasoning = None
        elif et == "tool_result":
            r = str(ev.get("result", ""))
            self._add_bubble("tool", f"✓ {ev.get('name', '')}: {r[:120]}")
        elif et == "usage":
            u = ev.get("usage") or {}
            self.set_context(u.get("total_tokens"), None)
        elif et == "done":
            content = ev.get("content", "")
            if content and self._current_assistant is None:
                self._add_bubble("assistant", content)
            self._current_assistant = None
            self._current_reasoning = None
            self._reasoning_buf = []
            self._clear_status()
            u = ev.get("usage") or {}
            if u.get("total_tokens") is not None:
                self.set_context(u.get("total_tokens"), None)
        elif et == "error":
            self._clear_status()
            self._add_bubble("error", f"⚠ {ev.get('error', 'Error desconocido')}")
