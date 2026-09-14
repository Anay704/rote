// In-page helper library for the web surface. Evaluated once per frame document; installs
// window.__rote. It never mutates the DOM: element references are held in a JS array, and
// redaction happens on the serialised output, not on the page.
//
// The same functions are used when RECORDING (to describe a control) and when REPLAYING (to find
// it again). That symmetry is what makes a label/text/table locator mean the same thing both times.
(() => {
  if (window.__rote && window.__rote.v === 4) return;

  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const key = (s) => norm(s).replace(/[:*]+$/, "").trim().toUpperCase();

  const INPUT_TEXT = new Set(["", "text", "search", "email", "tel", "number", "password", "url", "date"]);

  function role(el) {
    const explicit = el.getAttribute && el.getAttribute("role");
    if (explicit) return explicit.split(" ")[0];
    const tag = el.tagName;
    if (tag === "A" && el.hasAttribute("href")) return "link";
    if (tag === "BUTTON") return "button";
    if (tag === "INPUT") {
      const t = (el.getAttribute("type") || "").toLowerCase();
      if (["submit", "button", "reset", "image"].includes(t)) return "button";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "hidden") return null;
      if (INPUT_TEXT.has(t)) return "textbox";
      return null;
    }
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "SELECT") return el.multiple || el.size > 1 ? "listbox" : "combobox";
    if (isNonSemanticClickable(el)) return "clickable";
    return null;
  }

  function isNonSemanticClickable(el) {
    if (!(el instanceof HTMLElement)) return false;
    if (["A", "BUTTON", "INPUT", "SELECT", "TEXTAREA", "LABEL", "BODY", "HTML", "FORM"].includes(el.tagName)) return false;
    if (el.hasAttribute("onclick")) return true;
    const cs = getComputedStyle(el);
    if (cs.cursor === "pointer") {
      const parent = el.parentElement;
      return !(parent && getComputedStyle(parent).cursor === "pointer");
    }
    return false;
  }

  function visible(el) {
    if (!(el instanceof Element)) return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    if (el.checkVisibility) return el.checkVisibility({ visibilityProperty: true, opacityProperty: true });
    const cs = getComputedStyle(el);
    return cs.visibility !== "hidden" && cs.display !== "none";
  }

  function accName(el) {
    const aria = el.getAttribute("aria-label");
    if (aria) return norm(aria);
    const lb = el.getAttribute("aria-labelledby");
    if (lb) return norm(lb.split(/\s+/).map((id) => (document.getElementById(id) || {}).textContent || "").join(" "));
    const r = role(el);
    if (r === "textbox" || r === "combobox" || r === "listbox" || r === "checkbox" || r === "radio") {
      if (el.id) {
        const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (l) return norm(l.textContent);
      }
      const wrap = el.closest("label");
      if (wrap) return norm(wrap.textContent);
      return norm(el.getAttribute("title") || el.getAttribute("placeholder") || "");
    }
    if (el.tagName === "INPUT") return norm(el.value || el.getAttribute("alt") || el.getAttribute("title") || "");
    const t = norm(el.innerText || el.textContent);
    if (t) return t;
    const img = el.querySelector && el.querySelector("img[alt]");
    return norm((img && img.getAttribute("alt")) || el.getAttribute("title") || "");
  }

  // Text-bearing leaves: elements with their own non-empty text node.
  function textLeaves(root) {
    const out = [];
    const walker = document.createTreeWalker(root || document.body, NodeFilter.SHOW_ELEMENT);
    for (let el = walker.currentNode; el; el = walker.nextNode()) {
      if (!(el instanceof HTMLElement) || ["SCRIPT", "STYLE", "OPTION", "SELECT", "TEXTAREA"].includes(el.tagName)) continue;
      let own = "";
      for (const c of el.childNodes) if (c.nodeType === 3) own += c.textContent;
      if (norm(own) && visible(el)) out.push(el);
    }
    return out;
  }

  // The visual label an operator would read for a control: nearest text to the left on the same
  // line, else nearest text directly above. Pure geometry, no markup assumptions.
  function labelFor(el) {
    const r = el.getBoundingClientRect();
    const cy = r.top + r.height / 2;
    let best = null, bestD = Infinity;
    for (const t of textLeaves()) {
      if (t === el || el.contains(t) || t.contains(el)) continue;
      const tr = t.getBoundingClientRect();
      const tcy = tr.top + tr.height / 2;
      if (tr.right <= r.left + 2 && Math.abs(tcy - cy) <= Math.max(r.height, tr.height) / 2) {
        const d = r.left - tr.right;
        if (d < bestD && d < 320) { best = t; bestD = d; }
      }
    }
    if (!best) {
      for (const t of textLeaves()) {
        if (t === el || el.contains(t) || t.contains(el)) continue;
        const tr = t.getBoundingClientRect();
        const overlap = Math.min(tr.right, r.right) - Math.max(tr.left, r.left);
        if (tr.bottom <= r.top + 2 && overlap > 0) {
          const d = r.top - tr.bottom;
          if (d < bestD && d < 40) { best = t; bestD = d; }
        }
      }
    }
    if (!best) return "";
    let own = "";
    for (const c of best.childNodes) if (c.nodeType === 3) own += c.textContent;
    return norm(own);
  }

  const INTERACTIVE = new Set(["link", "button", "textbox", "combobox", "listbox", "checkbox", "radio", "clickable"]);

  function interactive() {
    const out = [];
    for (const el of document.body ? document.body.querySelectorAll("*") : []) {
      const r = role(el);
      if (r && INTERACTIVE.has(r) && visible(el)) out.push(el);
    }
    return out;
  }

  function cellHeader(td) {
    const tr = td.closest("tr"), table = td.closest("table");
    if (!tr || !table) return "";
    const idx = Array.prototype.indexOf.call(tr.cells, td);
    for (const row of table.rows) {
      if (row === tr) break;
      const c = row.cells[idx];
      if (c && norm(c.innerText)) return norm(c.innerText);
    }
    return "";
  }

  function describe(el) {
    const r = el.getBoundingClientRect();
    const rl = role(el) || (el.tagName === "TD" || el.tagName === "TH" ? "cell" : "text");
    const form = el.form || (el.closest && el.closest("form"));
    const t = (el.getAttribute("type") || "").toLowerCase();
    return {
      role: rl,
      name: rl === "cell" || rl === "text" ? "" : accName(el),
      label: ["textbox", "combobox", "listbox", "checkbox", "radio"].includes(rl) ? labelFor(el) : "",
      text: norm(el.innerText || el.value || "").slice(0, 200),
      tag: el.tagName.toLowerCase(),
      href: el.getAttribute("href"),
      target_frame: el.getAttribute("target"),
      submit: el.tagName === "INPUT" && ["submit", "image"].includes(t) || el.tagName === "BUTTON" && t !== "button" && !!form,
      form_method: form ? (form.getAttribute("method") || "get").toLowerCase() : null,
      options: el.tagName === "SELECT" ? Array.from(el.options).map((o) => norm(o.text)) : null,
      bbox: [r.left, r.top, r.width, r.height],
      header: el.tagName === "TD" || el.tagName === "TH" ? cellHeader(el) : "",
    };
  }

  // ------------------------------------------------------------------ snapshot for the model
  // Shapes that are masked in screenshots even when no sensitive label is adjacent (text is
  // additionally pattern-redacted in Python).
  const PII_SHAPES = [/\b\d{3}-\d{2}-\d{4}\b/, /\b\d{3}-\d{3}-\d{4}\b/, /\b\d{2}\/\d{2}\/\d{4}\b/];
  const BLOCK = new Set(["DIV", "P", "BR", "TR", "TABLE", "FORM", "H1", "H2", "H3", "H4", "H5", "H6", "LI", "UL", "OL", "CENTER", "HR", "FIELDSET", "SECTION", "HEADER", "FOOTER", "MAIN", "NAV"]);

  // maskRects: identity data, masked in every image. logRects: additionally masked in images that are
  // persisted (money amounts, and argument values the capability classifies as pii/financial).
  const MONEY = /\$\s?[\d,]+\.\d{2}/;
  function snapshot(sensitiveLabels, logTerms) {
    const refs = [];
    const sens = new Set((sensitiveLabels || []).map(key));
    const terms = (logTerms || []).filter((t) => t && t.length >= 2);
    const maskRects = [];
    const logRects = [];
    const out = [];

    const labelOfCell = (el) => {
      // value cells following a sensitive label cell (or label leaf) are redacted
      const td = el.closest && el.closest("td,th");
      if (!td) return false;
      let prev = td.previousElementSibling;
      while (prev && !norm(prev.innerText)) prev = prev.previousElementSibling;
      return !!prev && sens.has(key(prev.innerText));
    };

    function emitRef(el, kind) {
      const i = refs.length;
      refs.push(el);
      return `${kind}${i}`;
    }

    function walk(node) {
      if (node.nodeType === 3) {
        const t = norm(node.textContent);
        if (!t) return;
        const parent = node.parentElement;
        if (parent && PII_SHAPES.some((re) => re.test(t))) {
          const r = parent.getBoundingClientRect();
          maskRects.push([r.left, r.top, r.width, r.height]);
        }
        if (parent && (MONEY.test(t) || terms.some((term) => t.includes(term)))) {
          const r = parent.getBoundingClientRect();
          logRects.push([r.left, r.top, r.width, r.height]);
        }
        if (parent && labelOfCell(parent)) {
          const r = parent.getBoundingClientRect();
          maskRects.push([r.left, r.top, r.width, r.height]);
          out.push(" ‹redacted› ");
        } else {
          out.push(" " + t + " ");
        }
        return;
      }
      if (node.nodeType !== 1) return;
      const el = node;
      if (["SCRIPT", "STYLE", "NOSCRIPT", "HEAD"].includes(el.tagName)) return;
      if (el.tagName === "INPUT" && (el.type || "").toLowerCase() === "hidden") return;
      if (!visible(el) && el.tagName !== "BR") return;
      const r = role(el);
      if (r && INTERACTIVE.has(r)) {
        const ref = emitRef(el, "");
        const d = describe(el);
        let s = `[@${ref} ${r}`;
        if (d.name) s += ` "${d.name.slice(0, 60)}"`;
        if (d.label) s += ` label="${d.label}"`;
        if (r === "textbox") {
          const v = (el.getAttribute("type") || "").toLowerCase() === "password" ? (el.value ? "********" : "") : el.value;
          s += ` value="${v || ""}"`;
        }
        if (d.options) s += ` options=${JSON.stringify(d.options.slice(0, 12))}`;
        if (el.tagName === "SELECT") s += ` selected="${norm(el.options[el.selectedIndex]?.text || "")}"`;
        s += "]";
        out.push(" " + s + " ");
        if (r === "link" || r === "button" || r === "clickable" || el.tagName === "SELECT" || el.tagName === "INPUT" || el.tagName === "TEXTAREA") return;
      }
      if (el.tagName === "TD" || el.tagName === "TH") {
        const hasInteractive = el.querySelector("a[href],input,select,textarea,button,[onclick]");
        if (!hasInteractive && norm(el.innerText)) {
          const ref = emitRef(el, "");
          out.push(` {@${ref}} `);
        }
      }
      if (BLOCK.has(el.tagName)) out.push("\n");
      for (const c of el.childNodes) walk(c);
      if (el.tagName === "TD" || el.tagName === "TH") out.push(" |");
      if (BLOCK.has(el.tagName)) out.push("\n");
    }

    if (document.body) walk(document.body);
    window.__rote.refs = refs;
    const text = out.join("").split("\n").map((l) => l.replace(/\s+/g, " ").trim()).filter(Boolean).join("\n");
    return { text, count: refs.length, maskRects, logRects, title: document.title };
  }

  // ------------------------------------------------------------------ resolution for replay
  function innermost(list) {
    return list.filter((a) => !list.some((b) => b !== a && a.contains(b)));
  }

  function resolveLabel(r, label) {
    const k = key(label);
    return interactive().filter((el) => role(el) === r && key(labelFor(el)) === k);
  }

  function resolveText(text, r) {
    const k = key(text);
    const hits = interactive().filter((el) => {
      const rr = role(el);
      if (r && rr !== r) return false;
      if (!["button", "link", "clickable"].includes(rr)) return false;
      return key(el.tagName === "INPUT" ? el.value : el.innerText) === k;
    });
    return innermost(hits);
  }

  // column === null addresses key/value grids ("OPENING DEPOSIT | $500.00"): the first non-empty
  // cell after the cell equal to rowKey.
  function resolveTableCell(rowKey, column) {
    const rk = key(rowKey);
    const out = [];
    if (column === null || column === undefined) {
      for (const row of document.querySelectorAll("tr")) {
        const cells = Array.from(row.cells);
        const i = cells.findIndex((c) => key(c.innerText) === rk && !c.querySelector("table"));
        if (i < 0) continue;
        const v = cells.slice(i + 1).find((c) => norm(c.innerText));
        if (v && visible(v)) out.push(v);
      }
      return out;
    }
    const ck = key(column);
    for (const table of document.querySelectorAll("table")) {
      let colIdx = -1, headerRow = null;
      for (const row of table.rows) {
        if (row.closest("table") !== table) continue;
        if (colIdx < 0) {
          const i = Array.from(row.cells).findIndex((c) => key(c.innerText) === ck);
          if (i >= 0) { colIdx = i; headerRow = row; }
          continue;
        }
        if (Array.from(row.cells).some((c) => key(c.innerText) === rk) && row.cells[colIdx] && visible(row.cells[colIdx])) {
          out.push(row.cells[colIdx]);
        }
      }
    }
    return out;
  }

  function cssPath(el) {
    const parts = [];
    for (let e = el; e && e.nodeType === 1 && e !== document.documentElement; e = e.parentElement) {
      let i = 1;
      for (let s = e.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === e.tagName) i++;
      parts.unshift(`${e.tagName.toLowerCase()}:nth-of-type(${i})`);
    }
    return parts.join(" > ");
  }

  function resolveCss(sel) {
    try { return Array.from(document.querySelectorAll(sel)).filter(visible); } catch (e) { return []; }
  }

  // Row context for a data cell: helps the recorder choose a stable row key.
  function rowCells(el) {
    const tr = el.closest("tr");
    if (!tr) return [];
    return Array.from(tr.cells).map((c) => norm(c.innerText));
  }

  // A DOM dump safe to persist: label-adjacent values and form values are blanked on a clone.
  function redactedHTML(sensitiveLabels) {
    const sens = new Set((sensitiveLabels || []).map(key));
    const clone = document.documentElement.cloneNode(true);
    for (const td of clone.querySelectorAll("td,th")) {
      let prev = td.previousElementSibling;
      while (prev && !norm(prev.textContent)) prev = prev.previousElementSibling;
      if (prev && sens.has(key(prev.textContent)) && !td.querySelector("td")) td.textContent = "‹redacted›";
    }
    for (const inp of clone.querySelectorAll("input")) {
      if ((inp.getAttribute("type") || "").toLowerCase() === "password") inp.removeAttribute("value");
    }
    for (const s of clone.querySelectorAll("script")) s.textContent = "";
    return "<!doctype html>\n" + clone.outerHTML;
  }

  window.__rote = {
    v: 4, norm, key, role, accName, labelFor, describe, snapshot, interactive, redactedHTML,
    resolveLabel, resolveText, resolveTableCell, resolveCss, cssPath, rowCells, cellHeader,
    refs: [],
  };
})();
