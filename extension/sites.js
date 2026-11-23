/*
 * Anime Remote — что пульт знает о сайтах.
 *
 * ARSites.page()  — адаптер верхней страницы: название, список серий, переход к серии.
 *                   Есть готовые для animevost и HDrezka, для остальных — общий (только название).
 * ARSites.episodes(doc) — общий поиск серий внутри плеера (в любом фрейме): выпадающий список
 *                   «N серия», ряд пунктов «N серия / episode N» или кнопки «Следующая серия».
 *                   Так работают плееры, которые встраивают kinogo и похожие сайты.
 */
(function () {
  'use strict';

  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
  const clean = (t) => String(t || '').replace(/\s+/g, ' ').trim();

  // ------------------------------------------------------------------ адаптеры страниц

  const animevost = {
    id: 'animevost',
    match: () => /(^|\.)(animevost\.org|vost\.pw)$/.test(location.hostname) && !!$('#items .epizode'),
    title: () => clean(($('.shortstoryHead h1') || {}).textContent) || document.title,
    items: () => $$('#items .epizode'),
    labels() { return this.items().map((e) => clean(e.textContent)); },
    current() { return this.items().findIndex((e) => e.classList.contains('active')); },
    go(i) { const it = this.items()[i]; if (it) { it.click(); return true; } return false; },
    // какой серии принадлежит загруженный сейчас плеер (по номеру видео в адресе фрейма)
    playerEp() {
      const f = $('#player2 iframe') || $('#player iframe');
      const m = f && (f.getAttribute('src') || '').match(/[?&]play=(\d+)/);
      if (!m) { return -1; }
      return this.items().findIndex((e) => (e.getAttribute('onclick') || '').indexOf('(' + m[1] + ',') >= 0);
    },
    key: () => location.origin + location.pathname,
  };

  // HDrezka и её зеркала (домены меняются, поэтому узнаём по разметке).
  const hdrezka = {
    id: 'hdrezka',
    match: () => !!$('.b-post__title') && !!$('#cdnplayer, #oframecdnplayer, .b-player'),
    title() {
      const t = clean(($('.b-post__title h1') || $('.b-post__title') || {}).textContent);
      return t || document.title;
    },
    items: () => $$('.b-simple_episode__item'),
    labels() {
      const items = this.items();
      const seasons = new Set(items.map((e) => e.getAttribute('data-season_id')));
      return items.map((e) => {
        const s = e.getAttribute('data-season_id');
        const n = e.getAttribute('data-episode_id') || clean(e.textContent);
        return seasons.size > 1 ? s + '·' + n : String(n);
      });
    },
    current() { return this.items().findIndex((e) => e.classList.contains('active')); },
    go(i) {
      const it = this.items()[i];
      if (!it) { return false; }
      const s = it.getAttribute('data-season_id');
      const tab = s && $('.b-simple_season__item[data-tab_id="' + s + '"]');
      if (tab && !tab.classList.contains('active')) { tab.click(); }
      it.click();
      return true;
    },
    key: () => location.origin + location.pathname,
  };

  const generic = {
    id: 'generic',
    match: () => true,
    title() {
      const og = $('meta[property="og:title"]');
      const h1 = $('h1');
      return clean((og && og.content) || (h1 && h1.textContent) || document.title).slice(0, 200);
    },
    labels: () => null,
    current: () => -1,
    go: () => false,
    key: () => location.origin + location.pathname + location.search,
  };

  const ADAPTERS = [animevost, hdrezka, generic];

  // Стили, которые прячут лишнее внутри плееров конкретных сайтов в режиме «весь экран».
  function frameCss() {
    if (/(^|\.)(animevost\.org|vost\.pw)$/.test(location.hostname)) {
      return 'html.ar-theater #knpki{display:none!important}';      // полоска «Плеер №1 / №2»
    }
    return '';
  }

  // ------------------------------------------------------------------ серии внутри плееров

  const EP_RE = /^(?:(\d{1,4})\s*(?:-?(?:я|й|ая))?\s*(?:серия|серии|эпизод|эп\.?|episode|ep\.?)|(?:серия|эпизод|эп\.?|episode|ep\.?)\s*(\d{1,4}))$/i;
  const ACTIVE_RE = /(^|[\s_-])(active|current|selected|is-active|is-current|playing)/i;
  const NEXT_RE = /^(?:след[а-яё]*\.?\s*(?:сери[яи]|эпизод)|next\s*(?:episode|ep\.?)|next)$/i;
  const PREV_RE = /^(?:пред[а-яё]*\.?\s*(?:сери[яи]|эпизод)|prev(?:ious)?\s*(?:episode|ep\.?)|prev(?:ious)?)$/i;

  function epNumber(text) {
    const m = clean(text).match(EP_RE);
    return m ? Number(m[1] || m[2]) : null;
  }

  function isActive(el) {
    if (!el) { return false; }
    const aria = (el.getAttribute('aria-selected') || '') + ' ' + (el.getAttribute('aria-current') || '');
    if (/true|page|step/.test(aria)) { return true; }
    const cls = typeof el.className === 'string' ? el.className : (el.getAttribute('class') || '');
    return ACTIVE_RE.test(cls);
  }

  function labelOf(el) {
    return clean(el.getAttribute('aria-label') || el.getAttribute('title') ||
                 el.getAttribute('data-tooltip') || el.textContent).slice(0, 60);
  }

  function findButton(doc, re) {
    for (const el of $$('button, [role="button"], a, [class*="next" i], [class*="prev" i]', doc)) {
      const label = labelOf(el);
      if (label && re.test(label)) { return el; }
      const tip = el.querySelector('[class*="tooltip" i], [class*="sr-only" i]');
      if (tip && re.test(clean(tip.textContent))) { return el; }
    }
    return null;
  }

  function findGroup(doc) {
    // 1) выпадающий список серий
    for (const sel of $$('select', doc)) {
      const opts = Array.from(sel.options);
      if (opts.length < 2) { continue; }
      const hits = opts.filter((o) => epNumber(o.textContent) !== null).length;
      if (hits >= opts.length * 0.7) { return { kind: 'select', sel, items: opts }; }
    }
    // 2) ряд пунктов «N серия» с общим родителем
    const groups = new Map();
    for (const el of $$('li, div, span, a, button, option', doc)) {
      if (el.children.length > 3) { continue; }
      const t = el.textContent;
      if (!t || t.length > 40 || epNumber(t) === null) { continue; }
      let item = el;
      while (item.parentElement && item.parentElement.children.length === 1 &&
             clean(item.parentElement.textContent) === clean(t)) {
        item = item.parentElement;
      }
      const parent = item.parentElement;
      if (!parent) { continue; }
      if (!groups.has(parent)) { groups.set(parent, new Set()); }
      groups.get(parent).add(item);
    }
    let best = null;
    for (const [parent, set] of groups) {
      if (set.size >= 2 && (!best || set.size > best.items.length)) {
        best = { kind: 'list', parent, items: Array.from(set) };
      }
    }
    return best;
  }

  function groupCurrent(g) {
    if (g.kind === 'select') { return g.sel.selectedIndex; }
    let idx = g.items.findIndex(isActive);
    if (idx < 0) { idx = g.items.findIndex((it) => !!it.querySelector('[class*="active" i], [aria-selected="true"]')); }
    return idx;
  }

  function goGroup(g, i) {
    if (!(i >= 0 && i < g.items.length)) { return false; }
    if (g.kind === 'select') {
      g.sel.selectedIndex = i;
      g.sel.dispatchEvent(new Event('input', { bubbles: true }));
      g.sel.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }
    g.items[i].click();
    return true;
  }

  // Плеер Playerjs (на нём многие плееры, которые встраивают kinogo и похожие сайты): у него есть
  // свой API, им можно листать плейлист и переходить к серии по её номеру в плейлисте.
  function playerjs() {
    try {
      const w = window.wrappedJSObject;
      if (!w) { return null; }
      const list = w.pljssglobal;
      if (list && list.length) {
        for (let i = 0; i < list.length; i++) {
          if (list[i] && typeof list[i].api === 'function') { return list[i]; }
        }
      }
      if (w.player && typeof w.player.api === 'function') { return w.player; }
    } catch (e) { /* чужой объект */ }
    return null;
  }

  function playerjsEpisodes() {
    const pj = playerjs();
    if (!pj) { return null; }
    let pid = '';
    try { pid = String(pj.api('playlist_id') || ''); } catch (e) { return null; }
    if (!pid || pid === 'undefined') { return null; }           // одиночный файл, не сериал
    const m = pid.match(/-(\d{1,3})-(\d{1,4})$/);
    const call = (...args) => { try { pj.api(...args); return true; } catch (e) { return false; } };
    return {
      labels: null,
      cur: -1,
      pid,
      label: m ? m[2] + ' серия' + (Number(m[1]) > 1 ? ', ' + m[1] + ' сезон' : '') : '',
      canNext: true,
      canPrev: true,
      go: () => false,
      next: () => call('next'),
      prev: () => call('prev'),
      find: (id) => call('find', String(id)),
    };
  }

  // Что можно сделать с сериями в этом документе (фрейме плеера).
  function episodes(doc) {
    doc = doc || document;
    const g = findGroup(doc);
    const next = findButton(doc, NEXT_RE);
    const prev = findButton(doc, PREV_RE);
    if (!g && !next && !prev) { return playerjsEpisodes(); }
    const labels = g ? g.items.map((it) => {
      const n = epNumber(it.textContent);
      return n !== null ? String(n) : clean(it.textContent).slice(0, 12);
    }) : null;
    const cur = g ? groupCurrent(g) : -1;
    return {
      labels,
      cur,
      canNext: !!next || (!!g && cur >= 0 && cur < g.items.length - 1),
      canPrev: !!prev || (!!g && cur > 0),
      go(i) { return g ? goGroup(g, i) : false; },
      next() {
        if (g && cur >= 0 && cur < g.items.length - 1) { return goGroup(g, cur + 1); }
        if (next) { next.click(); return true; }
        return false;
      },
      prev() {
        if (g && cur > 0) { return goGroup(g, cur - 1); }
        if (prev) { prev.click(); return true; }
        return false;
      },
    };
  }

  self.ARSites = {
    page() { return ADAPTERS.find((a) => { try { return a.match(); } catch (e) { return false; } }); },
    episodes,
    frameCss,
    epNumber,
  };
})();
