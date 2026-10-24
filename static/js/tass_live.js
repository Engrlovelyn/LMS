/*
 * TassLive - keeps every dashboard current without a page refresh.
 *
 * One WebSocket per dashboard at /ws/live/. The server pushes a small "something
 * changed" signal (never record contents) and this decides what to re-fetch.
 *
 *   TassLive.on('materials', function (e) { ... });   // one kind
 *   TassLive.on(function (e) { ... });                 // anything
 *
 * `e.detail` is { kinds, classroom_id, scope, actor }.
 *
 * A dashboard declares what "refresh the current screen" means once, and that
 * is normally all the per-dashboard code needed:
 *
 *   TassLive.onRefresh(function () { ... });
 *
 * The generic listener below already wires the common case: a change in the
 * virtual classroom you are looking at, or a school-wide change, re-runs the
 * dashboard's own refresh hook. That is what makes a teacher's upload appear
 * on the enrolled students' screens with no reload.
 */
(function (global) {
  "use strict";

  var RETRY_MIN = 1000;
  var RETRY_MAX = 30000;
  var socket = null;
  var retry = RETRY_MIN;
  var timer = null;
  var closed = false;
  var handlers = [];
  var refreshHook = null;
  var last = null;

  function supportsLive() {
    if (typeof WebSocket === "undefined") return false;
    // file:// has no usable socket; some kiosk/test shells run without a host.
    if (!global.location || !/^https?:$/.test(global.location.protocol)) return false;
    return true;
  }

  function emit(detail) {
    last = detail;
    for (var i = 0; i < handlers.length; i++) {
      try {
        handlers[i](detail);
      } catch (err) {
        if (global.console) console.error("[TassLive] handler failed", err);
      }
    }
    if (typeof refreshHook === "function") {
      try {
        refreshHook(detail);
      } catch (err) {
        if (global.console) console.error("[TassLive] refresh hook failed", err);
      }
    }
  }

  function scheduleReconnect() {
    if (closed || timer) return;
    var wait = retry;
    retry = Math.min(retry * 2, RETRY_MAX);
    timer = setTimeout(function () {
      timer = null;
      connect();
    }, wait + Math.floor(Math.random() * 400));
  }

  function connect() {
    if (!supportsLive() || socket) return;
    var proto = global.location.protocol === "https:" ? "wss:" : "ws:";
    var url = proto + "//" + global.location.host + "/ws/live/";
    try {
      socket = new WebSocket(url);
    } catch (err) {
      socket = null;
      scheduleReconnect();
      return;
    }

    socket.onopen = function () {
      retry = RETRY_MIN;
      document.documentElement.setAttribute("data-tass-live", "on");
    };

    socket.onmessage = function (ev) {
      var data;
      try {
        data = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      if (!data || data.type === "hello" || data.type === "error") return;
      if (data.type !== "live") return;
      emit({
        kinds: data.kinds || ["*"],
        classroom_id: data.classroom_id == null ? null : Number(data.classroom_id),
        scope: data.scope || "classroom",
        actor: data.actor || null,
        at: Date.now()
      });
    };

    socket.onclose = function () {
      socket = null;
      document.documentElement.setAttribute("data-tass-live", "off");
      scheduleReconnect();
    };

    socket.onerror = function () {
      // onclose always follows; reconnect is handled there.
      if (global.console) console.debug("[TassLive] socket error");
    };
  }

  function start() {
    if (closed || !supportsLive()) return;
    connect();
    // A laptop that sleeps reopens the socket far too late to be trusted;
    // re-check on wake so a long-idle tab is never silently stale.
    global.addEventListener("focus", function () {
      if (!socket) {
        retry = RETRY_MIN;
        connect();
      }
    });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && !socket) {
        retry = RETRY_MIN;
        connect();
      }
    });
  }

  var TassLive = {
    // TassLive.on(fn)            -> every event
    // TassLive.on('materials', fn) -> only that kind (or "*")
    on: function (kindOrHandler, maybeHandler) {
      if (typeof kindOrHandler === "function") {
        handlers.push(kindOrHandler);
      } else if (typeof kindOrHandler === "string" && typeof maybeHandler === "function") {
        var kind = kindOrHandler;
        var handler = maybeHandler;
        handlers.push(function (d) {
          if (d.kinds.indexOf("*") >= 0 || d.kinds.indexOf(kind) >= 0) handler(d);
        });
      }
      return TassLive;
    },
    onRefresh: function (fn) {
      refreshHook = fn;
      return TassLive;
    },
    start: start,
    stop: function () {
      closed = true;
      if (timer) clearTimeout(timer);
      if (socket) socket.close();
      socket = null;
    },
    isConnected: function () {
      return !!socket && socket.readyState === 1;
    },
    lastEvent: function () {
      return last;
    },
    // Exposed so the generic hook can ask "is this event about my screen?"
    affects: function (classroomId) {
      if (!last) return false;
      if (last.scope === "school") return true;
      if (classroomId == null) return false;
      return String(last.classroom_id) === String(classroomId);
    }
  };

  global.TassLive = TassLive;

  /*
   * Default behaviour, applied to every dashboard.
   *
   * The dashboard exposes the id of the virtual classroom it currently has
   * open (teacher.html: currentVCId, student.html: currentSVCId). When a change
   * arrives for that classroom - or school-wide - the dashboard's own
   * onTassLiveRefresh() runs, which is where it re-renders the visible panels.
   * A dashboard that defines no hook simply does not auto-refresh; it still
   * works normally, so this can never break a page.
   */
  function autoWire() {
    function currentClassroomId() {
      if (typeof currentVCId !== "undefined" && currentVCId != null) return currentVCId;
      if (typeof currentSVCId !== "undefined" && currentSVCId != null) return currentSVCId;
      return null;
    }
    function relevant(d) {
      if (d.scope === "school") return true;
      var id = currentClassroomId();
      // With a classroom open, only that classroom's changes repaint the screen.
      // With none open the event is still handed over: the hook knows whether it
      // has a list or summary to update, and ignoring it here would mean a
      // change never lands on a dashboard that is showing "all my classrooms".
      if (id == null) return true;
      return String(d.classroom_id) === String(id);
    }

    // Debounced: a bulk publish can fire many events; re-render once.
    var pending = null;
    TassLive.on(function (d) {
      if (!relevant(d)) return;
      if (pending) clearTimeout(pending);
      pending = setTimeout(function () {
        pending = null;
        var fn =
          global.onTassLiveRefresh ||
          global.onTassPollRefresh ||
          (typeof refreshActiveView === "function" ? refreshActiveView : null);
        var active = document.activeElement;
        if (active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)) return;
        if (document.querySelector(".modal-overlay.open, [data-tass-edit='1']")) return;
        if (typeof fn === "function") {
          try {
            fn(d);
          } catch (err) {
            if (global.console) console.error("[TassLive] refresh failed", err);
          }
        }
      }, 220);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      autoWire();
      start();
    });
  } else {
    autoWire();
    start();
  }
})(typeof window !== "undefined" ? window : this);
