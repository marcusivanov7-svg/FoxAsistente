import sys
import re

content = open("ui.py", encoding="utf-8").read()

# 1. Update _build_tab_conexiones to use core.providers
build_tab_old = """        #  Proveedor activo para tareas delegadas 
        desde = _import_specialist_llm()
        prov_activo = (cfg.get("specialist_provider") or "gemini").lower()
        if prov_activo not in desde.PROVIDERS:
            prov_activo = "gemini"

        c_prov = self._card(lay, "PROVEEDOR PARA TAREAS DELEGADAS")
        plab = QLabel("Proveedor activo:")
        plab.setStyleSheet("color: #c9d1d9; font-size: 11px; background: transparent;")
        c_prov.addWidget(plab)

        self._prov_combo = QComboBox()
        self._prov_combo.setStyleSheet(
            "QComboBox { background: #0d1117; border: 1px solid #30363d; border-radius: 6px;"
            "color: #c9d1d9; padding: 8px 10px; font-size: 12px; }"
            "QComboBox:hover { border-color: #5bc0de; }"
            "QComboBox QAbstractItemView { background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; }"
        )
        self._prov_keys: list[str] = []
        for pid, pinfo in desde.PROVIDERS.items():
            self._prov_combo.addItem(pinfo["name"], pid)
            self._prov_keys.append(pid)
        idx = self._prov_combo.findData(prov_activo)
        if idx < 0: idx = 0"""

build_tab_new = """        #  Proveedor activo para tareas delegadas 
        try:
            from core import providers
            all_provs = providers.list_providers()
        except Exception:
            all_provs = []

        prov_activo = (cfg.get("specialist_provider") or "gemini").lower()
        if not any(p["id"] == prov_activo for p in all_provs):
            prov_activo = "gemini"

        c_prov = self._card(lay, "PROVEEDOR PARA TAREAS DELEGADAS")
        plab = QLabel("Proveedor activo:")
        plab.setStyleSheet("color: #c9d1d9; font-size: 11px; background: transparent;")
        c_prov.addWidget(plab)

        self._prov_combo = QComboBox()
        self._prov_combo.setStyleSheet(
            "QComboBox { background: #0d1117; border: 1px solid #30363d; border-radius: 6px;"
            "color: #c9d1d9; padding: 8px 10px; font-size: 12px; }"
            "QComboBox:hover { border-color: #5bc0de; }"
            "QComboBox QAbstractItemView { background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; }"
        )
        self._prov_keys: list[str] = []
        for pinfo in all_provs:
            self._prov_combo.addItem(pinfo["name"], pinfo["id"])
            self._prov_keys.append(pinfo["id"])
        idx = self._prov_combo.findData(prov_activo)
        if idx < 0: idx = 0"""

content = content.replace(build_tab_old, build_tab_new)

# 2. Update _refresh_specialist_models
refresh_old = """    def _refresh_specialist_models(self, prov: str, current: str):
        self._spec_model_combo.clear()
        try:
            desde = _import_specialist_llm()
            modelos = desde.PROVIDERS.get(prov, {}).get("models", [])
        except Exception:
            modelos = []
        for m in modelos:
            self._spec_model_combo.addItem(m, m)
        if current:
            idx = self._spec_model_combo.findText(current)
            if idx >= 0:
                self._spec_model_combo.setCurrentIndex(idx)"""

refresh_new = """    def _refresh_specialist_models(self, prov: str, current: str):
        self._spec_model_combo.clear()
        modelos = []
        try:
            from core import providers
            all_provs = providers.list_providers()
            for p in all_provs:
                if p["id"] == prov:
                    modelos = p.get("models", [])
                    break
        except Exception:
            pass
        for m in modelos:
            self._spec_model_combo.addItem(m, m)
        if current:
            idx = self._spec_model_combo.findText(current)
            if idx >= 0:
                self._spec_model_combo.setCurrentIndex(idx)"""

content = content.replace(refresh_old, refresh_new)

open("ui.py", "w", encoding="utf-8").write(content)
print("ui.py updated!")
