/**
 * SVI LMS — shared empty-state renderer.
 *
 * Dashboards used to ship placeholder numbers (invented averages, fabricated
 * attendance, demo assessment rows) so every chart had something to draw. That
 * data was indistinguishable from real data and, on the registrar dashboard,
 * was actually persisted to PostgreSQL. All of it has been removed.
 *
 * A widget now has exactly two legal states:
 *   1. real data, supplied by the backend bridge (apps/dashboards/context.py), or
 *   2. an explicit empty state rendered by this file.
 *
 * The markup is deliberately preserved — the canvas, the <tbody>, the stat card
 * all stay exactly where they are, so layout and styling are untouched. Only the
 * contents change. That also means wiring real data back up later is a one-line
 * change: call the normal render path instead of TASS.empty.*.
 *
 * ── Wiring registry ────────────────────────────────────────────────────────
 * Every call records what that slot will need from the database:
 *
 *   window.TASS.wire = {
 *     page: 'principal',        // set by each page as window.TASS.page
 *     slots: { charts: {...}, tables: {...}, stats: {...}, lists: {...} }
 *   }
 *
 * (This is distinct from window.__TASS_BRIDGE__, the localStorage-to-PostgreSQL
 * writer configured by api_bridge.js. That one is not a data source.)
 *
 * Call TASS.empty.report() in the console to dump it. This is the checklist for
 * the final database field list — nothing is left unwired by accident.
 */
window.TASS = window.TASS || {};

(function (T) {
  'use strict';

  var DEFAULT_MSG = 'No data yet.';
  var DEFAULT_HINT = '';
  var DASH = '\u2014'; // em dash, shown instead of a fabricated 0

  function el(id) { return typeof id === 'string' ? document.getElementById(id) : id; }

  function resolveList(target, root) {
    if (!target) return [];
    if (typeof target === 'string') {
      return Array.prototype.slice.call((root || document).querySelectorAll(target));
    }
    if (target.nodeType === 1) return [target];
    return Array.prototype.slice.call(target);
  }

  function note(bucket, name, source) {
    if (!name) return;
    var reg = T.wire || (T.wire = { page: T.page || '', slots: {} });
    reg.slots[bucket] = reg.slots[bucket] || {};
    reg.slots[bucket][name] = source || '';
  }

  function shell(kind, msg, hint) {
    var html = '<i class="fa-solid fa-chart-simple"></i>' +
               '<span class="tass-empty-msg">' + msg + '</span>';
    if (hint) html += '<span class="tass-empty-hint">' + hint + '</span>';
    return '<div class="tass-empty ' + kind + '">' + html + '</div>';
  }

  T.empty = {
    message: DEFAULT_MSG,
    dash: DASH,

    /**
     * Blank a chart slot. Keeps the <canvas> (hidden, full height) and shows a
     * message instead. Any live Chart.js instance must be destroyed first, or
     * use TASS.empty.chart with { destroy: fn }.
     *
     *   TASS.empty.chart('dashAttendChart', { source: 'school.attendance.by_week' });
     */
    chart: function (id, opts) {
      opts = opts || {};
      var canvas = el(id);
      if (!canvas) return;

      if (opts.destroy) { try { opts.destroy(); } catch (e) {} }
      else if (canvas.__tassChart && canvas.__tassChart.destroy) {
        canvas.__tassChart.destroy();
        canvas.__tassChart = null;
      }

      canvas.classList.add('tass-empty-canvas');

      var wrap = canvas.parentNode;
      if (!wrap) return;
      var existing = wrap.querySelector(':scope > .tass-empty');
      if (existing) existing.parentNode.removeChild(existing);
      wrap.insertAdjacentHTML('beforeend',
        shell('', opts.message || DEFAULT_MSG, opts.hint || DEFAULT_HINT));

      note('charts', id, opts.source || '');
    },

    /** Undo TASS.empty.chart — used when real data arrives. */
    fillChart: function (id) {
      var canvas = el(id);
      if (!canvas) return;
      canvas.classList.remove('tass-empty-canvas');
      var wrap = canvas.parentNode;
      var existing = wrap && wrap.querySelector(':scope > .tass-empty');
      if (existing) existing.parentNode.removeChild(existing);
    },

    /**
     * Blank a table body. colSpan should match the real column count so the
     * layout does not shift.
     *
     *   TASS.empty.table('#view-assessments .data-table tbody', 8,
     *                    { source: 'assessments.list' });
     */
    table: function (target, colSpan, opts) {
      opts = opts || {};
      var rows = resolveList(target);
      rows.forEach(function (body) {
        body.innerHTML = '<tr class="tass-empty-row"><td colspan="' +
          (colSpan || 1) + '">' + (opts.message || DEFAULT_MSG) +
          (opts.hint ? ' <span style="opacity:.7">(' + opts.hint + ')</span>' : '') +
          '</td></tr>';
      });
      if (typeof target === 'string') note('tables', target, opts.source || '');
    },

    /**
     * Replace stat-card values with a dash. Never pass a fallback number here —
     * a dash means "not measured", a number means "measured and it is this".
     *
     *   TASS.empty.stats('#view-attendance .stat-val', { source: 'attendance.summary' });
     */
    stats: function (target, opts) {
      opts = opts || {};
      var nodes = resolveList(target);
      nodes.forEach(function (n) {
        if (!n) return;
        n.textContent = opts.message || DASH;
        n.classList.add('tass-empty-val');
      });
      if (typeof target === 'string') note('stats', target, opts.source || '');
    },

    /** Blank a non-table container (notification panel, feed, card list). */
    list: function (target, opts) {
      opts = opts || {};
      var nodes = resolveList(target);
      nodes.forEach(function (n) {
        if (!n) return;
        n.innerHTML = shell('', opts.message || DEFAULT_MSG, opts.hint || DEFAULT_HINT);
      });
      if (typeof target === 'string') note('lists', target, opts.source || '');
    },

    /**
     * Clear a <select> of hardcoded options down to a single disabled
     * placeholder, so filter dropdowns cannot offer classes that do not exist.
     *
     *   TASS.empty.select('analyticsClassFilter', 'No classes available');
     */
    select: function (id, label, opts) {
      opts = opts || {};
      var node = el(id);
      if (!node) return;
      node.innerHTML = '<option value="">' + (label || 'No options') + '</option>';
      node.disabled = true;
      if (!opts.keepValue) node.value = '';
      note('selects', id, opts.source || '');
    },

    /** True when a collection actually has rows — the guard before rendering. */
    has: function (v) {
      if (v == null) return false;
      if (Array.isArray(v)) return v.length > 0;
      if (typeof v === 'object') return Object.keys(v).length > 0;
      return !!v;
    },

    /** Dump the wiring checklist to the console. */
    report: function () {
      var reg = T.wire;
      if (!reg || !reg.slots) { console.log('[TASS] no unwired slots on this page.'); return reg; }
      var total = 0;
      Object.keys(reg.slots).forEach(function (bucket) {
        var rows = Object.keys(reg.slots[bucket]);
        total += rows.length;
        console.groupCollapsed('[TASS] ' + bucket + ' (' + rows.length + ')');
        console.table(reg.slots[bucket]);
        console.groupEnd();
      });
      console.log('[TASS] ' + (reg.page || 'page') + ': ' + total + ' slot(s) awaiting database fields.');
      return reg;
    }
  };

  T.page = T.page || (document.body && document.body.getAttribute('data-tass-page')) || '';
  T.wire = T.wire || { page: T.page, slots: {} };
})(window.TASS);
