/* ═══════════════════════════════════════════════════════════
   SVI LMS · Shared UI helpers (final front-end reference shell)
   Sidebar, dark mode, view router, tabs, modals, toasts, panels.
   ═══════════════════════════════════════════════════════════ */

/* ── STATE ── */
let isDarkMode = false, sidebarExpanded = false, notifOpen = false, profileOpen = false;

/* ── THEME ── */
function setTheme(mode) {
  isDarkMode = (mode === 'dark');
  document.documentElement.setAttribute('data-theme', mode);
  const b = document.getElementById('darkToggleBtn');
  if (b) b.innerHTML = isDarkMode ? '<i class="fa-solid fa-sun"></i>' : '<i class="fa-solid fa-moon"></i>';
  try { localStorage.setItem('tass_theme', mode); } catch (e) {}
}

function initTheme() {
  let m = 'light';
  try { m = localStorage.getItem('tass_theme') || 'light'; } catch (e) {}
  if (m !== 'dark') m = 'light';
  setTheme(m);
}

function toggleDarkMode() {
  setTheme(isDarkMode ? 'light' : 'dark');
  showToast(isDarkMode ? 'Dark mode on' : 'Light mode on', 'success');
}

/* ── SIDEBAR ── */
function toggleSidebar() {
  sidebarExpanded = !sidebarExpanded;
  document.body.classList.toggle('sidebar-expanded', sidebarExpanded);
  closeAllPanels();
}

/* ── VIEW ROUTER (SPA-style, used by every dashboard) ─── */
function showView(id) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const v = document.getElementById('view-' + id);
  if (v) v.classList.add('active');
  document.querySelectorAll('.nav-item').forEach(btn => {
    if (btn.getAttribute('onclick') && btn.getAttribute('onclick').includes("'" + id + "'")) btn.classList.add('active');
  });
  closeAllPanels();
  if (id === 'analytics' && document.getElementById('view-analytics') && typeof initAnalyticsCharts === 'function') {
    setTimeout(initAnalyticsCharts, 150);
  }
}

function switchTab(id, el) {
  document.querySelectorAll('.tab-pane, .gp-pane, .struct-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn, .gp-tab, .struct-tab').forEach(b => b.classList.remove('active'));
  const pane = document.getElementById('tab-' + id) || document.getElementById(id);
  if (pane) pane.classList.add('active');
  if (el) el.classList.add('active');
}

/* ── MODALS ── */
function openModal(id) { const m = document.getElementById(id); if (m) m.classList.add('open'); }
function closeModal(id) { const m = document.getElementById(id); if (m) m.classList.remove('open'); }

/* ── TOAST ── */
let toastTimer;
function showToast(msg, type) {
  const t = document.getElementById('toast');
  if (!t) return;
  const m = document.getElementById('toastMsg');
  if (m) m.textContent = msg;
  t.className = 'toast ' + (type || 'success');
  const ic = t.querySelector('i');
  if (ic) ic.className = 'fa-solid ' + ((type === 'error') ? 'fa-exclamation-circle' : 'fa-check-circle');
  setTimeout(() => t.classList.add('show'), 10);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 3200);
}

/* ── HEADER PANELS ── */
function toggleNotif() {
  notifOpen = !notifOpen;
  const n = document.getElementById('notifPanel'), p = document.getElementById('profilePanel');
  if (n) n.classList.toggle('open', notifOpen);
  if (p) p.classList.remove('open');
  profileOpen = false;
}

function toggleProfile() {
  profileOpen = !profileOpen;
  const p = document.getElementById('profilePanel'), n = document.getElementById('notifPanel');
  if (p) p.classList.toggle('open', profileOpen);
  if (n) n.classList.remove('open');
  notifOpen = false;
}

function closeAllPanels() {
  notifOpen = false; profileOpen = false;
  const n = document.getElementById('notifPanel'), p = document.getElementById('profilePanel');
  if (n) n.classList.remove('open');
  if (p) p.classList.remove('open');
}

function markAllRead() {
  document.querySelectorAll('.notif-unread').forEach(u => u.classList.remove('notif-unread'));
  showToast('All read', 'success');
  closeAllPanels();
}

/* ── LOGOUT (real) ─────────────────────────────────────── */
function confirmLogout() {
  const m = document.getElementById('confirmModal');
  const icon = document.getElementById('confirmIcon'), title = document.getElementById('confirmTitle'),
        msg = document.getElementById('confirmMsg'), action = document.getElementById('confirmAction');
  if (m && icon && title && msg && action) {
    icon.textContent = '\u{1F44B}';
    title.textContent = 'Log Out?';
    msg.textContent = 'Are you sure you want to log out?';
    action.textContent = 'Log Out';
    action.className = 'btn btn-danger';
    action.onclick = function () {
      const f = document.getElementById('logoutForm');
      if (f) { f.submit(); }
      else if (window.LOGOUT_URL) { window.location.href = window.LOGOUT_URL; }
      else if (location.pathname.indexOf('/accounts/logout/') !== -1) { location.href = location.pathname; }
      else { location.href = '/accounts/logout/'; }
    };
    openModal('confirmModal');
    closeAllPanels();
    return;
  }
  if (window.LOGOUT_URL) window.location.href = window.LOGOUT_URL;
  else window.location.href = '/accounts/logout/';
}

/* ── UTILITIES ── */
function getCookie(name) {
  const match = document.cookie.match(new RegExp('(^|;\\s*)' + name + '=([^;]*)'));
  return match ? decodeURIComponent(match[2]) : null;
}

