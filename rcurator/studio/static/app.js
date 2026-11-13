(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const MAX_RESUME_BYTES = 15 * 1024 * 1024;
  const ALLOWED_RESUME_EXTS = [".pdf", ".txt", ".md", ".tex"];
  const PINNED_STORAGE_KEY = "rcurator.pinnedPresets";

  const PRESET_CATEGORIES = {
    systems_engineering: "systems",
    rust_and_compilers: "systems",
    linux_ricing_opensource: "systems",
    distributed_cloud_native: "systems",
    embedded_hardware_fpga: "systems",
    machine_learning: "ai_data",
    data_engineering_analytics: "ai_data",
    algorithms_mathematics: "ai_data",
    cognitive_science: "ai_data",
    frontend_ui_craft: "web_craft",
    fullstack_web_systems: "web_craft",
    game_engines_graphics: "web_craft",
    product_entrepreneurship: "web_craft",
    academic_research_epistemology: "science_sec",
    biotech_computational_sci: "science_sec",
    cybersecurity_infosec: "science_sec",
    quant_finance_economics: "science_sec",
  };

  const CATEGORY_TABS = [
    { id: "all", label: "All" },
    { id: "pinned", label: "Pinned" },
    { id: "systems", label: "Systems & Infra" },
    { id: "ai_data", label: "AI & Data" },
    { id: "web_craft", label: "Web & Craft" },
    { id: "science_sec", label: "Science & Security" },
  ];

  const state = {
    csrfToken: "",
    uiMode: "easy",
    aboutOpen: false,
    aboutData: null,
    aboutTheme: "all",
    aboutReturnFocusId: null,
    aboutReturnFocusEl: null,
    status: null,
    presets: [],
    activePresetId: null,
    activeManifest: null,
    originalManifest: null,
    excludedSubs: new Set(),
    resumeEvidences: [],
    resumeFilename: "",
    mixerWeights: {},
    lastAudit: null,
    easyCategory: "all",
    galleryCategory: "all",
    pinnedPresets: new Set(),
    auditTimer: null,
    confirmResolver: null,
    confirmReturnFocusEl: null,
  };

  function isSafeHttpsUrl(raw) {
    if (typeof raw !== "string" || !raw.trim()) return false;
    try {
      const parsed = new URL(raw);
      return parsed.protocol === "https:";
    } catch (_) {
      return false;
    }
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [key, val] of Object.entries(attrs)) {
        if (val === null || val === undefined || val === false) continue;
        if (key === "class") {
          node.className = String(val);
        } else if (key === "text") {
          node.textContent = String(val);
        } else if (key.startsWith("on") && typeof val === "function") {
          node.addEventListener(key.slice(2).toLowerCase(), val);
        } else if (val === true) {
          node.setAttribute(key, "");
        } else {
          node.setAttribute(key, String(val));
        }
      }
    }
    if (children !== undefined && children !== null) {
      const list = Array.isArray(children) ? children : [children];
      for (const child of list) {
        if (child === null || child === undefined) continue;
        if (typeof child === "string" || typeof child === "number") {
          node.appendChild(document.createTextNode(String(child)));
        } else {
          node.appendChild(child);
        }
      }
    }
    return node;
  }

  function svgEl(tag, attrs, children) {
    const node = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      for (const [key, val] of Object.entries(attrs)) {
        if (val !== null && val !== undefined) {
          node.setAttribute(key, String(val));
        }
      }
    }
    if (children) {
      const list = Array.isArray(children) ? children : [children];
      for (const child of list) {
        if (child) node.appendChild(child);
      }
    }
    return node;
  }

  function clearNode(node) {
    if (!node) return;
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function showToast(message, kind = "ok") {
    const rack = document.getElementById("toast-rack");
    if (!rack) return;
    let timerId = null;
    const dismiss = () => {
      if (timerId !== null) {
        window.clearTimeout(timerId);
        timerId = null;
      }
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    };
    const toast = el("div", { class: `toast toast-${kind}`, role: "status" }, [
      el("span", { text: message }),
      el("button", {
        type: "button",
        class: "toast-close",
        "aria-label": "Dismiss notification",
        text: "×",
        onclick: dismiss,
      }),
    ]);
    rack.appendChild(toast);
    const delay = kind === "err" || kind === "warn" ? 5500 : 3800;
    timerId = window.setTimeout(dismiss, delay);
  }

  function formatFriendlyError(rawMessage) {
    const msg = String(rawMessage || "Unknown error");
    const lower = msg.toLowerCase();
    if (lower.includes("cdp") || lower.includes("9222") || lower.includes("debug port") || lower.includes("econnrefused")) {
      return `${msg} — Hint: click "Open Reddit Browser" first so the debugging port is reachable.`;
    }
    if (lower.includes("not signed in") || lower.includes("login") || lower.includes("authenticated") || lower.includes("401") || lower.includes("403")) {
      return `${msg} — Hint: sign in to your Reddit account inside the debug browser window, then click Check Connection.`;
    }
    if (lower.includes("github") && (lower.includes("404") || lower.includes("rate limit"))) {
      return `${msg} — Hint: verify the GitHub username or add an optional GitHub token.`;
    }
    return msg;
  }

  async function withButtonBusy(btnOrId, busyText, fn) {
    const btn = typeof btnOrId === "string" ? document.getElementById(btnOrId) : btnOrId;
    if (!btn) {
      return await fn();
    }
    const origText = btn.textContent;
    btn.disabled = true;
    if (busyText) btn.textContent = busyText;
    try {
      return await fn();
    } finally {
      btn.disabled = false;
      btn.textContent = origText;
    }
  }

  function loadPinnedPresets() {
    try {
      const raw = localStorage.getItem(PINNED_STORAGE_KEY);
      if (!raw) return;
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        state.pinnedPresets = new Set(parsed.map((id) => String(id)));
      }
    } catch (_) {
      state.pinnedPresets = new Set();
    }
  }

  function savePinnedPresets() {
    try {
      localStorage.setItem(PINNED_STORAGE_KEY, JSON.stringify([...state.pinnedPresets]));
    } catch (_) {
      // ignore storage restrictions
    }
  }

  function togglePinPreset(presetId) {
    if (!presetId) return;
    if (state.pinnedPresets.has(presetId)) {
      state.pinnedPresets.delete(presetId);
    } else {
      state.pinnedPresets.add(presetId);
    }
    savePinnedPresets();
    renderEasyPresets();
    renderPresetGallery();
  }

  function slugifyName(raw, fallback = "manifest") {
    const slug = String(raw || "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_-]+/g, "-")
      .replace(/^-+|-+$/g, "");
    return slug || fallback;
  }

  function normalizeSubredditInput(raw) {
    return String(raw || "")
      .trim()
      .replace(/^\/?r\//i, "")
      .replace(/^\/+|\/+$/g, "");
  }

  function isValidSubredditName(name) {
    return /^[A-Za-z0-9_]{2,21}$/.test(name);
  }

  async function apiGet(path) {
    const res = await fetch(path, {
      method: "GET",
      headers: { Accept: "application/json" },
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
  }

  async function apiPost(path, bodyObj) {
    if (!state.csrfToken) {
      await refreshStatus(false);
    }
    const res = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        "X-Curator-Token": state.csrfToken,
      },
      body: JSON.stringify(bodyObj || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
  }

  async function apiUploadRaw(path, file) {
    if (!state.csrfToken) {
      await refreshStatus(false);
    }
    const buf = await file.arrayBuffer();
    const res = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": file.type || "application/octet-stream",
        "X-Filename": encodeURIComponent(file.name || "resume.pdf"),
        "X-Curator-Token": state.csrfToken,
      },
      body: buf,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
  }

  function switchTab(tabName) {
    if (state.aboutOpen) {
      closeAboutView(false);
    }
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      const active = btn.getAttribute("data-tab") === tabName;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll(".view").forEach((section) => {
      const active = section.id === `view-${tabName}`;
      section.classList.toggle("active", active);
      if (active) {
        section.removeAttribute("hidden");
      } else {
        section.setAttribute("hidden", "");
      }
    });
  }

  function getEffectiveManifest() {
    if (!state.activeManifest) return null;
    const clone = JSON.parse(JSON.stringify(state.activeManifest));
    if (state.excludedSubs.size === 0) return clone;

    const keepEntry = (entry) => {
      const name = typeof entry === "string" ? entry : entry && entry.name;
      return Boolean(name) && !state.excludedSubs.has(String(name).toLowerCase());
    };
    if (Array.isArray(clone.anchor_subreddits)) {
      clone.anchor_subreddits = clone.anchor_subreddits.filter(keepEntry);
    }
    if (Array.isArray(clone.domain_priming_subreddits)) {
      clone.domain_priming_subreddits = clone.domain_priming_subreddits.filter(keepEntry);
    }
    if (Array.isArray(clone.tier1_systems_subreddits)) {
      clone.tier1_systems_subreddits = clone.tier1_systems_subreddits.filter(keepEntry);
    }
    if (Array.isArray(clone.custom_feeds)) {
      clone.custom_feeds = clone.custom_feeds.map((feed) => ({
        ...feed,
        subreddits: (feed.subreddits || []).filter((s) => !state.excludedSubs.has(String(s).toLowerCase())),
      }));
    }
    return clone;
  }

  function setUiMode(mode) {
    if (state.aboutOpen) {
      closeAboutView(false);
    }
    const normalized = mode === "advanced" ? "advanced" : "easy";
    state.uiMode = normalized;
    document.body.classList.toggle("mode-easy", normalized === "easy");
    document.body.classList.toggle("mode-advanced", normalized === "advanced");

    const easyBtn = document.getElementById("btn-mode-easy");
    const advBtn = document.getElementById("btn-mode-advanced");
    if (easyBtn && advBtn) {
      easyBtn.classList.toggle("active", normalized === "easy");
      easyBtn.setAttribute("aria-pressed", normalized === "easy" ? "true" : "false");
      advBtn.classList.toggle("active", normalized === "advanced");
      advBtn.setAttribute("aria-pressed", normalized === "advanced" ? "true" : "false");
    }
    try {
      localStorage.setItem("rcurator.uiMode", normalized);
    } catch (_) {
      // ignore storage restrictions
    }
  }

  function clearStaleManifestBanners() {
    const easyBanner = document.getElementById("easy-apply-result");
    if (easyBanner) {
      easyBanner.className = "easy-banner hidden";
      clearNode(easyBanner);
    }
    const diffBox = document.getElementById("diff-preview-box");
    if (diffBox) {
      clearNode(diffBox);
    }
  }

  function updateEasyExportButtons() {
    const hasManifest = Boolean(getEffectiveManifest());
    const expBtn = document.getElementById("easy-btn-export-json");
    const copyBtn = document.getElementById("easy-btn-copy-json");
    if (expBtn) expBtn.disabled = !hasManifest;
    if (copyBtn) copyBtn.disabled = !hasManifest;
  }

  function setActiveManifest(manifest, resetExcluded = true, presetId = null, saveOriginal = true) {
    state.activeManifest = manifest;
    state.activePresetId = presetId || null;
    if (saveOriginal) {
      state.originalManifest = manifest ? JSON.parse(JSON.stringify(manifest)) : null;
    }
    if (resetExcluded) {
      state.excludedSubs.clear();
    }
    clearStaleManifestBanners();
    updateActiveBar();
    updateEasySelectedBadge();
    updateEasyExportButtons();
    renderEasyPresets();
    renderPresetGallery();
    renderEasyPresetPreview();
    renderInspector();
  }

  function updateEasySelectedBadge() {
    const badge = document.getElementById("easy-selected-badge");
    if (!badge) return;
    const m = getEffectiveManifest();
    badge.textContent = m ? `Selected: ${m.name || "Custom JSON"}` : "No preset selected";
  }

  function renderEasyPresetPreview() {
    const container = document.getElementById("easy-preset-preview");
    if (!container) return;
    clearNode(container);

    const m = getEffectiveManifest();
    if (!m) {
      container.classList.add("hidden");
      return;
    }

    const groups = {
      aesthetic_leisure: [],
      cognitive_curiosity: [],
      domain_priming: [],
    };
    const seen = new Set();
    const pushSub = (entry, fallbackCategory) => {
      const name = typeof entry === "string" ? entry : entry && entry.name;
      if (!name) return;
      const key = name.toLowerCase();
      if (seen.has(key)) return;
      seen.add(key);
      const cat = (typeof entry === "object" && entry.category) || fallbackCategory;
      const bucket = groups[cat] ? cat : fallbackCategory;
      groups[bucket].push(name);
    };

    for (const entry of m.anchor_subreddits || []) {
      pushSub(entry, "aesthetic_leisure");
    }
    const priming = m.domain_priming_subreddits || m.tier1_systems_subreddits || [];
    for (const entry of priming) {
      pushSub(entry, "domain_priming");
    }

    const gr = m.golden_ratio_distribution || {};
    const aesPct = Number(gr.aesthetic_leisure_target_pct ?? 40);
    const cogPct = Number(gr.cognitive_curiosity_target_pct ?? 35);
    const domPct = Number(gr.domain_priming_target_pct ?? 25);

    container.classList.remove("hidden");
    container.appendChild(
      el("div", { class: "easy-preview-head" }, [
        el("strong", { text: `${m.name || "Custom Pack"} — 3-Pillar Diet Breakdown` }),
        el("span", {
          class: "meta-line",
          text: `Target impression ratio: ${aesPct}% Leisure · ${cogPct}% Curiosity · ${domPct}% Domain`,
        }),
      ])
    );

    const cols = [
      ["aesthetic_leisure", "Leisure Anchors", `${aesPct}%`, "badge-aes"],
      ["cognitive_curiosity", "Cognitive Curiosity", `${cogPct}%`, "badge-cog"],
      ["domain_priming", "Domain Priming", `${domPct}%`, "badge-dom"],
    ];

    container.appendChild(
      el(
        "div",
        { class: "easy-preview-grid" },
        cols.map(([key, title, pct, badgeCls]) => {
          const subs = groups[key];
          return el("div", { class: "easy-preview-col" }, [
            el("div", { class: "easy-preview-col-head" }, [
              el("span", { text: title }),
              el("span", { class: `badge ${badgeCls}`, text: `${pct} · ${subs.length}` }),
            ]),
            el(
              "div",
              { class: "chip-row" },
              subs.length > 0
                ? subs.map((s) => el("span", { class: "chip", text: `r/${s}` }))
                : [el("span", { class: "meta-line", text: "None in Tier 1" })]
            ),
          ]);
        })
      )
    );

    const feeds = Array.isArray(m.custom_feeds) ? m.custom_feeds : [];
    if (feeds.length > 0) {
      const feedNodes = [el("span", { class: "meta-line", text: "Tier-2 Focus Feeds:" })];
      for (const feed of feeds) {
        const subs = Array.isArray(feed.subreddits) ? feed.subreddits : [];
        const label = feed.display_name || feed.feed_name || "Custom Feed";
        feedNodes.push(
          el("span", {
            class: "badge badge-dom",
            text: `${label} (${subs.length} subs)`,
          })
        );
      }
      container.appendChild(el("div", { class: "easy-preview-feeds" }, feedNodes));
    }
  }

  function updateActiveBar() {
    const nameEl = document.getElementById("active-manifest-name");
    const countsEl = document.getElementById("active-manifest-counts");
    if (!nameEl || !countsEl) return;
    const m = getEffectiveManifest();
    if (!m) {
      nameEl.textContent = "No manifest loaded";
      countsEl.textContent = "0 tier-1 · 0 custom feeds";
      return;
    }
    const anchors = Array.isArray(m.anchor_subreddits) ? m.anchor_subreddits.length : 0;
    const primingArr = m.domain_priming_subreddits || m.tier1_systems_subreddits;
    const priming = Array.isArray(primingArr) ? primingArr.length : 0;
    const feeds = Array.isArray(m.custom_feeds) ? m.custom_feeds.length : 0;
    nameEl.textContent = m.name || "Untitled Manifest";
    countsEl.textContent = `${anchors + priming} tier-1 · ${feeds} custom feeds`;
  }

  function exportManifest() {
    const m = getEffectiveManifest();
    if (!m) {
      showToast("Generate or load a preset before exporting.", "err");
      return;
    }
    const filename = `${slugifyName(state.activePresetId || m.name || "manifest")}.json`;
    const blob = new Blob([JSON.stringify(m, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = el("a", { href: url, download: filename });
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast(`Downloaded ${filename}`);
  }

  async function copyManifestJson() {
    const m = getEffectiveManifest();
    if (!m) {
      showToast("Generate or load a preset before copying.", "err");
      return;
    }
    const text = JSON.stringify(m, null, 2);
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        await navigator.clipboard.writeText(text);
        showToast("Copied manifest JSON to clipboard");
        return;
      }
      throw new Error("Clipboard API unavailable");
    } catch (_) {
      showToast("Could not access clipboard; use Export JSON instead.", "warn");
    }
  }

  async function handleImportManifestFile(file) {
    if (!file) return;
    if (file.name && !file.name.toLowerCase().endsWith(".json")) {
      showToast("Please select or drop a .json manifest file.", "err");
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      showToast("Manifest JSON exceeds 8 MB limit.", "err");
      return;
    }
    let parsed;
    try {
      const text = await file.text();
      parsed = JSON.parse(text);
    } catch (err) {
      showToast(`Invalid JSON file: ${err.message}`, "err");
      return;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      showToast("Manifest file must contain a JSON object.", "err");
      return;
    }
    try {
      const res = await apiPost("/api/import-manifest", {
        manifest: parsed,
        filename: file.name || "custom_manifest.json",
      });
      if (res.manifest) {
        setActiveManifest(res.manifest, true, null, true);
        if (res.audit) {
          state.lastAudit = res.audit;
          renderAnalytics(res.audit);
          renderInspector();
        } else {
          await auditActiveManifest();
        }
        showToast(`Opened custom JSON: ${res.manifest.name || file.name}`);
      }
    } catch (err) {
      showToast(`Import failed: ${err.message}`, "err");
    }
  }

  function getCdpPort() {
    const input = document.getElementById("input-cdp-port");
    if (!input) return 9222;
    const raw = String(input.value ?? "").trim();
    if (!raw || !/^\d+$/.test(raw)) {
      throw new Error("CDP port must be an integer between 1024 and 65535.");
    }
    const parsed = Number(raw);
    if (!Number.isInteger(parsed) || parsed < 1024 || parsed > 65535) {
      throw new Error("CDP port must be an integer between 1024 and 65535.");
    }
    return parsed;
  }

  function renderConnectionChecklist(status) {
    const container = document.getElementById("easy-connection-checklist");
    if (!container) return;
    clearNode(container);

    const browsers = status && Array.isArray(status.browsers) ? status.browsers : [];
    const hasBrowser = browsers.length > 0;
    const cdpOpen = Boolean(status && status.cdp_connected);
    const signedIn = Boolean(status && status.authenticated && status.username);

    const steps = [
      {
        title: `1. Browser ${hasBrowser ? "✓" : "○"}`,
        detail: hasBrowser
          ? browsers.map((b) => b.name || b.browser_id).slice(0, 2).join(", ")
          : "Install Chromium/Chrome/Brave (RPM or Flatpak)",
        cls: hasBrowser ? "done" : "pending",
      },
      {
        title: `2. Debug Port ${cdpOpen ? "✓" : "○"}`,
        detail: cdpOpen
          ? `CDP open on port ${status.cdp_port || 9222}`
          : "Click Open Reddit Browser",
        cls: cdpOpen ? "done" : hasBrowser ? "active" : "pending",
      },
      {
        title: `3. Reddit Login ${signedIn ? "✓" : "○"}`,
        detail: signedIn
          ? `Signed in as u/${status.username}`
          : "Sign in on reddit.com, then Check Connection",
        cls: signedIn ? "done" : cdpOpen ? "active" : "pending",
      },
    ];

    for (const step of steps) {
      container.appendChild(
        el("div", { class: `checklist-step ${step.cls}` }, [
          el("div", { class: "checklist-step-title", text: step.title }),
          el("div", { class: "checklist-step-detail", text: step.detail }),
        ])
      );
    }
  }

  async function refreshStatus(useInputPort = true) {
    try {
      let query = "";
      let requestedPort = null;
      if (useInputPort) {
        requestedPort = getCdpPort();
        query = `?cdp_port=${encodeURIComponent(requestedPort)}`;
      }
      const status = await apiGet(`/api/status${query}`);
      state.status = status;
      state.csrfToken = status.csrf_token || "";

      const portInput = document.getElementById("input-cdp-port");
      if (portInput && status.cdp_port) {
        portInput.value = String(status.cdp_port);
      }

      const cdpPill = document.getElementById("pill-cdp");
      if (cdpPill) {
        cdpPill.textContent = status.cdp_connected ? `CDP: port ${status.cdp_port}` : "CDP: offline";
        cdpPill.className = `status-pill ${status.cdp_connected ? "status-ok" : "status-off"}`;
      }

      const authPill = document.getElementById("pill-auth");
      if (authPill) {
        authPill.textContent = status.authenticated && status.username
          ? `Reddit: u/${status.username}`
          : "Reddit: disconnected";
        authPill.className = `status-pill ${status.authenticated ? "status-ok" : "status-off"}`;
      }

      const ontPill = document.getElementById("pill-ontology");
      if (ontPill) {
        ontPill.textContent = `Ontology: ${status.ontology_subreddit_count || 0} subs · ${status.presets_count || 0} presets`;
      }

      const cdpBadge = document.getElementById("console-cdp-badge");
      if (cdpBadge) {
        cdpBadge.textContent = `Port ${status.cdp_port || requestedPort || 9222}`;
      }

      updateEasyConnectionStatus(status);

      const browserSelect = document.getElementById("select-browser");
      if (browserSelect && Array.isArray(status.browsers)) {
        const prev = browserSelect.value;
        clearNode(browserSelect);
        browserSelect.appendChild(el("option", { value: "", text: "Auto-detect best browser" }));
        for (const b of status.browsers) {
          const id = b.browser_id || b.id || b.name || "";
          const label = b.name || b.label || id;
          if (id) {
            browserSelect.appendChild(el("option", { value: id, text: label }));
          }
        }
        browserSelect.value = prev;
      }
    } catch (err) {
      showToast(`Status error: ${err.message}`, "err");
    }
  }

  function updateTargetRatioBadges() {
    const sAes = document.getElementById("slider-aes");
    const sCog = document.getElementById("slider-cog");
    const sDom = document.getElementById("slider-dom");
    const aes = sAes ? Number(sAes.value) : 40;
    const cog = sCog ? Number(sCog.value) : 35;
    const dom = sDom ? Number(sDom.value) : 25;
    const label = `Target ${aes}/${cog}/${dom}`;
    const mixerBadge = document.getElementById("mixer-target-badge");
    const analyticsBadge = document.getElementById("analytics-target-badge");
    if (mixerBadge) mixerBadge.textContent = label;
    if (analyticsBadge) analyticsBadge.textContent = label;
  }

  // --- Golden Ratio Sliders ---
  function setupRatioSliders() {
    const sAes = document.getElementById("slider-aes");
    const sCog = document.getElementById("slider-cog");
    const sDom = document.getElementById("slider-dom");
    const vAes = document.getElementById("val-aes");
    const vCog = document.getElementById("val-cog");
    const vDom = document.getElementById("val-dom");

    function updateLabels() {
      vAes.textContent = `${sAes.value}%`;
      vCog.textContent = `${sCog.value}%`;
      vDom.textContent = `${sDom.value}%`;
      updateTargetRatioBadges();
    }

    function rebalance(changed) {
      const sliders = [sAes, sCog, sDom];
      const others = sliders.filter((s) => s !== changed);
      const val = Math.min(80, Math.max(10, Number(changed.value)));
      changed.value = String(val);
      const rem = 100 - val;
      const o0 = Number(others[0].value);
      const o1 = Number(others[1].value);
      const oSum = o0 + o1;
      let n0 = oSum > 0 ? Math.round((o0 / oSum) * rem) : Math.floor(rem / 2);
      n0 = Math.max(10, Math.min(rem - 10, n0));
      const n1 = rem - n0;
      others[0].value = String(n0);
      others[1].value = String(n1);
      updateLabels();
    }

    sAes.addEventListener("input", () => rebalance(sAes));
    sCog.addEventListener("input", () => rebalance(sCog));
    sDom.addEventListener("input", () => rebalance(sDom));

    const sTier1 = document.getElementById("slider-tier1");
    const vTier1 = document.getElementById("val-tier1");
    sTier1.addEventListener("input", () => {
      vTier1.textContent = `${sTier1.value} subs`;
    });

    document.getElementById("btn-reset-ratio").addEventListener("click", () => {
      sAes.value = "40";
      sCog.value = "35";
      sDom.value = "25";
      updateLabels();
    });

    updateLabels();
  }

  // --- Resume Dropzone ---
  function setupResumeDropzone() {
    const dropzone = document.getElementById("resume-dropzone");
    const fileInput = document.getElementById("resume-file-input");
    const statusEl = document.getElementById("resume-status");
    const previewEl = document.getElementById("resume-terms-preview");
    const clearBtn = document.getElementById("btn-clear-resume");

    function resetResumeState(message = "Processed strictly in memory · Never written to disk") {
      state.resumeEvidences = [];
      state.resumeFilename = "";
      clearNode(previewEl);
      statusEl.textContent = message;
      fileInput.value = "";
      if (clearBtn) clearBtn.classList.add("hidden");
    }

    if (clearBtn) {
      clearBtn.addEventListener("click", () => {
        resetResumeState();
        showToast("Cleared resume signals");
      });
    }

    async function handleFile(file) {
      if (!file) return;
      const fileName = String(file.name || "resume.pdf");
      const dotIdx = fileName.lastIndexOf(".");
      const ext = dotIdx >= 0 ? fileName.slice(dotIdx).toLowerCase() : "";
      if (!ALLOWED_RESUME_EXTS.includes(ext)) {
        const msg = `Unsupported file type (${ext || "unknown"}). Allowed: .pdf, .txt, .md, .tex`;
        resetResumeState(msg);
        showToast(msg, "err");
        return;
      }
      if (file.size <= 0) {
        const msg = "Selected resume file is empty.";
        resetResumeState(msg);
        showToast(msg, "err");
        return;
      }
      if (file.size > MAX_RESUME_BYTES) {
        const msg = "Resume exceeds 15 MiB limit.";
        resetResumeState(msg);
        showToast(msg, "err");
        return;
      }

      statusEl.textContent = `Parsing ${fileName} in memory...`;
      try {
        const res = await apiUploadRaw("/api/upload-resume", file);
        state.resumeEvidences = res.evidences || [];
        state.resumeFilename = res.filename || fileName;
        statusEl.textContent = `Parsed ${state.resumeFilename}: ${res.evidences_count || 0} signals extracted`;
        clearNode(previewEl);
        for (const item of (res.top_terms || []).slice(0, 12)) {
          previewEl.appendChild(
            el("span", { class: "chip", text: `${item.term} (${Number(item.weight).toFixed(1)})` })
          );
        }
        if (clearBtn) clearBtn.classList.remove("hidden");
        showToast(`Extracted ${res.evidences_count || 0} resume signals`);
      } catch (err) {
        resetResumeState(`Upload failed: ${err.message}`);
        showToast(`Resume error: ${err.message}`, "err");
      } finally {
        fileInput.value = "";
      }
    }

    dropzone.addEventListener("click", () => fileInput.click());
    dropzone.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        fileInput.click();
      }
    });
    fileInput.addEventListener("change", () => {
      const file = fileInput.files && fileInput.files[0];
      fileInput.value = "";
      if (file) {
        handleFile(file);
      }
    });
    dropzone.addEventListener("dragover", (ev) => {
      ev.preventDefault();
      dropzone.classList.add("dragover");
    });
    dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
    dropzone.addEventListener("drop", (ev) => {
      ev.preventDefault();
      dropzone.classList.remove("dragover");
      const file = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
      if (file) handleFile(file);
    });
  }

  // --- Generate Auto-Curation ---
  async function handleGenerate() {
    const btn = document.getElementById("btn-generate");
    const banner = document.getElementById("lab-result-banner");
    await withButtonBusy(btn, "Generating...", async () => {
      try {
        const cdpPort = getCdpPort();
        const payload = {
          workspace_path: document.getElementById("input-workspace").value.trim(),
          github_username: document.getElementById("input-github-user").value.trim(),
          github_token: document.getElementById("input-github-token").value.trim(),
          query: document.getElementById("input-query").value.trim(),
          resume_evidences: state.resumeEvidences,
          name: document.getElementById("input-manifest-name").value.trim() || "Auto-Generated Curation",
          target_ratio: [
            Number(document.getElementById("slider-aes").value),
            Number(document.getElementById("slider-cog").value),
            Number(document.getElementById("slider-dom").value),
          ],
          tier1_count: Number(document.getElementById("slider-tier1").value),
          discover_online: Boolean(document.getElementById("check-discover-online").checked),
          cdp_port: cdpPort,
        };

        const res = await apiPost("/api/generate", payload);
        if (res.manifest) {
          setActiveManifest(res.manifest, true, null, true);
          if (res.manifest.curation_provenance && res.manifest.curation_provenance.diet_health) {
            state.lastAudit = res.manifest.curation_provenance.diet_health;
            renderAnalytics(state.lastAudit);
            renderInspector();
          } else {
            await auditActiveManifest();
          }
          clearNode(banner);
          const warnings = Array.isArray(res.warnings) ? res.warnings : [];
          banner.className = `result-banner${warnings.length > 0 ? " warn" : ""}`;
          const msgWrap = el("div", {}, [
            el("div", {
              text: `Generated "${res.manifest.name}" using ${res.evidences_count || 0} signals (${(res.sources_used || []).join(", ") || "ontology"}).`,
            }),
            warnings.length > 0
              ? el("div", { class: "meta-line", text: `Warnings: ${warnings.join(" · ")}` })
              : null,
          ]);
          banner.appendChild(msgWrap);
          const jumpGroup = el("div", { class: "action-row-tight" }, [
            el("button", {
              type: "button",
              class: "btn btn-secondary btn-sm",
              text: "Open Inspector",
              onclick: () => switchTab("inspector"),
            }),
            el("button", {
              type: "button",
              class: "btn btn-ghost btn-sm",
              text: "View Health",
              onclick: () => {
                switchTab("analytics");
                auditActiveManifest();
              },
            }),
          ]);
          banner.appendChild(jumpGroup);
          showToast(
            warnings.length > 0 ? "Generated preset with warnings" : "Generated personalized curation manifest",
            warnings.length > 0 ? "warn" : "ok"
          );
        }
      } catch (err) {
        showToast(`Generate failed: ${formatFriendlyError(err.message)}`, "err");
      }
    });
  }

  // --- Inspector & Interactive Manifest Editor ---
  function scheduleAnalyticsRefresh() {
    if (state.auditTimer !== null) {
      window.clearTimeout(state.auditTimer);
    }
    state.auditTimer = window.setTimeout(() => {
      state.auditTimer = null;
      if (getEffectiveManifest()) {
        auditActiveManifest(true);
      }
    }, 250);
  }

  function applyManifestMutation(toastMsg = null) {
    updateActiveBar();
    updateEasySelectedBadge();
    updateEasyExportButtons();
    renderEasyPresetPreview();
    renderInspector();
    scheduleAnalyticsRefresh();
    if (toastMsg) showToast(toastMsg);
  }

  function ensureManifestArrays(m) {
    if (!Array.isArray(m.anchor_subreddits)) m.anchor_subreddits = [];
    if (!Array.isArray(m.domain_priming_subreddits)) {
      m.domain_priming_subreddits = Array.isArray(m.tier1_systems_subreddits)
        ? [...m.tier1_systems_subreddits]
        : [];
      delete m.tier1_systems_subreddits;
    }
    if (!Array.isArray(m.custom_feeds)) m.custom_feeds = [];
  }

  function removeSubFromTier1(m, key) {
    const filterFn = (entry) => {
      const name = typeof entry === "string" ? entry : entry && entry.name;
      return !name || name.toLowerCase() !== key;
    };
    if (Array.isArray(m.anchor_subreddits)) {
      m.anchor_subreddits = m.anchor_subreddits.filter(filterFn);
    }
    if (Array.isArray(m.domain_priming_subreddits)) {
      m.domain_priming_subreddits = m.domain_priming_subreddits.filter(filterFn);
    }
    if (Array.isArray(m.tier1_systems_subreddits)) {
      m.tier1_systems_subreddits = m.tier1_systems_subreddits.filter(filterFn);
    }
  }

  function removeSubFromCustomFeeds(m, key) {
    if (!Array.isArray(m.custom_feeds)) return;
    for (const feed of m.custom_feeds) {
      if (Array.isArray(feed.subreddits)) {
        feed.subreddits = feed.subreddits.filter((s) => String(s).toLowerCase() !== key);
      }
    }
  }

  function addSubredditToManifest(rawSub, pillar, targetTier) {
    const m = state.activeManifest;
    if (!m) {
      showToast("Load or generate a manifest first.", "err");
      return;
    }
    const subName = normalizeSubredditInput(rawSub);
    if (!isValidSubredditName(subName)) {
      showToast("Enter a valid subreddit name (2-21 letters, numbers, or underscores).", "err");
      return;
    }
    ensureManifestArrays(m);
    const key = subName.toLowerCase();
    state.excludedSubs.delete(key);

    if (targetTier === "tier1") {
      removeSubFromTier1(m, key);
      const entry = { name: subName, category: pillar || "domain_priming" };
      if (pillar === "domain_priming") {
        m.domain_priming_subreddits.push(entry);
      } else {
        m.anchor_subreddits.push(entry);
      }
    } else {
      let feed = m.custom_feeds.find((f) => f.feed_name === targetTier);
      if (!feed) {
        feed = m.custom_feeds[0];
      }
      if (!feed) {
        feed = {
          feed_name: "focus_domain",
          display_name: "Focus Domain",
          subreddits: [],
        };
        m.custom_feeds.push(feed);
      }
      if (!Array.isArray(feed.subreddits)) feed.subreddits = [];
      if (!feed.subreddits.some((s) => String(s).toLowerCase() === key)) {
        feed.subreddits.push(subName);
      }
    }
    applyManifestMutation(`Added r/${subName}`);
  }

  function moveSubredditTier(item) {
    const m = state.activeManifest;
    if (!m || !item) return;
    ensureManifestArrays(m);
    const key = item.name.toLowerCase();
    state.excludedSubs.delete(key);

    if (item.tier === "tier2_custom_feed") {
      removeSubFromCustomFeeds(m, key);
      removeSubFromTier1(m, key);
      const cat = item.category || "domain_priming";
      const entry = { name: item.name, category: cat };
      if (cat === "domain_priming") {
        m.domain_priming_subreddits.push(entry);
      } else {
        m.anchor_subreddits.push(entry);
      }
      applyManifestMutation(`Promoted r/${item.name} to Tier 1`);
    } else {
      removeSubFromTier1(m, key);
      let feed = m.custom_feeds[0];
      if (!feed) {
        feed = {
          feed_name: "focus_domain",
          display_name: "Focus Domain",
          subreddits: [],
        };
        m.custom_feeds.push(feed);
      }
      if (!Array.isArray(feed.subreddits)) feed.subreddits = [];
      if (!feed.subreddits.some((s) => String(s).toLowerCase() === key)) {
        feed.subreddits.push(item.name);
      }
      applyManifestMutation(`Moved r/${item.name} to Tier-2 (${feed.display_name || feed.feed_name})`);
    }
  }

  function addCustomFeedToManifest(rawFeedName) {
    const m = state.activeManifest;
    if (!m) {
      showToast("Load or generate a manifest first.", "err");
      return;
    }
    const displayName = String(rawFeedName || "").trim();
    if (!displayName) {
      showToast("Enter a Custom Feed name.", "err");
      return;
    }
    ensureManifestArrays(m);
    const feedSlug = slugifyName(displayName, "focus_domain").replace(/-/g, "_").slice(0, 48);
    if (m.custom_feeds.some((f) => String(f.feed_name).toLowerCase() === feedSlug)) {
      showToast(`Custom Feed "${feedSlug}" already exists.`, "err");
      return;
    }
    m.custom_feeds.push({
      feed_name: feedSlug,
      display_name: displayName,
      subreddits: [],
    });
    applyManifestMutation(`Created Custom Feed "${displayName}"`);
  }

  function removeCustomFeedFromManifest(feedName) {
    const m = state.activeManifest;
    if (!m || !Array.isArray(m.custom_feeds)) return;
    m.custom_feeds = m.custom_feeds.filter((f) => f.feed_name !== feedName);
    applyManifestMutation(`Removed Custom Feed "${feedName}"`);
  }

  function resetToOriginalManifest() {
    if (!state.originalManifest) {
      showToast("No original manifest to reset to.", "err");
      return;
    }
    const restored = JSON.parse(JSON.stringify(state.originalManifest));
    setActiveManifest(restored, true, state.activePresetId, false);
    scheduleAnalyticsRefresh();
    showToast("Reset manifest to original preset");
  }

  function renderInspectorEditorControls(m) {
    const editorBar = document.getElementById("inspector-editor-bar");
    const resetBtn = document.getElementById("btn-inspector-reset");
    const tierSelect = document.getElementById("inspector-add-tier");
    const feedsList = document.getElementById("inspector-feeds-list");

    if (resetBtn) {
      resetBtn.classList.toggle("hidden", !m || !state.originalManifest);
    }
    if (!editorBar) return;
    if (!m) {
      editorBar.classList.add("hidden");
      return;
    }
    editorBar.classList.remove("hidden");

    const feeds = Array.isArray(m.custom_feeds) ? m.custom_feeds : [];
    if (tierSelect) {
      const prev = tierSelect.value;
      clearNode(tierSelect);
      tierSelect.appendChild(el("option", { value: "tier1", text: "Tier-1 Following" }));
      for (const feed of feeds) {
        const fname = feed.feed_name || "focus_domain";
        const label = feed.display_name || fname;
        tierSelect.appendChild(el("option", { value: fname, text: `Tier-2: ${label}` }));
      }
      if ([...tierSelect.options].some((o) => o.value === prev)) {
        tierSelect.value = prev;
      }
    }

    if (feedsList) {
      clearNode(feedsList);
      for (const feed of feeds) {
        const fname = feed.feed_name || "focus_domain";
        const label = feed.display_name || fname;
        const count = Array.isArray(feed.subreddits) ? feed.subreddits.length : 0;
        feedsList.appendChild(
          el("span", { class: "feed-chip" }, [
            el("span", { text: `${label} (${count})` }),
            el("button", {
              type: "button",
              class: "feed-chip-remove",
              "aria-label": `Remove custom feed ${label}`,
              text: "×",
              onclick: () => removeCustomFeedFromManifest(fname),
            }),
          ])
        );
      }
    }
  }

  function buildSubredditItemsFromManifest(manifest) {
    const items = [];
    const prov = (manifest && manifest.curation_provenance) || {};
    const explanations = prov.explanations || {};

    const auditShares = new Map();
    const sharesList = (state.lastAudit && Array.isArray(state.lastAudit.per_subreddit_shares))
      ? state.lastAudit.per_subreddit_shares
      : [];
    for (const row of sharesList) {
      if (row && row.name) {
        auditShares.set(String(row.name).toLowerCase(), row);
      }
    }

    const tier1Map = new Map();
    for (const entry of manifest.anchor_subreddits || []) {
      const name = typeof entry === "string" ? entry : entry && entry.name;
      if (name) {
        tier1Map.set(name.toLowerCase(), {
          name,
          category: (typeof entry === "object" && entry.category) || "aesthetic_leisure",
          description: (typeof entry === "object" && entry.description) || "",
          entry: typeof entry === "object" ? entry : {},
        });
      }
    }
    const priming = manifest.domain_priming_subreddits || manifest.tier1_systems_subreddits || [];
    for (const entry of priming) {
      const name = typeof entry === "string" ? entry : entry && entry.name;
      if (name) {
        tier1Map.set(name.toLowerCase(), {
          name,
          category: (typeof entry === "object" && entry.category) || "domain_priming",
          description: (typeof entry === "object" && entry.description) || "",
          entry: typeof entry === "object" ? entry : {},
        });
      }
    }

    const tier2Set = new Set();
    for (const feed of manifest.custom_feeds || []) {
      for (const s of feed.subreddits || []) {
        tier2Set.add(String(s).toLowerCase());
      }
    }

    const seen = new Set();
    const pushItem = (name, defaultCat, defaultTier, desc, rawEntry = {}) => {
      const key = name.toLowerCase();
      if (seen.has(key)) return;
      seen.add(key);

      const exp = explanations[name] || explanations[key] || null;
      const auditRow = auditShares.get(key) || null;
      const inT1 = tier1Map.has(key);
      const inT2 = tier2Set.has(key);
      const tier = exp?.tier || (inT1 && inT2 ? "both" : inT1 ? "tier1_following" : "tier2_custom_feed");
      items.push({
        name: exp?.name || name,
        category: exp?.category || defaultCat || auditRow?.category || "domain_priming",
        tier,
        final_relevance: Number(exp?.final_relevance ?? 0),
        lexical_score: Number(exp?.lexical_score ?? 0),
        pagerank_score: Number(exp?.pagerank_score ?? 0),
        signal_density: Number(exp?.signal_density ?? auditRow?.signal_density ?? rawEntry?.signal_density ?? 0),
        estimated_daily_posts: Number(exp?.estimated_daily_posts ?? auditRow?.estimated_daily_posts ?? rawEntry?.estimated_daily_posts ?? 0),
        impression_share_pct: Number(exp?.impression_share_pct ?? auditRow?.impression_share_pct ?? 0),
        matched_terms: Array.isArray(exp?.matched_terms) ? exp.matched_terms : [],
        source_breakdown: exp?.source_breakdown || {},
        source_artifacts: Array.isArray(exp?.source_artifacts) ? exp.source_artifacts : [],
        graph_bridges: Array.isArray(exp?.graph_bridges) ? exp.graph_bridges : [],
        routing_reason: exp?.routing_reason || desc || defaultTier,
      });
    };

    for (const info of tier1Map.values()) {
      pushItem(info.name, info.category, "Tier-1 Following feed", info.description, info.entry);
    }
    for (const feed of manifest.custom_feeds || []) {
      for (const s of feed.subreddits || []) {
        const subName = String(s);
        const auditRow = auditShares.get(subName.toLowerCase());
        pushItem(
          subName,
          (auditRow && auditRow.category) || feed.category || "domain_priming",
          `Tier-2 Custom Feed (${feed.display_name || feed.feed_name || "focus"})`,
          ""
        );
      }
    }
    return items;
  }

  function pillarBadgeClass(category) {
    if (category === "aesthetic_leisure") return "badge-aes";
    if (category === "cognitive_curiosity") return "badge-cog";
    return "badge-dom";
  }

  function renderInspector() {
    const container = document.getElementById("inspector-cards");
    const algoBadge = document.getElementById("inspector-algo-badge");
    const topTermsEl = document.getElementById("inspector-top-terms");
    if (!container || !algoBadge || !topTermsEl) return;

    const activeEl = document.activeElement;
    const focusSub = activeEl && container.contains(activeEl) ? activeEl.getAttribute("data-focus-sub") : null;
    const focusRole = activeEl && container.contains(activeEl) ? activeEl.getAttribute("data-focus-role") : null;

    clearNode(container);
    clearNode(topTermsEl);

    const m = state.activeManifest;
    renderInspectorEditorControls(m);

    if (!m) {
      algoBadge.textContent = "No manifest loaded";
      container.appendChild(
        el("div", {
          class: "meta-line",
          text: "Generate a preset in Auto-Curate Lab or load one from the 17-Preset Gallery.",
        })
      );
      return;
    }

    const prov = m.curation_provenance || {};
    algoBadge.textContent = prov.algorithm || "Declarative Preset";
    for (const t of (prov.top_extracted_terms || []).slice(0, 14)) {
      topTermsEl.appendChild(
        el("span", {
          class: "chip",
          text: `${t.term} · w=${Number(t.weight || 0).toFixed(2)}`,
        })
      );
    }

    const search = (document.getElementById("inspector-search").value || "").trim().toLowerCase();
    const pillarFilter = document.getElementById("inspector-pillar").value;
    const tierFilter = document.getElementById("inspector-tier").value;

    const allItems = buildSubredditItemsFromManifest(m);
    const filtered = allItems.filter((item) => {
      if (pillarFilter !== "all" && item.category !== pillarFilter) return false;
      if (tierFilter === "tier1" && item.tier === "tier2_custom_feed") return false;
      if (tierFilter === "tier2" && item.tier === "tier1_following") return false;
      if (search) {
        const hay = [
          item.name,
          item.category,
          item.routing_reason,
          ...item.matched_terms.map((mt) => mt.term),
          ...item.graph_bridges,
        ]
          .join(" ")
          .toLowerCase();
        if (!hay.includes(search)) return false;
      }
      return true;
    });

    if (filtered.length === 0) {
      container.appendChild(el("div", { class: "meta-line", text: "No matching subreddits." }));
      return;
    }

    let restoreTarget = null;

    for (const item of filtered) {
      const subKey = item.name.toLowerCase();
      const isExcluded = state.excludedSubs.has(subKey);
      const toggle = el("input", {
        type: "checkbox",
        "data-focus-sub": subKey,
        "data-focus-role": "toggle",
      });
      toggle.checked = !isExcluded;
      toggle.addEventListener("change", () => {
        if (toggle.checked) {
          state.excludedSubs.delete(subKey);
        } else {
          state.excludedSubs.add(subKey);
        }
        applyManifestMutation();
      });

      const moveBtn = el("button", {
        type: "button",
        class: "btn btn-ghost btn-xs",
        "data-focus-sub": subKey,
        "data-focus-role": "move",
        text: item.tier === "tier2_custom_feed" ? "Promote to Tier 1" : "Move to Tier 2",
        onclick: () => moveSubredditTier(item),
      });

      if (focusSub === subKey && focusRole === "toggle") restoreTarget = toggle;
      if (focusSub === subKey && focusRole === "move") restoreTarget = moveBtn;

      const tierLabel =
        item.tier === "both"
          ? "Tier-1 + Custom Feed"
          : item.tier === "tier2_custom_feed"
            ? "Tier-2 Custom Feed"
            : "Tier-1 Following";

      const card = el("article", { class: `sub-card${isExcluded ? " excluded" : ""}` }, [
        el("div", { class: "sub-card-head" }, [
          el("span", { class: "sub-name", text: `r/${item.name}` }),
          el("div", { class: "action-row-tight" }, [
            moveBtn,
            el("label", { class: "meta-line" }, [toggle, " Active"]),
          ]),
        ]),
        el("div", { class: "chip-row" }, [
          el("span", { class: `badge ${pillarBadgeClass(item.category)}`, text: item.category }),
          el("span", { class: "badge", text: tierLabel }),
          item.final_relevance > 0
            ? el("span", { class: "badge badge-accent", text: `Rel ${item.final_relevance.toFixed(3)}` })
            : null,
        ]),
        el("div", { class: "meter-row" }, [
          el("span", { text: "Lexical" }),
          el("div", { class: "meter-track" }, [
            svgEl("svg", { viewBox: "0 0 100 7", width: "100%", height: "7", preserveAspectRatio: "none" }, [
              svgEl("rect", {
                x: 0,
                y: 0,
                width: Math.min(100, Math.max(0, item.lexical_score * 100)).toFixed(1),
                height: 7,
                rx: 3.5,
                fill: "#38bdf8",
              }),
            ]),
          ]),
          el("span", { text: item.lexical_score.toFixed(2) }),
        ]),
        el("div", { class: "meter-row" }, [
          el("span", { text: "PageRank" }),
          el("div", { class: "meter-track" }, [
            svgEl("svg", { viewBox: "0 0 100 7", width: "100%", height: "7", preserveAspectRatio: "none" }, [
              svgEl("rect", {
                x: 0,
                y: 0,
                width: Math.min(100, Math.max(0, item.pagerank_score * 100)).toFixed(1),
                height: 7,
                rx: 3.5,
                fill: "#a78bfa",
              }),
            ]),
          ]),
          el("span", { text: item.pagerank_score.toFixed(2) }),
        ]),
        el("div", {
          class: "meta-line",
          text: `Velocity: ${item.estimated_daily_posts.toFixed(1)} posts/d · Share: ${item.impression_share_pct.toFixed(1)}% · Density: ${item.signal_density.toFixed(2)}`,
        }),
        item.matched_terms.length > 0
          ? el(
              "div",
              { class: "chip-row" },
              item.matched_terms.slice(0, 6).map((mt) =>
                el("span", {
                  class: "chip",
                  text: `${mt.term}: +${Number(mt.contribution || 0).toFixed(2)}`,
                })
              )
            )
          : null,
        item.graph_bridges.length > 0
          ? el("div", {
              class: "meta-line",
              text: `Graph bridges: ${item.graph_bridges.slice(0, 4).join(", ")}`,
            })
          : null,
        item.source_artifacts.length > 0
          ? el("div", {
              class: "meta-line",
              text: `Sources: ${item.source_artifacts.slice(0, 3).join(", ")}`,
            })
          : null,
        item.routing_reason
          ? el("div", { class: "meta-line", text: item.routing_reason })
          : null,
      ]);
      container.appendChild(card);
    }

    if (restoreTarget && typeof restoreTarget.focus === "function") {
      restoreTarget.focus();
    }
  }

  function updateEasyConnectionStatus(status) {
    const badge = document.getElementById("easy-browser-badge");
    const msg = document.getElementById("easy-browser-status");
    if (!badge || !msg) return;
    if (status && status.authenticated && status.username) {
      badge.textContent = `Connected as u/${status.username}`;
      badge.className = "status-pill status-ok";
      msg.textContent = "Your Reddit account is connected and ready to update.";
    } else if (status && status.cdp_connected) {
      badge.textContent = "Browser open (sign in needed)";
      badge.className = "status-pill status-info";
      msg.textContent = "Browser is open. Sign in to Reddit in that window, then click Check Connection.";
    } else {
      badge.textContent = "Not connected";
      badge.className = "status-pill status-off";
      msg.textContent = "Launch the browser window, log in to your Reddit account once, then click Check Connection.";
    }
    renderConnectionChecklist(status);
  }

  function buildMiniRatioBar(gr) {
    const dist = gr || {};
    const aes = Math.max(0, Number(dist.aesthetic_leisure_target_pct ?? 40));
    const cog = Math.max(0, Number(dist.cognitive_curiosity_target_pct ?? 35));
    const dom = Math.max(0, Number(dist.domain_priming_target_pct ?? 25));
    const total = aes + cog + dom || 100;
    const wAes = (aes / total) * 100;
    const wCog = (cog / total) * 100;
    const wDom = Math.max(0, 100 - wAes - wCog);
    return el("div", {
      class: "mini-ratio-bar",
      title: `Target ratio ${aes}% / ${cog}% / ${dom}%`,
    }, [
      svgEl("svg", { viewBox: "0 0 100 6", width: "100%", height: "6", preserveAspectRatio: "none" }, [
        svgEl("rect", { x: 0, y: 0, width: wAes.toFixed(1), height: 6, fill: "#38bdf8" }),
        svgEl("rect", { x: wAes.toFixed(1), y: 0, width: wCog.toFixed(1), height: 6, fill: "#a78bfa" }),
        svgEl("rect", { x: (wAes + wCog).toFixed(1), y: 0, width: wDom.toFixed(1), height: 6, fill: "#f59e0b" }),
      ]),
    ]);
  }

  function renderCategoryChips(containerId, activeCat, onSelect) {
    const container = document.getElementById(containerId);
    if (!container) return;
    clearNode(container);
    for (const cat of CATEGORY_TABS) {
      const active = activeCat === cat.id;
      const count = cat.id === "pinned" ? state.pinnedPresets.size : null;
      const label = count !== null ? `${cat.label} (${count})` : cat.label;
      container.appendChild(
        el("button", {
          type: "button",
          class: `chip chip-btn${active ? " active" : ""}`,
          "aria-pressed": active ? "true" : "false",
          text: label,
          onclick: () => onSelect(cat.id),
        })
      );
    }
  }

  function filterAndSortPresets(searchQuery, categoryId) {
    const q = String(searchQuery || "").trim().toLowerCase();
    const filtered = state.presets.filter((p) => {
      if (categoryId === "pinned" && !state.pinnedPresets.has(p.id)) return false;
      if (categoryId !== "all" && categoryId !== "pinned") {
        const pCat = PRESET_CATEGORIES[p.id] || "other";
        if (pCat !== categoryId) return false;
      }
      if (!q) return true;
      const subsText = Array.isArray(p.preview_subreddits) ? p.preview_subreddits.join(" ") : "";
      return `${p.id} ${p.name} ${p.description} ${subsText}`.toLowerCase().includes(q);
    });
    return filtered.sort((a, b) => {
      const aPin = state.pinnedPresets.has(a.id) ? 0 : 1;
      const bPin = state.pinnedPresets.has(b.id) ? 0 : 1;
      if (aPin !== bPin) return aPin - bPin;
      return String(a.name || a.id).localeCompare(String(b.name || b.id));
    });
  }

  function buildPreviewChips(previewSubs) {
    const subs = Array.isArray(previewSubs) ? previewSubs.slice(0, 4) : [];
    if (subs.length === 0) return null;
    return el(
      "div",
      { class: "chip-row" },
      subs.map((s) => el("span", { class: "chip chip-xs", text: `r/${s}` }))
    );
  }

  function renderEasyPresets() {
    renderCategoryChips("easy-category-chips", state.easyCategory, (catId) => {
      state.easyCategory = catId;
      renderEasyPresets();
    });
    const grid = document.getElementById("easy-preset-grid");
    if (!grid) return;
    clearNode(grid);
    const searchEl = document.getElementById("easy-preset-search");
    const list = filterAndSortPresets(searchEl && searchEl.value, state.easyCategory);

    if (list.length === 0) {
      grid.appendChild(el("div", { class: "meta-line", text: "No topic packs match this filter." }));
      return;
    }

    for (const p of list) {
      const isSelected = state.activePresetId === p.id;
      const isPinned = state.pinnedPresets.has(p.id);
      const count = (p.anchor_count || 0) + (p.domain_priming_count || 0);
      const pinBtn = el("button", {
        type: "button",
        class: `pin-btn${isPinned ? " pinned" : ""}`,
        "aria-pressed": isPinned ? "true" : "false",
        "aria-label": `${isPinned ? "Unpin" : "Pin"} ${p.name}`,
        text: isPinned ? "★" : "☆",
        onclick: (ev) => {
          ev.stopPropagation();
          togglePinPreset(p.id);
        },
      });
      const card = el("div", {
        role: "button",
        tabindex: "0",
        class: `easy-preset-card${isSelected ? " selected" : ""}`,
        "aria-pressed": isSelected ? "true" : "false",
        onclick: () => loadSinglePreset(p.id),
        onkeydown: (ev) => {
          if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            loadSinglePreset(p.id);
          }
        },
      }, [
        el("div", { class: "preset-card-head" }, [
          el("div", { class: "preset-title", text: p.name }),
          pinBtn,
        ]),
        el("div", { class: "preset-desc", text: p.description || "" }),
        buildMiniRatioBar(p.golden_ratio_distribution),
        buildPreviewChips(p.preview_subreddits),
        el("div", { class: "meta-line", text: `${count} communities · ${p.custom_feeds_count || 0} topic feeds` }),
      ]);
      grid.appendChild(card);
    }
  }

  async function handleEasyLaunch() {
    await withButtonBusy("easy-btn-launch", "Opening Browser...", async () => {
      try {
        const port = getCdpPort();
        await apiPost("/api/launch-browser", { port, browser: null });
        showToast("Reddit browser opened");
        await refreshStatus(true);
      } catch (err) {
        showToast(`Could not open browser: ${formatFriendlyError(err.message)}`, "err");
      }
    });
  }

  function normalizeFailureName(item, fallback = "") {
    if (item && typeof item === "object") {
      return String(item.name || item.subreddit || item.feed_name || item.error || fallback).trim();
    }
    return String(item || fallback).trim();
  }

  function normalizeFailureError(err, fallback = "error") {
    if (err === null || err === undefined || err === "") return fallback;
    if (typeof err === "object") {
      return String(err.error || err.message || err.detail || JSON.stringify(err));
    }
    return String(err);
  }

  function extractSyncFailures(sync) {
    const s = sync || {};
    const subInfo = s.subscriptions || {};
    const rawFailedSubs = Array.isArray(subInfo.failed) ? subInfo.failed : [];
    const failedSubs = rawFailedSubs
      .map((x) => normalizeFailureName(x))
      .filter(Boolean);
    const customFeeds = s.custom_feeds || {};
    const failedFeeds = [];
    if (Array.isArray(customFeeds)) {
      for (const fdata of customFeeds) {
        if (fdata && typeof fdata === "object" && fdata.status === "failed") {
          const fname = normalizeFailureName(fdata, "custom_feed");
          const ferr = normalizeFailureError(fdata.error, `HTTP ${fdata.http_code || "error"}`);
          failedFeeds.push({ name: fname, error: ferr });
        }
      }
    } else if (customFeeds && typeof customFeeds === "object") {
      for (const [fname, fdata] of Object.entries(customFeeds)) {
        if (fdata && fdata.status === "failed") {
          const resolvedName = String(fdata.name || fdata.feed_name || fname || fdata.error || "custom_feed").trim();
          const ferr = normalizeFailureError(fdata.error, `HTTP ${fdata.http_code || "error"}`);
          failedFeeds.push({ name: resolvedName, error: ferr });
        }
      }
    }
    const followingFailed = Boolean(s.following_feed && s.following_feed.success === false);
    const warnings = (Array.isArray(s.warnings) ? s.warnings : [])
      .map((w) => (w && typeof w === "object" ? normalizeFailureError(w, "") : String(w || "")))
      .filter(Boolean);
    const hasFailures = Boolean(
      s.has_failures ||
      failedSubs.length > 0 ||
      failedFeeds.length > 0 ||
      followingFailed
    );
    return {
      hasFailures,
      failedSubs,
      failedFeeds,
      followingFailed,
      warnings,
      totalIssues: failedSubs.length + failedFeeds.length + (followingFailed ? 1 : 0),
    };
  }

  async function handleEasyPreview() {
    const m = getEffectiveManifest();
    if (!m) {
      showToast("Pick a preset or open a custom JSON file first.", "err");
      return;
    }
    const banner = document.getElementById("easy-apply-result");
    banner.className = "easy-banner hidden";
    clearNode(banner);
    await withButtonBusy("easy-btn-preview", "Previewing...", async () => {
      try {
        const cdpPort = getCdpPort();
        const res = await apiPost("/api/sync", {
          manifest: m,
          dry_run: true,
          prune: false,
          keep: [],
          cdp_port: cdpPort,
        });
        const sync = res.sync || {};
        const subInfo = sync.subscriptions || {};
        const toFollow = subInfo.subscribed || [];
        const already = subInfo.already_subscribed || [];
        const feedKeys = Object.keys(sync.custom_feeds || {});
        const healthScore = sync.analytics && sync.analytics.composite_health_score;
        const healthNote = typeof healthScore === "number" ? ` · Diet Health Score: ${healthScore.toFixed(1)}/100` : "";

        banner.className = "easy-banner";
        const summaryText = sync.cdp_reachable
          ? `Live Dry-Run (u/${sync.username || "reddit"}): would follow ${toFollow.length} new communities (${already.length} already followed), configure ${feedKeys.length} topic feed(s), and lock Home to Following${healthNote}.`
          : `Offline Preview (No Reddit login needed): would follow ${toFollow.length} Tier-1 communities, create ${feedKeys.length} deep-focus custom feed(s), and lock Home to Following${healthNote}.`;
        banner.appendChild(el("div", { text: summaryText }));
        if (toFollow.length > 0) {
          banner.appendChild(
            el("div", {
              class: "meta-line",
              text: `Communities: ${toFollow.slice(0, 12).map((s) => `r/${s}`).join(", ")}${toFollow.length > 12 ? ` (+${toFollow.length - 12} more)` : ""}`,
            })
          );
        }
        showToast("Preview ready (no changes made to Reddit)");
      } catch (err) {
        banner.className = "easy-banner err";
        banner.textContent = `Preview failed: ${formatFriendlyError(err.message)}`;
        showToast(`Preview failed: ${err.message}`, "err");
      }
    });
  }

  async function handleEasyApply() {
    const m = getEffectiveManifest();
    if (!m) {
      showToast("Pick a preset or open a custom JSON file first.", "err");
      return;
    }
    const banner = document.getElementById("easy-apply-result");
    banner.className = "easy-banner hidden";
    clearNode(banner);
    await withButtonBusy("easy-btn-apply", "Applying...", async () => {
      try {
        const cdpPort = getCdpPort();
        const res = await apiPost("/api/sync", {
          manifest: m,
          dry_run: false,
          prune: false,
          keep: [],
          cdp_port: cdpPort,
        });
        const sync = res.sync || {};
        const subInfo = sync.subscriptions || {};
        const added = (subInfo.subscribed || []).length;
        const already = (subInfo.already_subscribed || []).length;
        const feedEntries = Object.entries(sync.custom_feeds || {});
        const succeededFeeds = feedEntries.filter(([, f]) => !f || f.status !== "failed").length;
        const failures = extractSyncFailures(sync);

        if (failures.hasFailures) {
          const allFailed = added === 0 && already === 0 && succeededFeeds === 0;
          banner.className = `easy-banner ${allFailed ? "err" : "warn"}`;
          banner.appendChild(
            el("div", {
              text: allFailed
                ? `Sync failed for u/${sync.username || "reddit"} (${failures.totalIssues} issue(s)).`
                : `Partially updated u/${sync.username || "reddit"}: ${added} followed (${already} already followed), ${succeededFeeds}/${feedEntries.length} custom feed(s) updated, with ${failures.totalIssues} failure(s).`,
            })
          );
          if (failures.failedSubs.length > 0) {
            banner.appendChild(
              el("div", {
                class: "meta-line",
                text: `Failed communities: ${failures.failedSubs.map((s) => `r/${s}`).join(", ")}`,
              })
            );
          }
          if (failures.failedFeeds.length > 0) {
            banner.appendChild(
              el("div", {
                class: "meta-line",
                text: `Failed custom feeds: ${failures.failedFeeds.map((f) => `${f.name} (${f.error})`).join(", ")}`,
              })
            );
          }
          if (failures.followingFailed) {
            banner.appendChild(
              el("div", {
                class: "meta-line",
                text: "Home feed could not be switched to Following automatically — switch it manually in Reddit.",
              })
            );
          }
          if (failures.warnings.length > 0) {
            banner.appendChild(el("div", { class: "meta-line", text: failures.warnings.join(" · ") }));
          }
          showToast("Reddit sync finished with failures — review details", allFailed ? "err" : "warn");
        } else {
          banner.className = "easy-banner";
          banner.textContent = `Done! Connected as u/${sync.username || "reddit"}: ${added} new communities followed (${already} already followed), ${feedEntries.length} custom feed(s) updated, and your Reddit home feed is now set to Following.`;
          showToast("Reddit feed updated!");
        }
        await refreshStatus(true);
        await loadSnapshots();
      } catch (err) {
        banner.className = "easy-banner err";
        banner.textContent = `Could not apply yet: ${formatFriendlyError(err.message)}`;
        showToast(`Apply failed: ${err.message}`, "err");
      }
    });
  }

  // --- Preset Gallery & Mixer ---
  async function loadPresets() {
    try {
      const res = await apiGet("/api/presets");
      state.presets = res.presets || [];
      renderEasyPresets();
      renderPresetGallery();
      populateMixerSelect();
    } catch (err) {
      showToast(`Failed to load presets: ${err.message}`, "err");
    }
  }

  function populateMixerSelect() {
    const select = document.getElementById("mixer-preset-select");
    if (!select) return;
    clearNode(select);
    for (const p of state.presets) {
      select.appendChild(el("option", { value: p.id, text: `${p.name} (${p.id})` }));
    }
  }

  function renderPresetGallery() {
    renderCategoryChips("gallery-category-chips", state.galleryCategory, (catId) => {
      state.galleryCategory = catId;
      renderPresetGallery();
    });
    const grid = document.getElementById("preset-gallery-grid");
    if (!grid) return;
    clearNode(grid);
    const searchEl = document.getElementById("gallery-search");
    const list = filterAndSortPresets(searchEl && searchEl.value, state.galleryCategory);

    if (list.length === 0) {
      grid.appendChild(el("div", { class: "meta-line", text: "No presets match this filter." }));
      return;
    }

    for (const p of list) {
      const isSelected = state.activePresetId === p.id;
      const isPinned = state.pinnedPresets.has(p.id);
      const card = el("article", { class: `preset-card${isSelected ? " selected" : ""}` }, [
        el("div", {}, [
          el("div", { class: "preset-card-head" }, [
            el("div", { class: "preset-title", text: p.name }),
            el("button", {
              type: "button",
              class: `pin-btn${isPinned ? " pinned" : ""}`,
              "aria-pressed": isPinned ? "true" : "false",
              "aria-label": `${isPinned ? "Unpin" : "Pin"} ${p.name}`,
              text: isPinned ? "★" : "☆",
              onclick: () => togglePinPreset(p.id),
            }),
          ]),
          el("div", { class: "meta-line", text: `${p.id} · ${(p.anchor_count || 0) + (p.domain_priming_count || 0)} tier-1` }),
          el("p", { class: "preset-desc", text: p.description || "" }),
          buildMiniRatioBar(p.golden_ratio_distribution),
          buildPreviewChips(p.preview_subreddits),
        ]),
        el("div", { class: "action-row-tight" }, [
          el("button", {
            type: "button",
            class: "btn btn-secondary btn-sm",
            text: isSelected ? "Active" : "Load Preset",
            onclick: () => loadSinglePreset(p.id),
          }),
          el("button", {
            type: "button",
            class: "btn btn-ghost btn-sm",
            text: "+ Mix",
            onclick: () => addPresetToMixer(p.id),
          }),
        ]),
      ]);
      grid.appendChild(card);
    }
  }

  async function loadSinglePreset(presetId) {
    try {
      const manifest = await apiGet(`/api/presets/${encodeURIComponent(presetId)}`);
      setActiveManifest(manifest, true, presetId, true);
      showToast(`Loaded preset: ${manifest.name || presetId}`);
      await auditActiveManifest(true);
    } catch (err) {
      showToast(`Load failed: ${err.message}`, "err");
    }
  }

  function addPresetToMixer(presetId) {
    if (!presetId) return;
    if (!(presetId in state.mixerWeights)) {
      state.mixerWeights[presetId] = 1.0;
    }
    renderMixerList();
  }

  function updateMixerPercentages() {
    const list = document.getElementById("mixer-weights-list");
    if (!list) return;
    const entries = Object.entries(state.mixerWeights);
    const total = entries.reduce((acc, [, w]) => acc + Number(w || 0), 0) || 1;
    for (const [id, w] of entries) {
      const pct = Math.round((Number(w) / total) * 100);
      const labelNode = list.querySelector(`[data-mixer-label="${id}"]`);
      if (labelNode) {
        labelNode.textContent = `${id} (${pct}%)`;
      }
    }
  }

  function renderMixerList() {
    const list = document.getElementById("mixer-weights-list");
    if (!list) return;
    clearNode(list);
    const entries = Object.entries(state.mixerWeights);
    if (entries.length === 0) {
      list.appendChild(el("div", { class: "meta-line", text: "Add 2 or more presets to blend." }));
      return;
    }
    const total = entries.reduce((acc, [, w]) => acc + Number(w || 0), 0) || 1;
    for (const [id, w] of entries) {
      const pct = Math.round((Number(w) / total) * 100);
      const labelSpan = el("span", {
        "data-mixer-label": id,
        text: `${id} (${pct}%)`,
      });
      const slider = el("input", {
        class: "range",
        type: "range",
        min: "5",
        max: "100",
        value: String(Math.round(Number(w) * 50)),
        "aria-label": `Weight for ${id}`,
      });
      slider.addEventListener("input", () => {
        state.mixerWeights[id] = Number(slider.value) / 50;
        updateMixerPercentages();
      });

      const row = el("div", { class: "mixer-row", "data-mixer-id": id }, [
        el("div", { class: "mixer-row-head" }, [
          labelSpan,
          el("button", {
            type: "button",
            class: "btn btn-ghost btn-sm",
            text: "Remove",
            onclick: () => {
              delete state.mixerWeights[id];
              renderMixerList();
            },
          }),
        ]),
        slider,
      ]);
      list.appendChild(row);
    }
  }

  async function handleMixBlend() {
    const entries = Object.entries(state.mixerWeights);
    if (entries.length === 0) {
      showToast("Add presets to the mixer first.", "err");
      return;
    }
    const total = entries.reduce((acc, [, w]) => acc + Number(w), 0) || 1;
    const normalized = {};
    for (const [k, w] of entries) {
      normalized[k] = Number(w) / total;
    }
    const sTier1 = document.getElementById("slider-tier1");
    await withButtonBusy("btn-mix-blend", "Blending...", async () => {
      try {
        const res = await apiPost("/api/mix", {
          weights: normalized,
          target_ratio: [
            Number(document.getElementById("slider-aes").value),
            Number(document.getElementById("slider-cog").value),
            Number(document.getElementById("slider-dom").value),
          ],
          tier1_count: sTier1 ? Number(sTier1.value) : 18,
        });
        if (res.manifest) {
          setActiveManifest(res.manifest, true, null, true);
          showToast(`Blended ${entries.length} presets`);
          await auditActiveManifest(true);
        }
      } catch (err) {
        showToast(`Mix failed: ${err.message}`, "err");
      }
    });
  }

  // --- Diet Health & SVG Charts ---
  async function auditActiveManifest(silent = false) {
    const m = getEffectiveManifest();
    if (!m) {
      if (!silent) showToast("No active manifest to audit.", "err");
      return;
    }
    const runAudit = async () => {
      try {
        const res = await apiPost("/api/audit", { manifest: m });
        if (res.audit) {
          state.lastAudit = res.audit;
          renderAnalytics(res.audit);
          renderInspector();
        }
      } catch (err) {
        showToast(`Audit failed: ${err.message}`, "err");
      }
    };
    if (silent) {
      await runAudit();
    } else {
      await withButtonBusy("btn-audit-manifest", "Auditing...", runAudit);
    }
  }

  async function auditLiveAccount() {
    await withButtonBusy("btn-audit-live", "Auditing Live...", async () => {
      try {
        const cdpPort = getCdpPort();
        const res = await apiPost("/api/audit", {
          live: true,
          cdp_port: cdpPort,
          target_ratio: [
            Number(document.getElementById("slider-aes").value),
            Number(document.getElementById("slider-cog").value),
            Number(document.getElementById("slider-dom").value),
          ],
        });
        if (res.audit) {
          state.lastAudit = res.audit;
          renderAnalytics(res.audit);
          showToast("Audited live Reddit subscriptions");
        }
      } catch (err) {
        showToast(`Live audit error: ${formatFriendlyError(err.message)}`, "err");
      }
    });
  }

  function appendDonutRing(svg, cx, cy, r, strokeWidth, segments) {
    const circ = 2 * Math.PI * r;
    let offset = 0;
    for (const seg of segments) {
      const frac = Math.max(0, Math.min(1, Number(seg.pct ?? 0) / 100));
      const dash = frac * circ;
      if (dash <= 0.1) continue;
      const circle = svgEl("circle", {
        cx,
        cy,
        r,
        fill: "none",
        stroke: seg.color,
        "stroke-width": strokeWidth,
        "stroke-dasharray": `${dash.toFixed(2)} ${(circ - dash).toFixed(2)}`,
        "stroke-dashoffset": (-offset).toFixed(2),
        transform: `rotate(-90 ${cx} ${cy})`,
      });
      svg.appendChild(circle);
      offset += dash;
    }
  }

  function renderAnalytics(audit) {
    const donutStage = document.getElementById("svg-donut-container");
    const legendEl = document.getElementById("svg-donut-legend");
    const kpiGrid = document.getElementById("health-kpi-grid");
    const recList = document.getElementById("audit-recommendations");
    const tableWrap = document.getElementById("audit-shares-table");
    if (!donutStage || !legendEl || !kpiGrid || !recList || !tableWrap) return;

    clearNode(donutStage);
    clearNode(legendEl);
    clearNode(kpiGrid);
    clearNode(recList);
    clearNode(tableWrap);

    if (!audit) return;

    const actual = audit.actual_velocity_distribution_pct || {};
    const target = audit.target_distribution_pct || {};

    const tAes = Number(target.aesthetic_leisure ?? 40);
    const tCog = Number(target.cognitive_curiosity ?? 35);
    const tDom = Number(target.domain_priming ?? 25);

    const aAes = Number(actual.aesthetic_leisure ?? 0);
    const aCog = Number(actual.cognitive_curiosity ?? 0);
    const aDom = Number(actual.domain_priming ?? 0);

    const svg = svgEl("svg", { viewBox: "0 0 220 220", width: "220", height: "220" });
    svg.appendChild(
      svgEl("circle", { cx: 110, cy: 110, r: 82, fill: "none", stroke: "#1f2937", "stroke-width": 18 })
    );
    svg.appendChild(
      svgEl("circle", { cx: 110, cy: 110, r: 58, fill: "none", stroke: "#1f2937", "stroke-width": 12 })
    );

    appendDonutRing(svg, 110, 110, 82, 18, [
      { pct: aAes, color: "#38bdf8" },
      { pct: aCog, color: "#a78bfa" },
      { pct: aDom, color: "#f59e0b" },
    ]);
    appendDonutRing(svg, 110, 110, 58, 12, [
      { pct: tAes, color: "rgba(56,189,248,0.45)" },
      { pct: tCog, color: "rgba(167,139,250,0.45)" },
      { pct: tDom, color: "rgba(245,158,11,0.45)" },
    ]);

    const scoreTxt = svgEl("text", {
      x: 110,
      y: 108,
      "text-anchor": "middle",
      fill: "#e6edf7",
      "font-size": "24",
      "font-weight": "700",
      "font-family": "monospace",
    });
    scoreTxt.textContent = `${Math.round(Number(audit.composite_health_score ?? 0))}`;
    svg.appendChild(scoreTxt);

    const subTxt = svgEl("text", {
      x: 110,
      y: 126,
      "text-anchor": "middle",
      fill: "#93a1bc",
      "font-size": "10",
      "font-family": "sans-serif",
    });
    subTxt.textContent = "HEALTH / 100";
    svg.appendChild(subTxt);
    donutStage.appendChild(svg);

    const legendItems = [
      ["pillar-aes", `Aesthetic ${aAes.toFixed(1)}% (target ${tAes.toFixed(0)}%)`],
      ["pillar-cog", `Cognitive ${aCog.toFixed(1)}% (target ${tCog.toFixed(0)}%)`],
      ["pillar-dom", `Domain ${aDom.toFixed(1)}% (target ${tDom.toFixed(0)}%)`],
    ];
    for (const [dotClass, label] of legendItems) {
      legendEl.appendChild(
        el("span", { class: "legend-item" }, [el("span", { class: `pillar-dot ${dotClass}` }), label])
      );
    }

    const kpis = [
      ["Composite Health", `${Number(audit.composite_health_score ?? 0).toFixed(1)} / 100`],
      ["GR Fidelity", `${Number(audit.golden_ratio_fidelity_score ?? 0).toFixed(1)}%`],
      ["TVD Drift", Number(audit.golden_ratio_tvd_drift ?? 0).toFixed(3)],
      ["Shannon Entropy", Number(audit.shannon_domain_entropy_norm ?? 0).toFixed(3)],
      ["Gini Concentration", Number(audit.gini_velocity_concentration ?? 0).toFixed(3)],
      [
        "Max Dominance",
        `${Number(audit.max_subreddit_dominance_pct ?? 0).toFixed(1)}% (${audit.dominant_subreddit || "n/a"})`,
      ],
      ["Mean Signal Density", Number(audit.mean_signal_density ?? 0).toFixed(2)],
      ["Total Velocity", `${Number(audit.total_daily_velocity ?? 0).toFixed(1)} posts/d`],
    ];
    for (const [label, val] of kpis) {
      kpiGrid.appendChild(
        el("div", { class: "kpi-box" }, [
          el("div", { class: "kpi-label", text: label }),
          el("div", { class: "kpi-value", text: val }),
        ])
      );
    }

    const recs = audit.recommendations || [];
    if (recs.length === 0) {
      recList.appendChild(el("li", { text: "Diet is well-balanced across all Golden Ratio pillars." }));
    } else {
      for (const r of recs) {
        recList.appendChild(el("li", { text: r }));
      }
    }

    const shares = audit.per_subreddit_shares || [];
    if (shares.length > 0) {
      const table = el("table", {}, [
        el("thead", {}, [
          el("tr", {}, [
            el("th", { text: "Subreddit" }),
            el("th", { text: "Pillar" }),
            el("th", { text: "Posts/d" }),
            el("th", { text: "Share %" }),
          ]),
        ]),
        el(
          "tbody",
          {},
          shares.map((row) =>
            el("tr", {}, [
              el("td", { text: `r/${row.name}` }),
              el("td", { text: row.category || "" }),
              el("td", { text: Number(row.estimated_daily_posts ?? 0).toFixed(1) }),
              el("td", { text: `${Number(row.impression_share_pct ?? 0).toFixed(1)}%` }),
            ])
          )
        ),
      ]);
      tableWrap.appendChild(table);
    }
  }

  // --- Confirmation Modal ---
  function requestConfirmation({ title, message, confirmLabel = "Confirm", dangerous = true }) {
    const modal = document.getElementById("studio-confirm-modal");
    const titleEl = document.getElementById("confirm-modal-title");
    const msgEl = document.getElementById("confirm-modal-message");
    const proceedBtn = document.getElementById("btn-confirm-proceed");
    const cancelBtn = document.getElementById("btn-confirm-cancel");
    if (!modal || !titleEl || !msgEl || !proceedBtn || !cancelBtn) {
      return Promise.resolve(false);
    }
    if (state.confirmResolver) {
      state.confirmResolver(false);
      state.confirmResolver = null;
    }
    state.confirmReturnFocusEl = document.activeElement;
    titleEl.textContent = title || "Confirm Action";
    msgEl.textContent = message || "";
    proceedBtn.textContent = confirmLabel;
    proceedBtn.className = dangerous ? "btn btn-danger" : "btn btn-primary";
    modal.classList.remove("hidden");
    cancelBtn.focus();

    return new Promise((resolve) => {
      state.confirmResolver = resolve;
    });
  }

  function resolveConfirmation(accepted) {
    const modal = document.getElementById("studio-confirm-modal");
    if (modal) modal.classList.add("hidden");
    const resolver = state.confirmResolver;
    state.confirmResolver = null;
    if (state.confirmReturnFocusEl && typeof state.confirmReturnFocusEl.focus === "function") {
      state.confirmReturnFocusEl.focus();
    }
    state.confirmReturnFocusEl = null;
    if (resolver) resolver(Boolean(accepted));
  }

  // --- Structured Diff / Sync / Rollback Cards ---
  function buildRawJsonDetails(obj) {
    return el("details", { class: "raw-json-details" }, [
      el("summary", { text: "Raw JSON payload" }),
      el("pre", { class: "code-log", text: JSON.stringify(obj, null, 2) }),
    ]);
  }

  function renderDiffCard(box, diff) {
    clearNode(box);
    const toSub = diff.to_subscribe || [];
    const already = diff.already_subscribed || [];
    const toUnsub = diff.to_unsubscribe || [];
    const kept = diff.unmanaged_kept || [];
    const feeds = diff.custom_feeds || {};
    const unmanagedFeeds = diff.unmanaged_custom_feeds || [];

    const card = el("div", { class: "summary-card" }, [
      el("div", { class: "summary-badges" }, [
        el("span", { class: "badge badge-aes", text: `+ Subscribe (${toSub.length})` }),
        el("span", { class: "badge badge-accent", text: `✓ Already Followed (${already.length})` }),
        el("span", { class: `badge${toUnsub.length > 0 ? " badge-dom" : ""}`, text: `- Prune (${toUnsub.length})` }),
        el("span", { class: "badge", text: `Kept (${kept.length})` }),
        el("span", { class: "badge badge-cog", text: `Custom Feeds (${Object.keys(feeds).length})` }),
        unmanagedFeeds.length > 0
          ? el("span", { class: "badge", text: `Unmanaged Feeds (${unmanagedFeeds.length})` })
          : null,
      ]),
      toSub.length > 0
        ? el("div", { class: "meta-line", text: `Subscribe: ${toSub.map((s) => `r/${s}`).join(", ")}` })
        : el("div", { class: "meta-line", text: "All Tier-1 subreddits already subscribed." }),
      toUnsub.length > 0
        ? el("div", { class: "summary-warn-box", text: `Will prune: ${toUnsub.map((s) => `r/${s}`).join(", ")}` })
        : null,
      buildRawJsonDetails(diff),
    ]);
    box.appendChild(card);
  }

  function renderSyncCard(box, sync) {
    clearNode(box);
    const subInfo = sync.subscriptions || {};
    const subscribed = subInfo.subscribed || [];
    const already = subInfo.already_subscribed || [];
    const unsubscribed = subInfo.unsubscribed || [];
    const feeds = sync.custom_feeds || {};
    const failures = extractSyncFailures(sync);

    const cardCls = failures.hasFailures ? (subscribed.length === 0 && already.length === 0 ? " err" : " warn") : "";
    const modeLabel = sync.mode === "offline_dry_run"
      ? "Offline Dry-Run"
      : sync.dry_run
        ? "Live Dry-Run"
        : failures.hasFailures
          ? "Live Sync (Partial/Failed)"
          : "Live Sync Complete";

    const card = el("div", { class: `summary-card${cardCls}` }, [
      el("div", { class: "summary-badges" }, [
        el("span", { class: "badge badge-accent", text: modeLabel }),
        sync.username ? el("span", { class: "badge", text: `u/${sync.username}` }) : null,
        sync.snapshot_id ? el("span", { class: "badge", text: `Snapshot: ${sync.snapshot_id}` }) : null,
        el("span", { class: "badge badge-aes", text: `+ Subscribed (${subscribed.length})` }),
        el("span", { class: "badge", text: `✓ Followed (${already.length})` }),
        unsubscribed.length > 0 ? el("span", { class: "badge badge-dom", text: `- Unsubscribed (${unsubscribed.length})` }) : null,
        el("span", { class: "badge badge-cog", text: `Custom Feeds (${Object.keys(feeds).length})` }),
        failures.hasFailures
          ? el("span", { class: "badge badge-dom", text: `⚠ Failures (${failures.totalIssues})` })
          : null,
      ]),
      failures.hasFailures
        ? el("div", {
            class: "summary-warn-box",
            text: [
              failures.failedSubs.length > 0 ? `Failed subs: ${failures.failedSubs.join(", ")}` : "",
              failures.failedFeeds.length > 0 ? `Failed feeds: ${failures.failedFeeds.map((f) => `${f.name} (${f.error})`).join(", ")}` : "",
              failures.followingFailed ? "Following feed toggle failed" : "",
              ...failures.warnings,
            ]
              .filter(Boolean)
              .join(" · "),
          })
        : null,
      buildRawJsonDetails(sync),
    ]);
    box.appendChild(card);
  }

  function renderRollbackCard(box, rb) {
    clearNode(box);
    const restored = rb.restored_subscriptions || [];
    const removed = rb.removed_subscriptions || [];
    const restoredFeeds = Object.keys(rb.restored_custom_feeds || {});
    const deletedFeeds = rb.deleted_custom_feeds || [];
    const ok = rb.success !== false;

    const card = el("div", { class: `summary-card${ok ? "" : " err"}` }, [
      el("div", { class: "summary-badges" }, [
        el("span", { class: "badge badge-accent", text: rb.dry_run ? "Dry-Run Rollback" : "Live Rollback" }),
        el("span", { class: "badge", text: `Snapshot: ${rb.snapshot_id || ""}` }),
        rb.username ? el("span", { class: "badge", text: `u/${rb.username}` }) : null,
        el("span", { class: "badge badge-aes", text: `+ Restored (${restored.length})` }),
        el("span", { class: "badge badge-dom", text: `- Removed (${removed.length})` }),
        el("span", { class: "badge badge-cog", text: `Feeds (${restoredFeeds.length} restored / ${deletedFeeds.length} deleted)` }),
      ]),
      buildRawJsonDetails(rb),
    ]);
    box.appendChild(card);
  }

  // --- Console: Browser, Diff, Sync, Snapshots ---
  async function handleLaunchBrowser() {
    const log = document.getElementById("browser-launch-log");
    const preferred = document.getElementById("select-browser").value || null;
    await withButtonBusy("btn-launch-browser", "Launching...", async () => {
      try {
        const port = getCdpPort();
        log.textContent = "Launching browser...";
        const res = await apiPost("/api/launch-browser", { port, browser: preferred });
        log.textContent = JSON.stringify(res.launch || res, null, 2);
        showToast("Browser CDP ready");
        await refreshStatus(true);
      } catch (err) {
        log.textContent = `Launch error: ${formatFriendlyError(err.message)}`;
        showToast(`Launch failed: ${err.message}`, "err");
      }
    });
  }

  function getKeepList() {
    return (document.getElementById("input-keep").value || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  }

  async function handleDiff() {
    const m = getEffectiveManifest();
    if (!m) {
      showToast("Load or generate a manifest first.", "err");
      return;
    }
    const box = document.getElementById("diff-preview-box");
    clearNode(box);
    await withButtonBusy("btn-diff", "Computing...", async () => {
      try {
        const cdpPort = getCdpPort();
        const res = await apiPost("/api/diff", {
          manifest: m,
          prune: Boolean(document.getElementById("check-prune").checked),
          keep: getKeepList(),
          cdp_port: cdpPort,
        });
        renderDiffCard(box, res.diff || {});
        showToast("Computed live subscription diff");
      } catch (err) {
        box.appendChild(el("div", { class: "meta-line", text: `Diff error: ${formatFriendlyError(err.message)}` }));
        showToast(`Diff error: ${err.message}`, "err");
      }
    });
  }

  async function handleSync(dryRun) {
    const m = getEffectiveManifest();
    if (!m) {
      showToast("Load or generate a manifest first.", "err");
      return;
    }
    const prune = Boolean(document.getElementById("check-prune").checked);
    if (!dryRun && prune) {
      const confirmed = await requestConfirmation({
        title: "Confirm Prune Live Sync",
        message: "Prune mode (--prune) will unsubscribe from unmanaged subreddits on your live Reddit account (excluding your Keep Allowlist). A pre-sync snapshot will be saved first.",
        confirmLabel: "Proceed with Prune Sync",
        dangerous: true,
      });
      if (!confirmed) return;
    }

    const box = document.getElementById("diff-preview-box");
    clearNode(box);
    const btnId = dryRun ? "btn-sync-dry" : "btn-sync-live";
    const busyText = dryRun ? "Running Dry-Run..." : "Syncing...";
    await withButtonBusy(btnId, busyText, async () => {
      try {
        const cdpPort = getCdpPort();
        const res = await apiPost("/api/sync", {
          manifest: m,
          dry_run: Boolean(dryRun),
          prune,
          keep: getKeepList(),
          cdp_port: cdpPort,
        });
        const sync = res.sync || res;
        renderSyncCard(box, sync);
        const failures = extractSyncFailures(sync);
        if (failures.hasFailures) {
          showToast(
            dryRun ? "Dry-run reported warnings" : "Live sync finished with failures — review details",
            "err"
          );
        } else {
          showToast(dryRun ? "Dry-run sync completed" : "Live sync completed");
        }
        if (!dryRun) {
          await loadSnapshots();
          await refreshStatus(true);
        }
      } catch (err) {
        box.appendChild(el("div", { class: "meta-line", text: `Sync error: ${formatFriendlyError(err.message)}` }));
        showToast(`Sync error: ${err.message}`, "err");
      }
    });
  }

  async function loadSnapshots() {
    const wrap = document.getElementById("snapshots-table-wrap");
    if (!wrap) return;
    clearNode(wrap);
    try {
      const res = await apiGet("/api/snapshots");
      const snaps = res.snapshots || [];
      if (snaps.length === 0) {
        wrap.appendChild(el("div", { class: "meta-line", text: "No pre-sync snapshots recorded yet." }));
        return;
      }
      const table = el("table", {}, [
        el("thead", {}, [
          el("tr", {}, [
            el("th", { text: "Snapshot ID" }),
            el("th", { text: "Manifest" }),
            el("th", { text: "Created" }),
            el("th", { text: "User" }),
            el("th", { text: "Subs" }),
            el("th", { text: "Feeds" }),
            el("th", { text: "Actions" }),
          ]),
        ]),
        el(
          "tbody",
          {},
          snaps.map((s) =>
            el("tr", {}, [
              el("td", { text: s.snapshot_id || "" }),
              el("td", { text: s.manifest_name || "—" }),
              el("td", { text: s.created_at || "" }),
              el("td", { text: s.username || "" }),
              el("td", { text: String(s.subscriptions_count ?? 0) }),
              el("td", { text: String(s.custom_feeds_count ?? 0) }),
              el("td", {}, [
                el("div", { class: "action-row-tight" }, [
                  el("button", {
                    type: "button",
                    class: "btn btn-ghost btn-sm",
                    text: "Dry-Run",
                    onclick: (ev) => handleRollback(s.snapshot_id, true, ev.currentTarget),
                  }),
                  el("button", {
                    type: "button",
                    class: "btn btn-danger btn-sm",
                    text: "Rollback",
                    onclick: (ev) => handleRollback(s.snapshot_id, false, ev.currentTarget),
                  }),
                ]),
              ]),
            ])
          )
        ),
      ]);
      wrap.appendChild(table);
    } catch (err) {
      wrap.appendChild(el("div", { class: "meta-line", text: `Snapshots error: ${err.message}` }));
    }
  }

  async function handleRollback(snapshotId, dryRun, triggerBtn = null) {
    if (!dryRun) {
      const confirmed = await requestConfirmation({
        title: "Confirm Live Rollback",
        message: `Live Rollback will restore your Reddit subscriptions and custom feeds to snapshot "${snapshotId}", overwriting subsequent changes.`,
        confirmLabel: "Proceed with Rollback",
        dangerous: true,
      });
      if (!confirmed) return;
    }

    await withButtonBusy(triggerBtn, dryRun ? "Previewing..." : "Rolling back...", async () => {
      try {
        const cdpPort = getCdpPort();
        const res = await apiPost("/api/rollback", {
          snapshot_id: snapshotId,
          dry_run: dryRun,
          cdp_port: cdpPort,
        });
        const box = document.getElementById("diff-preview-box");
        const rb = res.rollback || res;
        renderRollbackCard(box, rb);
        if ((res && res.rollback && res.rollback.success === false) || (rb && rb.success === false)) {
          showToast(`Rollback finished with errors (${snapshotId}) — review details`, "err");
        } else {
          showToast(dryRun ? `Dry-run rollback previewed (${snapshotId})` : `Rolled back to ${snapshotId}`);
        }
        if (!dryRun) {
          await loadSnapshots();
          await refreshStatus(true);
        }
      } catch (err) {
        showToast(`Rollback failed: ${formatFriendlyError(err.message)}`, "err");
      }
    });
  }

  // --- Mode-Agnostic About & Philosophy View ---
  async function ensureAboutLoaded() {
    if (state.aboutData) return state.aboutData;
    const data = await apiGet("/api/about");
    state.aboutData = data;
    return data;
  }

  function renderAboutReferences() {
    const grid = document.getElementById("about-references-grid");
    const countBadge = document.getElementById("about-ref-count");
    const searchEl = document.getElementById("about-ref-search");
    if (!grid) return;
    clearNode(grid);

    const data = state.aboutData || {};
    const refs = Array.isArray(data.references) ? data.references : [];
    const query = ((searchEl && searchEl.value) || "").trim().toLowerCase();
    const activeTheme = state.aboutTheme || "all";

    const filtered = refs.filter((item) => {
      if (activeTheme !== "all" && item.theme !== activeTheme) return false;
      if (!query) return true;
      const hay = [
        item.authors,
        item.year,
        item.title,
        item.publication,
        item.doi,
        item.theme_label,
        item.key_finding,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return hay.includes(query);
    });

    if (countBadge) {
      countBadge.textContent = `${filtered.length} of ${refs.length} Verified Sources`;
    }

    if (filtered.length === 0) {
      grid.appendChild(el("div", { class: "meta-line", text: "No matching peer-reviewed sources found." }));
      return;
    }

    for (const item of filtered) {
      const linkNode = isSafeHttpsUrl(item.url)
        ? el("a", {
            class: "cite-link",
            href: item.url,
            target: "_blank",
            rel: "noopener noreferrer",
            text: item.doi ? `DOI: ${item.doi} ↗` : "Verified Source ↗",
          })
        : null;

      grid.appendChild(
        el("article", { class: "ref-card" }, [
          el("div", {}, [
            el("div", { class: "action-row-tight" }, [
              el("span", { class: "badge badge-accent", text: item.theme_label || item.theme || "Research" }),
              el("span", { class: "badge", text: String(item.year || "") }),
            ]),
            el("h3", { class: "ref-title", text: item.title || "" }),
            el("div", { class: "ref-meta", text: `${item.authors || ""} · ${item.publication || ""}` }),
          ]),
          el("p", { class: "ref-finding", text: item.key_finding || "" }),
          linkNode,
        ])
      );
    }
  }

  function renderAboutView() {
    const data = state.aboutData;
    if (!data) return;

    const titleEl = document.getElementById("about-title");
    const mottoEl = document.getElementById("about-motto");
    const sparkEl = document.getElementById("about-spark-summary");
    const countsRow = document.getElementById("about-counts-row");
    const statsGrid = document.getElementById("about-stats-grid");
    const invHead = document.getElementById("about-inversion-heading");
    const invSum = document.getElementById("about-inversion-summary");
    const mechList = document.getElementById("about-mechanisms-list");
    const harmHead = document.getElementById("about-harm-heading");
    const harmSum = document.getElementById("about-harm-summary");
    const backfireList = document.getElementById("about-backfire-list");
    const pillarsGrid = document.getElementById("about-pillars-grid");
    const controlsGrid = document.getElementById("about-controls-grid");
    const themeChips = document.getElementById("about-theme-chips");

    if (titleEl && data.title) titleEl.textContent = data.title;
    if (mottoEl) mottoEl.textContent = data.motto || "";
    if (sparkEl) sparkEl.textContent = (data.original_spark && data.original_spark.summary) || "";

    if (countsRow) {
      clearNode(countsRow);
      const refsCount = Array.isArray(data.references) ? data.references.length : 0;
      countsRow.appendChild(el("span", { class: "badge badge-accent", text: `${refsCount} Peer-Reviewed & Verified Sources` }));
      countsRow.appendChild(el("span", { class: "badge badge-aes", text: `${data.ontology_subreddit_count || 0} Ontology Communities` }));
      countsRow.appendChild(el("span", { class: "badge badge-cog", text: `${data.presets_count || 0} Curated Presets` }));
    }

    if (statsGrid) {
      clearNode(statsGrid);
      for (const stat of data.global_statistics || []) {
        const srcLink = isSafeHttpsUrl(stat.url)
          ? el("a", {
              class: "cite-link",
              href: stat.url,
              target: "_blank",
              rel: "noopener noreferrer",
              text: `${stat.source || "Source"} ↗`,
            })
          : el("span", { class: "meta-line", text: stat.source || "" });

        statsGrid.appendChild(
          el("article", { class: "about-stat-card" }, [
            el("div", { class: "about-stat-val", text: stat.value || "" }),
            el("div", { class: "about-stat-label", text: stat.label || "" }),
            el("div", { class: "preset-desc", text: stat.detail || "" }),
            srcLink,
          ])
        );
      }
    }

    const inv = data.core_inversion || {};
    if (invHead && inv.heading) invHead.textContent = inv.heading;
    if (invSum) invSum.textContent = inv.summary || "";
    if (mechList) {
      clearNode(mechList);
      for (const m of inv.mechanisms || []) {
        mechList.appendChild(
          el("li", {}, [el("strong", { text: `${m.name}: ` }), m.description || ""])
        );
      }
    }

    const harm = data.harm_reduction_thesis || {};
    if (harmHead && harm.heading) harmHead.textContent = harm.heading;
    if (harmSum) harmSum.textContent = harm.summary || "";
    if (backfireList) {
      clearNode(backfireList);
      for (const b of harm.why_bans_backfire || []) {
        backfireList.appendChild(
          el("li", {}, [el("strong", { text: `${b.name}: ` }), b.description || ""])
        );
      }
    }

    if (pillarsGrid) {
      clearNode(pillarsGrid);
      const badgeMap = {
        aesthetic_leisure: "badge-aes",
        cognitive_curiosity: "badge-cog",
        domain_priming: "badge-dom",
      };
      for (const p of data.pillars || []) {
        pillarsGrid.appendChild(
          el("div", { class: "about-mini-card" }, [
            el("div", { class: "action-row-tight" }, [
              el("span", { class: `badge ${badgeMap[p.id] || "badge-accent"}`, text: `${p.target_pct || 0}% Target` }),
              el("span", { class: "meta-line", text: p.tier || "" }),
            ]),
            el("div", { class: "about-mini-title", text: p.name || "" }),
            el("div", { class: "preset-desc", text: p.role || "" }),
          ])
        );
      }
    }

    if (controlsGrid) {
      clearNode(controlsGrid);
      for (const c of harm.controls || []) {
        controlsGrid.appendChild(
          el("div", { class: "about-mini-card" }, [
            el("div", { class: "about-mini-title", text: c.name || "" }),
            el("div", { class: "preset-desc", text: c.description || "" }),
          ])
        );
      }
    }

    if (themeChips) {
      const focusedTheme = document.activeElement && themeChips.contains(document.activeElement)
        ? document.activeElement.getAttribute("data-theme")
        : null;
      clearNode(themeChips);
      const refs = Array.isArray(data.references) ? data.references : [];
      const themes = Array.isArray(data.themes) && data.themes.length > 0
        ? data.themes
        : [{ id: "all", label: "All Sources" }];
      let restoreBtn = null;
      for (const t of themes) {
        const cnt = t.id === "all" ? refs.length : refs.filter((r) => r.theme === t.id).length;
        const active = (state.aboutTheme || "all") === t.id;
        const chipBtn = el("button", {
          type: "button",
          class: `chip chip-btn${active ? " active" : ""}`,
          "data-theme": t.id,
          text: `${t.label} (${cnt})`,
          onclick: () => {
            state.aboutTheme = t.id;
            renderAboutView();
          },
        });
        if (focusedTheme === t.id) restoreBtn = chipBtn;
        themeChips.appendChild(chipBtn);
      }
      if (restoreBtn) restoreBtn.focus();
    }

    renderAboutReferences();
  }

  async function openAboutView(openerId = null) {
    state.aboutReturnFocusEl = document.activeElement || null;
    if (openerId) {
      state.aboutReturnFocusId = openerId;
    } else if (document.activeElement && document.activeElement.id) {
      state.aboutReturnFocusId = document.activeElement.id;
    }
    state.aboutOpen = true;
    document.body.classList.add("about-open");
    if (typeof window.scrollTo === "function") {
      window.scrollTo(0, 0);
    }
    const section = document.getElementById("view-about");
    const topBtn = document.getElementById("btn-about");
    if (section) section.classList.remove("hidden");
    if (topBtn) {
      topBtn.classList.add("active");
      topBtn.setAttribute("aria-pressed", "true");
    }
    try {
      await ensureAboutLoaded();
      renderAboutView();
    } catch (err) {
      showToast(`Failed to load philosophy data: ${err.message}`, "err");
    }
    const closeBtn = document.getElementById("btn-about-close");
    if (closeBtn) closeBtn.focus();
  }

  function closeAboutView(restoreFocus = true) {
    state.aboutOpen = false;
    document.body.classList.remove("about-open");
    const section = document.getElementById("view-about");
    const topBtn = document.getElementById("btn-about");
    if (section) section.classList.add("hidden");
    if (topBtn) {
      topBtn.classList.remove("active");
      topBtn.setAttribute("aria-pressed", "false");
    }
    if (restoreFocus) {
      const openerById = state.aboutReturnFocusId ? document.getElementById(state.aboutReturnFocusId) : null;
      const target = openerById || state.aboutReturnFocusEl;
      if (target && typeof target.focus === "function") {
        target.focus();
      }
    }
    state.aboutReturnFocusId = null;
    state.aboutReturnFocusEl = null;
  }

  function setupJsonDropTarget(node) {
    if (!node) return;
    node.addEventListener("dragover", (ev) => {
      ev.preventDefault();
      node.classList.add("dragover");
    });
    node.addEventListener("dragleave", () => node.classList.remove("dragover"));
    node.addEventListener("drop", (ev) => {
      ev.preventDefault();
      node.classList.remove("dragover");
      const file = ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
      if (file) handleImportManifestFile(file);
    });
  }

  function setupEvents() {
    const tabButtons = [...document.querySelectorAll(".tab-btn")];
    tabButtons.forEach((btn, idx) => {
      btn.addEventListener("click", () => switchTab(btn.getAttribute("data-tab")));
      btn.addEventListener("keydown", (ev) => {
        if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
          ev.preventDefault();
          const delta = ev.key === "ArrowRight" ? 1 : -1;
          const nextIdx = (idx + delta + tabButtons.length) % tabButtons.length;
          const nextBtn = tabButtons[nextIdx];
          if (nextBtn) {
            switchTab(nextBtn.getAttribute("data-tab"));
            nextBtn.focus();
          }
        }
      });
    });

    document.querySelectorAll("#query-presets .chip-btn").forEach((chip) => {
      chip.addEventListener("click", () => {
        document.getElementById("input-query").value = chip.getAttribute("data-query") || "";
      });
    });

    document.getElementById("btn-mode-easy").addEventListener("click", () => setUiMode("easy"));
    document.getElementById("btn-mode-advanced").addEventListener("click", () => setUiMode("advanced"));
    document.getElementById("btn-about").addEventListener("click", () => {
      if (state.aboutOpen) {
        closeAboutView();
      } else {
        openAboutView("btn-about");
      }
    });
    document.getElementById("easy-btn-about").addEventListener("click", () => openAboutView("easy-btn-about"));
    document.getElementById("btn-about-close").addEventListener("click", () => closeAboutView());
    document.getElementById("about-ref-search").addEventListener("input", renderAboutReferences);

    const importInput = document.getElementById("import-manifest-input");
    const triggerImport = () => {
      importInput.value = "";
      importInput.click();
    };
    document.getElementById("btn-import-manifest").addEventListener("click", triggerImport);
    document.getElementById("easy-btn-import-json").addEventListener("click", triggerImport);
    document.getElementById("easy-btn-export-json").addEventListener("click", exportManifest);
    document.getElementById("easy-btn-copy-json").addEventListener("click", copyManifestJson);

    setupJsonDropTarget(document.getElementById("easy-step-1"));
    setupJsonDropTarget(document.querySelector(".topbar"));

    document.getElementById("easy-preset-search").addEventListener("input", renderEasyPresets);
    document.getElementById("easy-btn-launch").addEventListener("click", handleEasyLaunch);
    document.getElementById("easy-btn-check").addEventListener("click", () =>
      withButtonBusy("easy-btn-check", "Checking...", () => refreshStatus(true))
    );
    document.getElementById("easy-btn-preview").addEventListener("click", handleEasyPreview);
    document.getElementById("easy-btn-apply").addEventListener("click", handleEasyApply);
    importInput.addEventListener("change", () => {
      const file = importInput.files && importInput.files[0];
      importInput.value = "";
      if (file) {
        handleImportManifestFile(file);
      }
    });
    document.getElementById("btn-export-manifest").addEventListener("click", exportManifest);
    document.getElementById("btn-quick-audit").addEventListener("click", () => {
      switchTab("analytics");
      auditActiveManifest();
    });
    document.getElementById("btn-quick-inspect").addEventListener("click", () => switchTab("inspector"));
    document.getElementById("btn-quick-console").addEventListener("click", () => switchTab("console"));

    document.getElementById("btn-generate").addEventListener("click", handleGenerate);
    document.getElementById("inspector-search").addEventListener("input", renderInspector);
    document.getElementById("inspector-pillar").addEventListener("change", renderInspector);
    document.getElementById("inspector-tier").addEventListener("change", renderInspector);
    document.getElementById("btn-inspector-reset").addEventListener("click", resetToOriginalManifest);
    document.getElementById("btn-inspector-add-sub").addEventListener("click", () => {
      const subInput = document.getElementById("inspector-add-sub");
      const pillarSel = document.getElementById("inspector-add-pillar");
      const tierSel = document.getElementById("inspector-add-tier");
      addSubredditToManifest(subInput.value, pillarSel.value, tierSel.value);
      subInput.value = "";
    });
    document.getElementById("btn-inspector-add-feed").addEventListener("click", () => {
      const feedInput = document.getElementById("inspector-new-feed-name");
      addCustomFeedToManifest(feedInput.value);
      feedInput.value = "";
    });

    document.getElementById("gallery-search").addEventListener("input", renderPresetGallery);
    document.getElementById("btn-mixer-add").addEventListener("click", () => {
      addPresetToMixer(document.getElementById("mixer-preset-select").value);
    });
    document.getElementById("btn-mix-blend").addEventListener("click", handleMixBlend);
    document.getElementById("btn-mix-clear").addEventListener("click", () => {
      state.mixerWeights = {};
      renderMixerList();
    });

    document.getElementById("btn-audit-manifest").addEventListener("click", () => auditActiveManifest(false));
    document.getElementById("btn-audit-live").addEventListener("click", auditLiveAccount);

    document.getElementById("input-cdp-port").addEventListener("change", () => {
      try {
        getCdpPort();
        refreshStatus(true);
      } catch (err) {
        showToast(err.message, "err");
      }
    });
    document.getElementById("btn-launch-browser").addEventListener("click", handleLaunchBrowser);
    document.getElementById("btn-refresh-status").addEventListener("click", () =>
      withButtonBusy("btn-refresh-status", "Refreshing...", () => refreshStatus(true))
    );
    document.getElementById("btn-diff").addEventListener("click", handleDiff);
    document.getElementById("btn-sync-dry").addEventListener("click", () => handleSync(true));
    document.getElementById("btn-sync-live").addEventListener("click", () => handleSync(false));
    document.getElementById("btn-refresh-snapshots").addEventListener("click", () =>
      withButtonBusy("btn-refresh-snapshots", "Refreshing...", loadSnapshots)
    );

    document.getElementById("btn-confirm-proceed").addEventListener("click", () => resolveConfirmation(true));
    document.getElementById("btn-confirm-cancel").addEventListener("click", () => resolveConfirmation(false));

    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") {
        if (state.confirmResolver) {
          ev.preventDefault();
          resolveConfirmation(false);
          return;
        }
        if (state.aboutOpen) {
          ev.preventDefault();
          closeAboutView();
          return;
        }
      }
      if ((ev.ctrlKey || ev.metaKey) && !ev.altKey && ev.key.toLowerCase() === "o") {
        ev.preventDefault();
        const importInputEl = document.getElementById("import-manifest-input");
        importInputEl.value = "";
        importInputEl.click();
        return;
      }
      if ((ev.ctrlKey || ev.metaKey) && !ev.altKey && ev.key.toLowerCase() === "e") {
        ev.preventDefault();
        exportManifest();
        return;
      }
      const tag = (ev.target && ev.target.tagName) || "";
      if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
      if (ev.key === "?" && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
        ev.preventDefault();
        if (state.aboutOpen) {
          closeAboutView();
        } else {
          openAboutView();
        }
        return;
      }
      if (ev.ctrlKey || ev.altKey || ev.metaKey || ev.shiftKey) return;
      if (state.aboutOpen || state.confirmResolver) return;
      const tabMap = { "1": "lab", "2": "inspector", "3": "gallery", "4": "analytics", "5": "console" };
      if (tabMap[ev.key] && state.uiMode === "advanced") {
        switchTab(tabMap[ev.key]);
      }
    });
  }

  async function init() {
    let savedMode = "easy";
    try {
      if (localStorage.getItem("rcurator.uiMode") === "advanced") {
        savedMode = "advanced";
      }
    } catch (_) {
      savedMode = "easy";
    }
    loadPinnedPresets();
    setUiMode(savedMode);
    setupRatioSliders();
    setupResumeDropzone();
    setupEvents();
    renderInspector();
    renderMixerList();
    updateEasyExportButtons();
    await refreshStatus(false);
    await loadPresets();
    await loadSnapshots();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
