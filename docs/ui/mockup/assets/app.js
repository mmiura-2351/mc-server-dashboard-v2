/* Shared mockup runtime: shell injection, tabs, toasts, modals, fake live data.
 * i18n: every string the runtime renders goes through t(); page-static HTML
 * stays plain English in the mockup (the real app routes everything via t()).
 */

(function () {
  "use strict";

  // ---------- i18n (structure demo; en only) ----------
  const STRINGS = {
    en: {
      "nav.community": "Community",
      "nav.dashboard": "Dashboard",
      "nav.createServer": "Create server",
      "nav.settings": "Community settings",
      "nav.admin": "Platform admin",
      "nav.adminOverview": "Overview",
      "nav.adminUsers": "Users",
      "nav.adminCommunities": "Communities",
      "nav.adminWorkers": "Workers",
      "nav.adminVersions": "Versions & JARs",
      "nav.adminAudit": "Global audit",
      "nav.account": "Account",
      "conn.live": "live",
      "conn.degraded": "Reconnecting — updates may lag",
      "toast.mock": "Mockup: no real API call was made.",
    },
  };
  let LOCALE = "en";
  window.t = (key) => (STRINGS[LOCALE] && STRINGS[LOCALE][key]) || key;

  // ---------- shell ----------
  function navItem(href, icon, label, active) {
    return `<a class="nav-item${active ? " active" : ""}" href="${href}">
      <span class="ico">${icon}</span>${label}</a>`;
  }

  function buildShell() {
    const page = document.body.dataset.page || "";
    const sidebar = document.getElementById("sidebar");
    const topbar = document.getElementById("topbar");
    if (!sidebar || !topbar) return;

    sidebar.innerHTML = `
      <div class="brand"><span class="cube"></span>MC Dashboard</div>
      <div class="nav-group">
        <div class="nav-label">${t("nav.community")}</div>
        ${navItem("dashboard.html", "▦", t("nav.dashboard"), page === "dashboard")}
        ${navItem("server-create.html", "+", t("nav.createServer"), page === "server-create")}
        ${navItem("community-settings.html", "⚙", t("nav.settings"), page === "community-settings")}
      </div>
      <div class="nav-group">
        <div class="nav-label">${t("nav.admin")}</div>
        ${navItem("admin-overview.html", "◎", t("nav.adminOverview"), page === "admin-overview")}
        ${navItem("admin-users.html", "👤", t("nav.adminUsers"), page === "admin-users")}
        ${navItem("admin-communities.html", "▣", t("nav.adminCommunities"), page === "admin-communities")}
        ${navItem("admin-workers.html", "🖧", t("nav.adminWorkers"), page === "admin-workers")}
        ${navItem("admin-versions.html", "⬇", t("nav.adminVersions"), page === "admin-versions")}
        ${navItem("admin-audit.html", "≡", t("nav.adminAudit"), page === "admin-audit")}
      </div>
      <div class="sidebar-foot">api v1.0 · ui mockup</div>`;

    const degraded = document.body.dataset.conn === "degraded";
    topbar.innerHTML = `
      <button class="menu-toggle" aria-label="Open menu">☰</button>
      <div class="community-switcher" onclick="mockToast()">
        ${MOCK.currentCommunity.name} <span class="chev">▼</span>
      </div>
      <div class="spacer"></div>
      <div class="conn-indicator${degraded ? " degraded" : ""}">
        <span class="dot"></span><span class="conn-label">${degraded ? t("conn.degraded") : t("conn.live")}</span>
      </div>
      <span class="lang-switcher" style="font-size:12px;color:var(--text-dim);cursor:pointer" onclick="mockToast('Language toggled')" title="Language"><strong>EN</strong> / JA</span>
      <a class="user-menu" href="account.html" title="${t("nav.account")}">
        <span class="avatar">${MOCK.me.username.slice(0, 1).toUpperCase()}</span>
        <span class="user-label">${MOCK.me.username}</span>
      </a>`;

    // Drawer backdrop (inserted after sidebar for mobile drawer)
    const backdrop = document.createElement("div");
    backdrop.className = "drawer-backdrop";
    sidebar.parentNode.insertBefore(backdrop, sidebar.nextSibling);

    // Drawer toggle handlers
    function openDrawer() {
      sidebar.classList.add("open");
      backdrop.classList.add("open");
    }
    function closeDrawer() {
      sidebar.classList.remove("open");
      backdrop.classList.remove("open");
    }
    topbar.querySelector(".menu-toggle").addEventListener("click", function () {
      if (sidebar.classList.contains("open")) { closeDrawer(); } else { openDrawer(); }
    });
    backdrop.addEventListener("click", closeDrawer);
    sidebar.querySelectorAll(".nav-item").forEach(function (item) {
      item.addEventListener("click", closeDrawer);
    });

    const banner = document.createElement("div");
    banner.className = "mock-banner";
    banner.textContent = "mockup — no api";
    document.body.appendChild(banner);

    const zone = document.createElement("div");
    zone.id = "toast-zone";
    document.body.appendChild(zone);
  }

  // ---------- toasts ----------
  window.toast = function (msg, kind) {
    const zone = document.getElementById("toast-zone");
    if (!zone) return;
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = msg;
    zone.appendChild(el);
    setTimeout(() => el.remove(), 3200);
  };
  window.mockToast = function (msg) { toast(msg || t("toast.mock")); };

  // ---------- modals ----------
  window.openModal = function (id) { document.getElementById(id).classList.add("open"); };
  window.closeModal = function (id) { document.getElementById(id).classList.remove("open"); };

  // ---------- tabs (hash-aware) ----------
  function initTabs() {
    const tabs = document.querySelectorAll(".tab[data-pane]");
    if (!tabs.length) return;
    function activate(name) {
      tabs.forEach((tb) => tb.classList.toggle("active", tb.dataset.pane === name));
      document.querySelectorAll(".tabpane").forEach((p) =>
        p.classList.toggle("active", p.id === "pane-" + name));
    }
    tabs.forEach((tb) =>
      tb.addEventListener("click", (e) => {
        e.preventDefault();
        history.replaceState(null, "", "#" + tb.dataset.pane);
        activate(tb.dataset.pane);
      }));
    function fromHash() {
      const name = (location.hash || "").slice(1);
      activate(
        name && [...tabs].some((tb) => tb.dataset.pane === name)
          ? name
          : tabs[0].dataset.pane
      );
    }
    window.addEventListener("hashchange", fromHash);
    fromHash();
  }

  // ---------- fake live log stream ----------
  window.startLogStream = function (viewId, opts) {
    const view = document.getElementById(viewId);
    if (!view) return;
    const o = opts || {};
    MOCK.logLines.forEach((l) => appendLog(view, l));
    let i = 0;
    setInterval(() => {
      const now = new Date();
      const ts = now.toTimeString().slice(0, 8);
      const tmpl = MOCK.extraLogLines[i % MOCK.extraLogLines.length];
      appendLog(view, tmpl.replace("%T", ts));
      i++;
      if (o.gapEvery && i % o.gapEvery === 0) {
        const gap = document.createElement("span");
        gap.className = "ln gap";
        gap.textContent = "— connection fell behind, some events were dropped —";
        view.appendChild(gap);
      }
    }, o.interval || 2600);
  };

  function appendLog(view, line, cls) {
    const el = document.createElement("span");
    el.className = "ln" + (cls ? " " + cls : (line.includes(" WARN]") ? " stderr" : ""));
    el.textContent = line;
    view.appendChild(el);
    if (!view.dataset.paused) view.scrollTop = view.scrollHeight;
    while (view.children.length > 400) view.removeChild(view.firstChild);
  }
  window.appendLogLine = function (viewId, line, cls) {
    const view = document.getElementById(viewId);
    if (view) appendLog(view, line, cls);
  };

  // ---------- fake RCON ----------
  window.initRcon = function (inputId, viewId) {
    const input = document.getElementById(inputId);
    if (!input) return;
    const history = [];
    let hi = -1;
    const cannedOutput = {
      list: "There are 7 of a max of 20 players online: steve_99, alex_dig, enderchan, ...",
      "save-all": "Saved the game",
      tps: "TPS from last 1m, 5m, 15m: 19.8, 19.9, 20.0",
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowUp") { if (hi < history.length - 1) input.value = history[++hi] || ""; e.preventDefault(); }
      else if (e.key === "ArrowDown") { if (hi >= 0) input.value = history[--hi] || ""; e.preventDefault(); }
      else if (e.key === "Enter") {
        const cmd = input.value.trim();
        if (!cmd) return;
        history.unshift(cmd); hi = -1;
        appendLogLine(viewId, "> " + cmd, "cmd");
        const out = cannedOutput[cmd.split(" ")[0]] || "Executed: " + cmd;
        appendLogLine(viewId, out, "cmd-out");
        input.value = "";
      }
    });
  };

  // ---------- sparkline ----------
  window.renderSparkline = function (el, points) {
    const max = Math.max.apply(null, points) || 1;
    el.innerHTML = points
      .map((p) => `<i style="height:${Math.max(6, Math.round((p / max) * 100))}%"></i>`)
      .join("");
  };

  // ---------- dashboard live status demo ----------
  window.startStatusDemo = function () {
    // The "starting" server flips to running after a while; crashed blinks.
    setTimeout(() => {
      const pill = document.querySelector('[data-live-state="s-2"]');
      if (pill) {
        pill.className = "pill running";
        pill.textContent = "running";
        toast("creative-build is now running", "ok");
      }
    }, 7000);
  };

  // ---------- responsive table wrapping ----------
  function wrapTables() {
    document.querySelectorAll("table.data").forEach(function (tbl) {
      if (tbl.parentNode.classList.contains("table-wrap")) return;
      var wrap = document.createElement("div");
      wrap.className = "table-wrap";
      tbl.parentNode.insertBefore(wrap, tbl);
      wrap.appendChild(tbl);
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (document.getElementById("sidebar")) buildShell();
    initTabs();
    wrapTables();
  });
})();
