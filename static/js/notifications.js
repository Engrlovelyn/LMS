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
    attendance: { icon: "fa-calendar-xmark", bg: "#fee2e2", color: "#dc2626" },
    message: { icon: "fa-comment-dots", bg: "#dcfce7", color: "#15803d" }
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

  // Non classroom dashboards also receive school-wide announcements. Keep
  // those links inside the active role SPA and open its announcements view.
  // Teacher and student templates handle their richer classroom routes.
  function restoreRoleDashboardTarget() {
    if (window.__TASS_NOTIFICATION_ROUTE_HANDLED) return;
    var params = new URLSearchParams(window.location.search);
    var tab = params.get("notification_tab");
    if (!tab) return;
    var recordType = params.get("notification_record_type") || "";
    var recordId = params.get("notification_record") || "";
    var notificationId = params.get("notification_id") || "";
    var candidates = {
      announcements: ["announcements", "dashboard"],
      materials: ["materials", "classrooms", "dashboard"],
      assessments: ["assessments", "assessmentrepo", "dashboard"],
      attendance: ["attendance", "dashboard"],
      messages: ["messages", "dashboard"]
    }[tab] || ["dashboard"];
    var activeView = "";
    for (var i = 0; i < candidates.length; i++) {
      if (document.getElementById("view-" + candidates[i])) {
        activeView = candidates[i];
        break;
      }
    }
    if (activeView && typeof window.showView === "function") {
      try { window.showView(activeView); } catch (e) {}
    }
    if (tab === "announcements") {
      ["renderAnnouncements", "renderACAnnouncements", "renderAnnouncementsView"].forEach(function (name) {
        if (typeof window[name] === "function") { try { window[name](); } catch (e) {} }
      });
    }
    if (recordType && recordId) {
      window.setTimeout(function () {
        var rows = document.querySelectorAll("[data-notification-record-type]");
        for (var j = 0; j < rows.length; j++) {
          if (rows[j].getAttribute("data-notification-record-type") === recordType &&
              rows[j].getAttribute("data-notification-record") === recordId) {
            rows[j].scrollIntoView({ behavior: "smooth", block: "center" });
            rows[j].setAttribute("tabindex", "-1");
            rows[j].focus({ preventScroll: true });
            break;
          }
        }
      }, 180);
    }
    if (notificationId) {
      var url = cfg().summaryUrl || "/communication/notifications/summary/";
      fetch(url + (url.indexOf("?") === -1 ? "?" : "&") + "limit=100&_=" + Date.now(), {
        credentials: "same-origin",
        headers: { Accept: "application/json" }
      }).then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          var item = ((data && data.items) || []).find(function (entry) {
            return String(entry.id) === String(notificationId);
          });
          if (!item) return;
          var overlay = document.getElementById("tassRoleNotificationDetail");
          if (!overlay) {
            overlay = document.createElement("div");
            overlay.id = "tassRoleNotificationDetail";
            overlay.setAttribute("role", "dialog");
            overlay.setAttribute("aria-modal", "true");
            overlay.style.cssText = "position:fixed;inset:0;z-index:5000;background:rgba(15,23,42,.52);display:flex;align-items:center;justify-content:center;padding:18px;";
            overlay.innerHTML = '<div style="width:min(620px,100%);max-height:85vh;overflow:auto;background:var(--card-bg,#fff);color:var(--text-primary,#1e293b);border:1px solid var(--card-border,#dbe4f0);border-radius:16px;padding:22px;box-shadow:0 24px 70px rgba(15,23,42,.3);"><div style="display:flex;align-items:flex-start;gap:16px;"><div style="flex:1;min-width:0;"><h2 data-role-notification-title style="font-size:18px;margin:0 0 10px;"></h2><div data-role-notification-message style="white-space:pre-wrap;line-height:1.65;font-size:14px;"></div></div><button type="button" data-role-notification-close class="btn btn-outline btn-sm" aria-label="Close notification">Close</button></div></div>';
            overlay.addEventListener("click", function (event) {
              if (event.target === overlay || event.target.hasAttribute("data-role-notification-close")) overlay.remove();
            });
            document.addEventListener("keydown", function closeOnEscape(event) {
              if (event.key === "Escape" && document.getElementById("tassRoleNotificationDetail")) {
                document.getElementById("tassRoleNotificationDetail").remove();
                document.removeEventListener("keydown", closeOnEscape);
              }
            });
            document.body.appendChild(overlay);
          }
          overlay.querySelector("[data-role-notification-title]").textContent = item.record_title || item.title || "Announcement";
          overlay.querySelector("[data-role-notification-message]").textContent = item.record_message || item.message || "";
          var closeButton = overlay.querySelector("[data-role-notification-close]");
          if (closeButton) closeButton.focus();
        }).catch(function () {});
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
  window.renderNotifs = refresh;
  if (typeof window.renderStudentNotifs === "function" || document.getElementById("studentNotifList")) {
    window.renderStudentNotifs = refresh;
  }

  function boot() {
    document.addEventListener("click", onItemActivate, true);
    restoreRoleDashboardTarget();
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
