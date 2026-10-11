/**
 * SVI LMS — Pilot Guard.
 *
 * Cross-dashboard safety net for the pilot test:
 *   - Global error / unhandled-promise trapping with a non-blocking toast.
 *   - Safe fetch helper (window.tassFetch) that surfaces HTTP failures.
 *   - Responsive hardening: prevents accidental horizontal page overflow and
 *     keeps wide tables scrollable on phones/tablets (Firefox & Edge included).
 *   - Graceful image fallback so a broken cover photo never breaks a card.
 *
 * Loaded last on every dashboard; designed to be dependency-free and to fail
 * silently if a dashboard does not expose the helpers it tries to use.
 */
(function () {
  'use strict';

  function toast(msg) {
    try {
      if (typeof window.showToast === 'function') {
        window.showToast(msg, 'error');
      }
    } catch (e) { /* ignore */ }
  }

  function report(where, detail) {
    try { console.error('[TASS]', where, detail || ''); } catch (e) {}
  }

  window.addEventListener('error', function (ev) {
    report('error', ev && (ev.message || ev.error));
    toast('Something went wrong. Please try again.');
  });

  window.addEventListener('unhandledrejection', function (ev) {
    var reason = ev && ev.reason;
    report('promise', reason && (reason.message || reason));
    toast('A network request failed. Please try again.');
  });

  window.tassFetch = function (url, opts) {
    opts = opts || {};
    return fetch(url, opts).then(function (r) {
      if (!r.ok) { throw new Error('Request failed (' + r.status + ')'); }
      return r;
    });
  };

  function harden() {
    try {
      document.documentElement.style.overflowX = 'hidden';

      var style = document.createElement('style');
      style.textContent =
        'img{max-width:100%;height:auto;}' +
        'table{max-width:100%;}' +
        '.tass-scroll-x{overflow-x:auto;-webkit-overflow-scrolling:touch;}' +
        '@media(max-width:768px){' +
        '  .modal-box,.modal-content{max-width:96vw !important;margin:8px auto !important;}' +
        '  .modal-overlay{padding:8px !important;}' +
        '  button,.btn,.nav-item{min-height:40px;}' +
        '}';
      document.head.appendChild(style);

      var tables = document.querySelectorAll('table');
      for (var i = 0; i < tables.length; i++) {
        var t = tables[i];
        var parent = t.parentElement;
        if (parent && parent.classList && parent.classList.contains('tass-scroll-x')) { continue; }
        if (parent && parent.scrollWidth > parent.clientWidth + 4) {
          parent.classList.add('tass-scroll-x');
        }
      }
    } catch (e) {
      report('harden', e);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', harden);
  } else {
    harden();
  }

  // Graceful image fallback so a broken image never leaves a broken-image icon.
  //
  // This listener runs in the CAPTURE phase, i.e. before the element's own
  // onerror handler. Hiding the image unconditionally therefore defeated every
  // inline fallback (e.g. the profile button swapping in
  // profile-placeholder.jpg): the fallback loaded, but the element stayed
  // `visibility:hidden` forever. Skip any image that still has a live onerror
  // handler — if that fallback also fails, onerror has been nulled by then and
  // this branch picks the image up on the second error.
  document.addEventListener('error', function (ev) {
    var el = ev.target;
    if (!el || el.tagName !== 'IMG' || el.dataset.tassFallback) { return; }
    if (typeof el.onerror === 'function') { return; }
    el.dataset.tassFallback = '1';
    el.style.visibility = 'hidden';
  }, true);
})();