function e(s) {
  return String(s || '').replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
const escHtml = e;

/* ── GLOBAL CLICK HANDLERS ── */
document.addEventListener('click', function (ev) {
  if (notifOpen && !ev.target.closest('.icon-btn') && !ev.target.closest('#notifPanel')) {
    notifOpen = false; document.getElementById('notifPanel').classList.remove('open');
  }
  if (profileOpen && !ev.target.closest('.profile-btn') && !ev.target.closest('#profilePanel')) {
    profileOpen = false; document.getElementById('profilePanel').classList.remove('open');
  }
  document.querySelectorAll('.modal-overlay').forEach(function (m) {
    if (ev.target === m) m.classList.remove('open');
  });
});

document.addEventListener('DOMContentLoaded', initTheme);

/* ── URL ROUTER (clean paths + Back/Forward for SPA dashboards) ──
   Wraps the page's own showView/switchTab so every navigation updates the
   browser URL via history.pushState (no #). Restores state on load/popstate. */
(function () {
  if (!document.querySelector) return;
  if (!document.querySelector('body .view')) {
    document.addEventListener('DOMContentLoaded', function () { bind(); });
  } else { bind(); }
  function bind() {
    var suppress = false;
    var TAB_SLUGS = {
      'tla': 'teaching-and-learning',
      'stla': 'teaching-and-learning',
      'sannouncements': 'announcements',
      'sassessment': 'assessment',
      'sattendance': 'attendance',
      'smessages': 'messages'
    };
    var VIEW_SLUGS = {
      'activitylog': 'activity-log',
      'assessmentrepo': 'assessment-repository',
      'resourcelibrary': 'resource-library',
      'systemhealth': 'system-health',
      'acadyears': 'academic-years'
    };
    function buildSlugMaps(map) {
      var toSlug = {}, bySlug = {};
      Object.keys(map).forEach(function (id) {
        var slug = map[id];
        toSlug[id] = slug;
        (bySlug[slug] = bySlug[slug] || []).push(id);
      });
      return { toSlug: toSlug, bySlug: bySlug };
    }
    var T = buildSlugMaps(TAB_SLUGS);
    var V = buildSlugMaps(VIEW_SLUGS);
    function resolveSegment(seg, bySlug, prefix) {
      if (seg && document.getElementById(prefix + seg)) return seg;
      var ids = bySlug[seg];
      if (ids) { for (var i = 0; i < ids.length; i++) if (document.getElementById(prefix + ids[i])) return ids[i]; return ids[0]; }
      return null;
    }
    function pathParts() { return (location.pathname || '/').split('/').filter(Boolean); }
    function routeInfo() {
      var parts = pathParts(), n = parts.length, view = null, tabId = null, vIdx = -1;
      if (n >= 1) { var v = resolveSegment(parts[n - 1], V.bySlug, 'view-'); if (v) { view = v; vIdx = n - 1; } }
      if (!view && n >= 2) {
        var v2 = resolveSegment(parts[n - 2], V.bySlug, 'view-');
        var t2 = resolveSegment(parts[n - 1], T.bySlug, 'tab-');
        if (v2 && t2) { view = v2; tabId = t2; vIdx = n - 2; }
      }
      if (!view) return null;
      return { view: view, tabId: tabId, base: parts.slice(0, vIdx).join('/') };
    }
    function writePath(view, sub) {
      if (!view) return;
      var r = routeInfo(), base = r ? r.base : pathParts().join('/');
      var vs = V.toSlug[view] || view, ss = sub ? (T.toSlug[sub] || sub) : null;
      var path;
      if (view === 'dashboard') path = '/' + base + '/';
      else path = '/' + base + '/' + vs + (ss ? '/' + ss : '') + '/';
      path = path.replace(/\/{2,}/g, '/');
      if (location.pathname !== path) { try { history.pushState(null, '', path); } catch (e) {} }
    }
    function findTabButton(id) {
      var nodes = document.querySelectorAll('[onclick]');
      for (var i = 0; i < nodes.length; i++) {
        var oc = nodes[i].getAttribute('onclick') || '';
        if (oc.indexOf('switchTab') !== -1 && oc.indexOf(id) !== -1) return nodes[i];
      }
      return null;
    }
    function activeView() { var a = document.querySelector('.view.active'); return a ? a.id.replace(/^view-/, '') : null; }
    function applyRoute() {
      var r = routeInfo();
      if (!r || typeof window.showView !== 'function') return;
      var active = document.querySelector('.view.active');
      if (!active || active.id !== 'view-' + r.view) {
        suppress = true;
        try { window.showView(r.view); } finally { suppress = false; }
      }
      if (r.tabId && document.getElementById('tab-' + r.tabId)) {
        suppress = true;
        try {
          if (typeof window.switchTab === 'function') window.switchTab(r.tabId, findTabButton(r.tabId) || document.getElementById('tab-' + r.tabId));
          else { var pane = document.getElementById('tab-' + r.tabId); if (pane) pane.classList.add('active'); var btn = findTabButton(r.tabId); if (btn) btn.classList.add('active'); }
        } finally { suppress = false; }
      }
    }
    function normalize() {
      var parts = pathParts();
      if (parts.length > 2 && !routeInfo()) {
        try { history.replaceState(null, '', '/' + parts.slice(0, 2).join('/') + '/'); } catch (e) {}
      }
    }
    function wrapRouter() {
      var origView = window.showView;
      if (typeof origView === 'function') {
        window.showView = function (id) { origView(id); if (!suppress) writePath(activeView()); };
      }
      var origTab = window.switchTab;
      if (typeof origTab === 'function') {
        window.switchTab = function (id, el) { origTab(id, el); if (!suppress) writePath(activeView(), id); };
      }
    }
    if (!document.querySelector('.view')) return;
    wrapRouter();
    applyRoute();
    normalize();
    window.addEventListener('popstate', applyRoute);
  }
})();