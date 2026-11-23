/*
 * Anime Remote — фон расширения.
 *
 * Держит связь с телефонами (WebRTC напрямую, запасной путь через ретранслятор),
 * следит, в какой вкладке идёт видео, передаёт команды нужному фрейму,
 * включает полный экран окна и запоминает, на какой серии и минуте остановились.
 */
'use strict';

const api = browser;
const DEFAULT_PAGE = 'https://romanshap.github.io/anime-remote/';
const SELF = AR.randomId(9);              // имя этого браузера в сообщениях установки связи
const MIN_WATCHED = 30;                   // серия запоминается, когда просмотрено хотя бы 30 с
const END_MARGIN = 90;                    // последние 90 с (титры) = досмотрено
const MAX_TITLES = 100;
const RELAY_GAP_MS = 2500;                // не чаще одного сообщения состояния через ретранслятор

const cfg = { secret: null, relay: AR.DEFAULT_RELAY, page: DEFAULT_PAGE, autonext: false };
const tabs = new Map();                   // id вкладки → что о ней известно
const peers = new Map();                  // id телефона → связь с ним
const pendingResume = new Map();          // id вкладки → куда вернуться, когда страница откроется
let ctl = null;                           // вкладка, которой сейчас управляет пульт
let relay = null;
let relayState = 'off';
let progress = {};
let ready = null;

// ------------------------------------------------------------------ настройки и связь

async function init() {
  const st = await api.storage.local.get(['secret', 'relay', 'page', 'autonext', 'progress']);
  cfg.secret = AR.validSecret(st.secret) ? st.secret : AR.randomId(16);
  if (cfg.secret !== st.secret) { await api.storage.local.set({ secret: cfg.secret }); }
  cfg.relay = st.relay || AR.DEFAULT_RELAY;
  cfg.page = st.page || DEFAULT_PAGE;
  cfg.autonext = !!st.autonext;
  progress = st.progress && typeof st.progress === 'object' ? st.progress : {};
  await startRelay();
}

async function startRelay() {
  if (relay) { relay.stop(); }
  const keys = await AR.deriveKeys(cfg.secret);
  relay = new AR.Relay({
    relay: cfg.relay,
    topic: keys.topic,
    key: keys.key,
    onMessage: onRelay,
    onStatus: (s) => { relayState = s; },
  });
  relay.start();
}

function link() { return AR.pairingLink(cfg.page, cfg.secret, cfg.relay); }

function badge() {
  const n = Array.from(peers.values()).filter((p) => p.mode === 'p2p' || p.mode === 'relay').length;
  api.browserAction.setBadgeText({ text: n ? String(n) : '' });
  api.browserAction.setBadgeBackgroundColor({ color: '#ff9f1c' });
}

// ------------------------------------------------------------------ телефоны

function onRelay(m) {
  if (!m || m.from === SELF || typeof m.from !== 'string') { return; }
  if (m.t === 'offer') { handleOffer(m).catch(() => {}); return; }
  if (m.to && m.to !== SELF) { return; }
  let p = peers.get(m.from);
  if (!p) {
    p = { id: m.from, mode: 'relay', at: Date.now() };
    peers.set(m.from, p);
  }
  if (p.mode !== 'p2p') { p.mode = 'relay'; }
  p.at = Date.now();
  badge();
  onPeerMessage(p, m);
}

async function handleOffer(m) {
  if (typeof m.sdp !== 'string' || m.sdp.length > 30000) { return; }
  const old = peers.get(m.from);
  if (old && old.pc) { try { old.pc.close(); } catch (e) { /* уже закрыт */ } }
  const p = { id: m.from, mode: 'pending', at: Date.now(), lastSent: '' };
  peers.set(m.from, p);
  const pc = new RTCPeerConnection({ iceServers: AR.ICE_SERVERS });
  p.pc = pc;
  pc.ondatachannel = (e) => {
    const dc = e.channel;
    dc.onopen = () => {
      p.dc = dc;
      p.mode = 'p2p';
      p.lastSent = '';
      badge();
      pushAll(true);
    };
    dc.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (x) { return; }
      p.at = Date.now();
      onPeerMessage(p, msg);
    };
    dc.onclose = () => dropPeer(p);
  };
  pc.onconnectionstatechange = () => {
    if (pc.connectionState === 'failed' || pc.connectionState === 'closed') { dropPeer(p); }
  };
  await pc.setRemoteDescription({ type: 'offer', sdp: m.sdp });
  await pc.setLocalDescription(await pc.createAnswer());
  await AR.waitIce(pc, 2500);
  await relay.publish({ t: 'answer', from: SELF, to: m.from, sdp: pc.localDescription.sdp });
}

