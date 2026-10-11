/**
 * SVI LMS — "My Profile" persistence bridge.
 *
 * Each dashboard's My Profile modal is a client-side localStorage mock. This
 * script wraps the existing changeProfilePhoto()/saveProfile() globals so that,
 * once the local save succeeds, the same name/email/contact + avatar image are
 * POSTed to /api/profile/ and stored in PostgreSQL (avatars land in R2 via the
 * configured default storage). On success the header/panel photo is refreshed
 * with the stored URL.
 */
(function () {
  'use strict';

  function csrf() {
    var m = document.cookie.match(/csrftoken=([^;]+)/);
    return m ? m[1] : '';
  }

  function ensureCsrf(cb) {
    if (csrf()) { cb(); return; }
    var x = new XMLHttpRequest();
    x.open('GET', '/api/csrf/', true);
    x.onreadystatechange = function () { if (x.readyState === 4) cb(); };
    x.send();
  }

  var origPhoto = window.changeProfilePhoto;
  if (typeof origPhoto === 'function') {
    window.changeProfilePhoto = function (inp) {
      var f = inp && inp.files && inp.files[0];
      if (f) window.__tassAvatarFile = f;
      return origPhoto.apply(this, arguments);
    };
  }

  var origSave = window.saveProfile;
  if (typeof origSave !== 'function') return;

  window.saveProfile = function () {
    origSave.apply(this, arguments);
    var st = document.getElementById('profileStatus');
    if (!st || st.style.display !== 'block') return;

    var g = function (id) { var el = document.getElementById(id); return el ? el.value.trim() : ''; };
    var fd = new FormData();
    fd.append('name', g('profileName'));
    fd.append('email', g('profileEmail'));
    fd.append('contact', g('profileContact'));
    if (window.__tassAvatarFile) fd.append('avatar', window.__tassAvatarFile);

    ensureCsrf(function () {
      fetch('/api/profile/', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'X-CSRFToken': csrf() },
        body: fd
      }).then(function (r) { return r.json(); }).then(function (d) {
        if (!d || !d.ok) return;
        window.__tassAvatarFile = null;
        if (d.avatar) {
          try { if (typeof profileAvatarData !== 'undefined') profileAvatarData = d.avatar; } catch (e) {}
          if (typeof applyHeaderPhoto === 'function') applyHeaderPhoto(d.avatar);
          try {
            var key = (typeof PROFILE_KEY !== 'undefined') ? PROFILE_KEY : null;
            if (key) {
              var rec = JSON.parse(localStorage.getItem(key) || '{}');
              rec.avatar = d.avatar;
              localStorage.setItem(key, JSON.stringify(rec));
            }
          } catch (e) {}
        }
      }).catch(function () {});
    });
  };
})();
