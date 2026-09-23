/* ══════════════════════════════════════════════════════════════════
   fox · Agent — cliente (SPA) con interactividad y manejo de estado
   Vanilla JavaScript ES6+ — sin frameworks
   ══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  // ═══════════════════ 1. ESTADO GLOBAL ═══════════════════
  const appState = {
    currentTab: "chat",            // "chat" | "trajectory"
    workspace: "fox_asistente_v3",
    mode: "Standard mode",
    model: "auto",
    permissions: "workspace", // "read_only" | "workspace" | "full"
    effort: "Low",
    fontSize: 14,
    // backend
    ws: null,
    connected: false,
    providers: [],
    reasoningLevels: [],
    sessionId: null,
    sessions: [],
    busy: false,
    planMode: false,
    // render en curso
    currentAssistant: null,
    currentThink: null,
    thinkStart: 0,
    tools: {},
    metrics: { turns: 0, steps: 0, speed: 0, tokens: 0, cacheHit: 0 },
  };

  const $ = (id) => document.getElementById(id);

  // ═══════════════════ CONSTANTES DE UI ═══════════════════
  const PERMISSIONS = [
    { id: "read_only", label: "👁 Read Only" },
    { id: "workspace", label: "🔒 Workspace Write" },
    { id: "full", label: "🛡 Full access" },
  ];
  const MODES = ["Standard mode", "PTC mode", "Minimal mode", "Creator mode"];
  const WORKSPACES = ["fox_asistente_v3", "fox_asistente_v2", "Ungrouped"];

  // ═══════════════════ HELPERS ═══════════════════
  function esc(s) {
    return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function md(text) {
    let t = esc(text);
    t = t.replace(/```([\s\S]*?)```/g, (_, c) => `<pre><code>${c.trim()}</code></pre>`);
    t = t.replace(/`([^`]+)`/g, "<code>$1</code>");
    t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    t = t.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
    t = t.replace(/(^|\n)([-*] )(.+)/g, "$1<ul><li>$3</li></ul>");
    t = t.split(/\n{2,}/).map((p) =>
      p.includes("<ul>") || p.includes("<pre>")
        ? p.replace(/\n/g, "<br>")
        : `<p>${p.replace(/\n/g, "<br>")}</p>`
    ).join("");
    return t;
  }
  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (tag === 'button') e.type = 'button';
    if (cls) e.className = cls;
    if (html !== undefined) e.innerHTML = html;
    return e;
  }

  const messages = $("messages");
  const trajectory = $("trajectory");

  function hideEmpty() { const e = $("empty-state"); if (e) e.remove(); }
  let msgScrollAF = null;
  function scrollBottom() {
    if (msgScrollAF) return;
    msgScrollAF = requestAnimationFrame(() => {
      messages.scrollTop = messages.scrollHeight;
      msgScrollAF = null;
    });
  }

  // ═══════════════════ 2. DROPDOWNS (genérico + click outside) ═══════════════════
  function closeAllDropdowns() {
    document.querySelectorAll(".dd-menu, .dyn-menu").forEach((m) => m.classList.add("hidden"));
  }

  // Click outside: cierra todos los dropdowns cuando se hace clic fuera de un .dropdown
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".dropdown")) closeAllDropdowns();
  });

  // Crea un menú flotante dinámico (para workspace / mode) y lo muestra bajo el botón
  function openDynamicMenu(anchor, items, current, onSelect) {
    closeAllDropdowns();
    const menu = el("div", "dd-menu dyn-menu");
    items.forEach((item) => {
      const label = typeof item === "string" ? item : item.label;
      const val = typeof item === "string" ? item : item.id;
      const b = el("button", val === current ? "active" : "");
      b.textContent = label;
      b.onclick = (e) => {
        e.stopPropagation();
        onSelect(val, label);
        menu.remove();
      };
      menu.appendChild(b);
    });
    anchor.parentElement.style.position = "relative";
    anchor.parentElement.appendChild(menu);
  }

  function toggleDropdown(ddId) {
    const dd = $(ddId);
    const menu = dd.querySelector(".dd-menu");
    const wasHidden = menu.classList.contains("hidden");
    closeAllDropdowns();
    if (wasHidden) menu.classList.remove("hidden");
  }

  // ═══════════════════ A. NAVEGACIÓN (Tabs) ═══════════════════
  function switchTab(tab) {
    appState.currentTab = tab;
    document.querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.tab === tab);
    });
    $("chat-view").classList.toggle("active", tab === "chat");
    $("trajectory-view").classList.toggle("active", tab === "trajectory");
  }

  // ═══════════════════ B. MODAL DE CONFIGURACIÓN ═══════════════════
  function openModal() { $("settings-modal").classList.remove("hidden"); }
  function closeModal() { $("settings-modal").classList.add("hidden"); }

  const SETTING_TITLES = { general: "General", models: "Models", plugins: "Plugins", presets: "Agent presets" };
  function switchSetting(setting) {
    document.querySelectorAll(".modal-tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.setting === setting);
    });
    document.querySelectorAll(".setting-panel").forEach((p) => {
      p.classList.toggle("hidden", p.dataset.setting !== setting);
    });
    const head = $("settings-modal").querySelector(".modal-head span");
    if (head) head.textContent = SETTING_TITLES[setting] || "General";
  }

  // ═══════════════════ D. NUEVA SESIÓN ═══════════════════
  function resetToEmpty() {
    messages.querySelectorAll(".msg, .tool, .status-chip").forEach((m) => m.remove());
    appState.currentAssistant = null;
    appState.currentThink = null;
    appState.tools = {};
    if (!messages.querySelector(".empty-state")) {
      const empty = el("div", "empty-state");
      empty.id = "empty-state";
      empty.innerHTML =
        `<h2>🦊 Into the Unknown <span class="preview">[Preview]</span></h2>` +
        `<div class="empty-selectors">` +
        `<button class="pill-btn" id="empty-workspace">📁 ${appState.workspace} ⌵</button>` +
        `<button class="pill-btn" id="empty-mode">⚙ ${appState.mode} ⌵</button>` +
        `</div>`;
      messages.appendChild(empty);
      bindEmptySelectors();
    }
    appState.metrics = { turns: 0, steps: 0, speed: 0, tokens: 0, cacheHit: 0 };
    renderTelemetry();
  }

  function bindEmptySelectors() {
    const ew = $("empty-workspace");
    const em = $("empty-mode");
    if (ew) ew.onclick = (e) => {
      e.stopPropagation();
      const items = WORKSPACES.slice().concat([{ id: "__add__", label: "＋ Add workspace…" }]);
      openDynamicMenu(ew, items, appState.workspace, (val) => {
        if (val === "__add__") { pickWorkspace(); return; }
        appState.workspace = val;
        ew.textContent = `📁 ${val} ⌵`;
        renderSessions(appState.sessions, appState.sessionId);
      });
    };
    if (em) em.onclick = (e) => {
      e.stopPropagation();
      openDynamicMenu(em, MODES, appState.mode, (val) => {
        appState.mode = val;
        em.textContent = `⚙ ${val} ⌵`;
        $("mode-badge").textContent = `⚙ ${val}`;
      });
    };
  }

  // ═══════════════════ TELEMETRÍA ═══════════════════
  function renderTelemetry() {
    const m = appState.metrics;
    $("telemetry").textContent =
      `${m.turns} turns · ${m.steps} steps · ${m.speed} tok/s · ${m.tokens} tok · Cache hit ${m.cacheHit}%`;
  }

  let trajScrollAF = null;
  function trajNode(kind, text, cls = "") {
    const t = new Date().toLocaleTimeString("es", { hour12: false });
    const node = el("div", `traj-node ${cls}`);
    node.appendChild(el("span", "t-time", t));
    node.appendChild(el("span", "t-kind", kind));
    node.appendChild(el("span", "", text));
    trajectory.appendChild(node);
    if (!trajScrollAF) {
      trajScrollAF = requestAnimationFrame(() => {
        trajectory.scrollTop = trajectory.scrollHeight;
        trajScrollAF = null;
      });
    }
    const empty = trajectory.querySelector(".traj-empty");
    if (empty) empty.remove();
  }

  // ═══════════════════ RENDER DE MENSAJES ═══════════════════
  function appendUser(text) {
    hideEmpty();
    const m = el("div", "msg user");
    const b = el("div", "bubble", md(text));
    m.appendChild(b);
    const cp = el("button", "", "⧉");
    cp.title = "Copiar";
    cp.style.cssText = "opacity:.4;font-size:11px;align-self:flex-end;margin-top:3px;";
    cp.onclick = () => navigator.clipboard?.writeText(text);
    m.appendChild(cp);
    messages.appendChild(m);
    scrollBottom();
  }

  function ensureAssistant() {
    if (appState.currentAssistant) return appState.currentAssistant;
    hideEmpty();
    const m = el("div", "msg assistant");
    const b = el("div", "bubble");
    b.appendChild(el("div", "content"));
    m.appendChild(b);
    m.appendChild(el("div", "feedback"));
    messages.appendChild(m);
    appState.currentAssistant = b;
    return b;
  }

  function appendAssistantText(t) {
    const b = ensureAssistant();
    const c = b.querySelector(".content");
    c.dataset.raw = (c.dataset.raw || "") + t;
    c.innerHTML = md(c.dataset.raw);
    scrollBottom();
  }

  function ensureThink() {
    if (appState.currentThink) return appState.currentThink;
    appState.thinkStart = Date.now();
    const det = el("details", "think");
    det.open = false;
    det.innerHTML = `<summary><span class="think-label">Think</span><span class="think-time"></span></summary><div class="think-body"></div>`;
    const m = ensureAssistant().closest(".msg");
    m.insertBefore(det, m.querySelector(".feedback"));
    appState.currentThink = det;
    return det;
  }

  function appendThink(t) {
    const d = ensureThink();
    d.querySelector(".think-body").textContent += t;
    const secs = Math.round((Date.now() - appState.thinkStart) / 1000);
    d.querySelector(".think-time").textContent = `Deep diving... ${Math.floor(secs / 60)}m ${secs % 60}s`;
    scrollBottom();
  }

  function startTool(name, args) {
    hideEmpty();
    appState.currentAssistant = null;
    appState.currentThink = null;
    const argTxt = args && Object.keys(args).length ? JSON.stringify(args).slice(0, 80) : "";
    const det = el("details", "tool running");
    det.innerHTML =
      `<summary><span class="tool-verb">[ejecución]</span>` +
      `<span class="tool-name">${esc(name)}</span>` +
      `<span class="tool-args">${esc(argTxt)}</span>` +
      `<span class="tool-status">running…</span></summary>` +
      `<div class="tool-body"></div>`;
    messages.appendChild(det);
    appState.tools[name] = { el: det, body: det.querySelector(".tool-body") };
    trajNode("tool", `${name} ${argTxt}`, "tool");
    scrollBottom();
  }

  function finishTool(name, result, ok) {
    const t = appState.tools[name];
    if (!t) return;
    t.el.classList.remove("running");
    t.el.classList.add(ok === false ? "error" : "done");
    t.el.querySelector(".tool-status").textContent = ok === false ? "error" : "ok ✓";
    if (result !== undefined && result !== null) t.body.textContent = String(result).slice(0, 3000);
    delete appState.tools[name];
    scrollBottom();
  }

  function finishTurn(rawContent) {
    if (appState.currentAssistant) {
      const msg = appState.currentAssistant.closest(".msg");
      const fb = msg.querySelector(".feedback");
      const raw = appState.currentAssistant.querySelector(".content").dataset.raw || rawContent || "";
      fb.innerHTML = "";
      [["👍", "up"], ["👎", "down"]].forEach(([glyph, rating]) => {
        const b = el("button", "", glyph);
        b.onclick = () => {
          b.classList.add("voted");
          if (appState.ws) appState.ws.send(JSON.stringify({ action: "feedback", rating, content: raw, session_id: appState.sessionId }));
        };
        fb.appendChild(b);
      });
    }
    appState.currentAssistant = null;
    appState.currentThink = null;
    appState.tools = {};
    appState.busy = false;
    updateSendBtn();
  }

  function setStatus(text) {
    let chip = messages.querySelector(".status-chip.current");
    if (!text) { if (chip) chip.remove(); return; }
    if (!chip) {
      chip = el("div", "status-chip current");
      chip.innerHTML = '<span class="spinner"></span><span class="txt"></span>';
      messages.appendChild(chip);
    }
    chip.querySelector(".txt").textContent = text;
    scrollBottom();
  }

  // ═══════════════════ EVENTOS DEL BACKEND ═══════════════════
  function onEvent(ev) {
    switch (ev.type) {
      case "status": setStatus(ev.label || ""); break;
      case "delta": appendAssistantText(ev.text || ""); break;
      case "thinking": appendThink(ev.text || ""); break;
      case "tool_start": startTool(ev.name, ev.arguments); appState.metrics.steps++; renderTelemetry(); break;
      case "tool_result": finishTool(ev.name, ev.result, ev.error !== true); break;
      case "usage": {
        const u = ev.usage || {};
        appState.metrics.tokens = u.total_tokens ?? u.totalTokenCount ?? appState.metrics.tokens;
        appState.metrics.cacheHit = Math.round(((u.cached_tokens ?? 0) / Math.max(1, appState.metrics.tokens)) * 100);
        renderTelemetry();
        break;
      }
      case "done":
        if (ev.content && !appState.currentAssistant) { ensureAssistant(); appendAssistantText(ev.content); }
        finishTurn(ev.content);
        appState.metrics.turns++;
        appState.metrics.steps = 0;
        setStatus("");
        renderTelemetry();
        break;
      case "error": 
        finishTurn(""); 
        setStatus("❌ " + (ev.error || "Error")); 
        trajNode("error", ev.error || "Error", "error"); 
        
        const errMsg = el("div", "msg assistant");
        const errBub = el("div", "bubble");
        errBub.style.backgroundColor = "rgba(255, 0, 0, 0.1)";
        errBub.style.color = "#ff6b6b";
        errBub.textContent = "⚠️ Error del proveedor: " + (ev.error || "Error desconocido");
        errMsg.appendChild(errBub);
        messages.appendChild(errMsg);
        scrollBottom();
        break;
      case "plan":
        hideEmpty();
        const pm = el("div", "msg assistant");
        pm.appendChild(el("div", "bubble", md("📋 **Plan propuesto:**\n\n" + (ev.plan || ""))));
        messages.appendChild(pm);
        showPlanBanner();
        appState.busy = false; updateSendBtn();
        break;
      case "plan_approved": hidePlanBanner(); break;
      case "plan_rejected": hidePlanBanner(); setStatus("Plan rechazado."); break;
      case "session_new":
        appState.sessionId = ev.session_id;
        $("task-title").value = "Nueva sesión";
        renderSessions(ev.sessions || [], ev.session_id);
        resetToEmpty();
        break;
      case "history":
        appState.sessionId = ev.session_id;
        renderHistory(ev.messages || []);
        break;
      case "sessions": renderSessions(ev.sessions || [], ev.active); break;
      case "session_saved":
        appState.sessionId = ev.session_id;
        renderSessions(ev.sessions || [], ev.session_id);
        break;
    }
  }

  function renderHistory(msgs) {
    resetToEmpty();
    (msgs || []).forEach((m) => {
      if (!m || !m.role) return;
      if (m.role === "user") appendUser(m.content || "");
      else if (m.role === "assistant" && m.content) {
        const w = el("div", "msg assistant");
        const b = el("div", "bubble");
        b.appendChild(el("div", "content", md(m.content)));
        w.appendChild(b);
        messages.appendChild(w);
      } else if (m.role === "tool") {
        const det = el("details", "tool done");
        det.innerHTML = `<summary><span class="tool-verb">[ejecución]</span><span class="tool-name">${esc(m.name || "tool")}</span><span class="tool-status">ok ✓</span></summary><div class="tool-body">${esc(String(m.content || "").slice(0, 1500))}</div>`;
        messages.appendChild(det);
      }
    });
    scrollBottom();
  }

  // ═══════════════════ SESIONES (sidebar) ═══════════════════
  function relTime(ts) {
    if (!ts) return "";
    const d = Math.max(0, (Date.now() / 1000) - ts);
    if (d < 60) return "1min";
    if (d < 3600) return `${Math.floor(d / 60)}min`;
    if (d < 86400) return `${Math.floor(d / 3600)}h`;
    return `${Math.floor(d / 86400)}d`;
  }

  function renderSessions(list, activeId) {
    appState.sessions = list || [];
    const tree = $("ws-tree");
    tree.innerHTML = "";
    const ws = el("div", "ws-folder open");
    ws.innerHTML = `<div class="ws-folder-head"><span class="chev">▶</span><span>📁</span><span class="ws-name">${appState.workspace}</span></div>`;
    const children = el("div", "ws-children");
    appState.sessions.slice(0, 20).forEach((s) => {
      const li = el("div", "session-item" + (s.id === activeId ? " active" : ""));
      const title = el("input", "s-title");
      title.value = s.title || "Conversación";
      title.onkeydown = (e) => { if (e.key === "Enter") title.blur(); };
      li.appendChild(title);
      li.appendChild(el("span", "s-time", relTime(s.updated_at)));
      const del = el("button", "s-del", "✕");
      del.onclick = (e) => {
        e.stopPropagation();
        if (appState.ws) appState.ws.send(JSON.stringify({ action: "delete", session_id: s.id }));
      };
      li.appendChild(del);
      li.onclick = () => { if (appState.ws) appState.ws.send(JSON.stringify({ action: "load", session_id: s.id })); };
      children.appendChild(li);
    });
    if (appState.sessions.length > 20) {
      const more = el("button", "ghost-btn full", `Show more sessions (${appState.sessions.length - 20})`);
      children.appendChild(more);
    }
    ws.appendChild(children);
    tree.appendChild(ws);
  }

  // ═══════════════════ META (proveedores/modelos) ═══════════════════
  async function loadMeta() {
    try {
      const res = await fetch("/api/meta");
      const meta = await res.json();
      appState.providers = meta.providers || [];
      appState.reasoningLevels = meta.reasoning_levels || ["off", "low", "medium", "high", "max"];

      const provId = (meta.active && meta.active.provider) || "deepseek";
      const prov = appState.providers.find((p) => p.id === provId);
      const models = prov ? prov.models : [];
      appState.model = (meta.active && meta.active.model) || models[0] || appState.model;

      renderModelList(models);
      renderProviders();

      const es = $("effort-select");
      if (es) {
        es.innerHTML = "";
        appState.reasoningLevels.forEach((lv) => {
          const o = document.createElement("option");
          o.value = lv; o.textContent = lv.charAt(0).toUpperCase() + lv.slice(1);
          es.appendChild(o);
        });
        es.value = (appState.effort || "low").toLowerCase();
      }
      updateModelBtn();
    } catch (e) {
      console.error("loadMeta:", e);
    }
  }

  function renderModelList(models) {
    const list = $("model-list");
    if (!list) return;
    list.innerHTML = "";
    const items = [{ id: "auto", label: "Automático" }];
    (models || []).forEach((m) => items.push({ id: m, label: m }));
    items.forEach((it) => {
      const b = el("button", "model-item" + (appState.model === it.id ? " active" : ""));
      b.textContent = it.label;
      b.onclick = (e) => {
        e.stopPropagation();
        appState.model = it.id;
        updateModelBtn();
        closeAllDropdowns();
        list.querySelectorAll(".model-item").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
      };
      list.appendChild(b);
    });
  }

  function renderProviders() {
    const list = $("provider-list");
    if (!list) return;
    list.innerHTML = "";
    (appState.providers || []).forEach((p) => {
      const block = el("div", "provider-block");
      const head = el("div", "pb-row");
      const name = el("span", "", p.name);
      name.style.cssText = "flex:1;font-weight:600;color:var(--text);font-size:13px;";
      head.appendChild(name);
      const status = el("span", "", p.has_key ? "✓ key" : (p.needs_api_key ? "needs key" : "local"));
      status.style.cssText = "font-size:10px;color:var(--text-dim);border:1px solid var(--border);border-radius:4px;padding:1px 6px;";
      head.appendChild(status);
      if (p.is_custom) {
        const del = el("button", "pb-delete", "Borrar");
        del.onclick = async () => {
          try {
            await fetch("/api/provider/" + encodeURIComponent(p.id), { method: "DELETE" });
            await loadMeta();
          } catch (e) { console.error(e); }
        };
        head.appendChild(del);
      }
      block.appendChild(head);
      const models = el("div", "", "Modelos: " + ((p.models || []).length ? p.models.join(", ") : "—"));
      models.style.cssText = "color:var(--text-dim);font-size:11.5px;";
      block.appendChild(models);
      if (p.needs_api_key && !p.has_key) {
        const row = el("div", "pb-row");
        const inp = el("input", "input");
        inp.type = "password";
        inp.placeholder = "API key";
        const apply = el("button", "pb-apply", "Guardar key");
        apply.onclick = async () => {
          const k = inp.value.trim();
          if (!k) return;
          try {
            const r = await fetch("/api/key", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ provider: p.id, api_key: k }),
            });
            const d = await r.json();
            if (d.ok) { appState.providers = d.providers || appState.providers; renderProviders(); }
            else alert(d.error || "No se pudo guardar");
          } catch (e) { console.error(e); }
        };
        row.appendChild(inp); row.appendChild(apply);
        block.appendChild(row);
      }
      list.appendChild(block);
    });
  }

  function addProviderBlock(custom) {
    const list = $("provider-list");
    if (!list) return;
    const block = el("div", "provider-block");
    const actions = el("div", "pb-actions");
    const cancel = el("button", "pb-cancel", "Cancelar");
    cancel.onclick = () => block.remove();
    actions.appendChild(cancel);

    if (!custom) {
      const row = el("div", "pb-row");
      const sel = el("select", "select");
      (appState.providers || []).forEach((p) => {
        const o = document.createElement("option");
        o.value = p.id; o.textContent = p.name;
        sel.appendChild(o);
      });
      const inp = el("input", "input");
      inp.type = "password"; inp.placeholder = "API key";
      const apply = el("button", "pb-apply", "Guardar");
      apply.onclick = async () => {
        const k = inp.value.trim();
        if (!k) return;
        try {
          const r = await fetch("/api/key", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ provider: sel.value, api_key: k }),
          });
          const d = await r.json();
          if (d.ok) { appState.providers = d.providers || appState.providers; renderProviders(); }
          else alert(d.error || "No se pudo guardar");
        } catch (e) { console.error(e); }
      };
      actions.prepend(apply);
      row.appendChild(sel); row.appendChild(inp);
      block.appendChild(row);
    } else {
      const name = el("input", "input");
      name.placeholder = "Nombre";
      const url = el("input", "input");
      url.placeholder = "Base URL (OpenAI-compatible)";
      const key = el("input", "input");
      key.type = "password"; key.placeholder = "API key (opcional)";
      const apply = el("button", "pb-apply", "Agregar");
      apply.onclick = async () => {
        const n = name.value.trim(), u = url.value.trim();
        if (!n || !u) { alert("Nombre y URL son obligatorios"); return; }
        try {
          const r = await fetch("/api/provider", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: n, url: u, api_key: key.value.trim() }),
          });
          const d = await r.json();
          if (d.ok) { appState.providers = d.providers || appState.providers; renderProviders(); }
          else alert(d.error || "No se pudo agregar");
        } catch (e) { console.error(e); }
      };
      actions.prepend(apply);
      const r1 = el("div", "pb-row"); r1.appendChild(name);
      const r2 = el("div", "pb-row"); r2.appendChild(url);
      const r3 = el("div", "pb-row"); r3.appendChild(key);
      block.appendChild(r1); block.appendChild(r2); block.appendChild(r3);
    }

    block.appendChild(actions);
    list.prepend(block);
  }

  function pickWorkspace() {
    const picker = $("workspace-folder-picker");
    if (picker) picker.click();
  }

  function updateModelBtn() {
    const m = (appState.model === "auto" || !appState.model)
      ? "Automático"
      : appState.model.split("/").pop();
    const e = (appState.effort || "Low").charAt(0).toUpperCase() + (appState.effort || "Low").slice(1);
    const btn = $("model-btn");
    if (btn) btn.textContent = `🤖 ${m} · ${e} ⌵`;
  }

  function updatePermBtn() {
    const p = PERMISSIONS.find((x) => x.id === appState.permissions) || PERMISSIONS[1];
    $("permission-btn").textContent = `${p.label} ⌵`;
  }

  // ═══════════════════ SLASH COMMANDS ═══════════════════
  const SLASH = [
    { cmd: "/compact", desc: "Compactar historial de conversación antiguo" },
    { cmd: "/export", desc: "Descargar logs de la sesión como archivo" },
    { cmd: "/feedback", desc: "Registrar feedback de la sesión" },
    { cmd: "/goal", desc: "Definir o ver la meta de tareas de larga duración" },
    { cmd: "/permission", desc: "Cambiar política de permisos" },
    { cmd: "/plan", desc: "Entrar o salir de modo planificación" },
    { cmd: "/model", desc: "Cambiar modelo en caliente" },
  ];
  function showSlash() {
    const menu = $("slash-menu");
    menu.innerHTML = "";
    SLASH.forEach((s) => {
      const b = el("button", "slash-item");
      b.innerHTML = `<span class="cmd">${s.cmd}</span><span class="desc">${s.desc}</span>`;
      b.onclick = () => runSlash(s.cmd);
      menu.appendChild(b);
    });
    menu.classList.remove("hidden");
  }
  function hideSlash() { $("slash-menu").classList.add("hidden"); }
  function runSlash(cmd) {
    hideSlash();
    const p = $("prompt");
    switch (cmd) {
      case "/compact":
        if (appState.ws) appState.ws.send(JSON.stringify({ action: "clear" }));
        resetToEmpty(); break;
      case "/export": exportSession(); break;
      case "/feedback": $("feedback-btn").click(); break;
      case "/goal": p.value = "🎯 Meta: "; p.focus(); break;
      case "/permission": toggleDropdown("permission-dd"); break;
      case "/plan": togglePlan(); break;
      case "/model": toggleDropdown("model-dd"); break;
      default: p.value = cmd + " "; p.focus();
    }
  }
  function exportSession() {
    if (!appState.sessionId) return alert("No hay sesión activa.");
    const blob = new Blob([JSON.stringify({ session_id: appState.sessionId, exported_at: new Date().toISOString() }, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `fox_session_${appState.sessionId}.json`;
    a.click();
  }

  // ═══════════════════ PLAN MODE ═══════════════════
  function togglePlan() {
    appState.planMode = !appState.planMode;
    $("mode-badge").textContent = appState.planMode ? "📋 Plan mode" : `⚙ ${appState.mode}`;
  }
  function showPlanBanner() {
    let b = $("plan-banner");
    if (!b) {
      b = el("div", "plan-banner");
      b.id = "plan-banner";
      b.style.cssText = "display:flex;align-items:center;gap:10px;background:var(--accent-ghost);border:1px solid var(--accent-strong);border-radius:8px;padding:8px 12px;margin-bottom:6px;font-size:12.5px;color:var(--text-med);";
      b.innerHTML = `<span style="flex:1">📋 Plan propuesto — revisalo y aprobalo o pedí cambios.</span>`;
      const ok = el("button", "", "Aprobar");
      ok.style.cssText = "background:var(--green);border:none;color:#fff;border-radius:6px;padding:5px 10px;font-size:12px;";
      ok.onclick = () => { b.remove(); if (appState.ws) appState.ws.send(JSON.stringify({ action: "approve_plan" })); };
      const no = el("button", "", "Rechazar");
      no.style.cssText = "background:var(--red);border:none;color:#fff;border-radius:6px;padding:5px 10px;font-size:12px;";
      no.onclick = () => { b.remove(); if (appState.ws) appState.ws.send(JSON.stringify({ action: "reject_plan" })); };
      b.appendChild(ok); b.appendChild(no);
      $("input-dock").prepend(b);
    }
  }
  function hidePlanBanner() { const b = $("plan-banner"); if (b) b.remove(); }

  // ═══════════════════ ENVÍO ═══════════════════
  function updateSendBtn() {
    const btn = $("send-btn");
    btn.classList.toggle("stop", appState.busy);
    btn.textContent = appState.busy ? "■" : "↑";
  }
  function send() {
    const text = $("prompt").value.trim();
    if (!text || !appState.connected || appState.busy) return;
    $("prompt").value = "";
    appendUser(text);
    appState.busy = true;
    updateSendBtn();
    setStatus("Pensando…");
    appState.ws.send(JSON.stringify({
      action: "send",
      text,
      model: appState.model === "auto" ? null : appState.model,
      effort: appState.effort,
      sandbox: appState.permissions,
      session_id: appState.sessionId,
      plan: appState.planMode,
    }));
  }

  // ═══════════════════ WEBSOCKET ═══════════════════
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/agent`);
    appState.ws = ws;
    ws.onopen = () => {
      appState.connected = true;
      $("conn-dot").classList.add("online");
      ws.send(JSON.stringify({ action: "list" }));
    };
    ws.onclose = () => {
      appState.connected = false;
      $("conn-dot").classList.remove("online");
      setTimeout(connect, 1500);
    };
    ws.onmessage = (e) => {
      let ev; try { ev = JSON.parse(e.data); } catch { return; }
      onEvent(ev);
    };
    ws.onerror = () => ws.close();
  }

  // ═══════════════════ BINDINGS ═══════════════════
  // Tabs
  document.querySelectorAll(".tab").forEach((t) => {
    t.onclick = () => switchTab(t.dataset.tab);
  });

  // Settings modal
  $("settings-btn").onclick = openModal;
  $("settings-close").onclick = closeModal;
  $("settings-modal").querySelector(".modal-backdrop").onclick = closeModal;
  document.querySelectorAll(".modal-tab").forEach((t) => {
    t.onclick = () => switchSetting(t.dataset.setting);
  });

  // Dropdowns
  const pBtn = $("permission-btn");
  if (pBtn) pBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleDropdown("permission-dd"); });
  
  const mBtn = $("model-btn");
  if (mBtn) mBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleDropdown("model-dd"); });
  document.querySelectorAll("#permission-dd .dd-menu button").forEach((b) => {
    b.onclick = () => {
      appState.permissions = b.dataset.perm;
      updatePermBtn();
      closeAllDropdowns();
      document.querySelectorAll("#permission-dd .dd-menu button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
    };
  });

  // Model / effort
  $("effort-select").onchange = () => { appState.effort = $("effort-select").value; updateModelBtn(); };

  // Header mode badge
  $("mode-badge").onclick = (e) => {
    e.stopPropagation();
    openDynamicMenu($("mode-badge"), MODES, appState.mode, (val) => {
      appState.mode = val;
      $("mode-badge").textContent = `⚙ ${val}`;
    });
  };

  // Sidebar
  $("collapse-sidebar").onclick = () => $("sidebar").classList.toggle("collapsed");
  $("new-session").onclick = () => {
    appState.sessionId = null;
    $("task-title").value = "Nueva sesión";
    resetToEmpty();
    if (appState.ws) appState.ws.send(JSON.stringify({ action: "new" }));
  };

  // Input
  $("send-btn").onclick = () => { if (appState.busy) { appState.ws?.send(JSON.stringify({ action: "clear" })); } else send(); };
  const attachBtn = $("attach-btn");
  if (attachBtn) {
    attachBtn.onclick = () => {
      const fileInput = $("file-attachment");
      if (fileInput) fileInput.click();
    };
  }
  $("prompt").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  $("prompt").addEventListener("input", (e) => {
    if (e.target.value.startsWith("/")) showSlash(); else hideSlash();
  });

  // Settings controls
  $("font-plus").onclick = () => { appState.fontSize = Math.min(20, appState.fontSize + 1); applyFont(); };
  $("font-minus").onclick = () => { appState.fontSize = Math.max(11, appState.fontSize - 1); applyFont(); };
  function applyFont() {
    document.documentElement.style.setProperty("--font-ui", appState.fontSize + "px");
    $("font-size").textContent = appState.fontSize + "px";
  }
  document.querySelectorAll("#set-theme button").forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll("#set-theme button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
    };
  });
  $("set-permission").onchange = () => { appState.permissions = $("set-permission").value; updatePermBtn(); };
  $("set-density").onchange = () => {
    document.body.classList.toggle("compact", $("set-density").value === "compact");
  };
  $("open-config").onclick = () => alert("La configuración se edita en config/api_keys.json");
  $("add-provider-btn").onclick = () => addProviderBlock(false);
  $("add-custom-provider-btn").onclick = () => addProviderBlock(true);
  $("workspace-folder-picker").addEventListener("change", (e) => {
    e.preventDefault();
    const files = e.target.files;
    if (!files || !files.length) return;
    const rel = files[0].webkitRelativePath || files[0].name;
    const name = rel.split("/")[0];
    if (name) {
      appState.workspace = name;
      const ew = $("empty-workspace");
      if (ew) ew.textContent = `📁 ${name} ⌵`;
      renderSessions(appState.sessions, appState.sessionId);
    }
    e.target.value = "";
  });

  // Empty state selectors (re-bound after reset)
  bindEmptySelectors();

  // ═══════════════════ ARRANQUE ═══════════════════
  updatePermBtn();
  updateModelBtn();
  renderTelemetry();
  applyFont();
  loadMeta().then(connect);
})();
