# Fox Asistente — Modo Agente Web 

**¡Bienvenido!** Este archivo explica los cambios que hice para transformar tu Modo Agente en una interfaz web moderna, minimalista y con todas las funcionalidades de DeepSeek Harness, manteniendo la marca Fox.

---

## 📋 RESUMEN DE CAMBIOS

| Archivo | Cambio | Motivo |
|---------|--------|--------|
| `agent_web/static/index.html` | Nuevo HTML con sidebar, topbar, chat area, todo drawer | Copia directa del estilo DeepSeek Harness, adaptado a colores Fox | 
| `agent_web/static/style.css` | Tema oscuro Fox (`--accent: #ff6b00`) | Mantener identidad visual de Fox | 
| `agent_web/static/app.js` | Lógica cliente completa (WebSocket, streaming, tool cards, context ring) | Reemplaza la lógica anterior por la de DeepSeek Harness | 
| `agent_web/server.py` | FastAPI + WebSocket con AgentEngine compartido | Mismo backend que DeepSeek Harness | 
| `ui_agent_chat.py` | ChatPanel PyQt que muestra la web UI embebida | Integración fluida entre Modo Voz y Modo Agente | 
| `ui.py` | Integración completa: botón AGENTE, overlay, bridge de eventos | Comunicación Local ↔ Web | 
| `main.py` | EventBus para comunicación Local ↔ Web, servidor web integrado | Arquitectura limpia, mismo proceso | 
| `requirements.txt` | Nuevas dependencias: PyQt6-WebEngine, websockets, pydantic | Necesario para la nueva interfaz web | 

---

## 🚀 INSTALACIÓN Y EJECUCIÓN

```bash
# 1. Instalar dependencias nuevas
pip install -r requirements.txt

# 2. Ejecutar Fox Asistente (incluye ahora el servidor web del Modo Agente)
python main.py

# 3. Al arrancar, verás la ventana principal de Fox
#    - Haz clic en el botón "🤖 AGENTE" para abrir el Modo Agente web
#    - O usa el atajo: Alt+A (o Ctrl+M en Windows)
```

---

## 🎨 NUEVA INTERFAZ VISUAL

### Características principales:
- **Sidebar izquierdo** con configuración, sesiones, proveedor, modelo, permisos, carpetas
- **Top bar** con título "Fox · Modo Agente" y modo chip (full/workspace/read_only)
- **Context meter circular** mostrando % de tokens usados
- **Chat area** con burbujas, streaming cursor, thinking blocks, tool cards
- **Todo drawer** para tareas derivadas de herramientas
- **Plan mode** con aprobación/rechazo de planes
- **Feedback visual** 👍👎 después de cada respuesta
- **Dark theme Fox** con colores naranja `#ff6b00` como acento

### Atajos de teclado:
- `Ctrl+Enter` → Enviar mensaje
- `Shift+Enter` → Nueva línea
- `Alt+A` o `Ctrl+M` → Alternar Modo Agente
- `Ctrl+F` → Buscar en historial (cuando el panel de historial está abierto)
- `Esc` → Cerrar overlays (plan, todo, contexto)

---

## 🔧 FUNCIONALIDADES DEL MODO AGENTE

### 1. Multi-proveedor
- Gemini, OpenRouter, DeepSeek, Groq, Ollama, Anthropic, OpenAI
- Formulario para agregar proveedores OpenAI-compatibles personalizados
- Guardado automático de API keys en `config/api_keys.json`

### 2. Sesiones Persistentes
- Guarda conversaciones automáticamente
- Lista en sidebar con fecha y conteo de mensajes
- Cargar/restaurar cualquier sesión anterior
- Borrar sesiones no deseadas

### 3. Plan Mode
- Genera plan paso a paso automáticamente
- Espera aprobación del usuario antes de ejecutar
- Ideal para tareas complejas

### 4. Tool Cards
- Cada herramienta ejecutada muestra un card con estado (running/done/error)
- Output limitado a 2000 caracteres para evitar UI clutter
- Click para expandir/ver completo

