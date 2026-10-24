/*
 * TassPoll - safety-net auto-refresh for every dashboard.
 *
 * WebSockets keep screens current instantly while they are open, but a
 * socket can be down (no Redis), a laptop sleeps, or a tab comes back from
 * the background long after the connection dropped. TassPoll makes sure the
 * screen still shows what everyone else changed, with no page reload.
 *
 *   * Every INTERVAL ms (20s) it calls `window.onTassPollRefresh` once.
 *   * It also fires on `focus` and on becoming visible again, so a tab that
 *     was backgrounded catches up the moment it is looked at.
 *   * It never fires while the user is typing or is editing inside a modal
 *     (`textarea`, text input, contenteditable, or `.modal-overlay.open`),
 *     so an in-progress answer, announcement or form edit is never wiped by
 *     a background repaint.
 *   * A dashboard that defines no `onTassPollRefresh` is simply never
 *     touched; this can never break a page. The refresh hook should do its
 *     own change-detection and re-render only what actually changed.
 */
(function (global) {
  "use strict";

  var INTERVAL = 20000;
  var timer = null;
  var busy = false;

  function editingSurface() {
    var el = document.activeElement;
    if (el && (el.tagName === "TEXTAREA" || el.tagName === "INPUT" || (el.isContentEditable && el !== document.body))) {
      return true;
    }
    var open = document.querySelector(".modal-overlay.open, [data-tass-edit='1']");
    return !!open;
  }

  function tick() {
    if (busy) return;
    if (!document || document.hidden) return;
    if (editingSurface()) return;
    if (typeof global.onTassPollRefresh !== "function") return;
    busy = true;
    try {
      var out = global.onTassPollRefresh();
      if (out && typeof out.finally === "function") {
        out.finally(function () { busy = false; });
      } else {
        busy = false;
      }
    } catch (err) {
      busy = false;
      if (global.console) console.debug("[TassPoll] refresh failed", err);
    }
  }

  function start() {
    if (timer) return;
    timer = setInterval(tick, INTERVAL);
    global.addEventListener("focus", function () { setTimeout(tick, 0); });
    if (document.addEventListener) {
      document.addEventListener("visibilitychange", function () {
        if (!document.hidden) setTimeout(tick, 0);
      });
    }
  }

  var TassPoll = {
    start: start,
    stop: function () {
      if (timer) clearInterval(timer);
      timer = null;
    },
    setInterval: function (ms) {
      INTERVAL = Math.max(2000, parseInt(ms, 10) || 2000);
      if (timer) {
        clearInterval(timer);
        timer = null;
        start();
      }
    },
    tickNow: tick,
    isBusy: function () { return busy; }
  };

  global.TassPoll = TassPoll;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})(typeof window !== "undefined" ? window : this);