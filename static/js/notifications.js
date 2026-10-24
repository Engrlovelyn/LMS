/* Persistent header-bell notifications.
 *
 * Every dashboard already had a bell button and a notification panel, but the
 * panel was mock: a static "No new notifications" box that never changed, and
 * a badge that never lit. This script is the single real client for both the
 * server-rendered pages (base.html) and the standalone SPA dashboards. It reads
 * one config object, fetches the caller's own notifications, paints the panel,
 * and marks rows read server-side when opened.
 *
 * The server is the source of truth. Opening a notification goes through a
 * normal link to /communication/notifications/<id>/open/, which marks it read
 * and re-checks that the recipient may still reach the target, so nothing here
 * needs to guess at permissions.
 */
(function () {
  "use strict";

  var TYPE_META = {
    announcement: { icon: "fa-bullhorn", bg: "#fef3c7", color: "#d97706" },
    material: { icon: "fa-book-open", bg: "#e8f0fb", color: "#1e5faf" },
    assessment: { icon: "fa-file-pen", bg: "#ede9fe", color: "#7c3aed" },
    attendance: { icon: "fa-calendar-xmark", bg: "#fee2e2", color: "#dc2626" }
  };

  function cfg() {
    return window.tassNotifyConfig || {};
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function listEl() {
    if (cfg().listId) return document.getElementById(cfg().listId);
    return document.getElementById("notifList") || document.getElementById("studentNotifList");
  }

  function badgeEl() {
    var id = cfg().badgeId;
    if (id) return document.getElementById(id);
    return document.querySelector(".header-right .icon-btn .badge");
  }

  function csrfToken() {
    if (window.csrfToken) return window.csrfToken;
    var el = document.querySelector("#logoutForm input[name=csrfmiddlewaretoken]");
    if (el) return el.value;
    var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function setBadge(count) {
    var badge = badgeEl();
    if (!badge) return;
    if (count > 0) {
      badge.style.display = "";
      // A red dot, not the orange placeholder the mock used.
      badge.style.background = "#e11d48";
    } else {
      badge.style.display = "none";
    }
  }

  function emptyMarkup() {
    return (
      '<div style="text-align:center;padding:32px 16px;color:var(--text-muted);font-size:13px;">' +
      '<i class="fa-solid fa-bell-slash" style="font-size:28px;margin-bottom:10px;display:block;opacity:.5;"></i>' +
      "No new notifications</div>"
    );
  }

  function itemMarkup(n, index) {
    var meta = TYPE_META[n.type] || { icon: "fa-bell", bg: "#eef2ff", color: "#4f46e5" };
    var href = n.open_url || n.url || (cfg().allUrl || "/communication/notifications/");
    var idxAttr = ' data-tass-notif-idx="' + index + '"';
    var icon =
      '<div class="notif-dot-icon" style="background:' + meta.bg + ";color:" + meta.color + ';">' +
      '<i class="fa-solid ' + meta.icon + '"></i></div>';
    var body =
      '<div style="flex:1;min-width:0;">' +
      '<div class="notif-text" style="font-weight:600;">' + esc(n.title) + "</div>" +
      (n.message ? '<div class="notif-text" style="color:var(--text-muted);">' + esc(n.message) + "</div>" : "") +
      '<div class="notif-time">' + esc(n.created_label || n.created_at || "") + "</div>" +
      "</div>";
    // Assessment notifications get a "View" action that opens the student's
    // own result sheet (answers + score + correct answers). Everything else
    // keeps the single whole-row destination.
    if (n.view_url) {
      return (
        '<div class="notif-item' + (n.is_read ? "" : " notif-unread") +
        '" style="display:flex;gap:10px;align-items:flex-start;">' +
        '<a href="' + esc(href) + '"' + idxAttr + ' style="text-decoration:none;display:flex;gap:10px;color:inherit;flex:1;min-width:0;">' +
        icon + body + "</a>" +
        '<a class="btn btn-outline btn-sm" href="' + esc(n.view_url) + '" ' +
        'style="align-self:center;flex-shrink:0;padding:4px 10px;font-size:11.5px;text-decoration:none;" ' +
        'onclick="event.stopPropagation();"><i class="fa-solid fa-eye"></i> View</a>' +
        "</div>"
      );
    }
    return (
      '<a class="notif-item' + (n.is_read ? "" : " notif-unread") + '"' + idxAttr + ' ' +
      'href="' + esc(href) + '" style="text-decoration:none;display:flex;gap:10px;color:inherit;">' +
      icon + body +
      "</a>"
    );
  }

  // A dashboard can register ``tassNotifyConfig.onOpen`` to show a notification
  // in-page (e.g. jump to the exact announcement row) rather than following the
  // link. It receives the notification object and returns truthy when it handled
  // the click, which suppresses the default navigation.
  var currentItems = [];
  function onItemActivate(e) {
    var handler = cfg().onOpen;
    if (typeof handler !== "function") return;
    var node = e.target;
    var el = node && node.closest ? node.closest("[data-tass-notif-idx]") : null;
    if (!el) return;
    var idx = parseInt(el.getAttribute("data-tass-notif-idx"), 10);
    var n = currentItems[idx];
    if (!n) return;
    var handled = false;
    try { handled = handler(n, el); } catch (err) {}
    if (handled) e.preventDefault();
  }

  function render(data) {
    var el = listEl();
    if (!el) return;
    var items = (data && data.items) || [];
    currentItems = items;
    if (!items.length) {
      el.innerHTML = emptyMarkup();
    } else {
      el.innerHTML = items.map(function (n, i) { return itemMarkup(n, i); }).join("");
    }
    setBadge((data && data.unread_count) || 0);
  }

  function summaryUrl() {
    var base = cfg().summaryUrl || "/communication/notifications/summary/";
    var sep = base.indexOf("?") === -1 ? "?" : "&";
    return base + sep + "limit=20&_=" + Date.now();
  }

  function refresh() {
    return fetch(summaryUrl(), {
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) render(d); return d; })
      .catch(function () { return null; });
  }

  function markAllRead() {
    var url = cfg().markAllUrl || "/communication/notifications/mark-all-read/";
    var headers = { Accept: "application/json" };
    var token = csrfToken();
    if (token) headers["X-CSRFToken"] = token;
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: headers
    })
      .then(function () { setBadge(0); return refresh(); })
      .catch(function () { return null; });
  }

  // Opening the panel should always be showing fresh data, so toggleNotif
  // refreshes first. It keeps the existing open/close behaviour (and the
  // outside-click close in app.js / the SPA).
  function toggleNotif() {
    var panel = document.getElementById("notifPanel");
    if (!panel) { refresh(); return; }
    if (panel.classList.contains("open")) {
      panel.classList.remove("open");
    } else {
      panel.classList.add("open");
      if (typeof window.closeProfilePanel === "function") { try { window.closeProfilePanel(); } catch (e) {} }
      refresh();
    }
  }

  window.TassNotify = {
    refresh: refresh,
    render: render,
    markAllRead: markAllRead,
    setBadge: setBadge
  };

  // Replace the mock globals the templates wire into onclick= handlers.
  window.toggleNotif = toggleNotif;
  window.markAllRead = markAllRead;
  if (typeof window.renderStudentNotifs === "function" || document.getElementById("studentNotifList")) {
    window.renderStudentNotifs = refresh;
  }

  function boot() {
    document.addEventListener("click", onItemActivate, true);
    refresh();
    // A light poll keeps the red dot honest on pages without the live socket,
    // and refresh on the shared dashboard poll whenever it exists.
    setInterval(refresh, 30000);
    var prev = window.onTassPollRefresh;
    window.onTassPollRefresh = function () {
      var out = typeof prev === "function" ? prev.apply(this, arguments) : undefined;
      refresh();
      return out;
    };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