function dropPeer(p) {
  if (peers.get(p.id) !== p) { return; }
  if (p.mode === 'relay' && !p.pc) { return; }             // через ретранслятор: живёт до тишины
  peers.delete(p.id);
  try { if (p.pc) { p.pc.close(); } } catch (e) { /* уже закрыт */ }
  badge();
}

function onPeerMessage(p, m) {
  switch (m.t) {
    case 'hello':
      p.lastSent = '';
      if (p.mode === 'relay') { relayPush(true); } else { pushAll(true); }
      break;
    case 'cmd':
      exec(m).catch(() => {});
      break;
    case 'bye':
      peers.delete(p.id);
      try { if (p.pc) { p.pc.close(); } } catch (e) { /* уже закрыт */ }
      badge();
      break;
    default:
      break;
  }
}

setInterval(() => {      // телефоны через ретранслятор, которые давно молчат, считаем ушедшими
  const now = Date.now();
  for (const p of peers.values()) {
    if ((p.mode === 'relay' && now - p.at > 15 * 60 * 1000) || (p.mode === 'pending' && now - p.at > 60 * 1000)) {
      peers.delete(p.id);
      try { if (p.pc) { p.pc.close(); } } catch (e) { /* уже закрыт */ }
    }
  }
  badge();
}, 30000);

// ------------------------------------------------------------------ вкладки и фреймы

function tabRec(id) {
  let T = tabs.get(id);
  if (!T) {
    T = { id, frames: new Map(), feps: new Map(), page: null, playedAt: 0, seenAt: 0, curChangedAt: 0,
          full: false, want: null, hint: '', hintAt: 0, chain: [] };
    tabs.set(id, T);
  }
  return T;
}

function videoFrame(T) {
  const now = Date.now();
  let best = null;
  for (const [id, rec] of T.frames) {
    if (!rec.v.has || now - rec.at > 15000) { continue; }
    const s = (rec.v.paused ? 0 : 1e12) + (rec.v.dur > 0 ? 1e10 : 0) + (rec.v.area || 0);
    if (!best || s > best.s) { best = { id, rec, v: rec.v, s }; }
  }
  return best;
}

function hasVideo(T) { return !!videoFrame(T); }

function pickCtl() {
  let best = null;
  for (const T of tabs.values()) {
    if (!hasVideo(T)) { continue; }
    const t = Math.max(T.playedAt, T.seenAt);
    if (!best || t > best.t) { best = { id: T.id, t }; }
  }
  return best ? best.id : null;
}

function epSource(T) {
  if (T.page && T.page.eps && T.page.eps.length) {
    const n = T.page.eps.length;
    const cur = T.page.cur;
    return { scope: 'page', frameId: 0, eps: T.page.eps, cur, canNext: cur >= 0 && cur < n - 1, canPrev: cur > 0 };
  }
  // серии берём только у плеера, где идёт видео (или у самой страницы), а не у соседнего плеера на вкладке
  const vf = videoFrame(T);
  const frameId = vf && T.feps.has(vf.id) ? vf.id : T.feps.has(0) ? 0 : null;
  if (frameId === null) { return null; }
  const p = T.feps.get(frameId);
  return { scope: 'frame', frameId, eps: p.eps, cur: p.cur, canNext: !!p.canNext, canPrev: !!p.canPrev,
           label: p.label || '', pid: p.pid || '' };
}

