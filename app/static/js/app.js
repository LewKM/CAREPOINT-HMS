// CarePoint HMS — small progressive enhancements. Every page works without this file.
(function () {
  "use strict";

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };

  // Icons are server-rendered <svg><use> references into the sprite; JS-built ones use the same markup.
  const SPRITE = document.body.dataset.sprite || "/static/icons.svg";
  function iconSvg(name) {
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", `lucide lucide-${name}`);
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(NS, "use");
    use.setAttribute("href", `${SPRITE}#${name}`);
    svg.appendChild(use);
    return svg;
  }

  // ---- theme
  function applyTheme(t) {
    if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
    else delete document.documentElement.dataset.theme;
  }
  function isDark() {
    const t = document.documentElement.dataset.theme;
    return t ? t === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-theme-toggle]");
    if (!btn) return;
    const next = isDark() ? "light" : "dark";
    applyTheme(next);
    store.set("cp-theme", next);
  });

  // ---- mobile nav
  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-nav-toggle]")) document.body.classList.toggle("nav-open");
    else if (e.target.classList.contains("scrim")) document.body.classList.remove("nav-open");
  });

  // ---- confirm + double-submit guard
  document.addEventListener("submit", (e) => {
    const form = e.target;
    const msg = form.dataset.confirm;
    if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
    if (form.dataset.submitting) { e.preventDefault(); return; }
    form.dataset.submitting = "1";
    form.querySelectorAll("button[type=submit], button:not([type])").forEach((b) => { b.disabled = true; });
    setTimeout(() => { delete form.dataset.submitting; form.querySelectorAll("button").forEach((b) => { b.disabled = false; }); }, 8000);
  });

  // ---- flashes
  function dismiss(f) {
    f.classList.add("leaving");
    f.addEventListener("animationend", () => f.remove(), { once: true });
  }
  function flashes() {
    document.querySelectorAll(".flash").forEach((f) => {
      f.querySelector("button")?.addEventListener("click", () => dismiss(f));
      if (f.classList.contains("success")) setTimeout(() => dismiss(f), 6000);
    });
  }

  // ---- motion: stagger index for entrance animations, count-up stat values
  const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  function stagger() {
    document.querySelectorAll(".content, .stats, .stack, .grid, .beds").forEach((list) => {
      Array.from(list.children).forEach((el, i) => el.style.setProperty("--i", i));
    });
  }
  function countUp() {
    if (calm) return;
    document.querySelectorAll("[data-count]").forEach((el) => {
      const text = el.textContent.trim();
      const nums = text.match(/\d[\d,]*(\.\d+)?/g);
      if (!nums) return;
      const parts = nums.map((n) => ({ value: parseFloat(n.replace(/,/g, "")), decimals: (n.split(".")[1] || "").length, commas: n.includes(",") }));
      const fmt = (p, v) => p.commas || v >= 1000
        ? v.toLocaleString("en-US", { minimumFractionDigits: p.decimals, maximumFractionDigits: p.decimals })
        : v.toFixed(p.decimals);
      const start = performance.now(), dur = 900;
      function frame(now) {
        const t = Math.min(1, (now - start) / dur), e = 1 - Math.pow(1 - t, 3);
        let i = 0;
        el.textContent = text.replace(/\d[\d,]*(\.\d+)?/g, () => { const p = parts[i++]; return fmt(p, t < 1 ? p.value * e : p.value); });
        if (t < 1) requestAnimationFrame(frame);
      }
      requestAnimationFrame(frame);
    });
  }

  // ---- topbar elevation on scroll
  function topbar() {
    const bar = document.querySelector(".topbar");
    if (!bar) return;
    const update = () => bar.classList.toggle("scrolled", window.scrollY > 4);
    window.addEventListener("scroll", update, { passive: true });
    update();
  }

  // ---- "/" focuses patient search
  document.addEventListener("keydown", (e) => {
    if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.closest("input, textarea, select, [contenteditable]")) return;
    const search = document.querySelector("[data-search]");
    if (search) { e.preventDefault(); search.focus(); }
  });

  // ---- clickable table rows
  document.addEventListener("click", (e) => {
    const row = e.target.closest("tr[data-href]");
    if (!row || e.target.closest("a, button, form, input, select")) return;
    window.location = row.dataset.href;
  });

  // ---- patient lookup: <div class="lookup" data-lookup> with input.lookup-input + input[type=hidden]
  function initLookup(root) {
    const input = root.querySelector(".lookup-input");
    const hidden = root.querySelector("input[type=hidden]");
    const chip = root.querySelector(".lookup-chip");
    const results = root.querySelector(".lookup-results");
    let timer, active = -1, items = [];

    function choose(p) {
      hidden.value = p.id;
      chip.querySelector(".lookup-name").textContent = p.name;
      chip.querySelector(".lookup-meta").textContent = `${p.mrn} · ${p.age} y · ${p.gender}${p.phone ? " · " + p.phone : ""}`;
      chip.hidden = false; input.hidden = true; results.hidden = true;
    }
    chip.querySelector("button").addEventListener("click", () => {
      hidden.value = ""; chip.hidden = true; input.hidden = false; input.value = ""; input.focus();
    });
    function render() {
      results.innerHTML = "";
      if (!items.length) {
        results.innerHTML = '<div class="empty" style="padding:14px">No matching patients</div>';
      }
      items.forEach((p, i) => {
        const b = document.createElement("button");
        b.type = "button";
        if (i === active) b.className = "active";
        const av = document.createElement("span"); av.className = "avatar"; av.style.cssText = "width:28px;height:28px;font-size:11px";
        av.textContent = p.name.split(" ").slice(0, 2).map((s) => s[0]).join("").toUpperCase();
        const t = document.createElement("span");
        const n = document.createElement("div"); n.className = "strong"; n.textContent = p.name;
        const m = document.createElement("div"); m.className = "small muted"; m.textContent = `${p.mrn} · ${p.age} y · ${p.gender}${p.phone ? " · " + p.phone : ""}`;
        t.append(n, m); b.append(av, t);
        b.addEventListener("click", () => choose(p));
        results.appendChild(b);
      });
      results.hidden = false;
    }
    input.addEventListener("input", () => {
      clearTimeout(timer);
      const q = input.value.trim();
      if (q.length < 2) { results.hidden = true; return; }
      timer = setTimeout(async () => {
        try {
          const r = await fetch(`/patients/lookup?q=${encodeURIComponent(q)}`, { headers: { Accept: "application/json" } });
          items = r.ok ? await r.json() : []; active = -1; render();
        } catch (err) { items = []; render(); }
      }, 200);
    });
    input.addEventListener("keydown", (e) => {
      if (results.hidden || !items.length) return;
      if (e.key === "ArrowDown") { active = Math.min(items.length - 1, active + 1); render(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { active = Math.max(0, active - 1); render(); e.preventDefault(); }
      else if (e.key === "Enter" && active >= 0) { choose(items[active]); e.preventDefault(); }
      else if (e.key === "Escape") { results.hidden = true; }
    });
    document.addEventListener("click", (e) => { if (!root.contains(e.target)) results.hidden = true; });
  }

  // ---- repeatable rows (prescription items)
  function initRepeat(root) {
    const tpl = root.querySelector("template");
    const list = root.querySelector("[data-rows]");
    const add = () => list.appendChild(tpl.content.cloneNode(true));
    root.querySelector("[data-add-row]").addEventListener("click", add);
    list.addEventListener("click", (e) => {
      const rm = e.target.closest("[data-remove-row]");
      if (rm && list.children.length > 1) rm.closest(".rx-row").remove();
    });
    if (!list.children.length) add();
  }

  // ---- auto-fill quantity: dose count × frequency per day × days for tablets
  const PER_DAY = { od: 1, "once daily": 1, bd: 2, "twice daily": 2, tds: 3, "three times daily": 3, qid: 4, "four times daily": 4, nocte: 1, stat: 1 };
  document.addEventListener("change", (e) => {
    const row = e.target.closest(".rx-row");
    if (!row || !e.target.matches("[name=frequency], [name=duration_days], [name=dose]")) return;
    const qty = row.querySelector("[name=quantity]");
    if (qty.dataset.touched) return;
    const per = PER_DAY[(row.querySelector("[name=frequency]").value || "").toLowerCase().trim()];
    const days = parseInt(row.querySelector("[name=duration_days]").value, 10);
    const dose = parseFloat(row.querySelector("[name=dose]").value) || 1;
    if (per && days) qty.value = Math.ceil(per * days * (dose < 10 ? dose : 1));
  });
  document.addEventListener("input", (e) => { if (e.target.matches("[name=quantity]")) e.target.dataset.touched = "1"; });

  // ---- page progress: the database is remote, so give every navigation immediate feedback
  const progress = () => document.querySelector(".page-progress");
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (a.target || a.hasAttribute("download") || a.origin !== location.origin) return;
    if (a.pathname === location.pathname && a.search === location.search && a.hash) return;
    progress()?.classList.add("active");
  });
  document.addEventListener("submit", (e) => { if (!e.defaultPrevented) progress()?.classList.add("active"); });
  window.addEventListener("pageshow", () => progress()?.classList.remove("active"));

  // ---- command palette (Ctrl/⌘+K): pages and actions for this role, plus live patient search
  function initPalette() {
    const dlg = document.querySelector("dialog.palette");
    const dataEl = document.getElementById("palette-data");
    if (!dlg || !dataEl) return;
    const entries = JSON.parse(dataEl.textContent);
    const input = dlg.querySelector("input");
    const list = dlg.querySelector(".palette-results");
    let shown = [], active = 0, timer, seq = 0;

    function draw() {
      list.replaceChildren();
      if (!shown.length) {
        const none = document.createElement("div"); none.className = "palette-empty"; none.textContent = "Nothing matches.";
        list.appendChild(none); return;
      }
      let group = null;
      shown.forEach((it, i) => {
        if (it.group !== group) {
          group = it.group;
          const h = document.createElement("div"); h.className = "palette-group"; h.textContent = group; list.appendChild(h);
        }
        const a = document.createElement("a");
        a.href = it.href; a.setAttribute("role", "option");
        if (i === active) { a.className = "active"; a.setAttribute("aria-selected", "true"); }
        const label = document.createElement("span"); label.className = "grow"; label.textContent = it.label;
        a.append(iconSvg(it.icon), label);
        if (it.meta) { const m = document.createElement("span"); m.className = "muted small"; m.textContent = it.meta; a.appendChild(m); }
        a.addEventListener("mousemove", () => { if (active !== i) { active = i; draw(); } });
        list.appendChild(a);
      });
      list.querySelector("a.active")?.scrollIntoView({ block: "nearest" });
    }
    function filter() {
      const q = input.value.trim().toLowerCase();
      const words = q.split(/\s+/).filter(Boolean);
      shown = entries.filter((e) => words.every((w) => `${e.label} ${e.group}`.toLowerCase().includes(w)));
      active = 0; draw();
      clearTimeout(timer);
      if (q.length < 2) return;
      const mine = ++seq;
      timer = setTimeout(async () => {
        try {
          const r = await fetch(`/patients/lookup?q=${encodeURIComponent(q)}`, { headers: { Accept: "application/json" } });
          if (!r.ok || mine !== seq) return;
          const patients = (await r.json()).map((p) => ({
            label: p.name, href: `/patients/${p.id}`, icon: "user-round", group: "Patients", meta: `${p.mrn} · ${p.age} y`,
          }));
          shown = shown.filter((e) => e.group !== "Patients").concat(patients); draw();
        } catch (err) { /* offline: pages and actions still work */ }
      }, 180);
    }
    function open() {
      if (dlg.open) return;
      input.value = ""; filter(); dlg.showModal(); input.focus();
    }
    input.addEventListener("input", filter);
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { active = Math.min(shown.length - 1, active + 1); draw(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { active = Math.max(0, active - 1); draw(); e.preventDefault(); }
      else if (e.key === "Enter" && shown[active]) { e.preventDefault(); progress()?.classList.add("active"); location.href = shown[active].href; }
    });
    dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
    document.addEventListener("click", (e) => { if (e.target.closest("[data-palette-open]")) open(); });
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); dlg.open ? dlg.close() : open(); }
    });
  }

  // ---- live regions: <div id="…" data-live="30"> refreshes itself from the server so queues stay current.
  // Rows carry data-key; ones that weren't there before get highlighted. Skipped while the person is
  // working inside the region, and slowed down in a background tab, which gets a ● in its title instead.
  function initLive() {
    const regions = Array.from(document.querySelectorAll("[data-live][id]"));
    if (!regions.length) return;
    const baseTitle = document.title;
    const every = Math.max(10, parseInt(regions[0].dataset.live, 10) || 30) * 1000;
    let stopped = false;

    function keys(root) { return new Set(Array.from(root.querySelectorAll("[data-key]"), (el) => el.dataset.key)); }
    function busy(region) {
      return region.contains(document.activeElement) && document.activeElement !== document.body
        || region.querySelector("form[data-submitting]");
    }
    async function refresh() {
      if (stopped) return;
      try {
        const r = await fetch(location.href, { headers: { "X-Live": "1" }, credentials: "same-origin" });
        if (r.redirected || !r.ok) { stopped = r.redirected; return; }  // signed out: stop quietly
        const doc = new DOMParser().parseFromString(await r.text(), "text/html");
        let arrived = false;
        regions.forEach((region) => {
          const fresh = doc.getElementById(region.id);
          if (!fresh || busy(region) || fresh.innerHTML === region.innerHTML) return;
          const before = keys(region);
          region.classList.add("settled");
          region.innerHTML = fresh.innerHTML;
          region.querySelectorAll("[data-key]").forEach((el) => {
            if (!before.has(el.dataset.key)) { el.classList.add("is-new"); arrived = true; }
          });
        });
        if (arrived && document.hidden) document.title = "● " + baseTitle;
        document.querySelectorAll("[data-live-stamp]").forEach((el) => {
          el.textContent = "Updated " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        });
      } catch (err) { /* network blip: try again next tick */ }
    }
    (function tick() { setTimeout(() => refresh().finally(tick), document.hidden ? every * 2 : every); })();
    document.addEventListener("visibilitychange", () => { if (!document.hidden) { document.title = baseTitle; refresh(); } });
  }

  document.addEventListener("DOMContentLoaded", () => {
    initPalette();
    initLive();
    stagger();
    countUp();
    topbar();
    flashes();
    document.querySelectorAll("[data-lookup]").forEach(initLookup);
    document.querySelectorAll("[data-repeat]").forEach(initRepeat);
  });
})();
