/*
 * Anime Remote — общий протокол расширения и страницы телефона.
 * Этот файл один и тот же в extension/ и docs/ (tools/pack.py копирует и сверяет).
 *
 * Связь: телефон и браузер находят друг друга через ретранслятор ntfy.sh (только
 * сообщения для установки связи), дальше говорят напрямую по WebRTC. Если прямую связь
 * установить не удалось, команды идут через тот же ретранслятор.
 * Всё, что уходит в ретранслятор, зашифровано (AES-GCM). Ключ и имя канала выводятся
 * из секрета, который есть только в ссылке-приглашении (после #), на сервер он не попадает.
 */
(function (root) {
  'use strict';

  const DEFAULT_RELAY = 'https://ntfy.sh';
  const MAX_SKEW_MS = 10 * 60 * 1000;
  const ICE_SERVERS = [{ urls: ['stun:stun.l.google.com:19302', 'stun:stun.cloudflare.com:3478'] }];
  const enc = new TextEncoder();
  const dec = new TextDecoder();

  function b64u(bytes) {
    bytes = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    let s = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  function unb64u(str) {
    str = String(str).replace(/-/g, '+').replace(/_/g, '/');
    while (str.length % 4) { str += '='; }
    const b = atob(str);
    const out = new Uint8Array(b.length);
    for (let i = 0; i < b.length; i++) { out[i] = b.charCodeAt(i); }
    return out;
  }

  function randomId(n) { return b64u(crypto.getRandomValues(new Uint8Array(n || 16))); }

  function validSecret(s) { return typeof s === 'string' && /^[A-Za-z0-9_-]{22,64}$/.test(s); }

  async function deriveKeys(secret) {
    if (!validSecret(secret)) { throw new Error('bad secret'); }
    const base = await crypto.subtle.importKey('raw', unb64u(secret), 'HKDF', false, ['deriveBits', 'deriveKey']);
    const salt = enc.encode('anime-remote/1');
    const bits = await crypto.subtle.deriveBits(
      { name: 'HKDF', hash: 'SHA-256', salt, info: enc.encode('topic') }, base, 192);
    const key = await crypto.subtle.deriveKey(
      { name: 'HKDF', hash: 'SHA-256', salt, info: enc.encode('key') }, base,
      { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
    return { topic: 'ar-' + b64u(bits), key };
  }

  const canZip = typeof CompressionStream === 'function' && typeof DecompressionStream === 'function';

  async function through(bytes, stream) {
    const s = new Blob([bytes]).stream().pipeThrough(stream);
    return new Uint8Array(await new Response(s).arrayBuffer());
  }

  // Зашифровать объект в строку для ретранслятора. Длинное сначала сжимается.
  async function seal(key, obj) {
    let body = enc.encode(JSON.stringify(obj));
    let flag = 0;
    if (canZip && body.length > 300) {
      body = await through(body, new CompressionStream('deflate-raw'));
      flag = 1;
    }
    const plain = new Uint8Array(body.length + 1);
    plain[0] = flag;
    plain.set(body, 1);
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const ct = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, key, plain));
    const out = new Uint8Array(12 + ct.length);
    out.set(iv);
    out.set(ct, 12);
    return b64u(out);
  }

  async function unseal(key, text) {
    try {
      const all = unb64u(String(text).trim());
      if (all.length < 29) { return null; }
      const plain = new Uint8Array(await crypto.subtle.decrypt(
        { name: 'AES-GCM', iv: all.subarray(0, 12) }, key, all.subarray(12)));
      let body = plain.subarray(1);
      if (plain[0] === 1) {
        if (!canZip) { return null; }
        body = await through(body, new DecompressionStream('deflate-raw'));
      }
      const obj = JSON.parse(dec.decode(body));
      return obj && typeof obj === 'object' ? obj : null;
    } catch (e) {
      return null;     // чужое, повреждённое или не наш ключ
    }
  }

  /*
   * Канал через ntfy: подписка по SSE (EventSource), отправка POST-запросом.
   * Сообщения не хранятся на сервере (Cache: no): доходят только тем, кто сейчас подписан.
   */
  class Relay {
    constructor(opts) {
      this.url = String(opts.relay || DEFAULT_RELAY).replace(/\/+$/, '');
      this.topic = opts.topic;
      this.key = opts.key;
      this.onMessage = opts.onMessage;
      this.onStatus = opts.onStatus || function () {};
      this.es = null;
      this.seen = new Set();
      this.closed = true;
      this.retry = 0;
      this.isOpen = false;
      this.timer = null;
    }

    start() {
      this.closed = false;
      this._connect();
    }

    _connect() {
      if (this.closed || this.es) { return; }
      let es;
      try {
        es = new EventSource(this.url + '/' + this.topic + '/sse');
      } catch (e) {
        this._later();
        return;
      }
      this.es = es;
      es.onopen = () => { this.retry = 0; this.isOpen = true; this.onStatus('open'); };
      es.onmessage = (e) => { this._incoming(e.data); };
      es.onerror = () => {
        this.isOpen = false;
        this.onStatus('error');
        if (es.readyState === 2) {          // CLOSED: сам EventSource больше пробовать не будет
          es.close();
          if (this.es === es) { this.es = null; }
          this._later();
        }
      };
    }

    _later() {
      if (this.closed) { return; }
      clearTimeout(this.timer);
      const delay = Math.min(60000, 2000 * Math.pow(2, this.retry++));
      this.timer = setTimeout(() => this._connect(), delay);
    }

    async _incoming(data) {
      let m;
      try { m = JSON.parse(data); } catch (e) { return; }
      if (!m || m.event !== 'message' || typeof m.message !== 'string') { return; }
      if (m.id) {
        if (this.seen.has(m.id)) { return; }
        this.seen.add(m.id);
        if (this.seen.size > 300) { this.seen = new Set([m.id]); }
      }
      const obj = await unseal(this.key, m.message);
      if (!obj || typeof obj.t !== 'string') { return; }
      if (!(Math.abs(Date.now() - Number(obj.ts || 0)) < MAX_SKEW_MS)) { return; }   // старое или с чужими часами
      try { this.onMessage(obj); } catch (e) { /* ошибки обработчика не должны рвать канал */ }
    }

    async publish(obj) {
      const msg = Object.assign({}, obj, { ts: Date.now() });
      const body = await seal(this.key, msg);
      if (body.length > 4000) { throw new Error('message too big'); }   // ntfy превращает >4 КБ во вложение
      const r = await fetch(this.url + '/' + this.topic, {
        method: 'POST',
        body,
        headers: { 'Cache': 'no', 'Firebase': 'no' },
        credentials: 'omit',
        cache: 'no-store',
      });
      if (!r.ok) { throw new Error('relay ' + r.status); }
    }

    stop() {
      this.closed = true;
      this.isOpen = false;
      clearTimeout(this.timer);
      if (this.es) { this.es.close(); }
      this.es = null;
    }
  }

  // Дождаться, пока соберутся все адреса для WebRTC (без «trickle»: одно сообщение вместо многих).
  function waitIce(pc, ms) {
    return new Promise((resolve) => {
      if (pc.iceGatheringState === 'complete') { resolve(); return; }
      const t = setTimeout(done, ms || 3000);
      function done() {
        clearTimeout(t);
        pc.removeEventListener('icegatheringstatechange', check);
        resolve();
      }
      function check() { if (pc.iceGatheringState === 'complete') { done(); } }
      pc.addEventListener('icegatheringstatechange', check);
    });
  }

  function pairingLink(pageUrl, secret, relay) {
    let link = String(pageUrl).replace(/#.*$/, '') + '#k=' + secret;
    if (relay && String(relay).replace(/\/+$/, '') !== DEFAULT_RELAY) {
      link += '&r=' + encodeURIComponent(relay);
    }
    return link;
  }

  function parseLink(hash) {
    const out = {};
    String(hash || '').replace(/^#/, '').split('&').forEach((part) => {
      const i = part.indexOf('=');
      if (i > 0) { out[part.slice(0, i)] = decodeURIComponent(part.slice(i + 1)); }
    });
    return { secret: validSecret(out.k) ? out.k : null, relay: /^https?:\/\//.test(out.r || '') ? out.r : null };
  }

  root.AR = {
    DEFAULT_RELAY, ICE_SERVERS, b64u, unb64u, randomId, validSecret, deriveKeys, seal, unseal,
    Relay, waitIce, pairingLink, parseLink,
  };
})(typeof self !== 'undefined' ? self : this);