// Фрейм с плеером Playerjs (у него есть номер серии в плейлисте), даже если видео сейчас идёт не в нём.
function pidFrame(T) {
  for (const [frameId, p] of T.feps) {
    if (p && p.pid) { return { frameId, pid: p.pid }; }
  }
  return null;
}

function toFrame(T, frameId, msg) {
  return api.tabs.sendMessage(T.id, msg, { frameId }).catch(() => null);
}

api.runtime.onMessage.addListener((m, sender) => {
  if (sender.id !== api.runtime.id) { return undefined; }
  const fromOwnPage = typeof sender.url === 'string' && sender.url.startsWith(api.runtime.getURL(''));
  if (!fromOwnPage && sender.tab && typeof sender.tab.id === 'number') {
    onContent(m, sender.tab, sender.frameId || 0);
    return undefined;
  }
  return onPopup(m);
});

function onContent(m, tab, frameId) {
  const T = tabRec(tab.id);
  const now = Date.now();
  switch (m.k) {
    case 'video': {
      const v = m.v || {};
      if (!v.has) {
        T.frames.delete(frameId);
        if (ctl === tab.id && !hasVideo(T)) { ctl = pickCtl() || ctl; }
        break;
      }
      const prev = T.frames.get(frameId);
      const rec = { v, at: now, firstAt: prev && prev.v.vid === v.vid ? prev.firstAt : now };
      T.frames.set(frameId, rec);
      T.seenAt = now;
      if (!v.paused) { T.playedAt = now; ctl = tab.id; }
      if (ctl === null || !tabs.has(ctl) || !hasVideo(tabs.get(ctl))) { ctl = tab.id; }
      checkWant(T, frameId, rec);
      track(T, frameId, rec);
      if (T.full) {
        const vf = videoFrame(T);
        if (vf && T.chain[T.chain.length - 1] !== vf.id) { applyTheater(T); }
      }
      break;
    }
    case 'page': {
      const p = m.p || {};
      const prev = T.page;
      if (!prev || prev.key !== p.key || (prev.cur >= 0 && p.cur >= 0 && prev.cur !== p.cur)) { T.curChangedAt = now; }
      T.page = p;
      break;
    }
    case 'feps': {
      const prev = T.feps.get(frameId);
      if (m.p) {
        if (prev && ((prev.cur >= 0 && m.p.cur >= 0 && prev.cur !== m.p.cur) || (prev.pid && m.p.pid && prev.pid !== m.p.pid))) {
          T.curChangedAt = now;
        }
        T.feps.set(frameId, m.p);
      } else {
        T.feps.delete(frameId);
      }
      break;
    }
    case 'hint':
      T.hint = String(m.code || '');
      T.hintAt = now;
      break;
    case 'ended':
      if (cfg.autonext && ctl === tab.id) { navigate(T, 'next'); }
      break;
    case 'esc':
      if (T.full) { setFull(T, false); }
      break;
    default:
      return;
  }
  schedulePush();
}

api.tabs.onRemoved.addListener((id) => {
  tabs.delete(id);
  pendingResume.delete(id);
  if (ctl === id) { ctl = pickCtl(); }
  schedulePush();
});

api.webNavigation.onCommitted.addListener((d) => {
  const T = tabs.get(d.tabId);
  if (!T) { return; }
  if (d.frameId === 0) {                 // страница сменилась: всё, что знали о ней и её фреймах, устарело
    T.frames.clear();
    T.feps.clear();
    T.page = null;
    T.chain = [];
  } else {
    T.frames.delete(d.frameId);
    T.feps.delete(d.frameId);
  }
});

// ------------------------------------------------------------------ команды с телефона

