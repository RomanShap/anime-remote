# Anime Remote

Пульт для [animevost.org](https://animevost.org) с телефона: переключать серии, пауза, перемотка, звук, полный экран, и пульт сам помнит, на какой серии и минуте ты остановился.

*A phone remote for animevost.org: a small Python server on the computer, a page for the phone, and a userscript for Firefox. Python standard library only, no install.*

## Как это устроено

- `server.py` — маленький сервер на компьютере (порт 8765). Телефон открывает его страницу и шлёт команды, скрипт в браузере их выполняет. Отвечает только устройствам из домашней сети.
- `remote.html` — страница пульта для телефона.
- `anime-remote.user.js` — скрипт для Firefox (ставится через Violentmonkey или Tampermonkey), который нажимает кнопки на странице animevost.
- Где остановился, сервер хранит в `progress.json` рядом с собой. Серия запоминается, когда идёт дольше 30 секунд. Последние 90 секунд (титры) считаются досмотром, и «продолжить» включит следующую серию.

## Steam Deck

Всё делается в режиме рабочего стола (Steam → Питание → Переключиться на рабочий стол).

1. **Firefox:** поставь его из Discover, если его ещё нет.
2. **Скачать пульт:** открой Konsole и выполни
   ```bash
   git clone https://github.com/RomanShap/anime-remote.git ~/anime-remote
   ```
   Или на странице репозитория нажми Code → Download ZIP и распакуй в домашнюю папку как `anime-remote`.
3. **Запустить:**
   ```bash
   bash ~/anime-remote/start.sh
   ```
   В окне появится адрес пульта для телефона, например `http://192.168.0.42:8765`. Окно не закрывай, пока смотришь.
4. **Скрипт в Firefox:** поставь дополнение Violentmonkey, потом открой `http://127.0.0.1:8765/anime-remote.user.js` и нажми «Установить».
5. **Телефон** (тот же Wi‑Fi): открой адрес из окна и добавь страницу на главный экран.

Перед первым просмотром один раз кликни по странице аниме на Deck: без этого Firefox не даёт запускать видео, и пульт покажет подсказку. Дальше всё с телефона.

**Полный экран на Deck.** Плеер растягивается на всё окно Firefox. Чтобы спряталась и рамка окна, в режиме рабочего стола включи полный экран в самом Firefox (меню ☰ → значок ⤢ в строке «Масштаб»), или поставь `xdotool`, тогда пульт нажимает F11 сам. В игровом режиме Firefox и так во весь экран.

**Обновить:** `cd ~/anime-remote && git pull`, потом перезапусти `start.sh`. Если менялся скрипт, открой ссылку из шага 4 ещё раз.

### Запуск вместе с Deck (по желанию)

Чтобы пульт стартовал сам, в том числе в игровом режиме:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/anime-remote.service <<'EOF'
[Unit]
Description=Anime Remote

[Service]
ExecStart=/usr/bin/python3 %h/anime-remote/server.py
Restart=on-failure

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now anime-remote.service
```

Адрес для телефона тогда смотри командой `journalctl --user -u anime-remote -n 20`. Выключить: `systemctl --user disable --now anime-remote.service`.

## Windows

Нужен Python 3. Запусти `Anime Remote.bat`, дальше как в шагах 4–5 выше. Полный экран (F11) пульт включает сам, если окно Firefox с аниме активно.

## Если телефон не подключается

- Телефон и компьютер должны быть в одной сети Wi‑Fi (гостевая сеть роутера часто отделена).
- Адрес мог смениться после перезагрузки роутера: актуальный всегда написан в окне сервера.
- На Windows при первом запуске разреши Python доступ в сеть.

## Тесты

`test/` — проверки в отдельном Firefox через Marionette, твой браузер не трогают. Запускай их против отдельного экземпляра сервера, чтобы не задеть свой список:

```bash
python server.py --port 8766 --no-open --data test/progress-test.json
AR_PORT=8766 python test/run_test.py
AR_PORT=8766 python test/run_progress.py
```

`run_visible.py` открывает видимое окно на ~40 секунд (проверка F11), не трогай мышь, пока он идёт.
