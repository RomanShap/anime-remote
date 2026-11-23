'use strict';
/* global qrcode */

const api = browser;
const $ = (id) => document.getElementById(id);
let currentLink = '';

const HINTS = {
  autoplay: 'Firefox не дал включить видео без клика. Кликни один раз по странице с видео или разреши автовоспроизведение: значок слева в адресной строке → «Автовоспроизведение» → «Разрешить аудио и видео».',
  muted: 'Видео идёт без звука: Firefox не даёт включить звук без клика. Кликни по странице один раз или разреши автовоспроизведение для этого сайта (значок слева в адресной строке).',
};

function drawQr(text) {
  const qr = qrcode(0, 'M');
  qr.addData(text);
  qr.make();
  const n = qr.getModuleCount();
  const canvas = $('qr');
  const quiet = 3;
  const cell = Math.floor(canvas.width / (n + quiet * 2));
  const size = cell * (n + quiet * 2);
  const off = Math.floor((canvas.width - size) / 2);
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#000000';
  for (let r = 0; r < n; r++) {
    for (let c = 0; c < n; c++) {
      if (qr.isDark(r, c)) { ctx.fillRect(off + (c + quiet) * cell, off + (r + quiet) * cell, cell, cell); }
    }
  }
}

function setLink(link) {
  if (link === currentLink) { return; }
  currentLink = link;
  $('linkText').value = link;
  drawQr(link);
}

async function refresh() {
  const s = await api.runtime.sendMessage({ k: 'popup' });
  if (!s) { return; }
  setLink(s.link);
  if (document.activeElement !== $('page')) { $('page').value = s.page; }
  if (document.activeElement !== $('relay')) { $('relay').value = s.relay; }

  const relayOk = s.relayState === 'open';
  $('relayDot').className = 'dot ' + (relayOk ? 'ok' : 'warn');
  $('relayText').textContent = relayOk ? 'Готов к подключению' : 'Нет связи с интернетом, пробую ещё';

  const direct = s.phones.filter((m) => m === 'p2p').length;
  const viaRelay = s.phones.filter((m) => m === 'relay').length;
  $('phoneDot').className = 'dot ' + (direct ? 'ok' : viaRelay ? 'warn' : '');
  $('phoneText').textContent = direct ? 'Телефон подключён напрямую' + (direct > 1 ? ' (' + direct + ')' : '')
    : viaRelay ? 'Телефон подключён через интернет' : 'Телефон не подключён';

  $('nowRow').hidden = !s.title;
  $('now').textContent = s.title || '';
  $('hint').hidden = !HINTS[s.hint];
  $('hint').textContent = HINTS[s.hint] || '';
}

$('copy').onclick = async () => {
  try {
    await navigator.clipboard.writeText(currentLink);
    $('copy').textContent = 'Скопировано';
  } catch (e) {
    $('copy').textContent = 'Не вышло';
  }
  setTimeout(() => { $('copy').textContent = 'Скопировать ссылку'; }, 1500);
};

$('open').onclick = () => { api.tabs.create({ url: currentLink }); window.close(); };

// В окне расширения confirm() не работает: просим нажать второй раз.
let resetArmed = 0;
$('reset').onclick = async () => {
  if (Date.now() - resetArmed > 4000) {
    resetArmed = Date.now();
    $('reset').textContent = 'Нажми ещё раз: старая ссылка перестанет работать';
    setTimeout(() => { $('reset').textContent = 'Новая ссылка (старая перестанет работать)'; }, 4000);
    return;
  }
  resetArmed = 0;
  const r = await api.runtime.sendMessage({ k: 'reset' });
  if (r) { setLink(r.link); }
  $('reset').textContent = 'Готово: подключи телефон заново';
};

$('save').onclick = async () => {
  const r = await api.runtime.sendMessage({ k: 'settings', page: $('page').value.trim(), relay: $('relay').value.trim() });
  if (r) { setLink(r.link); }
  refresh();
};

refresh();
setInterval(refresh, 1000);