async function exec(m) {
  const a = m.a;
  if (a === 'state') { pushAll(true); relayPush(true); return; }
  if (a === 'autonext') {
    cfg.autonext = !!m.v;
    await api.storage.local.set({ autonext: cfg.autonext });
    schedulePush();
    return;
  }
  if (a === 'forget') {
    if (typeof m.v === 'string' && progress[m.v]) { delete progress[m.v]; saveProgress(true); }
    schedulePush();
    return;
  }
  if (a === 'resume') { await resume(String(m.v || '')); return; }
  if (ctl === null || !tabs.has(ctl)) { ctl = pickCtl(); }
  const T = ctl !== null ? tabs.get(ctl) : null;
  if (!T) { return; }
  const vf = videoFrame(T);
  switch (a) {
    case 'toggle':
    case 'play':
      if (vf) { await toFrame(T, vf.id, { c: a }); } else { T.want = { play: true, seek: null, ep: null, since: Date.now() }; }
      break;
    case 'pause':
    case 'mute':
      if (vf) { await toFrame(T, vf.id, { c: a }); }
      break;
    case 'seek':
    case 'vol':
      if (vf && typeof m.v === 'number' && isFinite(m.v)) { await toFrame(T, vf.id, { c: a, v: m.v }); }
      break;
    case 'next':
    case 'prev':
    case 'ep':
      navigate(T, a, m.v);
      break;
    case 'full':
      await setFull(T, !T.full);
      break;
    default:
      break;
  }
  schedulePush();
}

function navigate(T, how, val) {
  const src = epSource(T);
  if (!src) { return; }
  let target = null;
  if (src.eps && src.eps.length) {
    target = how === 'ep' ? Number(val) : how === 'next' ? src.cur + 1 : src.cur - 1;
    if (!(target >= 0 && target < src.eps.length)) { return; }
  } else if (how === 'ep') {
    return;
  }
  T.want = { play: true, seek: null, ep: target, since: Date.now() };
  T.curChangedAt = Date.now();
  toFrame(T, src.frameId, { c: how, v: target, scope: src.scope });
}

// Что сделать, когда появится видео нужной серии: запустить и, если надо, перемотать.
function checkWant(T, frameId, rec) {
  const w = T.want;
  if (!w) { return; }
  const now = Date.now();
  if (now - w.since > 90000) { T.want = null; return; }
  const vf = videoFrame(T);
  const target = typeof w.frameId === 'number' ? w.frameId : vf ? vf.id : null;
  if (target !== frameId || !rec.v.has) { return; }
  if (!w.sameVideo && rec.firstAt < w.since - 200) { return; }       // это ещё видео прежней серии
  if (w.pid) {
    const fp = T.feps.get(frameId);
    if (!fp || fp.pid !== w.pid) { return; }
  }
  if (w.ep !== null && w.ep !== undefined) {
    const src = epSource(T);
    if (!src || src.cur !== w.ep) { return; }
    const pe = T.page && T.page.playerEp;
    if (typeof pe === 'number' && pe >= 0 && pe !== w.ep) { return; }
  }
  if (w.play && rec.v.paused && !w.playSent) {
    w.playSent = true;
    toFrame(T, frameId, { c: 'play' });
  }
  if (w.seek && !rec.v.paused && Math.abs(rec.v.time - w.seek) <= 20) {
    w.seek = null;                                       // сайт сам вернул на это место
  }
  if (w.seek && !rec.v.paused && rec.v.dur > 0) {
    if (!w.seekAt) { w.seekAt = now + 1200; }           // некоторые плееры умеют перематывать только после старта
    else if (now >= w.seekAt) { toFrame(T, frameId, { c: 'seekTo', v: w.seek }); w.seek = null; }
  }
  if ((!w.play || !rec.v.paused) && !w.seek) { T.want = null; }
}

// ------------------------------------------------------------------ полный экран

async function setFull(T, on) {
  T.full = on;
  try {
    const tab = await api.tabs.get(T.id);
    const win = await api.windows.get(tab.windowId);
    if (on) {
      if (win.state !== 'fullscreen') {
        T.prevWinState = win.state;
        await api.windows.update(tab.windowId, { state: 'fullscreen' });
      }
      if (!tab.active) { await api.tabs.update(T.id, { active: true }); }
    } else if (win.state === 'fullscreen') {
      const back = T.prevWinState && T.prevWinState !== 'fullscreen' && T.prevWinState !== 'minimized' ? T.prevWinState : 'normal';
      await api.windows.update(tab.windowId, { state: back });
    }
  } catch (e) { /* окно закрыли */ }
  await applyTheater(T);
  schedulePush();
}

