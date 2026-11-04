/**
 * SVI LMS profile editor persistence.
 */
(function () {
  'use strict';

  function csrf() {
    var m = document.cookie.match(/csrftoken=([^;]+)/);
    return m ? m[1] : (window.csrfToken || '');
  }

  function ensureCsrf() {
    if (csrf()) return Promise.resolve(csrf());
    return fetch('/api/csrf/', { credentials: 'same-origin' }).then(function () {
      return csrf();
    });
  }

  var origPhoto = window.changeProfilePhoto;
  if (typeof origPhoto === 'function') {
    window.changeProfilePhoto = function (input) {
      var file = input && input.files && input.files[0];
      if (file) window.__tassAvatarFile = file;
      return origPhoto.apply(this, arguments);
    };
  }

  var origSave = window.saveProfile;
  if (typeof origSave !== 'function' || origSave.__tassServerManaged) return;
  var saving = false;

  window.saveProfile = function () {
    var saveArgs = Array.prototype.slice.call(arguments);
    if (saving) return;
    var root = document.getElementById('profileName');
    var modal = root && root.closest('.modal-overlay');
    if (typeof window.tassValidate === 'function' && !window.tassValidate(modal)) return;

    var value = function (id) {
      var el = document.getElementById(id);
      return el ? String(el.value || '').trim() : '';
    };
    var name = value('profileName');
    var email = value('profileEmail');
    var contact = value('profileContact');
    var fileInput = document.getElementById('profilePhoto');
    var avatarFile = window.__tassAvatarFile || (fileInput && fileInput.files && fileInput.files[0]);
    var fd = new FormData();
    fd.append('name', name);
    fd.append('email', email);
    fd.append('contact', contact);
    if (avatarFile) fd.append('avatar', avatarFile);

    var status = document.getElementById('profileStatus');
    var saveButton = modal && modal.querySelector('.modal-footer button:not(.profile-edit-btn)');
    saving = true;
    if (saveButton) saveButton.disabled = true;
    if (status) {
      status.style.display = 'block';
      status.style.color = 'var(--text-muted)';
      status.textContent = 'Saving profile...';
    }

    ensureCsrf().then(function (token) {
      return fetch('/api/profile/', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'X-CSRFToken': token },
        body: fd
      });
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok || !data || !data.ok) {
          throw new Error((data && data.message) || 'Could not save your profile. Please try again.');
        }
        if (avatarFile && !data.avatar) {
          throw new Error('The server did not confirm the profile photo. Please try again.');
        }
        if (data.avatar) {
          try { if (typeof profileAvatarData !== 'undefined') profileAvatarData = data.avatar; } catch (e) {}
          if (typeof window.applyHeaderPhoto === 'function') window.applyHeaderPhoto(data.avatar);
          window.__TASS_PROFILE__ = Object.assign({}, window.__TASS_PROFILE__ || {}, {
            name: name, email: email, contact: contact, avatar: data.avatar
          });
        }
        window.__tassAvatarFile = null;
        if (fileInput) fileInput.value = '';
        origSave.apply(window, saveArgs);
        var key = null;
        try { if (typeof PROFILE_KEY === 'string') key = PROFILE_KEY; } catch (e) {}
        if (!key && typeof window.PROFILE_KEY === 'string') key = window.PROFILE_KEY;
        if (key) {
          try {
            var record = JSON.parse(localStorage.getItem(key) || '{}');
            record.avatar = data.avatar || record.avatar || '';
            record.name = name;
            record.email = email;
            record.contact = contact;
            localStorage.setItem(key, JSON.stringify(record));
          } catch (e) {}
        }
        var view = window.__tassProfileView;
        if (view && typeof view.enterView === 'function') setTimeout(view.enterView, 700);
      });
    }).catch(function (error) {
      if (status) {
        status.style.display = 'block';
        status.style.color = 'var(--danger)';
        status.textContent = error && error.message ? error.message : 'Could not save your profile. Please try again.';
      }
      if (typeof window.showToast === 'function') window.showToast(status ? status.textContent : 'Could not save your profile.', 'error');
    }).then(function () {
      saving = false;
      if (saveButton) saveButton.disabled = false;
    });
  };
})();

