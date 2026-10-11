/**
 * SVI LMS — shared school-activity calendar.
 *
 * A real month calendar: weekday header row, six week rows, today highlighted,
 * events drawn as solid category-coloured bars that continue across the days
 * they span. Click any day to see its agenda; the System Administrator and the
 * Principal also get add / edit / delete.
 *
 * Usage inside a dashboard view:
 *
 *   <div id="calRoot"></div>
 *   TASS.calendar.mount('calRoot', { data: payload.calendar });
 *
 * The first month arrives inside the page payload so the grid paints with no
 * request; moving to another month fetches /api/calendar/?start=&end=.
 */
(function (global) {
  'use strict';

  var MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
                'July', 'August', 'September', 'October', 'November', 'December'];
  var MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                      'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var DOW_FULL = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  var DOW_SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  var DOW_MIN = ['S', 'M', 'T', 'W', 'T', 'F', 'S'];

  // Keys match SchoolEvent.CATEGORY_CHOICES in apps/communication/models.py.
  var CATEGORY = {
    event:    { color: '#1e5faf', soft: '#e7f0fb', label: 'School Event' },
    academic: { color: '#7c3aed', soft: '#f0e9fe', label: 'Academic' },
    exam:     { color: '#dc2626', soft: '#fdeaea', label: 'Exam' },
    meeting:  { color: '#0891b2', soft: '#e0f5f9', label: 'Meeting' },
    holiday:  { color: '#16a34a', soft: '#e6f6ec', label: 'Holiday' },
    activity: { color: '#ea580c', soft: '#fdeee5', label: 'Activity' }
  };
  function catOf(key) { return CATEGORY[key] || { color: '#64748b', soft: '#eef2f7', label: key || 'Event' }; }

  // ── helpers ───────────────────────────────────────────────────────────────
  function esc(v) {
    if (v === null || v === undefined) return '';
    return String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function iso(y, m, d) { return y + '-' + pad(m + 1) + '-' + pad(d); }

  // Parse YYYY-MM-DD locally so the browser never shifts the day.
  function parseISO(v) {
    if (!v) return null;
    var p = String(v).slice(0, 10).split('-');
    if (p.length !== 3) return null;
    var d = new Date(+p[0], +p[1] - 1, +p[2]);
    return isNaN(d.getTime()) ? null : d;
  }
  function sameDay(a, b) {
    return !!a && !!b && a.getFullYear() === b.getFullYear() &&
           a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }
  function dayNumber(a, b) {
    return Math.round((Date.UTC(b.getFullYear(), b.getMonth(), b.getDate()) -
                       Date.UTC(a.getFullYear(), a.getMonth(), a.getDate())) / 86400000);
  }
  function timeLabel(ev) {
    if (ev.allDay || !ev.startTime) return '';
    return ev.startTime + (ev.endTime ? ' – ' + ev.endTime : '');
  }

  // ── the calendar ──────────────────────────────────────────────────────────
  function Calendar(root, opts) {
    this.root = typeof root === 'string' ? document.getElementById(root) : root;
    if (!this.root) return;
    this.opts = opts || {};

    var boot = this.opts.data || {};
    this.canEdit = !!boot.canEdit;
    this.today = parseISO(boot.today) || new Date();

    this.categories = (boot.categories && boot.categories.length)
      ? boot.categories.map(function (c) {
          var k = c.key || c.value || c;
          var meta = catOf(k);
          return { key: k, label: c.label || meta.label };
        })
      : Object.keys(CATEGORY).map(function (k) { return { key: k, label: CATEGORY[k].label }; });

    var m = String(boot.month || iso(this.today.getFullYear(), this.today.getMonth(), 1).slice(0, 7)).split('-');
    this.year = +m[0];
    this.month = +m[1] - 1;

    this.events = (boot.events || []).slice();
    this.selected = this.today;
    this.editingId = null;
    this.busy = false;

    this.shell();
    this.render();
  }

  Calendar.prototype.api = '/api/calendar/';

  // Index events by every date they cover, flagging where the run starts/ends
  // so the bar can be drawn as one continuous strip.
  Calendar.prototype.index = function () {
    var map = {};
    this.events.forEach(function (ev) {
      var from = parseISO(ev.startDate);
      if (!from) return;
      var to = parseISO(ev.endDate) || from;
      var span = dayNumber(from, to);
      if (span < 0) span = 0;
      if (span > 90) span = 90;
      for (var k = 0; k <= span; k++) {
        var d = new Date(from.getFullYear(), from.getMonth(), from.getDate() + k);
        var key = iso(d.getFullYear(), d.getMonth(), d.getDate());
        (map[key] = map[key] || []).push({
          ev: ev, start: k === 0, end: k === span, alone: span === 0
        });
      }
    });
    return map;
  };

  Calendar.prototype.shell = function () {
    this.root.classList.add('tass-cal');
    this.root.innerHTML =
      '<div class="tcal-top">' +
        '<div class="tcal-month">' +
          '<button class="tcal-nav" type="button" data-act="prev" aria-label="Previous month"><i class="fa-solid fa-chevron-left"></i></button>' +
          '<h2 class="tcal-label" id="tcalLabel"></h2>' +
          '<button class="tcal-nav" type="button" data-act="next" aria-label="Next month"><i class="fa-solid fa-chevron-right"></i></button>' +
          '<button class="tcal-today" type="button" data-act="today">Today</button>' +
        '</div>' +
        '<div class="tcal-legend" id="tcalLegend"></div>' +
      '</div>' +
      '<div class="tcal-frame">' +
        '<div class="tcal-dows" id="tcalDows"></div>' +
        '<div class="tcal-grid" id="tcalGrid"></div>' +
      '</div>' +
      '<div class="tcal-agenda">' +
        '<div class="tcal-agenda-head">' +
          '<div>' +
            '<div class="tcal-agenda-date" id="tcalSelDate"></div>' +
            '<div class="tcal-agenda-sub" id="tcalSelSub"></div>' +
          '</div>' +
          (this.canEdit
            ? '<button class="tcal-add" type="button" data-act="add"><i class="fa-solid fa-plus"></i> Add activity</button>'
            : '<span class="tcal-ro"><i class="fa-solid fa-lock"></i> Read-only</span>') +
        '</div>' +
        '<div class="tcal-agenda-list" id="tcalList"></div>' +
      '</div>' +
      '<div class="tcal-modal" id="tcalModal" hidden>' +
        '<div class="tcal-modal-card" role="dialog" aria-modal="true">' +
          '<div class="tcal-modal-head"><span id="tcalModalTitle">Add activity</span>' +
            '<button class="tcal-x" type="button" data-act="close" aria-label="Close"><i class="fa-solid fa-xmark"></i></button>' +
          '</div>' +
          '<div class="tcal-modal-body" id="tcalModalBody"></div>' +
        '</div>' +
      '</div>';

    this.root.addEventListener('click', this.onClick.bind(this));
  };

  Calendar.prototype.render = function () {
    var label = this.root.querySelector('#tcalLabel');
    if (label) label.textContent = MONTHS[this.month] + ' ' + this.year;

    var dows = this.root.querySelector('#tcalDows');
    if (dows) {
      dows.innerHTML = DOW_SHORT.map(function (d, i) {
        return '<div class="tcal-dow"><span class="full">' + DOW_FULL[i] + '</span>' +
               '<span class="short">' + d + '</span></div>';
      }).join('');
    }

    var lg = this.root.querySelector('#tcalLegend');
    if (lg) {
      lg.innerHTML = this.categories.map(function (c) {
        var m = catOf(c.key);
        return '<span class="tcal-key"><i style="background:' + m.color + '"></i>' + esc(c.label) + '</span>';
      }).join('');
    }

    this.renderGrid();
    this.renderAgenda();
  };

  Calendar.prototype.renderGrid = function () {
    var grid = this.root.querySelector('#tcalGrid');
    if (!grid) return;
    var map = this.index();
    var first = new Date(this.year, this.month, 1);
    var start = new Date(first.getFullYear(), first.getMonth(), 1 - first.getDay());
    // Only draw the weeks the month actually spans. The old fixed six-row loop
    // always appended a mostly-empty strip, which made the card ~100px taller
    // than needed for the eleven months that fit in five weeks.
    var spanDays = first.getDay() + new Date(this.year, this.month + 1, 0).getDate();
    var weeks = Math.ceil(spanDays / 7);

    var html = '';
    for (var w = 0; w < weeks; w++) {
      html += '<div class="tcal-week">';
      for (var i = 0; i < 7; i++) {
        var d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + w * 7 + i);
        var key = iso(d.getFullYear(), d.getMonth(), d.getDate());
        var items = (map[key] || []).sort(function (a, b) {
          if (a.ev.allDay !== b.ev.allDay) return a.ev.allDay ? -1 : 1;
          return (a.ev.startTime || '99:99') < (b.ev.startTime || '99:99') ? -1 : 1;
        });

        var chips = items.map(function (it) {
          var m = catOf(it.ev.category);
          var t = timeLabel(it.ev);
          var cls = 'tcal-bar';
          if (it.start) cls += ' s';
          if (it.end) cls += ' e';
          if (it.alone) cls += ' alone';
          return '<div class="' + cls + '" data-id="' + esc(it.ev.id) + '" ' +
                 'style="--c:' + m.color + ';--cs:' + m.soft + '" ' +
                 'title="' + esc(it.ev.title + (t ? ' · ' + t : '') +
                                 (it.ev.location ? ' · ' + it.ev.location : '')) + '">' +
                 (t ? '<b>' + esc(t) + '</b>' : '') +
                 '<span class="t">' + esc(it.ev.title) + '</span>' +
               '</div>';
        }).join('');

        var extra = items.length > 3
          ? '<button class="tcal-more" type="button" data-act="more" data-date="' + key + '">+' +
            (items.length - 3) + ' more</button>'
          : '';

        // Mark the day itself, not just the bars: a free day and a scheduled day
        // looked identical once the month was scanned at a glance. `has` tints
        // the cell and dots the date with the colour of what is booked that day.
        var firstMeta = items.length ? catOf(items[0].ev.category) : null;

        html += '<div class="tcal-day' +
                (d.getMonth() !== this.month ? ' out' : '') +
                (items.length ? ' has' : '') +
                (sameDay(d, this.today) ? ' today' : '') +
                (sameDay(d, this.selected) ? ' sel' : '') +
                '" data-date="' + key + '"' +
                (firstMeta ? ' style="--dc:' + firstMeta.color + '"' : '') + '>' +
                '<div class="tcal-num">' + d.getDate() + '</div>' +
                '<div class="tcal-bars">' + chips + extra + '</div>' +
              '</div>';
      }
      html += '</div>';
    }
    grid.innerHTML = html;
  };

  Calendar.prototype.renderAgenda = function () {
    var dEl = this.root.querySelector('#tcalSelDate');
    var sEl = this.root.querySelector('#tcalSelSub');
    var lEl = this.root.querySelector('#tcalList');
    if (!lEl) return;

    var key = iso(this.selected.getFullYear(), this.selected.getMonth(), this.selected.getDate());
    var items = (this.index()[key] || []);

    if (dEl) {
      dEl.textContent = DOW_FULL[this.selected.getDay()] + ', ' +
        MONTHS_SHORT[this.selected.getMonth()] + ' ' + this.selected.getDate() + ', ' + this.selected.getFullYear();
    }
    if (sEl) {
      sEl.textContent = items.length === 0
        ? 'Nothing scheduled'
        : items.length + (items.length === 1 ? ' activity' : ' activities');
    }

    if (!items.length) {
      lEl.innerHTML = '<div class="tcal-empty">' +
        '<i class="fa-regular fa-calendar"></i>' +
        '<div>No activities scheduled for this day.</div>' +
        (this.canEdit ? '<button class="tcal-link" type="button" data-act="add">Add one</button>' : '') +
        '</div>';
      return;
    }

    var self = this;
    lEl.innerHTML = items.map(function (it) {
      var ev = it.ev, m = catOf(ev.category), t = timeLabel(ev);
      return '<div class="tcal-item" data-id="' + esc(ev.id) + '" style="--c:' + m.color + ';--cs:' + m.soft + '">' +
        '<div class="tcal-item-hd">' +
          '<i class="fa-solid fa-circle" style="color:' + m.color + '"></i>' +
          '<span class="tcal-item-title">' + esc(ev.title) + '</span>' +
          '<span class="tcal-chip">' + esc(ev.categoryLabel || m.label) + '</span>' +
        '</div>' +
        '<div class="tcal-item-meta">' +
          (t ? '<span><i class="fa-regular fa-clock"></i> ' + esc(t) + '</span>' : '<span><i class="fa-solid fa-sun"></i> All day</span>') +
          (ev.spansMultipleDays && ev.endDate
            ? '<span><i class="fa-solid fa-arrow-right-long"></i> through ' + esc(ev.endDate) + '</span>' : '') +
          (ev.location ? '<span><i class="fa-solid fa-location-dot"></i> ' + esc(ev.location) + '</span>' : '') +
          (ev.createdBy ? '<span><i class="fa-regular fa-user"></i> ' + esc(ev.createdBy) + '</span>' : '') +
        '</div>' +
        (ev.description ? '<div class="tcal-item-desc">' + esc(ev.description) + '</div>' : '') +
        (self.canEdit
          ? '<div class="tcal-item-acts">' +
              '<button type="button" data-act="edit" data-id="' + esc(ev.id) + '"><i class="fa-solid fa-pen"></i> Edit</button>' +
              '<button type="button" class="danger" data-act="delete" data-id="' + esc(ev.id) + '"><i class="fa-solid fa-trash"></i> Delete</button>' +
            '</div>'
          : '') +
      '</div>';
    }).join('');
  };

  // ── data ──────────────────────────────────────────────────────────────────
  Calendar.prototype.load = function () {
    var self = this;
    var first = new Date(this.year, this.month, 1);
    var start = new Date(first.getFullYear(), first.getMonth(), 1 - first.getDay());
    var end = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 41);
    var q = '?start=' + iso(start.getFullYear(), start.getMonth(), start.getDate()) +
            '&end=' + iso(end.getFullYear(), end.getMonth(), end.getDate());

    return this.request('GET', this.api + q)
      .then(function (res) {
        self.events = (res && res.events) || [];
        self.render();
      })
      .catch(function (e) { self.toast(e.message || 'Could not load the calendar.', true); });
  };

  Calendar.prototype.goTo = function (y, m) { this.year = y; this.month = m; this.load(); };

  Calendar.prototype.request = function (method, url, body) {
    if (this.busy) return Promise.reject(new Error('Please wait for the last change to finish.'));
    this.busy = true;
    this.root.classList.add('busy');
    var self = this;

    var headers = { 'X-Requested-With': 'XMLHttpRequest' };
    if (body !== undefined) { headers['Content-Type'] = 'application/json'; }
    if (method !== 'GET') {
      var t = global.csrfToken || (document.querySelector('[name=csrfmiddlewaretoken]') || {}).value || '';
      if (t) headers['X-CSRFToken'] = t;
    }
    var init = { method: method, credentials: 'same-origin', headers: headers };
    if (body !== undefined) init.body = JSON.stringify(body);

    return fetch(url, init)
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok || j.ok === false) throw new Error(j.message || ('Request failed (' + r.status + ').'));
          return j;
        });
      })
      .then(function (j) { self.busy = false; self.root.classList.remove('busy'); return j; },
            function (e) { self.busy = false; self.root.classList.remove('busy'); throw e; });
  };

  Calendar.prototype.toast = function (msg, isError) {
    var el = document.createElement('div');
    el.className = 'tcal-toast' + (isError ? ' err' : '');
    el.textContent = msg;
    this.root.appendChild(el);
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 4200);
  };

  // ── editor ────────────────────────────────────────────────────────────────
  Calendar.prototype.openModal = function (title, html) {
    var m = this.root.querySelector('#tcalModal');
    var t = this.root.querySelector('#tcalModalTitle');
    var b = this.root.querySelector('#tcalModalBody');
    if (!m) return;
    if (t) t.textContent = title;
    if (b) b.innerHTML = html;
    m.hidden = false;
    var f = b && b.querySelector('input,select,textarea');
    if (f) f.focus();
  };
  Calendar.prototype.closeModal = function () {
    var m = this.root.querySelector('#tcalModal');
    if (m) m.hidden = true;
    this.editingId = null;
  };

  Calendar.prototype.form = function (ev) {
    var d = ev || {};
    var opts = this.categories.map(function (c) {
      return '<option value="' + esc(c.key) + '"' + (d.category === c.key ? ' selected' : '') + '>' + esc(c.label) + '</option>';
    }).join('');
    var timed = d.allDay === false;
    var self = this;
    var when = iso(this.selected.getFullYear(), this.selected.getMonth(), this.selected.getDate());

    return '<div class="tcal-form">' +
      '<label class="tcal-fld wide"><span>Title <b>*</b></span>' +
        '<input id="fTitle" type="text" maxlength="200" value="' + esc(d.title || '') + '" placeholder="e.g. Founders Week"></label>' +
      '<label class="tcal-fld"><span>Category</span><select id="fCat">' + opts + '</select></label>' +
      '<label class="tcal-fld"><span>Status</span><select id="fStatus">' +
        ['published', 'draft'].map(function (s) {
          return '<option value="' + s + '"' + ((d.status || 'published') === s ? ' selected' : '') + '>' +
                 s.charAt(0).toUpperCase() + s.slice(1) + '</option>';
        }).join('') + '</select></label>' +
      '<label class="tcal-fld"><span>Start date <b>*</b></span>' +
        '<input id="fStart" type="date" value="' + esc(d.startDate || when) + '"></label>' +
      '<label class="tcal-fld"><span>End date <i>blank = one day</i></span>' +
        '<input id="fEnd" type="date" value="' + esc(d.endDate || '') + '"></label>' +
      '<label class="tcal-fld chk"><span>All day</span>' +
        '<input id="fAll" type="checkbox"' + (timed ? '' : ' checked') + '></label>' +
      '<label class="tcal-fld"><span>Start time</span>' +
        '<input id="fStartT" type="time" value="' + esc(d.startTime || '') + '"' + (timed ? '' : ' disabled') + '></label>' +
      '<label class="tcal-fld"><span>End time</span>' +
        '<input id="fEndT" type="time" value="' + esc(d.endTime || '') + '"' + (timed ? '' : ' disabled') + '></label>' +
      '<label class="tcal-fld wide"><span>Location</span>' +
        '<input id="fLoc" type="text" maxlength="200" value="' + esc(d.location || '') + '" placeholder="e.g. Main Gym"></label>' +
      '<label class="tcal-fld wide"><span>Description</span>' +
        '<textarea id="fDesc" rows="3" placeholder="Optional notes">' + esc(d.description || '') + '</textarea></label>' +
      '<div class="tcal-form-acts">' +
        '<button class="tcal-btn ghost" type="button" data-act="close">Cancel</button>' +
        '<button class="tcal-btn primary" type="button" data-act="save">' +
          (this.editingId ? 'Save changes' : 'Add activity') + '</button>' +
      '</div>' +
      (function () { // keep the time inputs in sync with the all-day checkbox
        setTimeout(function () {
          var box = self.root.querySelector('#fAll');
          if (!box) return;
          box.addEventListener('change', function () {
            var a = self.root.querySelector('#fStartT'), b2 = self.root.querySelector('#fEndT');
            if (a) a.disabled = box.checked;
            if (b2) b2.disabled = box.checked;
          });
        }, 0);
      })() +
    '</div>';
  };

  Calendar.prototype.add = function () {
    if (!this.canEdit) return;
    this.editingId = null;
    this.openModal('Add activity', this.form({ startDate: iso(this.selected.getFullYear(), this.selected.getMonth(), this.selected.getDate()) }));
  };

  Calendar.prototype.find = function (id) {
    for (var i = 0; i < this.events.length; i++) {
      if (String(this.events[i].id) === String(id)) return this.events[i];
    }
    return null;
  };

  Calendar.prototype.edit = function (id) {
    if (!this.canEdit) return;
    var ev = this.find(id);
    if (!ev) { this.toast('That activity is no longer on the calendar.', true); return; }
    this.editingId = ev.id;
    this.openModal('Edit activity', this.form(ev));
  };

  Calendar.prototype.save = function () {
    var self = this;
    var q = function (sel) { var el = this.root.querySelector(sel); return el ? el.value : ''; }.bind(this);
    var allDayEl = this.root.querySelector('#fAll');
    var allDay = allDayEl ? allDayEl.checked : true;
    var end = q('#fEnd');

    var payload = {
      title: q('#fTitle').trim(),
      category: q('#fCat'),
      status: q('#fStatus') || 'published',
      startDate: q('#fStart'),
      endDate: end || null,          // null means a single day
      allDay: allDay,
      startTime: allDay ? null : (q('#fStartT') || null),
      endTime: allDay ? null : (q('#fEndT') || null),
      location: q('#fLoc').trim(),
      description: q('#fDesc').trim()
    };

    if (!payload.title) { this.toast('Please give the activity a title.', true); return; }
    if (!payload.startDate) { this.toast('Please choose a start date.', true); return; }
    if (payload.endDate && payload.endDate < payload.startDate) {
      this.toast('The end date cannot be earlier than the start date.', true); return;
    }
    if (!payload.allDay && !payload.startTime) {
      this.toast('A timed activity needs a start time.', true); return;
    }

    var editing = this.editingId;
    this.request(editing ? 'PUT' : 'POST', this.api + (editing ? editing + '/' : ''), payload)
      .then(function () {
        self.closeModal();
        self.toast(editing ? 'Activity updated.' : 'Activity added.');
        self.selected = parseISO(payload.startDate) || self.selected;
        return self.load();
      })
      .catch(function (e) { self.toast(e.message, true); });
  };

  Calendar.prototype.remove = function (id) {
    var self = this;
    var ev = this.find(id);
    if (!global.confirm('Delete "' + (ev ? ev.title : 'this activity') + '"? This cannot be undone.')) return;
    this.request('DELETE', this.api + id + '/')
      .then(function () { self.toast('Activity deleted.'); return self.load(); })
      .catch(function (e) { self.toast(e.message, true); });
  };

  // ── events ────────────────────────────────────────────────────────────────
  Calendar.prototype.onClick = function (e) {
    var t = e.target;
    var a = t.closest ? t.closest('[data-act]') : null;
    var act = a ? a.getAttribute('data-act') : null;

    if (act === 'prev') { var p = new Date(this.year, this.month - 1, 1); return this.goTo(p.getFullYear(), p.getMonth()); }
    if (act === 'next') { var n = new Date(this.year, this.month + 1, 1); return this.goTo(n.getFullYear(), n.getMonth()); }
    if (act === 'today') {
      this.selected = new Date(this.today.getTime());
      return this.goTo(this.today.getFullYear(), this.today.getMonth());
    }
    if (act === 'add') return this.add();
    if (act === 'edit') return this.edit(a.getAttribute('data-id'));
    if (act === 'delete') return this.remove(a.getAttribute('data-id'));
    if (act === 'close') return this.closeModal();
    if (act === 'save') return this.save();
    if (act === 'more') { this.selected = parseISO(a.getAttribute('data-date')); return this.render(); }

    if (act === 'close-modal-bg') return this.closeModal();

    var bar = t.closest ? t.closest('.tcal-bar') : null;
    if (bar) return this.edit(bar.getAttribute('data-id'));

    var day = t.closest ? t.closest('.tcal-day') : null;
    if (day) {
      this.selected = parseISO(day.getAttribute('data-date'));
      this.render();
    }
  };

  // ── mount ─────────────────────────────────────────────────────────────────
  var instances = {};
  function mount(target, opts) {
    var id = typeof target === 'string' ? target : (target && target.id);
    if (id && instances[id]) return instances[id];
    var cal = new Calendar(target, opts);
    if (id) instances[id] = cal;
    return cal;
  }

  global.TASS = global.TASS || {};
  global.TASS.calendar = {
    mount: mount,
    get: function (id) { return instances[id]; },
    MONTHS: MONTHS, DOW: DOW_FULL, CATEGORY: CATEGORY
  };
})(window);