// Растянуть плеер: каждому фрейму по пути от вкладки до видео сказать, какой фрейм внутри растянуть.
async function applyTheater(T) {
  const old = T.chain || [];
  if (!T.full) {
    old.forEach((id) => toFrame(T, id, { c: 'theater', on: false }));
    T.chain = [];
    return;
  }
  const vf = videoFrame(T);
  if (!vf) { return; }
  let frames = [];
  try { frames = await api.webNavigation.getAllFrames({ tabId: T.id }); } catch (e) { return; }
  const parent = new Map(frames.map((f) => [f.frameId, f.parentFrameId]));
  const chain = [];
  for (let id = vf.id; id !== undefined && id !== -1 && chain.length < 12; id = parent.get(id)) { chain.unshift(id); }
  if (chain[0] !== 0) { return; }
  old.filter((id) => !chain.includes(id)).forEach((id) => toFrame(T, id, { c: 'theater', on: false }));
  T.chain = chain;
  chain.forEach((id, i) => toFrame(T, id, { c: 'theater', on: true, child: i < chain.length - 1 ? chain[i + 1] : null }));
}

// ------------------------------------------------------------------ где остановился

let saveTimer = null;
function saveProgress(now) {
  clearTimeout(saveTimer);
  const doSave = () => {
    const keys = Object.keys(progress);
    if (keys.length > MAX_TITLES) {
      keys.sort((a, b) => progress[a].updated - progress[b].updated)
        .slice(0, keys.length - MAX_TITLES).forEach((k) => { delete progress[k]; });
    }
    api.storage.local.set({ progress });
  };
  if (now) { doSave(); } else { saveTimer = setTimeout(doSave, 5000); }
}

function isFinished(e) { return e.dur > 0 && e.time >= e.dur - END_MARGIN; }

function track(T, frameId, rec) {
  const v = rec.v;
  if (!T.page || !v.has || v.time < MIN_WATCHED) { return; }
  const vf = videoFrame(T);
  if (!vf || vf.id !== frameId) { return; }
  const src = epSource(T);
  const ep = src ? src.cur : -1;
  const pe = T.page.playerEp;
  if (typeof pe === 'number' && pe >= 0) {
    if (pe !== ep) { return; }                         // плеер ещё от прежней серии
  } else if (rec.firstAt < T.curChangedAt - 500) {
    return;
  }
  const key = T.page.key;
  const old = progress[key];
  const pid = src && src.pid ? src.pid : '';
  const same = old && old.ep === ep && (old.pid || '') === pid;
  if (v.paused && !same) { return; }                   // другую серию только открыли, но не смотрели
  if (same && old.time === v.time) { return; }
  progress[key] = {
    key,
    url: T.page.url,
    title: String(T.page.title || '').slice(0, 200),
    site: T.page.site,
    ep,
    epLabel: src && src.eps && ep >= 0 ? String(src.eps[ep]).slice(0, 20) : (src && src.label ? src.label : ''),
    pid,
    total: src && src.eps ? src.eps.length : 0,
    scope: src ? src.scope : null,
    time: v.time,
    dur: v.dur,
    updated: Date.now(),
  };
  saveProgress(!same || v.paused);
}

function progressList() {
  return Object.values(progress)
    .sort((a, b) => b.updated - a.updated)
    .map((e) => ({ key: e.key, title: e.title, site: e.site, ep: e.ep, epLabel: e.epLabel, pid: e.pid || '', total: e.total,
                   time: e.time, dur: e.dur, updated: e.updated, finished: isFinished(e) }));
}