/**
 * SVI LMS — "My Profile" view-first controller.
 * Name and email are not editable by users.
 */
(function () {
  'use strict';

  var LOCKED_FIELDS = ['profileName', 'profileEmail'];
  var EDITABLE_FIELDS = ['profileContact'];

  function modalRoot() {
    var el = document.getElementById('profileName');
    return el ? el.closest('.modal-overlay') : null;
  }

  function footer(root) { return root ? root.querySelector('.modal-footer') : null; }

  function buttonMatching(root, re) {
    if (!root) return null;
    var btns = root.querySelectorAll('.modal-footer button');
    for (var i = 0; i < btns.length; i++) {
      if (re.test(btns[i].textContent || '')) return btns[i];
    }
    return null;
  }

  function ensureEditButton(root) {
    var f = footer(root);
    if (!f) return null;
    var btn = f.querySelector('.profile-edit-btn');
    if (!btn) {
      btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn btn-primary profile-edit-btn';
      btn.innerHTML = '<i class="fa-solid fa-pen"></i> Edit Profile';
      btn.addEventListener('click', function () { setEdit(true); });
      f.appendChild(btn);
    }
    return btn;
  }

  function lockField(el) {
    if (!el) return;
    el.readOnly = true;
    el.setAttribute('readonly', 'readonly');
    el.disabled = true;
    el.setAttribute('disabled', 'disabled');
  }

  function setEdit(edit) {
    var root = modalRoot();
    if (!root) return;

    for (var i = 0; i < LOCKED_FIELDS.length; i++) {
      var el = document.getElementById(LOCKED_FIELDS[i]);
      if (!el) continue;
      lockField(el);
      var view = document.getElementById(LOCKED_FIELDS[i] + '_view');
      if (!view) {
        view = document.createElement('div');
        view.id = LOCKED_FIELDS[i] + '_view';
        view.className = 'profile-view-val';
        if (el.parentNode) el.parentNode.insertBefore(view, el.nextSibling);
      }
      view.textContent = (el.value && String(el.value).trim()) ? el.value : '—';
      view.style.display = '';
      el.style.display = 'none';
    }

    for (var j = 0; j < EDITABLE_FIELDS.length; j++) {
      var el2 = document.getElementById(EDITABLE_FIELDS[j]);
      if (!el2) continue;
      var view2 = document.getElementById(EDITABLE_FIELDS[j] + '_view');
      if (!view2) {
        view2 = document.createElement('div');
        view2.id = EDITABLE_FIELDS[j] + '_view';
        view2.className = 'profile-view-val';
        if (el2.parentNode) el2.parentNode.insertBefore(view2, el2.nextSibling);
      }
      view2.textContent = (el2.value && String(el2.value).trim()) ? el2.value : '—';
      view2.style.display = edit ? 'none' : '';
      el2.style.display = edit ? '' : 'none';
      el2.readOnly = !edit;
    }

    var photo = root.querySelector('.profile-photo-btn');
    if (photo) photo.style.display = edit ? '' : 'none';

    var save = buttonMatching(root, /save/i);
    if (save) save.style.display = edit ? '' : 'none';

    var editBtn = ensureEditButton(root);
    if (editBtn) editBtn.style.display = edit ? 'none' : '';

    var cancel = buttonMatching(root, /cancel|close/i);
    if (cancel) cancel.textContent = edit ? 'Cancel' : 'Close';
  }

  window.__tassProfileView = {
    enterView: function () { setEdit(false); },
    enterEdit: function () { setEdit(true); }
  };

  function applyLocks() {
    var root = modalRoot();
    if (root) {
      LOCKED_FIELDS.forEach(function(id) {
        var el = document.getElementById(id);
        if (el) lockField(el);
      });
      setEdit(false);
    }
  }

  function boot() {
    applyLocks();
    
