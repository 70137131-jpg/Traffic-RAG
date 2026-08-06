/* TrafficBot — "LANE" direction (design 1a), vanilla port.
   Talks to the Flask backend: GET /ready, POST /ask, POST /upload. */
(function () {
  'use strict';

  var CHIPS = [
    'Fine for running a red light?',
    'Commercial vehicle curfew hours',
    'When can I impound a vehicle?'
  ];

  var els = {
    body:    document.getElementById('body'),
    empty:   document.getElementById('empty'),
    thread:  document.getElementById('thread'),
    chips:   document.getElementById('chips'),
    input:   document.getElementById('input'),
    send:    document.getElementById('send-btn'),
    recent:  document.getElementById('recent-btn'),
    histPanel: document.getElementById('hist-panel'),
    histList:  document.getElementById('hist-list'),
    docBtn:  document.getElementById('doc-btn'),
    docName: document.getElementById('doc-name'),
    online:  document.getElementById('online'),
    file:    document.getElementById('file-input')
  };

  var state = { doc: 'traffic_laws.md', history: [], histOpen: false };

  // ── helpers ───────────────────────────────────────────────────────────────
  function metaStr(md) {
    if (!md) return 'source';
    return Object.keys(md).map(function (k) { return k + ' ' + md[k]; }).join('  ·  ');
  }

  function scrollDown() { els.body.scrollTop = els.body.scrollHeight; }

  function showEmpty(show) { els.empty.hidden = !show; }

  // ── message construction ──────────────────────────────────────────────────
  function addUser(text) {
    var turn = document.createElement('div');
    turn.className = 'turn';
    var wrap = document.createElement('div');
    wrap.className = 'msg-user';
    var bar = document.createElement('span');
    bar.className = 'bar';
    var p = document.createElement('p');
    p.textContent = text;
    wrap.appendChild(bar);
    wrap.appendChild(p);
    turn.appendChild(wrap);
    els.thread.appendChild(turn);
    scrollDown();
  }

  // Returns a controller with setOk / setNone / setError to fill the bot turn.
  function addBotLoading() {
    var turn = document.createElement('div');
    turn.className = 'turn';
    var bot = document.createElement('div');
    bot.className = 'msg-bot';
    bot.innerHTML =
      '<div class="bot-loading"><span class="bar"></span>' +
      '<span class="lbl">Searching the code</span></div>';
    turn.appendChild(bot);
    els.thread.appendChild(turn);
    scrollDown();

    return {
      answerEl: null,
      setOk: function () {
        bot.innerHTML = '<p class="bot-answer"></p>';
        this.answerEl = bot.querySelector('.bot-answer');
      },
      setText: function (t) { if (this.answerEl) { this.answerEl.textContent = t; scrollDown(); } },
      setNone: function () {
        bot.innerHTML =
          '<div class="bot-none"><p class="t">Not in this document.</p>' +
          '<p class="d">Nothing in <strong></strong> matches that. Try naming the offence, ' +
          'or load a different code book.</p></div>';
        bot.querySelector('strong').textContent = state.doc;
      },
      setError: function () {
        bot.innerHTML =
          '<div class="bot-err"><p class="t">Signal lost.</p>' +
          '<p class="d">The archive didn’t answer. Ask again in a moment.</p></div>';
      },
      attachSources: function (sources) {
        if (!sources || !sources.length) return;
        var wrap = document.createElement('div');
        wrap.className = 'src-wrap';

        var btn = document.createElement('button');
        btn.className = 'why-btn';
        btn.type = 'button';

        var list = document.createElement('div');
        list.className = 'src-list';
        list.hidden = true;

        sources.forEach(function (s) {
          var card = document.createElement('div');
          card.className = 'src';
          var meta = document.createElement('div');
          meta.className = 'meta';
          meta.textContent = metaStr(s.metadata);
          var content = document.createElement('div');
          content.className = 'content';
          content.textContent = s.content;
          card.appendChild(meta);
          card.appendChild(content);
          list.appendChild(card);
        });

        var n = sources.length;
        function label() {
          return list.hidden
            ? 'Why? ' + n + ' clause' + (n === 1 ? '' : 's') + '  ↓'
            : 'Hide the clauses  ↑';
        }
        btn.textContent = label();
        btn.addEventListener('click', function () {
          list.hidden = !list.hidden;
          btn.textContent = label();
          scrollDown();
        });

        wrap.appendChild(btn);
        wrap.appendChild(list);
        bot.appendChild(wrap);
        scrollDown();
      }
    };
  }

  // Time-based reveal (~150 chars/sec), guaranteed final commit with sources.
  function typeOut(bot, full, sources) {
    var RATE = 150, start = performance.now(), total = (full.length / RATE) * 1000;
    bot.setOk();
    var iv = setInterval(function () {
      var n = Math.min(full.length, Math.floor(((performance.now() - start) / 1000) * RATE));
      bot.setText(full.slice(0, n));
      if (n >= full.length) clearInterval(iv);
    }, 50);
    setTimeout(function () {
      clearInterval(iv);
      bot.setText(full);
      bot.attachSources(sources);
    }, total + 60);
  }

  // ── history drawer ──────────────────────────────────────────────────────
  function pushHistory(q) {
    state.history = [q].concat(state.history.filter(function (h) { return h !== q; })).slice(0, 8);
    renderHistory();
  }

  function renderHistory() {
    els.histList.innerHTML = '';
    if (!state.history.length) {
      var e = document.createElement('div');
      e.className = 'hist-empty';
      e.textContent = 'Nothing yet.';
      els.histList.appendChild(e);
      return;
    }
    state.history.forEach(function (q) {
      var b = document.createElement('button');
      b.className = 'hist-item';
      b.type = 'button';
      b.textContent = q;
      b.addEventListener('click', function () { toggleHistory(false); send(q); });
      els.histList.appendChild(b);
    });
  }

  function toggleHistory(force) {
    state.histOpen = typeof force === 'boolean' ? force : !state.histOpen;
    els.histPanel.hidden = !state.histOpen;
    if (state.histOpen) renderHistory();
  }

  // ── ask flow ──────────────────────────────────────────────────────────────
  function send(override) {
    var q = String(override || els.input.value || '').trim();
    if (!q) return;
    if (!override) els.input.value = '';

    showEmpty(false);
    addUser(q);
    var bot = addBotLoading();
    pushHistory(q);

    fetch('/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: q })
    }).then(function (r) {
      if (!r.ok) throw new Error('bad');
      return r.json();
    }).then(function (data) {
      if (!data || !data.answer) { bot.setNone(); return; }
      typeOut(bot, data.answer, data.sources || []);
    }).catch(function () {
      bot.setError();
    });
  }

  // ── document upload ───────────────────────────────────────────────────────
  function upload(e) {
    var file = e.target.files && e.target.files[0];
    if (!file) return;
    var prev = state.doc;
    setDoc('indexing…');

    var fd = new FormData();
    fd.append('file', file);
    fetch('/upload', { method: 'POST', body: fd })
      .then(function (r) {
        return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || 'fail'); return d; });
      })
      .then(function (d) {
        setDoc(d.filename);
        showEmpty(false);
        var bot = addBotLoading();
        bot.setOk();
        bot.setText('Knowledge base switched to ' + d.filename + '. Ask away.');
      })
      .catch(function () {
        setDoc(prev);
        showEmpty(false);
        var bot = addBotLoading();
        bot.setError();
      });
    e.target.value = '';
  }

  function setDoc(name) { state.doc = name; els.docName.textContent = name; }

  function setOnline(ok) {
    els.online.classList.toggle('offline', !ok);
    els.online.querySelector('span:last-child').textContent = ok ? 'Online' : 'Offline';
  }

  // ── init ──────────────────────────────────────────────────────────────────
  CHIPS.forEach(function (t) {
    var b = document.createElement('button');
    b.className = 'chip';
    b.type = 'button';
    b.textContent = t;
    b.addEventListener('click', function () { send(t); });
    els.chips.appendChild(b);
  });

  els.send.addEventListener('click', function () { send(); });
  els.input.addEventListener('keydown', function (e) { if (e.key === 'Enter') send(); });
  els.recent.addEventListener('click', function () { toggleHistory(); });
  els.docBtn.addEventListener('click', function () { els.file.click(); });
  els.file.addEventListener('change', upload);

  fetch('/ready').then(function (r) { return r.json(); }).then(function (d) {
    if (d && d.document) setDoc(d.document);
    setOnline(!d || d.database === 'ok');
  }).catch(function () { setOnline(false); });

  els.input.focus();
})();