async function resume(key) {
  const e = progress[key];
  if (!e) { return; }
  let ep = e.ep;
  let t = e.time;
  if (isFinished(e)) {
    if (e.total && ep + 1 < e.total) { ep += 1; t = 0; } else { t = 0; }
  }
  if (ctl === null || !tabs.has(ctl)) { ctl = pickCtl(); }
  const T = ctl !== null ? tabs.get(ctl) : null;
  const pid = ep === e.ep ? e.pid || '' : '';
  if (T && T.page && T.page.key === key) {
    startResume(T, ep, t, pid);
    return;
  }
  let tabId;
  try {
    if (T) {
      await api.tabs.update(T.id, { url: e.url, active: true });
      tabId = T.id;
    } else {
      const tab = await api.tabs.create({ url: e.url, active: true });
      tabId = tab.id;
    }
  } catch (x) { return; }
  pendingResume.set(tabId, { key, ep, t, pid, since: Date.now() });
  ctl = tabId;
}

function startResume(T, ep, t, pid) {
  const src = epSource(T);
  const seek = t >= MIN_WATCHED ? t : null;
  const pf = pid ? pidFrame(T) : null;
  if (pf) {                                              // плеер с плейлистом (Playerjs): к серии по её номеру
    const same = pf.pid === pid;
    T.want = { play: true, seek, ep: null, pid, frameId: pf.frameId, since: Date.now(), sameVideo: same };
    if (!same) {
      T.curChangedAt = Date.now();
      toFrame(T, pf.frameId, { c: 'find', v: pid });
    } else {
      const rec = T.frames.get(pf.frameId);
      if (rec) { checkWant(T, pf.frameId, rec); }
    }
  } else if (src && src.eps && ep >= 0 && ep < src.eps.length && src.cur !== ep) {
    T.want = { play: true, seek, ep, since: Date.now() };
    T.curChangedAt = Date.now();
    toFrame(T, src.frameId, { c: 'ep', v: ep, scope: src.scope });
  } else {
    T.want = { play: true, seek, ep: null, since: Date.now(), sameVideo: true };
    const vf = videoFrame(T);
    if (vf) { checkWant(T, vf.id, vf.rec); }
  }
}

setInterval(() => {      // открыли страницу, чтобы продолжить: ждём, пока она расскажет о себе
  const now = Date.now();
  for (const [tabId, r] of pendingResume) {
    const T = tabs.get(tabId);
    if (now - r.since > 60000) { pendingResume.delete(tabId); continue; }
    if (!T || !T.page || T.page.key !== r.key) { continue; }
    if (!r.seenAt) { r.seenAt = now; }
    if (!hasVideo(T) && !r.revealed && now - r.seenAt > 2500) {
      r.revealed = true;                  // плеер грузится, только когда его видно: прокрутить к нему
      toFrame(T, 0, { c: 'reveal' });
    }
    const src = epSource(T);
    const ready = r.pid ? !!pidFrame(T) : r.ep >= 0 ? !!(src && src.eps && src.eps.length > r.ep) : true;
    if (!ready && now - r.since < 20000) { continue; }
    pendingResume.delete(tabId);
    startResume(T, r.ep, r.t, r.pid);
  }
}, 700);

// ------------------------------------------------------------------ состояние для телефонов

function tabState(T) {
  const vf = videoFrame(T);
  const v = vf ? vf.v : null;
  const src = epSource(T);
  return {
    title: T.page ? T.page.title : '',
    site: T.page ? T.page.site : '',
    key: T.page ? T.page.key : '',
    eps: src && src.eps ? src.eps : null,
    cur: src ? src.cur : -1,
    epLabel: src && src.label ? src.label : '',
    pid: src && src.pid ? src.pid : '',
    canNext: src ? !!src.canNext : false,
    canPrev: src ? !!src.canPrev : false,
    video: !!v,
    paused: v ? v.paused : true,
    time: v ? v.time : 0,
    dur: v ? v.dur : 0,
    vol: v ? v.vol : null,
    muted: v ? v.muted : false,
    full: !!T.full,
    hint: Date.now() - T.hintAt < 20000 ? T.hint : '',
  };
}

