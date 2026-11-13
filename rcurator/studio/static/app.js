(() => {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";

  const state = {
    csrfToken: "",
    status: null,
    presets: [],
    activeManifest: null,
    excludedSubs: new Set(),
    resumeEvidences: [],
    resumeFilename: "",
    mixerWeights: {},
    lastAudit: null,
  };

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [key, val] of Object.entries(attrs)) {
        if (val === null || val === undefined) continue;
        if (key === "class") {
          node.className = String(val);
        } else if (key === "text") {
          node.textContent = String(val);
        } else if (key.startsWith("on") && typeof val === "function") {
          node.addEventListener(key.slice(2).toLowerCase(), val);
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
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function showToast(message, kind = "ok") {
    const rack = document.getElementById("toast-rack");
    if (!rack) return;
    const toast = el("div", { class: `toast toast-${kind}`, text: message });
    rack.appendChild(toast);
    window.setTimeout(() => {
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }, 3800);
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
      await refreshStatus();
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
      await refreshStatus();
    }
    const buf = await file.arrayBuffer();
    const res = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": file.type || "application/octet-stream",
        "X-Filename": file.name || "resume.pdf",
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
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      const active = btn.getAttribute("data-tab") === tabName;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll(".view").forEach((section) => {
      section.classList.toggle("active", section.id === `view-${tabName}`);
    });
  }

  function getEffectiveManifest() {
    if (!state.activeManifest) return null;
    const clone = JSON.parse(JSON.stringify(state.activeManifest));
    if (state.excludedSubs.size === 0) return clone;

    const keepEntry = (entry) => {
      const name = typeof entry === "string" ? entry : entry.name;
      return name && !state.excludedSubs.has(name.toLowerCase());
    };
    if (Array.isArray(clone.anchor_subreddits)) {
      clone.anchor_subreddits = clone.anchor_subreddits.filter(keepEntry);
    }
    if (Array.isArray(clone.domain_priming_subreddits)) {
      clone.domain_priming_subreddits = clone.domain_priming_subreddits.filter(keepEntry);
    }
    if (Array.isArray(clone.custom_feeds)) {
      clone.custom_feeds = clone.custom_feeds.map((feed) => ({
        ...feed,
        subreddits: (feed.subreddits || []).filter((s) => !state.excludedSubs.has(String(s).toLowerCase())),
      }));
    }
    return clone;
  }

  function setActiveManifest(manifest, resetExcluded = true) {
    state.activeManifest = manifest;
    if (resetExcluded) {
      state.excludedSubs.clear();
    }
    updateActiveBar();
    renderInspector();
  }

  function updateActiveBar() {
    const nameEl = document.getElementById("active-manifest-name");
    const countsEl = document.getElementById("active-manifest-counts");
    const m = getEffectiveManifest();
    if (!m) {
      nameEl.textContent = "No manifest loaded";
      countsEl.textContent = "0 tier-1 · 0 custom feeds";
      return;
    }
    const anchors = Array.isArray(m.anchor_subreddits) ? m.anchor_subreddits.length : 0;
    const priming = Array.isArray(m.domain_priming_subreddits || m.tier1_systems_subreddits)
      ? (m.domain_priming_subreddits || m.tier1_systems_subreddits).length
      : 0;
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
    const blob = new Blob([JSON.stringify(m, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = el("a", { href: url, download: "manifest.json" });
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
    showToast("Downloaded manifest.json");
  }

  async function refreshStatus() {
    try {
      const status = await apiGet("/api/status");
      state.status = status;
      state.csrfToken = status.csrf_token || "";

      const cdpPill = document.getElementById("pill-cdp");
      cdpPill.textContent = status.cdp_connected ? `CDP: port ${status.cdp_port}` : "CDP: offline";
      cdpPill.className = `status-pill ${status.cdp_connected ? "status-ok" : "status-off"}`;

      const authPill = document.getElementById("pill-auth");
      authPill.textContent = status.authenticated && status.username
        ? `Reddit: u/${status.username}`
        : "Reddit: disconnected";
      authPill.className = `status-pill ${status.authenticated ? "status-ok" : "status-off"}`;

      const ontPill = document.getElementById("pill-ontology");
      ontPill.textContent = `Ontology: ${status.ontology_subreddit_count || 0} subs · ${status.presets_count || 0} presets`;

      const cdpBadge = document.getElementById("console-cdp-badge");
      if (cdpBadge) {
        cdpBadge.textContent = `Port ${status.cdp_port || 9222}`;
      }

      const browserSelect = document.getElementById("select-browser");
      if (browserSelect && Array.isArray(status.browsers)) {
        const prev = browserSelect.value;
        clearNode(browserSelect);
        browserSelect.appendChild(el("option", { value: "", text: "Auto-detect best browser" }));
        for (const b of status.browsers) {
          const id = b.id || b.name || "";
          const label = b.label || b.name || id;
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
  }

  // --- Resume Dropzone ---
  function setupResumeDropzone() {
    const dropzone = document.getElementById("resume-dropzone");
    const fileInput = document.getElementById("resume-file-input");
    const statusEl = document.getElementById("resume-status");
    const previewEl = document.getElementById("resume-terms-preview");

    async function handleFile(file) {
      if (!file) return;
      statusEl.textContent = `Parsing ${file.name} in memory...`;
      try {
        const res = await apiUploadRaw("/api/upload-resume", file);
        state.resumeEvidences = res.evidences || [];
        state.resumeFilename = res.filename || file.name;
        statusEl.textContent = `Parsed ${state.resumeFilename}: ${res.evidences_count || 0} signals extracted`;
        clearNode(previewEl);
        for (const item of (res.top_terms || []).slice(0, 12)) {
          previewEl.appendChild(
            el("span", { class: "chip", text: `${item.term} (${Number(item.weight).toFixed(1)})` })
          );
        }
        showToast(`Extracted ${res.evidences_count || 0} resume signals`);
      } catch (err) {
        statusEl.textContent = `Upload failed: ${err.message}`;
        showToast(`Resume error: ${err.message}`, "err");
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
      if (fileInput.files && fileInput.files[0]) {
        handleFile(fileInput.files[0]);
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
    btn.disabled = true;
    btn.textContent = "Generating...";

    try {
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
      };

      const res = await apiPost("/api/generate", payload);
      if (res.manifest) {
        setActiveManifest(res.manifest, true);
        if (res.manifest.curation_provenance && res.manifest.curation_provenance.diet_health) {
          renderAnalytics(res.manifest.curation_provenance.diet_health);
        }
        clearNode(banner);
        banner.classList.remove("hidden");
        banner.appendChild(
          el("span", {
            text: `Generated "${res.manifest.name}" using ${res.evidences_count || 0} signals (${(res.sources_used || []).join(", ") || "ontology"}).`,
          })
        );
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
        showToast("Generated personalized curation manifest");
      }
    } catch (err) {
      showToast(`Generate failed: ${err.message}`, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = "Generate Personalized Preset";
    }
  }

  // --- Inspector ---
  function buildSubredditItemsFromManifest(manifest) {
    const items = [];
    const prov = (manifest && manifest.curation_provenance) || {};
    const explanations = prov.explanations || {};

    const tier1Map = new Map();
    for (const entry of manifest.anchor_subreddits || []) {
      const name = typeof entry === "string" ? entry : entry.name;
      if (name) {
        tier1Map.set(name.toLowerCase(), {
          name,
          category: entry.category || "aesthetic_leisure",
          description: entry.description || "",
        });
      }
    }
    const priming = manifest.domain_priming_subreddits || manifest.tier1_systems_subreddits || [];
    for (const entry of priming) {
      const name = typeof entry === "string" ? entry : entry.name;
      if (name) {
        tier1Map.set(name.toLowerCase(), {
          name,
          category: entry.category || "domain_priming",
          description: entry.description || "",
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
    const pushItem = (name, defaultCat, defaultTier, desc) => {
      const key = name.toLowerCase();
      if (seen.has(key)) return;
      seen.add(key);

      const exp = explanations[name] || explanations[key] || null;
      const inT1 = tier1Map.has(key);
      const inT2 = tier2Set.has(key);
      const tier = exp?.tier || (inT1 && inT2 ? "both" : inT1 ? "tier1_following" : "tier2_custom_feed");
      items.push({
        name: exp?.name || name,
        category: exp?.category || defaultCat || "domain_priming",
        tier,
        final_relevance: Number(exp?.final_relevance || 0),
        lexical_score: Number(exp?.lexical_score || 0),
        pagerank_score: Number(exp?.pagerank_score || 0),
        signal_density: Number(exp?.signal_density || 0),
        estimated_daily_posts: Number(exp?.estimated_daily_posts || 0),
        impression_share_pct: Number(exp?.impression_share_pct || 0),
        matched_terms: Array.isArray(exp?.matched_terms) ? exp.matched_terms : [],
        source_breakdown: exp?.source_breakdown || {},
        source_artifacts: Array.isArray(exp?.source_artifacts) ? exp.source_artifacts : [],
        graph_bridges: Array.isArray(exp?.graph_bridges) ? exp.graph_bridges : [],
        routing_reason: exp?.routing_reason || desc || defaultTier,
      });
    };

    for (const info of tier1Map.values()) {
      pushItem(info.name, info.category, "Tier-1 Following feed", info.description);
    }
    for (const feed of manifest.custom_feeds || []) {
      for (const s of feed.subreddits || []) {
        pushItem(String(s), "domain_priming", `Tier-2 Custom Feed (${feed.feed_name || "focus"})`, "");
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
    clearNode(container);
    clearNode(topTermsEl);

    const m = state.activeManifest;
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

    for (const item of filtered) {
      const isExcluded = state.excludedSubs.has(item.name.toLowerCase());
      const toggle = el("input", { type: "checkbox" });
      toggle.checked = !isExcluded;
      toggle.addEventListener("change", () => {
        const key = item.name.toLowerCase();
        if (toggle.checked) {
          state.excludedSubs.delete(key);
        } else {
          state.excludedSubs.add(key);
        }
        updateActiveBar();
        renderInspector();
      });

      const tierLabel =
        item.tier === "both"
          ? "Tier-1 + Custom Feed"
          : item.tier === "tier2_custom_feed"
            ? "Tier-2 Custom Feed"
            : "Tier-1 Following";

      const card = el("article", { class: `sub-card${isExcluded ? " excluded" : ""}` }, [
        el("div", { class: "sub-card-head" }, [
          el("span", { class: "sub-name", text: `r/${item.name}` }),
          el("label", { class: "meta-line" }, [toggle, " Active"]),
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
            el("div", {
              class: "meter-fill",
              style: `width: ${Math.min(100, Math.max(0, item.lexical_score * 100)).toFixed(1)}%`,
            }),
          ]),
          el("span", { text: item.lexical_score.toFixed(2) }),
        ]),
        el("div", { class: "meter-row" }, [
          el("span", { text: "PageRank" }),
          el("div", { class: "meter-track" }, [
            el("div", {
              class: "meter-fill meter-fill-ppr",
              style: `width: ${Math.min(100, Math.max(0, item.pagerank_score * 100)).toFixed(1)}%`,
            }),
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
  }

  // --- Preset Gallery & Mixer ---
  async function loadPresets() {
    try {
      const res = await apiGet("/api/presets");
      state.presets = res.presets || [];
      renderPresetGallery();
      populateMixerSelect();
    } catch (err) {
      showToast(`Failed to load presets: ${err.message}`, "err");
    }
  }

  function populateMixerSelect() {
    const select = document.getElementById("mixer-preset-select");
    clearNode(select);
    for (const p of state.presets) {
      select.appendChild(el("option", { value: p.id, text: `${p.name} (${p.id})` }));
    }
  }

  function renderPresetGallery() {
    const grid = document.getElementById("preset-gallery-grid");
    clearNode(grid);
    const q = (document.getElementById("gallery-search").value || "").trim().toLowerCase();
    const list = state.presets.filter((p) => {
      if (!q) return true;
      return `${p.id} ${p.name} ${p.description}`.toLowerCase().includes(q);
    });

    for (const p of list) {
      const card = el("article", { class: "preset-card" }, [
        el("div", {}, [
          el("div", { class: "preset-title", text: p.name }),
          el("div", { class: "meta-line", text: `${p.id} · ${p.anchor_count + p.domain_priming_count} tier-1` }),
          el("p", { class: "preset-desc", text: p.description || "" }),
        ]),
        el("div", { class: "action-row-tight" }, [
          el("button", {
            type: "button",
            class: "btn btn-secondary btn-sm",
            text: "Load Preset",
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
      setActiveManifest(manifest, true);
      showToast(`Loaded preset: ${manifest.name || presetId}`);
      await auditActiveManifest();
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

  function renderMixerList() {
    const list = document.getElementById("mixer-weights-list");
    clearNode(list);
    const entries = Object.entries(state.mixerWeights);
    if (entries.length === 0) {
      list.appendChild(el("div", { class: "meta-line", text: "Add 2 or more presets to blend." }));
      return;
    }
    const total = entries.reduce((acc, [, w]) => acc + Number(w || 0), 0) || 1;
    for (const [id, w] of entries) {
      const pct = Math.round((Number(w) / total) * 100);
      const slider = el("input", {
        class: "range",
        type: "range",
        min: "5",
        max: "100",
        value: String(Math.round(Number(w) * 50)),
      });
      slider.addEventListener("input", () => {
        state.mixerWeights[id] = Number(slider.value) / 50;
        renderMixerList();
      });

      const row = el("div", { class: "mixer-row" }, [
        el("div", { class: "mixer-row-head" }, [
          el("span", { text: `${id} (${pct}%)` }),
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
    try {
      const res = await apiPost("/api/mix", {
        weights: normalized,
        target_ratio: [
          Number(document.getElementById("slider-aes").value),
          Number(document.getElementById("slider-cog").value),
          Number(document.getElementById("slider-dom").value),
        ],
      });
      if (res.manifest) {
        setActiveManifest(res.manifest, true);
        showToast(`Blended ${entries.length} presets`);
        await auditActiveManifest();
      }
    } catch (err) {
      showToast(`Mix failed: ${err.message}`, "err");
    }
  }

  // --- Diet Health & SVG Charts ---
  async function auditActiveManifest() {
    const m = getEffectiveManifest();
    if (!m) {
      showToast("No active manifest to audit.", "err");
      return;
    }
    try {
      const res = await apiPost("/api/audit", { manifest: m });
      if (res.audit) {
        state.lastAudit = res.audit;
        renderAnalytics(res.audit);
      }
    } catch (err) {
      showToast(`Audit failed: ${err.message}`, "err");
    }
  }

  async function auditLiveAccount() {
    try {
      const res = await apiPost("/api/audit", {
        live: true,
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
      showToast(`Live audit error: ${err.message}`, "err");
    }
  }

  function appendDonutRing(svg, cx, cy, r, strokeWidth, segments) {
    const circ = 2 * Math.PI * r;
    let offset = 0;
    for (const seg of segments) {
      const frac = Math.max(0, Math.min(1, Number(seg.pct || 0) / 100));
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

    clearNode(donutStage);
    clearNode(legendEl);
    clearNode(kpiGrid);
    clearNode(recList);
    clearNode(tableWrap);

    if (!audit) return;

    const actual = audit.actual_velocity_distribution_pct || {};
    const target = audit.target_distribution_pct || {};

    const svg = svgEl("svg", { viewBox: "0 0 220 220", width: "220", height: "220" });
    svg.appendChild(
      svgEl("circle", { cx: 110, cy: 110, r: 82, fill: "none", stroke: "#1f2937", "stroke-width": 18 })
    );
    svg.appendChild(
      svgEl("circle", { cx: 110, cy: 110, r: 58, fill: "none", stroke: "#1f2937", "stroke-width": 12 })
    );

    appendDonutRing(svg, 110, 110, 82, 18, [
      { pct: actual.aesthetic_leisure || 0, color: "#38bdf8" },
      { pct: actual.cognitive_curiosity || 0, color: "#a78bfa" },
      { pct: actual.domain_priming || 0, color: "#f59e0b" },
    ]);
    appendDonutRing(svg, 110, 110, 58, 12, [
      { pct: target.aesthetic_leisure || 40, color: "rgba(56,189,248,0.45)" },
      { pct: target.cognitive_curiosity || 35, color: "rgba(167,139,250,0.45)" },
      { pct: target.domain_priming || 25, color: "rgba(245,158,11,0.45)" },
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
    scoreTxt.textContent = `${Math.round(Number(audit.composite_health_score || 0))}`;
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
      ["pillar-aes", `Aesthetic ${Number(actual.aesthetic_leisure || 0).toFixed(1)}% (target ${Number(target.aesthetic_leisure || 40).toFixed(0)}%)`],
      ["pillar-cog", `Cognitive ${Number(actual.cognitive_curiosity || 0).toFixed(1)}% (target ${Number(target.cognitive_curiosity || 35).toFixed(0)}%)`],
      ["pillar-dom", `Domain ${Number(actual.domain_priming || 0).toFixed(1)}% (target ${Number(target.domain_priming || 25).toFixed(0)}%)`],
    ];
    for (const [dotClass, label] of legendItems) {
      legendEl.appendChild(
        el("span", { class: "legend-item" }, [el("span", { class: `pillar-dot ${dotClass}` }), label])
      );
    }

    const kpis = [
      ["Composite Health", `${Number(audit.composite_health_score || 0).toFixed(1)} / 100`],
      ["GR Fidelity", `${Number(audit.golden_ratio_fidelity_score || 0).toFixed(1)}%`],
      ["TVD Drift", Number(audit.golden_ratio_tvd_drift || 0).toFixed(3)],
      ["Shannon Entropy", Number(audit.shannon_domain_entropy_norm || 0).toFixed(3)],
      ["Gini Concentration", Number(audit.gini_velocity_concentration || 0).toFixed(3)],
      [
        "Max Dominance",
        `${Number(audit.max_subreddit_dominance_pct || 0).toFixed(1)}% (${audit.dominant_subreddit || "n/a"})`,
      ],
      ["Mean Signal Density", Number(audit.mean_signal_density || 0).toFixed(2)],
      ["Total Velocity", `${Number(audit.total_daily_velocity || 0).toFixed(1)} posts/d`],
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
              el("td", { text: Number(row.estimated_daily_posts || 0).toFixed(1) }),
              el("td", { text: `${Number(row.impression_share_pct || 0).toFixed(1)}%` }),
            ])
          )
        ),
      ]);
      tableWrap.appendChild(table);
    }
  }

  // --- Console: Browser, Diff, Sync, Snapshots ---
  async function handleLaunchBrowser() {
    const log = document.getElementById("browser-launch-log");
    const port = Number(document.getElementById("input-cdp-port").value || 9222);
    const preferred = document.getElementById("select-browser").value || null;
    log.textContent = "Launching browser...";
    try {
      const res = await apiPost("/api/launch-browser", { port, browser: preferred });
      log.textContent = JSON.stringify(res.launch || res, null, 2);
      showToast("Browser CDP ready");
      await refreshStatus();
    } catch (err) {
      log.textContent = `Launch error: ${err.message}`;
      showToast(`Launch failed: ${err.message}`, "err");
    }
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
    try {
      const res = await apiPost("/api/diff", {
        manifest: m,
        prune: Boolean(document.getElementById("check-prune").checked),
        keep: getKeepList(),
      });
      const diff = res.diff || {};
      box.appendChild(
        el("div", { class: "code-log", text: JSON.stringify(diff, null, 2) })
      );
      showToast("Computed live subscription diff");
    } catch (err) {
      box.appendChild(el("div", { class: "meta-line", text: `Diff error: ${err.message}` }));
      showToast(`Diff error: ${err.message}`, "err");
    }
  }

  async function handleSync(dryRun) {
    const m = getEffectiveManifest();
    if (!m) {
      showToast("Load or generate a manifest first.", "err");
      return;
    }
    const box = document.getElementById("diff-preview-box");
    clearNode(box);
    try {
      const res = await apiPost("/api/sync", {
        manifest: m,
        dry_run: Boolean(dryRun),
        prune: Boolean(document.getElementById("check-prune").checked),
        keep: getKeepList(),
      });
      box.appendChild(
        el("div", { class: "code-log", text: JSON.stringify(res.sync || res, null, 2) })
      );
      showToast(dryRun ? "Dry-run sync completed" : "Live sync completed");
      if (!dryRun) {
        await loadSnapshots();
      }
    } catch (err) {
      box.appendChild(el("div", { class: "meta-line", text: `Sync error: ${err.message}` }));
      showToast(`Sync error: ${err.message}`, "err");
    }
  }

  async function loadSnapshots() {
    const wrap = document.getElementById("snapshots-table-wrap");
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
                    onclick: () => handleRollback(s.snapshot_id, true),
                  }),
                  el("button", {
                    type: "button",
                    class: "btn btn-danger btn-sm",
                    text: "Rollback",
                    onclick: () => handleRollback(s.snapshot_id, false),
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

  async function handleRollback(snapshotId, dryRun) {
    try {
      const res = await apiPost("/api/rollback", { snapshot_id: snapshotId, dry_run: dryRun });
      const box = document.getElementById("diff-preview-box");
      clearNode(box);
      box.appendChild(el("div", { class: "code-log", text: JSON.stringify(res.rollback || res, null, 2) }));
      showToast(dryRun ? `Dry-run rollback previewed (${snapshotId})` : `Rolled back to ${snapshotId}`);
    } catch (err) {
      showToast(`Rollback failed: ${err.message}`, "err");
    }
  }

  function setupEvents() {
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.getAttribute("data-tab")));
    });

    document.querySelectorAll("#query-presets .chip-btn").forEach((chip) => {
      chip.addEventListener("click", () => {
        document.getElementById("input-query").value = chip.getAttribute("data-query") || "";
      });
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

    document.getElementById("gallery-search").addEventListener("input", renderPresetGallery);
    document.getElementById("btn-mixer-add").addEventListener("click", () => {
      addPresetToMixer(document.getElementById("mixer-preset-select").value);
    });
    document.getElementById("btn-mix-blend").addEventListener("click", handleMixBlend);
    document.getElementById("btn-mix-clear").addEventListener("click", () => {
      state.mixerWeights = {};
      renderMixerList();
    });

    document.getElementById("btn-audit-manifest").addEventListener("click", auditActiveManifest);
    document.getElementById("btn-audit-live").addEventListener("click", auditLiveAccount);

    document.getElementById("btn-launch-browser").addEventListener("click", handleLaunchBrowser);
    document.getElementById("btn-refresh-status").addEventListener("click", refreshStatus);
    document.getElementById("btn-diff").addEventListener("click", handleDiff);
    document.getElementById("btn-sync-dry").addEventListener("click", () => handleSync(true));
    document.getElementById("btn-sync-live").addEventListener("click", () => handleSync(false));
    document.getElementById("btn-refresh-snapshots").addEventListener("click", loadSnapshots);

    document.addEventListener("keydown", (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "e") {
        ev.preventDefault();
        exportManifest();
        return;
      }
      const tag = (ev.target && ev.target.tagName) || "";
      if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
      const tabMap = { "1": "lab", "2": "inspector", "3": "gallery", "4": "analytics", "5": "console" };
      if (tabMap[ev.key]) {
        switchTab(tabMap[ev.key]);
      }
    });
  }

  async function init() {
    setupRatioSliders();
    setupResumeDropzone();
    setupEvents();
    renderInspector();
    renderMixerList();
    await refreshStatus();
    await loadPresets();
    await loadSnapshots();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
