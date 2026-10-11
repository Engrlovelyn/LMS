/* Age/Sex demographics panel, shared by every staff dashboard.
 *
 * Each role supplies the same row shape from Django:
 *   { name, code, gender, birth, group, meta }
 * where an empty ``gender`` or ``birth`` means the value is not on file and is
 * drawn as an em dash rather than guessed. Callers do:
 *
 *   TASS.demo.setRows(data.demographics.rows);   // once, when data loads
 *   TASS.demo.render({ prefix: 'dm' });          // whenever the view opens
 *
 * The renderer fills, when the elements exist:
 *   #{prefix}Male #{prefix}Female #{prefix}AvgAge #{prefix}Total
 *   #{prefix}AgeChart #{prefix}GenderChart #{prefix}Table #{prefix}Search
 */
(function () {
  'use strict';

  var cachedRows = [];
  var charts = {};
  var DASH = '—';

  function esc(value) {
    return String(value === null || value === undefined ? '' : value).replace(
      /[&<>"']/g,
      function (c) {
        return {
          '&': '&amp;',
          '<': '&lt;',
          '>': '&gt;',
          '"': '&quot;',
          "'": '&#39;'
        }[c];
      }
    );
  }

  function ageOf(iso) {
    if (!iso) return '';
    var p = String(iso).split('-');
    if (p.length < 3) return '';
    var y = +p[0], m = +p[1], d = +p[2];
    if (!y || !m || !d) return '';
    var now = new Date();
    var age = now.getFullYear() - y;
    var hadBirthday =
      now.getMonth() + 1 > m ||
      (now.getMonth() + 1 === m && now.getDate() >= d);
    if (!hadBirthday) age -= 1;
    return age >= 0 && age < 130 ? age : '';
  }

  function setText(id, value) {
    var el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  function chart(id, config) {
    var el = document.getElementById(id);
    if (!el || typeof Chart === 'undefined') return;
    if (charts[id]) charts[id].destroy();
    charts[id] = new Chart(el, config);
  }

  function barChart(id, ages) {
    if (!ages.length) {
      chart(id, { data: { labels: [], datasets: [] } });
      return;
    }
    var min = Math.min.apply(null, ages);
    var max = Math.max.apply(null, ages);
    var bins = [];
    for (var a = min; a <= max; a++) {
      bins.push({ age: a, n: ages.filter(function (x) { return x === a; }).length });
    }
    chart(id, {
      type: 'bar',
      data: {
        labels: bins.map(function (b) { return b.age + ' yrs'; }),
        datasets: [
          {
            label: 'People',
            data: bins.map(function (b) { return b.n; }),
            backgroundColor: '#1e5faf',
            borderRadius: 6
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { display: false } },
          y: { beginAtZero: true, ticks: { precision: 0 } }
        }
      }
    });
  }

  function genderChart(id, male, female) {
    if (!male && !female) {
      chart(id, { data: { labels: [], datasets: [] } });
      return;
    }
    chart(id, {
      type: 'doughnut',
      data: {
        labels: ['Male', 'Female', 'Not recorded'],
        datasets: [
          {
            data: [male, female, 0],
            backgroundColor: ['#1e5faf', '#16a34a', '#94a3b8'],
            borderWidth: 0,
            hoverOffset: 6
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '62%',
        plugins: { legend: { position: 'bottom' } }
      }
    });
  }

  function renderTable(prefix, rows, columns) {
    var body = document.getElementById(prefix + 'Table');
    if (!body) return;
    if (!rows.length) {
      body.innerHTML =
        '<tr class="tass-empty-row"><td colspan="' +
        columns +
        '" style="text-align:center;padding:24px 12px;color:var(--text-muted);">' +
        'No data yet. Rows appear once records carry a sex or birthdate.</td></tr>';
      return;
    }
    body.innerHTML = rows
      .map(function (r) {
        var age = ageOf(r.birth);
        return (
          '<tr><td style="font-weight:600;">' +
          esc(r.name || DASH) +
          '</td><td style="color:var(--text-muted);font-size:12px;">' +
          esc(r.code || DASH) +
          '</td><td style="text-align:center;">' +
          (age === '' ? DASH : age) +
          '</td><td>' +
          esc(r.group || DASH) +
          '</td><td style="text-align:center;">' +
          esc(r.gender || DASH) +
          '</td></tr>'
        );
      })
      .join('');
  }

  function render(opts) {
    opts = opts || {};
    var prefix = opts.prefix || 'dm';
    if (opts.rows) cachedRows = opts.rows;
    var rows = cachedRows;

    var searchEl = document.getElementById(prefix + 'Search');
    var search = searchEl ? String(searchEl.value || '').toLowerCase() : '';
    if (search) {
      rows = rows.filter(function (r) {
        return (
          (r.name + ' ' + (r.code || '') + ' ' + (r.group || ''))
            .toLowerCase()
            .indexOf(search) !== -1
        );
      });
    }

    var male = rows.filter(function (r) { return r.gender === 'Male'; }).length;
    var female = rows.filter(function (r) { return r.gender === 'Female'; }).length;
    var ages = rows
      .map(function (r) { return ageOf(r.birth); })
      .filter(function (a) { return a !== ''; });

    var hasSex = rows.some(function (r) {
      return r.gender === 'Male' || r.gender === 'Female';
    });
    setText(prefix + 'Male', hasSex ? male : DASH);
    setText(prefix + 'Female', hasSex ? female : DASH);
    setText(
      prefix + 'AvgAge',
      ages.length
        ? Math.round(
            ages.reduce(function (a, b) { return a + b; }, 0) / ages.length
          )
        : DASH
    );
    setText(prefix + 'Total', rows.length || DASH);

    barChart(prefix + 'AgeChart', ages);
    genderChart(prefix + 'GenderChart', male, female);
    renderTable(prefix, rows, 5);
  }

  window.TASS = window.TASS || {};
  window.TASS.demo = {
    setRows: function (rows) {
      cachedRows = Array.isArray(rows) ? rows : [];
    },
    rows: function () {
      return cachedRows;
    },
    ageOf: ageOf,
    render: render
  };
})();
