/**
 * TASS LMS — localStorage → PostgreSQL write bridge.
 *
 * The dashboards persist every save to localStorage. This poller watches the
 * keys listed in window.__TASS_BRIDGE__.watch and, whenever one of them
 * changes, POSTs the equivalent payload to the /api/* endpoints so the same
 * data lands in PostgreSQL too. It is intentionally non-invasive: the SPA's
 * own localStorage persistence still runs, and if the server is unreachable
 * the page continues to work exactly as before.
 *
 * Config shape (set on window before this file loads):
 *   window.__TASS_BRIDGE__ = {
 *     interval: 4000,
 *     watch: [
 *       {
 *         key:      'localStorage key to watch',
 *         endpoint: 'accounts/',            // path under /api/
 *         map:      function(value){ ... }  // returns a POST payload,
 *                                           // OR returns an array of payloads
 *                                           // when `each:true`.
 *         each:     true,                   // post each array item separately
 *         guard:    function(value){...}    // optional; skip when falsy/throws
 *       }
 *     ]
 *   };
 */
(function () {
  'use strict';
  var cfg = window.__TASS_BRIDGE__ || { watch: [] };
  var watch = Array.isArray(cfg.watch) ? cfg.watch : [];
  var last = {};
  var busy = false;
  var timer = null;

  function getCookie(name) {
    try {
      var m = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
      return m ? decodeURIComponent(m.pop()) : '';
    } catch (e) { return ''; }
  }

  function ensureCsrf(done) {
    if (getCookie('csrftoken')) { done(); return; }
    var x = new XMLHttpRequest();
    x.open('GET', '/api/csrf/', true);
    x.onreadystatechange = function () { if (x.readyState === 4) done(); };
    x.send();
  }

  function post(path, payload) {
    return new Promise(function (resolve) {
      ensureCsrf(function () {
        var x = new XMLHttpRequest();
        x.open('POST', '/api/' + path, true);
        x.setRequestHeader('Content-Type', 'application/json');
        x.setRequestHeader('X-CSRFToken', getCookie('csrftoken'));
        x.onreadystatechange = function () {
          if (x.readyState !== 4) return;
          var out = { ok: false };
          try { out = JSON.parse(x.responseText || '{}'); } catch (e) {}
          resolve(out);
        };
        try { x.send(JSON.stringify(payload)); } catch (e) { resolve({ ok: false }); }
      });
    });
  }

  function snapshot(key) {
    try {
      var raw = localStorage.getItem(key);
      return raw === null ? null : JSON.stringify(JSON.parse(raw));
    } catch (e) { return null; }
  }

  function isFreshChange(key) {
    var s = snapshot(key);
    if (s === last[key]) return false;
    last[key] = s;
    return s !== null;
  }

  function payloadsFor(rule, value) {
    if (rule.guard) {
      try { if (!rule.guard(value)) return []; } catch (e) { return []; }
    }
    var payload = rule.map(value);
    if (rule.each && Array.isArray(payload)) return payload;
    return [payload];
  }

  function route(rule) {
    var value = null;
    try { value = JSON.parse(localStorage.getItem(rule.key) || 'null'); } catch (e) { value = null; }
    if (value === null || value === undefined) return Promise.resolve();
    var list = payloadsFor(rule, value);
    var chain = Promise.resolve();
    list.forEach(function (p) {
      if (p === undefined || p === null) return;
      chain = chain.then(function () { return post(rule.endpoint, p); });
    });
    return chain;
  }

  function tick() {
    if (timer) { clearTimeout(timer); timer = null; }
    if (busy) { schedule(); return; }
    var dirty = watch.filter(function (r) { return isFreshChange(r.key); });
    if (!dirty.length) { schedule(); return; }
    busy = true;
    var chain = Promise.resolve();
    dirty.forEach(function (r) {
      chain = chain.then(function () { return route(r); });
    });
    chain.then(function () { busy = false; schedule(); })
          .catch(function () { busy = false; schedule(); });
  }

  function schedule() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(tick, cfg.interval || 4000);
  }

  schedule();
})();