/*
 * Anime Remote — работает в каждой вкладке и в каждом фрейме.
 *
 * В любом фрейме: находит главное видео, сообщает фону его состояние, выполняет команды
 * (пауза, перемотка, звук), растягивает плеер на всё окно, ищет серии внутри плеера.
 * В верхнем фрейме дополнительно: узнаёт сайт (animevost, HDrezka, остальные), название
 * и список серий страницы, переключает серии.
 */
(function () {
  'use strict';
  if (window.__animeRemoteLoaded) { return; }
  window.__animeRemoteLoaded = true;

  const api = typeof browser !== 'undefined' ? browser : chrome;
  const TOP = window === window.top;
  const S = self.ARSites;

  let alive = true;
  function send(msg) {
    if (!alive) { return; }
    try {
      const p = api.runtime.sendMessage(msg);
      if (p && p.catch) { p.catch(() => {}); }
    } catch (e) {
      alive = false;           // расширение обновили или выключили: этот скрипт больше не нужен
    }
  }

  // ------------------------------------------------------------------ поиск видео

  let video = null;
  let shadowScanAt = 0;
  const hooked = new WeakSet();
  const ids = new WeakMap();
  let nextId = 1;

  function shadowVideos(rootNode, out) {
    for (const el of rootNode.querySelectorAll('*')) {
      const sr = el.openOrClosedShadowRoot || el.shadowRoot;
      if (sr) {
        sr.querySelectorAll('video').forEach((v) => out.push(v));
        shadowVideos(sr, out);
      }
    }
    return out;
  }

  function allVideos() {
    const list = Array.from(document.getElementsByTagName('video'));
    if (!list.length && Date.now() - shadowScanAt > 15000) {
      shadowScanAt = Date.now();
      return shadowVideos(document, []);
    }
    return list;
  }

  function area(v) {
    const r = v.getBoundingClientRect();
    return Math.max(0, r.width) * Math.max(0, r.height);
  }

  function score(v) {
    const a = area(v);
    if (a < 160 * 90) { return -1; }                               // крошечное: реклама, превью
    const d = v.duration;
    if (isFinite(d) && d > 0 && d < 45) { return -1; }             // короткий ролик: реклама, заставка
    return (v.paused ? 0 : 1e9) + (v.readyState > 0 ? 1e8 : 0) + a;
  }

  function pickVideo() {
    let best = null;
    let bestScore = -1;
    for (const v of allVideos()) {
      const s = score(v);
      if (s > bestScore) { best = v; bestScore = s; }
    }
    return best;
  }

  function videoId(v) {
    if (!ids.has(v)) { ids.set(v, nextId++); }
    return ids.get(v) + '|' + String(v.currentSrc || v.src || '').slice(0, 300);
  }

  function hook(v) {
    if (hooked.has(v)) { return; }
    hooked.add(v);
    ['play', 'pause', 'seeked', 'volumechange', 'durationchange', 'loadedmetadata', 'emptied'].forEach((ev) => {
      v.addEventListener(ev, () => report(true));
    });
    v.addEventListener('playing', () => { if (mutedByUs && !v.muted) { mutedByUs = false; } report(true); });
    v.addEventListener('ended', () => { if (v === video) { send({ k: 'ended' }); } report(true); });
  }

  // ------------------------------------------------------------------ состояние видео → фон

  let lastSent = '';
  let lastSentAt = 0;

  function videoInfo() {
    const v = video;
    if (!v || !v.isConnected) { return { has: false }; }
    return {
      has: true,
      paused: v.paused,
      ended: v.ended,
      time: Math.floor(v.currentTime || 0),
      dur: isFinite(v.duration) ? Math.floor(v.duration) : 0,
      vol: Math.round(v.volume * 100),
      muted: v.muted,
      area: Math.round(area(v)),
      vid: videoId(v),
      ready: v.readyState,
    };
  }

  function report(force) {
    const info = videoInfo();
    if (!info.has && !lastSent) { return; }                         // видео не было и нет
    const s = JSON.stringify(info);
    const now = Date.now();
    if (!force && s === lastSent && now - lastSentAt < 5000) { return; }
    lastSent = info.has ? s : '';
    lastSentAt = now;
    send({ k: 'video', v: info });
  }

  // ------------------------------------------------------------------ серии

  let pageSent = '';
  function pageInfo() {
    const a = S.page();
    let labels = null;
    let cur = -1;
    try { labels = a.labels(); cur = a.current(); } catch (e) { /* разметка сайта поменялась */ }
    return {
      site: a.id,
      title: String(a.title() || '').slice(0, 200),
      key: a.key(),
      url: location.href,
      eps: labels && labels.length ? labels : null,
      cur,
      playerEp: a.playerEp ? a.playerEp() : null,
    };
  }

  let frameEps = null;
  let frameSent = '';
  function scanFrameEpisodes() {
    frameEps = video ? S.episodes(document) : null;     // серии ищем только рядом с видео
    const info = frameEps ? { eps: frameEps.labels, cur: frameEps.cur, canNext: frameEps.canNext, canPrev: frameEps.canPrev,
                              label: frameEps.label || '', pid: frameEps.pid || '' } : null;
    const s = JSON.stringify(info);
    if (s !== frameSent) {
      frameSent = s;
      send({ k: 'feps', p: info });
    }
  }

  function sendPage(force) {
    if (!TOP) { return; }
    const p = pageInfo();
    const s = JSON.stringify(p);
    if (force || s !== pageSent) {
      pageSent = s;
      send({ k: 'page', p });
    }
  }

  // ------------------------------------------------------------------ воспроизведение

  let mutedByUs = false;

  function setHint(code) { send({ k: 'hint', code }); }

  function clickPlayerPlay() {
    // Плеер ещё не начал грузить видео: нажимаем его собственную кнопку «играть» (ближайшую к центру видео).
    if (!video) { return false; }
    const vr = video.getBoundingClientRect();
    const cx = vr.left + vr.width / 2;
    const cy = vr.top + vr.height / 2;
    let best = null;
    let bestD = Infinity;
    for (const el of document.querySelectorAll('[class*="play" i], [aria-label*="play" i], [aria-label*="смотр" i], [aria-label*="воспр" i]')) {
      const cls = String(el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className);
      const aria = String(el.getAttribute('aria-label') || '');
      // только большая кнопка запуска поверх видео, не переключатель пауза/играть на панели
      if (!/big|start|overlay|centr|center|play[-_]?(btn|button|icon)|(btn|button)[-_]?play/i.test(cls + ' ' + aria) &&
          !/^(play|смотреть|воспроизвести)$/i.test(aria.trim())) { continue; }
      const r = el.getBoundingClientRect();
      if (r.width < 20 || r.height < 20 || r.width > vr.width * 0.9) { continue; }
      const d = Math.hypot(r.left + r.width / 2 - cx, r.top + r.height / 2 - cy);
      if (d < bestD) { best = el; bestD = d; }
    }
    if (best) { best.click(); return true; }
    return false;
  }

  async function doPlay(v) {
    if (!v) { return false; }
    try {
      await v.play();
      setHint('');
      return true;
    } catch (e) {
      if (e && e.name === 'NotAllowedError') {
        // Firefox не даёт включить звук без клика по странице: запускаем без звука.
        if (!v.muted) {
          v.muted = true;
          mutedByUs = true;
          try {
            await v.play();
            setHint('muted');
            return true;
          } catch (e2) {
            v.muted = false;
            mutedByUs = false;
          }
        }
        setHint('autoplay');
        return false;
      }
      clickPlayerPlay();
      return false;
    }
  }

  function playSoon(v) {
    doPlay(v);
    // MSE-плееры иногда не отдают данные, пока не нажмёшь их кнопку
    setTimeout(() => {
      if (v.isConnected && v.paused && v.readyState < 2) { clickPlayerPlay(); }
    }, 3000);
  }

  async function unmute(v) {
    v.muted = false;
    if (v.volume === 0) { v.volume = 0.5; }
    await new Promise((r) => setTimeout(r, 500));
    if (v.paused && mutedByUs) {
      // браузер остановил видео со звуком: возвращаем как было
      v.muted = true;
      try { await v.play(); } catch (e) { /* ничего */ }
      setHint('autoplay');
      return;
    }
    mutedByUs = false;
    setHint('');
  }

  // ------------------------------------------------------------------ весь экран

  const style = document.createElement('style');
  style.textContent =
    'html.ar-theater, html.ar-theater body { overflow: hidden !important; }' +
    'html.ar-theater body * { visibility: hidden !important; }' +
    'html.ar-theater .ar-stretch, html.ar-theater .ar-stretch * { visibility: visible !important; }' +
    '.ar-stretch { position: fixed !important; inset: 0 !important; top: 0 !important; left: 0 !important;' +
    ' width: 100vw !important; height: 100vh !important; max-width: none !important; max-height: none !important;' +
    ' min-width: 0 !important; min-height: 0 !important; margin: 0 !important; padding: 0 !important;' +
    ' border: 0 !important; transform: none !important; z-index: 2147483647 !important; background: #000 !important; }' +
    '.ar-stretch video, video.ar-stretch { object-fit: contain !important; }' +
    '.ar-stretch video { width: 100% !important; height: 100% !important; max-width: none !important; max-height: none !important; }' +
    '.ar-anc { transform: none !important; filter: none !important; contain: none !important; perspective: none !important;' +
    ' will-change: auto !important; backdrop-filter: none !important; clip-path: none !important; }' +
    'html.ar-idle, html.ar-idle * { cursor: none !important; }' +
    S.frameCss();
  function ensureStyle() {
    if (!style.isConnected) { (document.head || document.documentElement).appendChild(style); }
  }

  function playerBox(v) {
    // Самый внешний контейнер того же размера, что и видео: так видны и субтитры плеера.
    let box = v;
    const vr = v.getBoundingClientRect();
    let el = v.parentElement;
    while (el && el !== document.body && el !== document.documentElement) {
      const r = el.getBoundingClientRect();
      if (Math.abs(r.width - vr.width) > 12 || Math.abs(r.height - vr.height) > 12) { break; }
      box = el;
      el = el.parentElement;
    }
    return box;
  }

  function unstretch() {
    document.documentElement.classList.remove('ar-theater', 'ar-idle');
    document.querySelectorAll('.ar-stretch').forEach((e) => e.classList.remove('ar-stretch'));
    document.querySelectorAll('.ar-anc').forEach((e) => e.classList.remove('ar-anc'));
  }

  function stretch(el) {
    unstretch();
    if (!el) { return false; }
    ensureStyle();
    el.classList.add('ar-stretch');
    for (let a = el.parentElement; a && a !== document.documentElement; a = a.parentElement) {
      a.classList.add('ar-anc');
    }
    document.documentElement.classList.add('ar-theater');
    return true;
  }

  function frameElementById(frameId) {
    for (const f of document.querySelectorAll('iframe, frame, embed, object')) {
      try {
        if (api.runtime.getFrameId(f) === frameId) { return f; }
      } catch (e) { /* не фрейм */ }
    }
    return null;
  }

  let theater = null;       // { child } или null
  function applyTheater() {
    if (!theater) { unstretch(); return true; }
    if (theater.child !== null && theater.child !== undefined) {
      return stretch(frameElementById(theater.child));
    }
    return video ? stretch(playerBox(video)) : false;
  }

  let idleTimer = null;
  document.addEventListener('mousemove', () => {
    document.documentElement.classList.remove('ar-idle');
    clearTimeout(idleTimer);
    if (theater) {
      idleTimer = setTimeout(() => {
        if (theater) { document.documentElement.classList.add('ar-idle'); }
      }, 3000);
    }
  }, true);

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && theater) { send({ k: 'esc' }); }
  }, true);

  // ------------------------------------------------------------------ команды от фона

  function clamp(x, lo, hi) { return Math.min(hi, Math.max(lo, x)); }

  function command(m) {
    const v = video;
    const val = Number(m.v);
    switch (m.c) {
      case 'toggle':
        if (v) { if (v.paused) { playSoon(v); } else { v.pause(); } }
        break;
      case 'play': if (v && v.paused) { playSoon(v); } break;
      case 'pause': if (v) { v.pause(); } break;
      case 'seek':
        if (v && isFinite(val)) {
          const end = isFinite(v.duration) ? Math.max(0, v.duration - 1) : Infinity;
          v.currentTime = clamp(v.currentTime + val, 0, end);
        }
        break;
      case 'seekTo':
        if (v && isFinite(val) && Math.abs(v.currentTime - val) > 20) {
          const end = isFinite(v.duration) ? Math.max(0, v.duration - 1) : Infinity;
          v.currentTime = clamp(val, 0, end);
        }
        break;
      case 'vol':
        if (v && isFinite(val)) {
          if (mutedByUs || v.muted) { unmute(v); }
          v.volume = clamp(Math.round((v.volume + val) * 100) / 100, 0, 1);
        }
        break;
      case 'mute':
        if (v) { if (v.muted) { unmute(v); } else { v.muted = true; } }
        break;
      case 'theater':
        theater = m.on ? { child: m.child } : null;
        return { ok: applyTheater() };
      case 'ep':
      case 'next':
      case 'prev': {
        const scope = m.scope || 'page';
        let ok = false;
        if (scope === 'page' && TOP) {
          const a = S.page();
          const n = (a.labels() || []).length;
          const cur = a.current();
          const target = m.c === 'ep' ? val : m.c === 'next' ? cur + 1 : cur - 1;
          if (target >= 0 && target < n) { ok = a.go(target); }
        } else if (scope === 'frame') {
          const e = S.episodes(document);
          if (e) { ok = m.c === 'ep' ? e.go(val) : m.c === 'next' ? e.next() : e.prev(); }
        }
        setTimeout(() => { sendPage(false); scanFrameEpisodes(); }, 300);
        return { ok };
      }
      case 'find': {                         // перейти к серии плейлиста Playerjs по её номеру
        const e = S.episodes(document);
        const ok = !!(e && e.find && e.find(m.v));
        setTimeout(() => { sendPage(false); scanFrameEpisodes(); }, 300);
        return { ok };
      }
      case 'reveal': {                       // прокрутить к самому большому плееру, чтобы он загрузился
        let best = null;
        let bestArea = 0;
        for (const el of document.querySelectorAll('iframe, video')) {
          const r = el.getBoundingClientRect();
          const a = r.width * r.height;
          if (a > bestArea && r.width >= 300) { best = el; bestArea = a; }
        }
        if (best) { best.scrollIntoView({ block: 'center' }); }
        return { ok: !!best };
      }
      case 'report':
        report(true);
        sendPage(true);
        frameSent = '';
        scanFrameEpisodes();
        break;
      default:
        break;
    }
    setTimeout(() => report(true), 150);
    return { ok: true };
  }

  api.runtime.onMessage.addListener((m) => {
    if (!m || typeof m.c !== 'string') { return undefined; }
    try { return Promise.resolve(command(m)); } catch (e) { return Promise.resolve({ ok: false }); }
  });

  // ------------------------------------------------------------------ цикл

  let tick = 0;
  function loop() {
    if (!alive) { return; }
    tick++;
    const cur = video;
    if (!cur || !cur.isConnected || tick % 3 === 0) {
      const v = pickVideo();
      if (v !== video) {
        video = v;
        if (v) { hook(v); }
        if (theater) { applyTheater(); }
        report(true);
      }
    }
    report(false);
    if (tick % 3 === 0) { scanFrameEpisodes(); }
    if (TOP) { sendPage(false); }
  }
  setInterval(loop, 1000);
  loop();
  if (TOP) { sendPage(true); }
})();
