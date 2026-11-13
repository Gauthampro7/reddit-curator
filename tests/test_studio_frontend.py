"""Behavioral frontend unit & DOM harness tests for Web Studio (Slice 4)."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import textwrap
import unittest
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "rcurator" / "studio" / "static"


class _DOMTreeBuilder(HTMLParser):
    """Parse index.html into a JSON-serializable element tree for the Node DOM harness."""

    VOID_TAGS = frozenset({"meta", "link", "input", "br", "hr", "img"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: dict[str, Any] = {"tag": "document", "attrs": {}, "children": []}
        self.stack: list[dict[str, Any]] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = {
            "tag": tag.lower(),
            "attrs": {k: ("" if v is None else v) for k, v in attrs},
            "children": [],
        }
        self.stack[-1]["children"].append(node)
        if tag.lower() not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i]["tag"] == t:
                del self.stack[i:]
                break

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1]["children"].append(data)


def _html_to_tree(html_text: str) -> dict[str, Any]:
    parser = _DOMTreeBuilder()
    parser.feed(html_text)
    parser.close()
    return parser.root


_NODE_HARNESS_PRELUDE = textwrap.dedent(
    r"""
    const assert = require("node:assert/strict");
    const fs = require("node:fs");
    const vm = require("node:vm");

    const CONFIG = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));

    class ClassList {
      constructor(el) { this.el = el; }
      _tokens() {
        return String(this.el._attrs.class || "").trim().split(/\s+/).filter(Boolean);
      }
      _save(arr) { this.el._attrs.class = arr.join(" "); }
      add(...cls) {
        const s = new Set(this._tokens());
        for (const c of cls) if (c) s.add(c);
        this._save([...s]);
      }
      remove(...cls) {
        const drop = new Set(cls);
        this._save(this._tokens().filter((c) => !drop.has(c)));
      }
      contains(c) { return this._tokens().includes(c); }
      toggle(c, force) {
        const has = this.contains(c);
        const want = force === undefined ? !has : Boolean(force);
        if (want) this.add(c); else this.remove(c);
        return want;
      }
    }

    function parseSelector(sel) {
      // Supports tag, #id, .class, [attr], [attr="val"], and descendant space combinator
      const parts = sel.trim().split(/\s+/);
      return parts.map((part) => {
        const req = { tag: null, id: null, classes: [], attrs: [] };
        let rest = part;
        const attrRe = /\[([^\]=]+)(?:="([^"]*)")?\]/g;
        let m;
        while ((m = attrRe.exec(part)) !== null) {
          req.attrs.push([m[1], m[2]]);
        }
        rest = rest.replace(attrRe, "");
        const idMatch = rest.match(/#([A-Za-z0-9_-]+)/);
        if (idMatch) {
          req.id = idMatch[1];
          rest = rest.replace(idMatch[0], "");
        }
        const clsMatches = [...rest.matchAll(/\.([A-Za-z0-9_-]+)/g)];
        for (const cm of clsMatches) {
          req.classes.push(cm[1]);
        }
        rest = rest.replace(/\.[A-Za-z0-9_-]+/g, "");
        if (rest) req.tag = rest.toLowerCase();
        return req;
      });
    }

    function matchesSimple(el, req) {
      if (!el || el.nodeType !== 1) return false;
      if (req.tag && el.tagName.toLowerCase() !== req.tag) return false;
      if (req.id && el.id !== req.id) return false;
      for (const c of req.classes) {
        if (!el.classList.contains(c)) return false;
      }
      for (const [k, v] of req.attrs) {
        if (!el.hasAttribute(k)) return false;
        if (v !== undefined && el.getAttribute(k) !== v) return false;
      }
      return true;
    }

    class FakeElement {
      constructor(tag, ns = null) {
        this.nodeType = 1;
        this.tagName = tag.toUpperCase();
        this.namespaceURI = ns;
        this._attrs = {};
        this._children = [];
        this.parentNode = null;
        this._listeners = {};
        this.classList = new ClassList(this);
        this.checked = false;
        this.disabled = false;
        this.files = [];
        this._value = "";
      }
      get id() { return this._attrs.id || ""; }
      set id(v) { this._attrs.id = String(v); }
      get className() { return this._attrs.class || ""; }
      set className(v) { this._attrs.class = String(v); }
      get value() {
        if (this.tagName === "SELECT" && !this._valueSet) {
          const first = this._children.find((c) => c.nodeType === 1 && c.tagName === "OPTION");
          return first ? first.value : "";
        }
        return this._value;
      }
      set value(v) {
        this._value = String(v);
        this._valueSet = true;
      }
      get options() {
        return this._children.filter((c) => c.nodeType === 1 && c.tagName === "OPTION");
      }
      get firstChild() { return this._children[0] || null; }
      get textContent() {
        return this._children.map((c) => (typeof c === "string" ? c : c.textContent)).join("");
      }
      set textContent(v) {
        this._children = v === "" || v === null || v === undefined ? [] : [String(v)];
      }
      setAttribute(k, v) {
        const key = String(k);
        const val = String(v);
        this._attrs[key] = val;
        if (key === "value") {
          this._value = val;
          this._valueSet = true;
        }
      }
      getAttribute(k) {
        const key = String(k);
        return Object.prototype.hasOwnProperty.call(this._attrs, key) ? this._attrs[key] : null;
      }
      hasAttribute(k) {
        return Object.prototype.hasOwnProperty.call(this._attrs, String(k));
      }
      removeAttribute(k) {
        delete this._attrs[String(k)];
      }
      appendChild(child) {
        if (typeof child === "string") {
          this._children.push(child);
          return child;
        }
        if (child.nodeType === 3) {
          this._children.push(child.data);
          return child;
        }
        child.parentNode = this;
        this._children.push(child);
        return child;
      }
      removeChild(child) {
        const idx = this._children.indexOf(child);
        if (idx >= 0) {
          this._children.splice(idx, 1);
          if (child && typeof child === "object") child.parentNode = null;
        }
        return child;
      }
      contains(node) {
        if (node === this) return true;
        for (const c of this._children) {
          if (c && typeof c === "object" && c.contains(node)) return true;
        }
        return false;
      }
      addEventListener(type, fn) {
        if (!this._listeners[type]) this._listeners[type] = [];
        this._listeners[type].push(fn);
      }
      dispatchEvent(ev) {
        ev.target = ev.target || this;
        ev.currentTarget = this;
        if (typeof ev.preventDefault !== "function") ev.preventDefault = () => {};
        if (typeof ev.stopPropagation !== "function") ev.stopPropagation = () => { ev._stopped = true; };
        const handlers = this._listeners[ev.type] || [];
        for (const h of handlers) h(ev);
        if (!ev._stopped && ev.bubbles !== false && this.parentNode && this.parentNode.dispatchEvent) {
          this.parentNode.dispatchEvent(ev);
        }
      }
      click() {
        if (this.disabled) return;
        if (this.tagName === "INPUT" && this._attrs.type === "checkbox") {
          this.checked = !this.checked;
          this.dispatchEvent({ type: "change", target: this });
        }
        this.dispatchEvent({ type: "click", target: this });
      }
      focus() {
        globalThis.document.activeElement = this;
      }
      querySelectorAll(sel) {
        const chain = parseSelector(sel);
        const results = [];
        const walk = (node) => {
          for (const c of node._children) {
            if (!c || typeof c === "string") continue;
            if (matchesChain(c, chain, this)) results.push(c);
            walk(c);
          }
        };
        walk(this);
        return results;
      }
      querySelector(sel) {
        return this.querySelectorAll(sel)[0] || null;
      }
    }

    function matchesChain(el, chain, root) {
      if (!matchesSimple(el, chain[chain.length - 1])) return false;
      if (chain.length === 1) return true;
      let cur = el.parentNode;
      let idx = chain.length - 2;
      while (cur && cur !== root) {
        if (matchesSimple(cur, chain[idx])) {
          idx -= 1;
          if (idx < 0) return true;
        }
        cur = cur.parentNode;
      }
      return false;
    }

    function buildTree(nodeSpec, doc) {
      if (typeof nodeSpec === "string") return nodeSpec;
      const el = new FakeElement(nodeSpec.tag === "document" ? "document" : nodeSpec.tag);
      for (const [k, v] of Object.entries(nodeSpec.attrs || {})) {
        el.setAttribute(k, v);
      }
      for (const ch of nodeSpec.children || []) {
        const built = buildTree(ch, doc);
        el.appendChild(built);
      }
      return el;
    }

    function createStudioHarness(fetchHandler) {
      const root = buildTree(CONFIG.domTree);
      const body = root.querySelector("body");
      const docListeners = {};
      const store = new Map();
      const createdUrls = [];
      const revokedUrls = [];
      const clipboardWrites = [];
      const pendingTimers = [];
      let nextTimerId = 1;

      const document = {
        body,
        activeElement: body,
        createElement: (tag) => new FakeElement(tag),
        createElementNS: (ns, tag) => new FakeElement(tag, ns),
        createTextNode: (t) => ({ nodeType: 3, data: String(t) }),
        getElementById: (id) => root.querySelector(`#${id}`),
        querySelector: (s) => root.querySelector(s),
        querySelectorAll: (s) => root.querySelectorAll(s),
        addEventListener: (t, fn) => {
          if (!docListeners[t]) docListeners[t] = [];
          docListeners[t].push(fn);
        },
        dispatchEvent: (ev) => {
          ev.preventDefault = ev.preventDefault || (() => {});
          ev.stopPropagation = ev.stopPropagation || (() => {});
          for (const fn of docListeners[ev.type] || []) fn(ev);
        },
      };

      const windowObj = {
        setTimeout: (fn, ms) => {
          const id = nextTimerId++;
          pendingTimers.push({ id, fn, ms, cleared: false });
          return id;
        },
        clearTimeout: (id) => {
          const t = pendingTimers.find((x) => x.id === id);
          if (t) t.cleared = true;
        },
        scrollTo: () => {},
      };

      const sandbox = {
        console,
        Math,
        Number,
        String,
        Boolean,
        Array,
        Object,
        Set,
        Map,
        JSON,
        Date,
        Promise,
        Error,
        URL: class FakeURL {
          constructor(raw) {
            const m = String(raw).match(/^(https?:)\/\/[^/\s]+/);
            if (!m) throw new Error("Invalid URL");
            this.protocol = m[1];
          }
          static createObjectURL(blob) {
            const u = `blob:studio-${createdUrls.length + 1}`;
            createdUrls.push({ url: u, blob });
            return u;
          }
          static revokeObjectURL(u) {
            revokedUrls.push(u);
          }
        },
        Blob: class FakeBlob {
          constructor(parts, opts) {
            this.parts = parts;
            this.type = opts && opts.type;
          }
        },
        encodeURIComponent,
        decodeURIComponent,
        document,
        window: windowObj,
        localStorage: {
          getItem: (k) => (store.has(k) ? store.get(k) : null),
          setItem: (k, v) => store.set(k, String(v)),
          removeItem: (k) => store.delete(k),
        },
        navigator: {
          clipboard: {
            writeText: async (text) => {
              clipboardWrites.push(text);
            },
          },
        },
        fetch: async (path, opts = {}) => {
          const res = await fetchHandler(path, opts);
          const status = res.status ?? 200;
          const ok = status >= 200 && status < 300;
          return {
            ok,
            status,
            json: async () => res.body ?? {},
          };
        },
      };
      globalThis.document = document;
      vm.runInNewContext(CONFIG.appJs, sandbox, { filename: "app.js" });
      return {
        document,
        window: windowObj,
        store,
        createdUrls,
        revokedUrls,
        clipboardWrites,
        flushTimers: () => {
          for (const t of pendingTimers) {
            if (!t.cleared) {
              t.cleared = true;
              t.fn();
            }
          }
        },
        boot: async () => {
          for (const fn of docListeners["DOMContentLoaded"] || []) {
            await fn({ type: "DOMContentLoaded" });
          }
        },
      };
    }

    async function flushMicrotasks(n = 60) {
      for (let i = 0; i < n; i++) await Promise.resolve();
      await new Promise((r) => setTimeout(r, 5));
      for (let i = 0; i < n; i++) await Promise.resolve();
    }
    """
)


class TestStudioFrontend(unittest.TestCase):
    """Verify Slice 4 frontend bug fixes, UX workflows, and CSP invariants via Node."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        cls.index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        cls.styles_css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        cls.dom_tree = _html_to_tree(cls.index_html)

    def _run_node_scenario(self, scenario_js: str) -> None:
        payload = {
            "appJs": self.app_js,
            "domTree": self.dom_tree,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.json"
            script_path = Path(tmpdir) / "test_runner.js"
            cfg_path.write_text(json.dumps(payload), encoding="utf-8")
            full_js = (
                _NODE_HARNESS_PRELUDE
                + "\n(async () => {\n"
                + scenario_js
                + '\n})().catch((err) => { console.error(err); process.exit(1); });\n'
            )
            script_path.write_text(full_js, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(script_path), str(cfg_path)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if proc.returncode != 0:
                self.fail(f"Node harness failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")

    def test_node_syntax_check_and_strict_csp_invariants(self) -> None:
        proc = subprocess.run(
            ["node", "--check", str(STATIC_DIR / "app.js")],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function("):
            self.assertNotIn(sink, self.app_js)
        self.assertIsNone(re.search(r"\bstyle\s*:", self.app_js))
        self.assertNotIn('setAttribute("style"', self.app_js)
        self.assertNotIn("setAttribute('style'", self.app_js)

        for name, content in (
            ("index.html", self.index_html),
            ("styles.css", self.styles_css),
            ("app.js", self.app_js),
        ):
            scrubbed = content.replace("http://www.w3.org/2000/svg", "")
            self.assertIsNone(
                re.search(r"https?://", scrubbed, flags=re.IGNORECASE),
                f"External URL in {name}",
            )

    def test_initial_cdp_port_preservation_validation_and_checklist(self) -> None:
        self._run_node_scenario(
            r"""
            const calls = [];
            const h = createStudioHarness(async (path) => {
              calls.push(path);
              if (path.startsWith("/api/status")) {
                const match = path.match(/cdp_port=(\d+)/);
                const port = match ? Number(match[1]) : 9333;
                return {
                  body: {
                    ok: true,
                    csrf_token: "tok-1",
                    cdp_port: port,
                    cdp_connected: true,
                    authenticated: false,
                    username: null,
                    ontology_subreddit_count: 292,
                    presets_count: 17,
                    browsers: [{ browser_id: "chromium", name: "Chromium" }],
                  },
                };
              }
              if (path === "/api/presets") return { body: { presets: [] } };
              if (path === "/api/snapshots") return { body: { snapshots: [] } };
              return { body: {} };
            });

            await h.boot();
            assert.equal(calls[0], "/api/status", "Initial boot must omit ?cdp_port=");
            const portInput = h.document.getElementById("input-cdp-port");
            assert.equal(portInput.value, "9333", "Input must sync to server cdp_port");
            assert.equal(h.document.getElementById("console-cdp-badge").textContent, "Port 9333");

            // 3-step connection checklist reflects detected browser + open port + pending login
            const checklistText = h.document.getElementById("easy-connection-checklist").textContent;
            assert.ok(checklistText.includes("Chromium"));
            assert.ok(checklistText.includes("9333"));

            // Valid port change triggers /api/status?cdp_port=9444
            portInput.value = "9444";
            portInput.dispatchEvent({ type: "change", target: portInput });
            await flushMicrotasks();
            assert.ok(calls.includes("/api/status?cdp_port=9444"));

            // Invalid ports (80, 70000, abc) must fail client-side without calling backend
            const prevCount = calls.length;
            for (const bad of ["80", "70000", "abc"]) {
              portInput.value = bad;
              h.document.getElementById("btn-launch-browser").click();
              await flushMicrotasks();
            }
            assert.equal(calls.length, prevCount, "Invalid CDP ports must not dispatch API calls");
            assert.ok(h.document.getElementById("toast-rack").textContent.includes("1024 and 65535"));
            """
        )

    def test_mixer_slider_preserves_dom_node_mid_drag(self) -> None:
        self._run_node_scenario(
            r"""
            const h = createStudioHarness(async (path) => {
              if (path.startsWith("/api/status")) {
                return { body: { ok: true, csrf_token: "tok", cdp_port: 9222, browsers: [] } };
              }
              if (path === "/api/presets") {
                return {
                  body: {
                    presets: [
                      { id: "systems_engineering", name: "Systems", description: "", anchor_count: 4, domain_priming_count: 6, custom_feeds_count: 1, golden_ratio_distribution: {}, preview_subreddits: ["rust"] },
                      { id: "rust_and_compilers", name: "Rust", description: "", anchor_count: 4, domain_priming_count: 6, custom_feeds_count: 1, golden_ratio_distribution: {}, preview_subreddits: ["compilers"] },
                    ],
                  },
                };
              }
              if (path === "/api/snapshots") return { body: { snapshots: [] } };
              return { body: {} };
            });

            await h.boot();
            const addBtn = h.document.getElementById("btn-mixer-add");
            const select = h.document.getElementById("mixer-preset-select");
            select.value = "systems_engineering";
            addBtn.click();
            select.value = "rust_and_compilers";
            addBtn.click();

            const list = h.document.getElementById("mixer-weights-list");
            const slidersBefore = list.querySelectorAll('input[type="range"]');
            assert.equal(slidersBefore.length, 2);
            const activeSlider = slidersBefore[0];

            // Simulate dragging slider across multiple input ticks
            activeSlider.value = "100";
            activeSlider.dispatchEvent({ type: "input", target: activeSlider });
            activeSlider.value = "80";
            activeSlider.dispatchEvent({ type: "input", target: activeSlider });

            const slidersAfter = list.querySelectorAll('input[type="range"]');
            assert.equal(slidersAfter[0], activeSlider, "Slider DOM node must be preserved during drag");
            assert.ok(list.contains(activeSlider));
            const label0 = list.querySelector('[data-mixer-label="systems_engineering"]');
            const label1 = list.querySelector('[data-mixer-label="rust_and_compilers"]');
            assert.equal(label0.textContent, "systems_engineering (62%)");
            assert.equal(label1.textContent, "rust_and_compilers (38%)");
            """
        )

    def test_partial_and_full_sync_failures_surfaced(self) -> None:
        self._run_node_scenario(
            r"""
            const samplePreset = {
              name: "Systems Engineering",
              preset_id: "systems_engineering",
              anchor_subreddits: [{ name: "unixporn", category: "aesthetic_leisure" }],
              domain_priming_subreddits: [{ name: "rust", category: "domain_priming" }],
              custom_feeds: [{ feed_name: "f1", display_name: "F1", subreddits: ["linux"] }],
            };
            const h = createStudioHarness(async (path, opts) => {
              if (path.startsWith("/api/status")) {
                return { body: { ok: true, csrf_token: "tok", cdp_port: 9222, cdp_connected: true, authenticated: true, username: "tester", browsers: [] } };
              }
              if (path === "/api/presets") {
                return { body: { presets: [{ id: "systems_engineering", name: "Systems", description: "", anchor_count: 1, domain_priming_count: 1, custom_feeds_count: 1, golden_ratio_distribution: {}, preview_subreddits: ["rust"] }] } };
              }
              if (path === "/api/presets/systems_engineering") return { body: samplePreset };
              if (path === "/api/audit") {
                return {
                  body: {
                    audit: {
                      composite_health_score: 90,
                      actual_velocity_distribution_pct: { aesthetic_leisure: 40, cognitive_curiosity: 35, domain_priming: 25 },
                      target_distribution_pct: { aesthetic_leisure: 40, cognitive_curiosity: 35, domain_priming: 25 },
                      per_subreddit_shares: [],
                      recommendations: [],
                    },
                  },
                };
              }
              if (path === "/api/snapshots") return { body: { snapshots: [] } };
              if (path === "/api/sync") {
                return {
                  body: {
                    sync: {
                      dry_run: false,
                      username: "tester",
                      has_failures: true,
                      subscriptions: {
                        dry_run: false,
                        subscribed: ["rust"],
                        already_subscribed: [],
                        unsubscribed: [],
                        failed: ["badsub"],
                      },
                      custom_feeds: {
                        f1: { status: "failed", http_code: 400, error: "bad feed" },
                      },
                      following_feed: { success: false },
                    },
                  },
                };
              }
              return { body: {} };
            });

            await h.boot();
            const card = h.document.getElementById("easy-preset-grid").querySelector(".easy-preset-card");
            card.click();
            await flushMicrotasks();

            h.document.getElementById("easy-btn-apply").click();
            await flushMicrotasks();

            const banner = h.document.getElementById("easy-apply-result");
            assert.ok(banner.classList.contains("warn") || banner.classList.contains("err"), "Partial sync failure must not show green success");
            assert.ok(!banner.textContent.includes("Done!"), "Must not print green Done! message on failure");
            assert.ok(banner.textContent.includes("badsub"));
            assert.ok(banner.textContent.includes("f1"));

            h.document.getElementById("btn-sync-live").click();
            await flushMicrotasks();
            const diffBox = h.document.getElementById("diff-preview-box");
            assert.ok(diffBox.textContent.includes("Failures"));
            assert.ok(diffBox.textContent.includes("badsub"));
            """
        )

    def test_non_ascii_resume_filename_and_stale_evidence_reset(self) -> None:
        self._run_node_scenario(
            r"""
            const uploadHeaders = [];
            const generatePayloads = [];
            let uploadShouldFail = false;

            const h = createStudioHarness(async (path, opts) => {
              if (path.startsWith("/api/status")) return { body: { ok: true, csrf_token: "tok", cdp_port: 9222, browsers: [] } };
              if (path === "/api/presets") return { body: { presets: [] } };
              if (path === "/api/snapshots") return { body: { snapshots: [] } };
              if (path === "/api/upload-resume") {
                uploadHeaders.push(opts.headers);
                if (uploadShouldFail) return { status: 400, body: { error: "corrupt pdf" } };
                return {
                  body: {
                    filename: "résumé_日本語.pdf",
                    evidences_count: 1,
                    evidences: [{ term: "rust", weight: 3.0 }],
                    top_terms: [{ term: "rust", weight: 3.0 }],
                  },
                };
              }
              if (path === "/api/generate") {
                generatePayloads.push(JSON.parse(opts.body));
                return { body: { manifest: { name: "Gen", anchor_subreddits: [], domain_priming_subreddits: [], custom_feeds: [] }, evidences_count: 0, sources_used: [] } };
              }
              if (path === "/api/audit") return { body: { audit: { composite_health_score: 50, actual_velocity_distribution_pct: {}, target_distribution_pct: {}, per_subreddit_shares: [], recommendations: [] } } };
              return { body: {} };
            });

            await h.boot();
            const dropzone = h.document.getElementById("resume-dropzone");
            const fileInput = h.document.getElementById("resume-file-input");

            // Non-ASCII upload sets encodeURIComponent X-Filename
            const goodFile = {
              name: "résumé_日本語.pdf",
              size: 1024,
              type: "application/pdf",
              arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer,
            };
            fileInput.files = [goodFile];
            fileInput.value = "C:\\fakepath\\resume.pdf";
            fileInput.dispatchEvent({ type: "change", target: fileInput });
            await flushMicrotasks();

            assert.equal(fileInput.value, "", "fileInput.value must reset after change");
            assert.equal(uploadHeaders[0]["X-Filename"], encodeURIComponent("résumé_日本語.pdf"));
            assert.ok(!h.document.getElementById("btn-clear-resume").classList.contains("hidden"));

            // Client-side >15 MiB check must reject without network call and clear stale evidences
            const bigFile = {
              name: "huge.pdf",
              size: 16 * 1024 * 1024,
              type: "application/pdf",
              arrayBuffer: async () => { throw new Error("Should not read oversized file"); },
            };
            dropzone.dispatchEvent({ type: "drop", dataTransfer: { files: [bigFile] } });
            await flushMicrotasks();
            assert.equal(uploadHeaders.length, 1, "Oversized upload must be blocked client-side");

            h.document.getElementById("btn-generate").click();
            await flushMicrotasks();
            assert.deepEqual(generatePayloads[0].resume_evidences, [], "Stale resumeEvidences must be cleared on validation error");

            // Upload again, then fail server-side -> stale evidences cleared
            fileInput.files = [goodFile];
            fileInput.dispatchEvent({ type: "change", target: fileInput });
            await flushMicrotasks();
            uploadShouldFail = true;
            fileInput.files = [goodFile];
            fileInput.dispatchEvent({ type: "change", target: fileInput });
            await flushMicrotasks();

            h.document.getElementById("btn-generate").click();
            await flushMicrotasks();
            assert.deepEqual(generatePayloads[1].resume_evidences, [], "Stale resumeEvidences must be cleared on failed upload");
            """
        )

    def test_inspector_interactive_mutations_and_desync_fixes(self) -> None:
        self._run_node_scenario(
            r"""
            const presetManifest = {
              name: "Systems Engineering",
              preset_id: "systems_engineering",
              golden_ratio_distribution: {
                aesthetic_leisure_target_pct: 40,
                cognitive_curiosity_target_pct: 60,
                domain_priming_target_pct: 0,
              },
              anchor_subreddits: [
                { name: "unixporn", category: "aesthetic_leisure" },
                { name: "askscience", category: "cognitive_curiosity" },
              ],
              tier1_systems_subreddits: [
                { name: "linux", category: "domain_priming" },
              ],
              custom_feeds: [],
            };

            const syncPayloads = [];
            const h = createStudioHarness(async (path, opts) => {
              if (path.startsWith("/api/status")) return { body: { ok: true, csrf_token: "tok", cdp_port: 9222, browsers: [] } };
              if (path === "/api/presets") {
                return { body: { presets: [{ id: "systems_engineering", name: "Systems Engineering", description: "Desc", anchor_count: 2, domain_priming_count: 1, custom_feeds_count: 0, golden_ratio_distribution: presetManifest.golden_ratio_distribution, preview_subreddits: ["unixporn", "linux"] }] } };
              }
              if (path === "/api/presets/systems_engineering") return { body: JSON.parse(JSON.stringify(presetManifest)) };
              if (path === "/api/import-manifest") {
                const body = JSON.parse(opts.body);
                return {
                  body: {
                    filename: body.filename,
                    manifest: body.manifest,
                    audit: {
                      composite_health_score: 88,
                      actual_velocity_distribution_pct: { aesthetic_leisure: 40, cognitive_curiosity: 60, domain_priming: 0 },
                      target_distribution_pct: { aesthetic_leisure: 40, cognitive_curiosity: 60, domain_priming: 0 },
                      per_subreddit_shares: [],
                      recommendations: [],
                    },
                  },
                };
              }
              if (path === "/api/audit") {
                return {
                  body: {
                    audit: {
                      composite_health_score: 92,
                      actual_velocity_distribution_pct: { aesthetic_leisure: 40, cognitive_curiosity: 60, domain_priming: 0 },
                      target_distribution_pct: { aesthetic_leisure: 40, cognitive_curiosity: 60, domain_priming: 0 },
                      per_subreddit_shares: [
                        { name: "unixporn", category: "aesthetic_leisure", estimated_daily_posts: 95.0, impression_share_pct: 14.2, signal_density: 0.86 },
                      ],
                      recommendations: [],
                    },
                  },
                };
              }
              if (path === "/api/sync") {
                syncPayloads.push(JSON.parse(opts.body));
                return { body: { sync: { dry_run: true, subscriptions: { subscribed: [], already_subscribed: [], failed: [] }, custom_feeds: {} } } };
              }
              if (path === "/api/snapshots") return { body: { snapshots: [] } };
              return { body: {} };
            });

            await h.boot();
            h.document.getElementById("easy-preset-grid").querySelector(".easy-preset-card").click();
            await flushMicrotasks();

            // 1. Inspector merges per_subreddit_shares (not 0.00)
            const cardsEl = h.document.getElementById("inspector-cards");
            assert.ok(cardsEl.textContent.includes("95.0 posts/d"));
            assert.ok(cardsEl.textContent.includes("14.2%"));
            assert.ok(cardsEl.textContent.includes("0.86"));

            // 2. 0% target ratio preserved via ?? (not replaced by 25%)
            const legendText = h.document.getElementById("svg-donut-legend").textContent;
            assert.ok(legendText.includes("target 0%"), `Expected target 0% in ${legendText}`);

            // 3. Unchecking legacy tier1_systems_subreddits filters it out in getEffectiveManifest()
            const linuxToggle = cardsEl.querySelector('input[data-focus-sub="linux"]');
            assert.ok(linuxToggle);
            linuxToggle.click();
            h.document.getElementById("btn-sync-dry").click();
            await flushMicrotasks();
            assert.equal(syncPayloads[0].manifest.tier1_systems_subreddits.length, 0);

            // 4. Add custom r/zig, move to Tier 2, promote to Tier 1, create/remove custom feed, reset
            const addInput = h.document.getElementById("inspector-add-sub");
            addInput.value = "r/zig";
            h.document.getElementById("btn-inspector-add-sub").click();
            assert.ok(cardsEl.textContent.includes("r/zig"));

            const moveBtn = cardsEl.querySelector('button[data-focus-sub="zig"]');
            assert.equal(moveBtn.textContent, "Move to Tier 2");
            moveBtn.click();
            const promotedBtn = cardsEl.querySelector('button[data-focus-sub="zig"]');
            assert.equal(promotedBtn.textContent, "Promote to Tier 1");
            promotedBtn.click();
            assert.equal(cardsEl.querySelector('button[data-focus-sub="zig"]').textContent, "Move to Tier 2");

            const newFeedInput = h.document.getElementById("inspector-new-feed-name");
            newFeedInput.value = "Deep Zig";
            h.document.getElementById("btn-inspector-add-feed").click();
            assert.ok(h.document.getElementById("inspector-feeds-list").textContent.includes("Deep Zig"));

            // Reset to original preset removes r/zig
            h.document.getElementById("btn-inspector-reset").click();
            assert.ok(!cardsEl.textContent.includes("r/zig"));

            // 5. Importing custom JSON clears activePresetId highlight even if preset_id is inside JSON,
            //    and clears #easy-apply-result / #diff-preview-box
            h.document.getElementById("easy-apply-result").textContent = "STALE";
            const importInput = h.document.getElementById("import-manifest-input");
            importInput.files = [{
              name: "edited.json",
              size: 100,
              text: async () => JSON.stringify(presetManifest),
            }];
            importInput.dispatchEvent({ type: "change", target: importInput });
            await flushMicrotasks();

            const selectedEasy = h.document.getElementById("easy-preset-grid").querySelectorAll(".selected");
            assert.equal(selectedEasy.length, 0, "Custom JSON import must clear active preset highlight");
            assert.equal(h.document.getElementById("easy-apply-result").textContent, "");
            """
        )

    def test_confirmation_gates_snapshots_and_keyboard_a11y(self) -> None:
        self._run_node_scenario(
            r"""
            const syncCalls = [];
            const rbCalls = [];

            const h = createStudioHarness(async (path, opts) => {
              if (path.startsWith("/api/status")) return { body: { ok: true, csrf_token: "tok", cdp_port: 9222, browsers: [] } };
              if (path === "/api/presets") {
                return { body: { presets: [{ id: "systems_engineering", name: "Systems", description: "", anchor_count: 1, domain_priming_count: 1, custom_feeds_count: 0, golden_ratio_distribution: {}, preview_subreddits: ["rust"] }] } };
              }
              if (path === "/api/presets/systems_engineering") {
                return { body: { name: "Systems", anchor_subreddits: [{ name: "unixporn", category: "aesthetic_leisure" }], domain_priming_subreddits: [{ name: "rust", category: "domain_priming" }], custom_feeds: [] } };
              }
              if (path === "/api/audit") return { body: { audit: { composite_health_score: 90, actual_velocity_distribution_pct: {}, target_distribution_pct: {}, per_subreddit_shares: [], recommendations: [] } } };
              if (path === "/api/snapshots") {
                return {
                  body: {
                    snapshots: [
                      { snapshot_id: "snap_1", manifest_name: "Systems Pack", created_at: "2026-11-13", username: "tester", subscriptions_count: 10, custom_feeds_count: 1 },
                    ],
                  },
                };
              }
              if (path === "/api/sync") {
                syncCalls.push(JSON.parse(opts.body));
                return { body: { sync: { dry_run: false, subscriptions: { subscribed: ["rust"], already_subscribed: [], failed: [] }, custom_feeds: {} } } };
              }
              if (path === "/api/rollback") {
                rbCalls.push(JSON.parse(opts.body));
                return { body: { rollback: { snapshot_id: "snap_1", username: "tester", dry_run: false, restored_subscriptions: ["unixporn"], removed_subscriptions: [], restored_custom_feeds: {}, deleted_custom_feeds: [], success: true } } };
              }
              if (path === "/api/about") {
                return { body: { title: "About", motto: "M", references: [], global_statistics: [], pillars: [], themes: [{ id: "all", label: "All" }] } };
              }
              return { body: {} };
            });

            await h.boot();
            h.document.getElementById("easy-preset-grid").querySelector(".easy-preset-card").click();
            await flushMicrotasks();

            // Snapshots table includes manifest_name
            const snapTable = h.document.getElementById("snapshots-table-wrap");
            assert.ok(snapTable.textContent.includes("Systems Pack"));

            // Prune Live Sync requires confirmation
            const pruneCb = h.document.getElementById("check-prune");
            pruneCb.checked = true;
            h.document.getElementById("btn-sync-live").click();
            await flushMicrotasks();
            assert.equal(syncCalls.length, 0, "Prune live sync must wait for confirmation");
            const modal = h.document.getElementById("studio-confirm-modal");
            assert.ok(!modal.classList.contains("hidden"));

            h.document.getElementById("btn-confirm-cancel").click();
            await flushMicrotasks();
            assert.equal(syncCalls.length, 0);

            h.document.getElementById("btn-sync-live").click();
            await flushMicrotasks();
            h.document.getElementById("btn-confirm-proceed").click();
            await flushMicrotasks();
            assert.equal(syncCalls.length, 1);
            assert.equal(syncCalls[0].prune, true);

            // Live Rollback requires confirmation
            const buttons = snapTable.querySelectorAll("button");
            const rbBtn = buttons.find((b) => b.textContent === "Rollback");
            rbBtn.click();
            await flushMicrotasks();
            assert.equal(rbCalls.length, 0, "Live rollback must wait for confirmation");
            h.document.getElementById("btn-confirm-proceed").click();
            await flushMicrotasks();
            assert.equal(rbCalls.length, 1);

            // Export manifest uses slugified filename and delayed revokeObjectURL
            h.document.getElementById("btn-export-manifest").click();
            assert.equal(h.createdUrls.length, 1);
            assert.equal(h.revokedUrls.length, 0, "revokeObjectURL should be delayed, not synchronous");
            h.flushTimers();
            assert.equal(h.revokedUrls.length, 1);

            // Keyboard shortcuts guarded against modifiers & About open; ArrowRight navigates tabs
            h.document.getElementById("btn-mode-advanced").click();
            h.document.dispatchEvent({ type: "keydown", key: "2", ctrlKey: true });
            assert.ok(h.document.getElementById("view-lab").classList.contains("active"), "Ctrl+2 must not switch tab");

            h.document.dispatchEvent({ type: "keydown", key: "?" });
            await flushMicrotasks();
            assert.ok(h.document.body.classList.contains("about-open"));
            h.document.dispatchEvent({ type: "keydown", key: "2" });
            assert.ok(h.document.body.classList.contains("about-open"), "Number keys must not switch tabs while About is open");
            h.document.dispatchEvent({ type: "keydown", key: "Escape" });
            assert.ok(!h.document.body.classList.contains("about-open"));

            const tab1 = h.document.getElementById("tab-lab");
            tab1.dispatchEvent({ type: "keydown", key: "ArrowRight" });
            assert.ok(h.document.getElementById("view-inspector").classList.contains("active"));
            """
        )


if __name__ == "__main__":
    unittest.main()
