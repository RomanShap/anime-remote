// ==UserScript==
// @name         Anime Remote (animevost)
// @namespace    anime-remote.local
// @version      1.2.0
// @description  Пульт для animevost с телефона: серии, пауза, перемотка, звук, полный экран, запоминание места остановки. Работает вместе с server.py на этом компьютере.
// @match        https://animevost.org/*
// @match        https://*.vost.pw/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// @noframes
// ==/UserScript==

(function () {
  'use strict';

  const BASE = 'http://127.0.0.1:8765';
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));

  const episodes = () => $$('#items .epizode');
  if (!episodes().length) { return; }            // не страница с сериями

  // ---------------------------------------------------------------- связь с server.py

  function http(method, path, body, timeout) {
    const url = BASE + path;
    const headers = { 'X-Anime-Remote': '1' };
    const data = body ? JSON.stringify(body) : undefined;
    if (data) { headers['Content-Type'] = 'application/json'; }
    if (typeof GM_xmlhttpRequest === 'function') {
      return new Promise((resolve, reject) => {
        GM_xmlhttpRequest({
          method, url, headers, data, timeout: timeout || 10000,
          onload: (r) => {
            try { resolve(JSON.parse(r.responseText)); } catch (e) { reject(e); }
          },
          onerror: () => reject(new Error('network')),
          ontimeout: () => reject(new Error('timeout')),
          onabort: () => reject(new Error('abort')),
        });
      });
    }
    // запасной путь без менеджера скриптов (прямой запрос со страницы)
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeout || 10000);
    return fetch(url, { method, headers, body: data, signal: ctl.signal, cache: 'no-store' })
      .then((r) => r.json())
      .finally(() => clearTimeout(timer));
  }

  // ---------------------------------------------------------------- страница и плеер

  const currentIndex = () => episodes().findIndex((e) => e.classList.contains('active'));
  const frame = () => $('#player2 iframe') || $('#player iframe');
  function frameDoc() {
    try { const f = frame(); return f ? f.contentDocument : null; } catch (e) { return null; }
  }
  function video() {
    const d = frameDoc();
    return d ? d.querySelector('video') : null;
  }
  function titleText() {
    const h = $('.shortstoryHead h1') || $('h1');
    return (h ? h.textContent : document.title).replace(/\s+/g, ' ').trim();
  }
  const PAGE_URL = location.origin + location.pathname;

  // Номер видео в загруженном сейчас плеере (frame5.php?play=ID).
  function frameVideoId() {
    try {
      const d = frameDoc();
      const m = d && d.location.search.match(/[?&]play=(\d+)/);
      return m ? m[1] : '';
    } catch (e) { return ''; }
  }
  // Какой серии принадлежит загруженный плеер. Сразу после переключения сайт уже подсветил
  // новую серию, а старый плеер ещё на месте: в этот момент его время к новой серии не относится.
  function frameEpisode() {
    const id = frameVideoId();
    if (!id) { return -1; }
    return episodes().findIndex((e) => (e.getAttribute('onclick') || '').indexOf('(' + id + ',') >= 0);
  }

  let hint = '';
  let hintUntil = 0;
  function setHint(code, ms) { hint = code; hintUntil = Date.now() + (ms || 8000); }

  let autonext = false;
  try { autonext = localStorage.getItem('ar_autonext') === '1'; } catch (e) {}

  // ---------------------------------------------------------------- полный экран
  // Настоящий полный экран браузер даёт включить только по нажатию самого зрителя, поэтому:
  // плеер растягивается на всё окно (CSS), а окно Firefox переводится в F11 через server.py.

  const THEATER = 'ar-theater';
  const css = document.createElement('style');
  css.textContent =
    'html.' + THEATER + ', html.' + THEATER + ' body { overflow: hidden !important; }' +
    'html.' + THEATER + ' body :not(#player2, #player2 *) { visibility: hidden !important; }' +
    'html.' + THEATER + ' #player2, html.' + THEATER + ' #player2 * { visibility: visible !important; }' +
    'html.' + THEATER + ' #player2 iframe { position: fixed !important; top: 0 !important; left: 0 !important;' +
    ' width: 100vw !important; height: 100vh !important; max-width: none !important; max-height: none !important;' +
    ' margin: 0 !important; border: 0 !important; z-index: 2147483647 !important; background: #000 !important; }';
  (document.head || document.documentElement).appendChild(css);

  const FRAME_CSS =
    'html.' + THEATER + ' #knpki { display: none !important; }' +
    'html.ar-idle, html.ar-idle * { cursor: none !important; }';

  const inTheater = () => document.documentElement.classList.contains(THEATER);
  const isFull = () => inTheater() || !!document.fullscreenElement;
  const winFull = () => (typeof window.fullScreen === 'boolean'
    ? window.fullScreen
    : window.innerHeight >= screen.height - 1);

  function setTheater(on) {
    document.documentElement.classList.toggle(THEATER, on);
    const d = frameDoc();
    if (d && d.documentElement) {
      d.documentElement.classList.toggle(THEATER, on);
      if (!on) { d.documentElement.classList.remove('ar-idle'); }
    }
  }

  let syncToken = 0;
  async function syncWindow(want) {
    const token = ++syncToken;
    const stale = () => token !== syncToken || inTheater() !== want;
    if (hint === 'f11') { hintUntil = 0; }
    await sleep(400);
    // При смене серии из полноэкранного режима самого плеера Firefox выходит из него не сразу:
    // до 3 с ждём, пока состояние окна разойдётся с нужным, и только тогда жмём F11.
    // Если нажатие не подействовало (окно ещё перестраивалось), пробуем второй раз.
    for (let attempt = 0; attempt < 2; attempt++) {
      for (let i = 0; i < 12; i++) {
        if (stale()) { return; }
        if (!document.fullscreenElement && winFull() !== want) { break; }
        await sleep(250);
      }
      if (document.fullscreenElement || winFull() === want) { return; }
      await sleep(300);
      if (stale() || document.fullscreenElement || winFull() === want) { return; }
      let r = null;
      try { r = await http('POST', '/api/key', { key: 'f11', title: document.title }); } catch (e) {}
      if (r && r.reason === 'unsupported') { return; }   // нажать F11 нечем (Linux без X11): плеер и так на всё окно
      if (!r || !r.ok) { setHint('f11', 12000); return; }
      await sleep(1500);
    }
  }

  async function setFull(on) {
    if (!on && document.fullscreenElement) {
      try { await document.exitFullscreen(); } catch (e) {}
    }
    setTheater(on);
    syncWindow(on);
  }

  // ---------------------------------------------------------------- серии и воспроизведение

  let switchToken = 0;

  function tryPlay(v) {
    let p;
    try { p = v.play(); } catch (e) { return; }
    if (p && p.then) {
      p.then(
        () => { if (hint === 'autoplay') { hintUntil = 0; } },
        (e) => { if (e && e.name === 'NotAllowedError') { setHint('autoplay', 20000); } }
      );
    }
  }

  async function playWhenReady(token) {
    const t0 = Date.now();
    while (Date.now() - t0 < 60000) {
      if (token !== switchToken) { return; }
      const v = video();
      if (v) { tryPlay(v); return; }
      await sleep(300);
    }
  }

  async function goTo(idx, play) {
    const eps = episodes();
    if (!(idx >= 0 && idx < eps.length)) { return; }
    const wasFull = isFull();
    const old = frame();
    const token = ++switchToken;
    eps[idx].click();                                   // сайт сам подгружает плеер этой серии
    if (wasFull) { setTheater(true); }
    const t0 = Date.now();
    while (!frame() || frame() === old) {
      if (token !== switchToken || Date.now() - t0 > 15000) { return; }
      await sleep(150);
    }
    if (wasFull) { syncWindow(true); }
    if (play !== false) { playWhenReady(token); }
  }

  // ---------------------------------------------------------------- где остановился
  // Место (серия и время) запоминает server.py по состоянию, которое мы ему шлём.
  // Здесь только возврат к сохранённому месту.

  let pendingSeek = null;                               // { idx, t }: куда перемотать серию idx

  function armSeek() {
    const want = pendingSeek;
    const v = video();
    const d = frameDoc();
    if (!want || !v || !d || want.idx !== currentIndex() || frameEpisode() !== want.idx) { return; }
    pendingSeek = null;
    const seek = () => {
      if (isFinite(v.duration) && Math.abs(v.currentTime - want.t) > 20) {
        v.currentTime = clamp(want.t, 0, Math.max(0, v.duration - 1));
      }
    };
    if (v.played && v.played.length) { seek(); return; }   // серия уже шла: просто перематываем
    // Основной плеер сайта сам возвращает место, если помнит его (localStorage). Тогда не мешаем.
    let own = false;
    try { own = !!(d.getElementById('aniland') && localStorage.getItem('vost_pos_' + frameVideoId())); } catch (e) {}
    if (!own) { v.addEventListener('playing', () => setTimeout(seek, 1200), { once: true }); }
  }

  function resumeAt(s, play) {
    const n = episodes().length;
    if (!s || !(s.ep >= 0) || !n) { return; }
    const done = s.duration > 0 && s.time >= s.duration - 90;   // титры: серия досмотрена
    let idx = Math.min(s.ep, n - 1);
    let t = s.time;
    if (done && s.ep + 1 < n) { idx = s.ep + 1; t = 0; }        // значит, дальше следующая
    else if (done) {
      if (play) { setHint('nonew', 10000); }                    // новой серии на сайте ещё нет
      if (currentIndex() !== idx) { goTo(idx, false); }
      return;
    }
    pendingSeek = t >= 30 ? { idx, t } : null;
    if (idx === currentIndex() && frameEpisode() === idx && video()) {
      armSeek();
      if (play) { tryPlay(video()); }
    } else {
      goTo(idx, play);
    }
  }

  let greeted = false;
  async function hello() {                              // раз за загрузку страницы: что по ней сохранено
    if (greeted) { return; }
    greeted = true;
    let r = null;
    try { r = await http('POST', '/api/hello', { url: PAGE_URL }); } catch (e) { greeted = false; return; }
    const s = r && r.saved;
    if (!s) { return; }
    if (r.resume) { resumeAt(s, true); return; }        // с телефона просили продолжить именно это аниме
    const done = s.duration > 0 && s.time >= s.duration - 90;
    if (currentIndex() === 0 && s.ep > 0) {
      resumeAt(s, false);                               // сайт забыл серию (например, стёрты куки) и открыл первую
    } else if (s.ep === currentIndex() && !done && s.time >= 30) {
      pendingSeek = { idx: s.ep, t: s.time };           // серия та же: подстрахуем место, если сайт его не помнит
      armSeek();
    }
  }

  function exec(cmd) {
    const v = video();
    const val = Number(cmd.value);
    switch (cmd.action) {
      case 'next': goTo(currentIndex() + 1); break;
      case 'prev': goTo(currentIndex() - 1); break;
      case 'ep': goTo(val); break;
      case 'toggle':
        if (!v) { playWhenReady(++switchToken); } else if (v.paused) { tryPlay(v); } else { v.pause(); }
        break;
      case 'play': if (v) { tryPlay(v); } else { playWhenReady(++switchToken); } break;
      case 'pause': if (v) { v.pause(); } break;
      case 'seek':
        if (v && isFinite(v.duration) && isFinite(val)) {
          v.currentTime = clamp(v.currentTime + val, 0, Math.max(0, v.duration - 1));
        }
        break;
      case 'vol':
        if (v && isFinite(val)) {
          v.muted = false;
          v.volume = clamp(Math.round((v.volume + val) * 100) / 100, 0, 1);
        }
        break;
      case 'mute': if (v) { v.muted = !v.muted; } break;
      case 'full': setFull(!isFull()); break;
      case 'autonext':
        autonext = !!cmd.value;
        try { localStorage.setItem('ar_autonext', autonext ? '1' : '0'); } catch (e) {}
        break;
      case 'resume': resumeAt(cmd.value, true); break;          // вернуться к сохранённому месту
      case 'open':                                              // открыть другое аниме в этой же вкладке
        try {
          const u = new URL(String(cmd.value));
          if (u.protocol === 'https:' && u.host === location.host) { location.href = u.href; }
        } catch (e) {}
        break;
    }
  }

  // ---------------------------------------------------------------- слежение за плеером

  const hookedDocs = new WeakSet();
  const hookedVideos = new WeakSet();

  function onKey(e) {
    if (e.key === 'Escape' && inTheater()) { setFull(false); }
  }
  document.addEventListener('keydown', onKey, true);

  function hookFrame() {
    const d = frameDoc();
    if (d && d.documentElement && d.readyState !== 'loading' && !hookedDocs.has(d)) {
      hookedDocs.add(d);
      const st = d.createElement('style');
      st.textContent = FRAME_CSS;
      (d.head || d.documentElement).appendChild(st);
      d.addEventListener('keydown', onKey, true);
      let idle = null;
      d.addEventListener('mousemove', () => {           // в полном экране прячем курсор через 3 с
        d.documentElement.classList.remove('ar-idle');
        clearTimeout(idle);
        idle = setTimeout(() => { if (inTheater()) { d.documentElement.classList.add('ar-idle'); } }, 3000);
      }, true);
    }
    if (d && d.documentElement) { d.documentElement.classList.toggle(THEATER, inTheater()); }
    const v = video();
    if (v && !hookedVideos.has(v)) {
      hookedVideos.add(v);
      v.addEventListener('ended', () => {
        const i = currentIndex();
        if (autonext && i >= 0 && i < episodes().length - 1) { goTo(i + 1); }
      });
    }
    if (pendingSeek) { armSeek(); }
  }
  setInterval(hookFrame, 500);

  // ---------------------------------------------------------------- обмен с сервером

  function snapshot() {
    const fe = frameEpisode();
    const v = fe < 0 || fe === currentIndex() ? video() : null;   // плеер прежней серии не считаем
    return {
      url: PAGE_URL,
      title: titleText(),
      episodes: episodes().map((e) => e.textContent.trim()),
      current: currentIndex(),
      video: !!v,
      paused: v ? v.paused : true,
      time: v ? Math.floor(v.currentTime || 0) : 0,
      duration: v && isFinite(v.duration) ? Math.floor(v.duration) : 0,
      volume: v ? Math.round(v.volume * 100) : null,
      muted: v ? v.muted : false,
      full: isFull(),
      autonext,
      hint: Date.now() < hintUntil ? hint : '',
    };
  }

  let serverUp = false;
  let lastSent = '';
  let lastSentAt = 0;

  setInterval(() => {
    if (document.hidden || !serverUp) { return; }
    const s = JSON.stringify(snapshot());
    if (s === lastSent && Date.now() - lastSentAt < 4000) { return; }
    lastSent = s;
    lastSentAt = Date.now();
    http('POST', '/api/state', JSON.parse(s)).catch(() => {});
  }, 1000);

  (async function pollLoop() {
    let since = -1;
    let pause = 2000;
    for (;;) {
      if (document.hidden) { since = -1; await sleep(1500); continue; }   // командует только видимая вкладка
      try {
        const r = await http('GET', '/api/poll?since=' + since, null, 30000);
        since = r.seq;
        serverUp = true;
        pause = 2000;
        hello();
        if (!document.hidden) { (r.cmds || []).forEach((c) => { try { exec(c); } catch (e) {} }); }
      } catch (e) {
        serverUp = false;                                // server.py не запущен: тихо ждём
        await sleep(pause);
        pause = Math.min(10000, pause + 2000);
      }
    }
  })();
})();