function fullState() {
  if (ctl === null || !tabs.has(ctl) || !hasVideo(tabs.get(ctl))) {
    const next = pickCtl();
    if (next !== null) { ctl = next; }
  }
  const T = ctl !== null ? tabs.get(ctl) : null;
  const tab = T && (hasVideo(T) || (T.page && T.page.eps)) ? tabState(T) : null;
  return { t: 'state', from: SELF, tab, autonext: cfg.autonext, progress: progressList() };
}

let pushTimer = null;
function schedulePush() {
  if (pushTimer) { return; }
  pushTimer = setTimeout(() => { pushTimer = null; pushAll(false); relayPush(false); }, 150);
}

function pushAll(force) {
  if (!peers.size) { return; }
  const s = JSON.stringify(fullState());
  for (const p of peers.values()) {
    if (p.mode !== 'p2p' || !p.dc || p.dc.readyState !== 'open') { continue; }
    if (force || s !== p.lastSent) {
      try { p.dc.send(s); p.lastSent = s; } catch (e) { dropPeer(p); }
    }
  }
}
setInterval(() => pushAll(false), 1000);

// Через ретранслятор шлём только заметные перемены (не каждую секунду): у ntfy.sh лимит сообщений.
const rp = { sig: '', at: 0, timer: null };
function significant(st) {
  const tab = st.tab ? Object.assign({}, st.tab, { time: 0 }) : null;
  return JSON.stringify({ tab, autonext: st.autonext, progress: st.progress.slice(0, 10).map((e) => [e.key, e.ep, e.finished]) });
}

function relayPush(force) {
  const any = Array.from(peers.values()).some((p) => p.mode === 'relay');
  if (!any || !relay) { return; }
  const st = fullState();
  const sig = significant(st);
  if (!force && sig === rp.sig) { return; }
  const wait = rp.at + RELAY_GAP_MS - Date.now();
  if (wait > 0) {
    clearTimeout(rp.timer);
    rp.timer = setTimeout(() => relayPush(force), wait);
    return;
  }
  rp.sig = sig;
  rp.at = Date.now();
  sendRelayState(st).catch(() => {});
}

async function sendRelayState(st) {
  const tries = [
    st,
    Object.assign({}, st, { progress: st.progress.slice(0, 10) }),
    Object.assign({}, st, { progress: st.progress.slice(0, 5), tab: st.tab ? Object.assign({}, st.tab, { eps: null }) : null }),
  ];
  for (const s of tries) {
    try { await relay.publish(s); return; } catch (e) {
      if (!/too big/.test(String(e && e.message))) { return; }
    }
  }
}

// ------------------------------------------------------------------ окно расширения

async function onPopup(m) {
  await ready;
  switch (m && m.k) {
    case 'popup': {
      const T = ctl !== null ? tabs.get(ctl) : null;
      return {
        link: link(),
        relay: cfg.relay,
        page: cfg.page,
        relayState: relay && relay.isOpen ? 'open' : relayState,
        phones: Array.from(peers.values()).filter((p) => p.mode !== 'pending').map((p) => p.mode),
        title: T && T.page && hasVideo(T) ? T.page.title : '',
        hint: T && Date.now() - T.hintAt < 20000 ? T.hint : '',
      };
    }
    case 'reset':
      cfg.secret = AR.randomId(16);
      await api.storage.local.set({ secret: cfg.secret });
      for (const p of peers.values()) { try { if (p.pc) { p.pc.close(); } } catch (e) { /* уже закрыт */ } }
      peers.clear();
      badge();
      await startRelay();
      return { link: link() };
    case 'settings': {
      const relayUrl = /^https?:\/\/[^\s]+$/.test(m.relay || '') ? String(m.relay).replace(/\/+$/, '') : AR.DEFAULT_RELAY;
      const pageUrl = /^https?:\/\/[^\s#]+$/.test(m.page || '') ? m.page : DEFAULT_PAGE;
      cfg.relay = relayUrl;
      cfg.page = pageUrl;
      await api.storage.local.set({ relay: relayUrl, page: pageUrl });
      await startRelay();
      return { link: link() };
    }
    default:
      return undefined;
  }
}

ready = init();