### 5. Context Management
- Barra circular mostrando % de tokens usados vs límite del modelo
- Detalles expandibles: tokens de entrada, salida, cache
- Compactación automática de contexto cuando se llena

### 6. Workspace Folders
- Permitir solo carpetas específicas en modo workspace/read_only
- Chips visuales para carpetas agregadas
- Seguridad real: PathGuard valida rutas antes de ejecutar herramientas

### 7. Subagentes Paralelos
- Ejecutar tareas independientes mientras la conversación principal continúa
- Histórico separado para cada subagente
- Ideal para research, scraping, batch operations

---

## 🔄 PUENTE LOCAL ↔ WEB

El Modo Agente web puede controlar la app local:

| Comando | Acción | Uso en Web UI |
|---------|--------|---------------|
| `show_overlay` | Mostrar/ocultar el orbe del Modo Voz | Botón "Volver a Voz" en el header web |
| `speak_tts` | TTS para respuesta del agente | Botón 🎤 en la interfaz web |
| `open_app` | Abrir aplicación en PC | Botón 📁 (seleccionar carpeta) |
| `file_action` | Ejecutar acción de archivo | Botón 📁 + seleccionar carpeta |
| `voice_mode` | Cambiar configuración de voz | Acceso a settings desde Modo Voz |

Todo esto se hace mediante WebSocket (`/ws/agent`) — **no hay red externa**.

---

## 📁 ARCHIVOS MODIFICADOS

```bash
# Archivos principales cambiados (revisa los diffs completos en el repo)
agent_web/static/index.html    # Nueva interfaz HTML
agent_web/static/style.css       # Tema oscuro Fox
agent_web/static/app.js          # Lógica cliente completa
agent_web/server.py              # FastAPI + WebSocket
ui_agent_chat.py                 # Panel PyQt para mostrar la web UI
ui.py                            # Integración Modo Agente en la UI principal
main.py                          # EventBus y servidor web integrado
requirements.txt                 # Nuevas dependencias
```

---

## 🐛 SOLUCIÓN DE PROBLEMAS COMUNES

### 1. La web UI no carga (servidor no listo)
- Espera 5-10 segundos al arrancar la app
- Verifica que `python main.py` se esté ejecutando (ver consola)
- Si el puerto 8765 está ocupado, el servidor reintenta automáticamente
- Puedes abrir manualmente: `http://127.0.0.1:8765/` en el navegador

### 2. WebEngine no disponible (PyQt6-WebEngine no instalado)
- `pip install PyQt6-WebEngine==6.7.0`
- Si aún no funciona, el sistema caerá al panel PyQt (fallback)

### 3. Las herramientas no funcionan en Modo Agente
- Verifica que el `tool_executor` en `main.py` está usando el mismo dispatcher que el Modo Voz
- Revisa `FOX_EVENT_BUS` y las suscripciones en `start_agent_web_server()`

### 4. Las sesiones no se guardan
- Verifica permisos de escritura en `memory/agent_sessions.json`
- Revisa el log en consola para errores de escritura

---

## 🎯 PRÓXIMOS PASOS (Opcional)

1. **Multimodal**: Agregar adjuntar imágenes en el chat web (ya implementado en `app.js`)
2. **Exportar conversaciones**: Botón para descargar MD/JSON
3. **Búsqueda en historial**: Ctrl+F para encontrar mensajes anteriores
4. **Personalización**: Guardar tema claro/oscuro, tamaño fuente
5. **Notificaciones**: Toast notifications para eventos asíncronos

---

## 📞 CONTACTO / AYUDA

- Repositorio: `https://github.com/tu-usuario/fox_asistente_v3`
- Si necesitas ayuda para integrar herramientas específicas, avísame qué acciones quieres exponer en el Modo Agente
- Para contribuir: envía PRs con los archivos modificados listados arriba

¡Disfruta del nuevo Modo Agente Fox! 🚀
