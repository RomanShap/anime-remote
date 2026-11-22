#!/usr/bin/env python3
"""Anime Remote: пульт для animevost.org с телефона.

Телефон открывает страницу с этого сервера и шлёт команды      POST /api/cmd
Скрипт в Firefox (anime-remote.user.js) забирает их            GET  /api/poll  (длинный опрос)
и сообщает, что сейчас играет                                  POST /api/state
Телефон читает это состояние                                   GET  /api/state
Скрипт просит нажать F11 в окне Firefox (полный экран)         POST /api/key

Где остановился: сервер сам запоминает для каждого аниме последнюю серию и время
(файл progress.json рядом с этим скриптом) и отдаёт список телефону вместе с состоянием.
Телефон просит вернуться к сохранённому месту                  POST /api/resume
или убрать аниме из списка                                     POST /api/forget
Скрипт при открытии страницы спрашивает, что сохранено         POST /api/hello

Только стандартная библиотека Python. Отвечает только адресам из локальной сети.
"""
import argparse
import ctypes
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))

CMD_TTL = 10.0     # команда старше 10 с уже не выполняется (вкладка была закрыта и т.п.)
POLL_WAIT = 20.0   # сколько держим длинный опрос
TAB_ALIVE = 8.0    # вкладка считается открытой, если состояние приходило не позже 8 с назад
MAX_BODY = 256 * 1024

MIN_WATCHED = 30   # серия считается начатой, когда просмотрено хотя бы 30 с
END_MARGIN = 90    # последние 90 с серии (титры) = досмотрел
MAX_TITLES = 50    # сколько аниме держим в списке
RESUME_TTL = 90.0  # сколько ждём, пока откроется страница, на которую просили вернуться

ACTIONS = {"next", "prev", "ep", "toggle", "play", "pause", "seek", "vol", "mute", "full", "autonext"}
ALLOWED_ORIGIN = re.compile(r"^https://([a-z0-9-]+\.)*(animevost\.org|vost\.pw)$")
SITE_URL = re.compile(r"^https://([a-z0-9-]+\.)*(animevost\.org|vost\.pw)/[^\s\"'<>\\]{1,300}$")
BROWSERS = {"firefox.exe", "firefox"}

STATIC = {
    "/": ("remote.html", "text/html; charset=utf-8"),
    "/index.html": ("remote.html", "text/html; charset=utf-8"),
    "/anime-remote.user.js": ("anime-remote.user.js", "text/javascript; charset=utf-8"),
}


class Hub:
    """Очередь команд и последнее состояние вкладки."""

    def __init__(self):
        self.cond = threading.Condition()
        self.seq = 0
        self.cmds = []          # [(seq, время, команда)]
        self.state = None
        self.state_at = 0.0
        self.pending = None     # (url, срок) страницы, к которой просили вернуться

    def push(self, cmd):
        with self.cond:
            self.seq += 1
            self.cmds.append((self.seq, time.monotonic(), cmd))
            del self.cmds[:-50]
            self.cond.notify_all()
            return self.seq

    def poll(self, since, wait):
        deadline = time.monotonic() + wait
        with self.cond:
            if since < 0 or since > self.seq:      # первый опрос или сервер перезапускали
                return {"seq": self.seq, "cmds": []}
            while self.seq <= since:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(left)
            now = time.monotonic()
            fresh = [c for s, t, c in self.cmds if s > since and now - t < CMD_TTL]
            return {"seq": self.seq, "cmds": fresh}

    def set_state(self, state):
        with self.cond:
            self.state = state
            self.state_at = time.monotonic()

    def get_state(self):
        with self.cond:
            age = time.monotonic() - self.state_at if self.state else None
            online = age is not None and age < TAB_ALIVE
            return {"online": online, "state": self.state if online else None}

    # страница, к которой просили вернуться, но она ещё не открыта (или открывается)
    def expect(self, url):
        with self.cond:
            self.pending = (url, time.monotonic() + RESUME_TTL)

    def take_pending(self, url):
        with self.cond:
            pending = self.pending
            if pending and pending[0] == url and time.monotonic() < pending[1]:
                self.pending = None
                return True
            return False


class Progress:
    """Где остановился: для каждого аниме последняя серия и время. Хранится в JSON-файле."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.items = {}
        self.written = {}        # url -> время серии при последней записи на диск
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.items = {k: v for k, v in data.items()
                              if isinstance(v, dict) and SITE_URL.match(k) and isinstance(v.get("ep"), int)}
        except (OSError, ValueError):
            pass

    def _write(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.items, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"Не удалось записать {self.path}: {e}")

    def track(self, st):
        """Запомнить место по очередному состоянию вкладки. Меньше 30 с просмотра не считается."""
        url, ep, t, dur = st.get("url"), st.get("current"), st.get("time"), st.get("duration")
        if not (isinstance(url, str) and SITE_URL.match(url)) or not st.get("video"):
            return
        if isinstance(ep, bool) or not isinstance(ep, int) or ep < 0:
            return
        if isinstance(t, bool) or not isinstance(t, (int, float)) or t < MIN_WATCHED:
            return
        paused = bool(st.get("paused"))
        with self.lock:
            old = self.items.get(url)
            same = old is not None and old.get("ep") == ep
            if paused and not same:          # другую серию только открыл, но не смотрел
                return
            if same and old.get("time") == int(t):
                return
            eps = st.get("episodes") if isinstance(st.get("episodes"), list) else []
            self.items[url] = {
                "title": str(st.get("title") or "")[:200],
                "url": url,
                "ep": ep,
                "epName": str(eps[ep])[:40] if ep < len(eps) else "",
                "time": int(t),
                "duration": int(dur) if isinstance(dur, (int, float)) and not isinstance(dur, bool) else 0,
                "total": len(eps),
                "updated": int(time.time()),
            }
            if len(self.items) > MAX_TITLES:
                for k in sorted(self.items, key=lambda k: self.items[k]["updated"])[:-MAX_TITLES]:
                    del self.items[k]
            if not same or paused or abs(int(t) - self.written.get(url, -999)) >= 10:
                self.written[url] = int(t)
                self._write()

    def get(self, url):
        with self.lock:
            item = self.items.get(url)
            return dict(item) if item else None

    def forget(self, url):
        with self.lock:
            if self.items.pop(url, None) is None:
                return False
            self.written.pop(url, None)
            self._write()
            return True

    def listing(self):
        with self.lock:
            rows = sorted(self.items.values(), key=lambda v: v.get("updated", 0), reverse=True)
            return [dict(v, finished=is_finished(v)) for v in rows]


def is_finished(item):
    dur = item.get("duration") or 0
    return dur > 0 and item.get("time", 0) >= dur - END_MARGIN


HUB = Hub()
PROGRESS = None      # создаётся в main()
OPEN_PAGES = True    # --no-open выключает открытие страниц в браузере (для тестов)


# ---------------------------------------------------------------- F11 в окне Firefox

def key_backend():
    """Чем нажимать F11: 'win' (Windows), 'xdotool' (Linux с X11, например Steam Deck) или ''."""
    if os.name == "nt":
        return "win"
    if os.environ.get("DISPLAY") and shutil.which("xdotool"):
        return "xdotool"
    return ""


def xdotool(*args):
    try:
        out = subprocess.run(["xdotool", *args], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def foreground_window():
    """(имя exe, заголовок) окна на переднем плане, ('', '') если узнать не удалось."""
    backend = key_backend()
    if backend == "xdotool":
        # Firefox из Flatpak живёт в своей песочнице и PID окна там чужой, поэтому узнаём его по заголовку.
        title = xdotool("getactivewindow", "getwindowname") or ""
        return ("firefox" if norm(title).endswith("mozilla firefox") else ""), title
    if backend != "win":
        return "", ""
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "", ""
    buf = ctypes.create_unicode_buffer(1024)
    user32.GetWindowTextW(hwnd, buf, 1024)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    handle = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        size = wintypes.DWORD(1024)
        path = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            exe = os.path.basename(path.value).lower()
        kernel32.CloseHandle(handle)
    return exe, buf.value


def norm(text):
    return " ".join(str(text or "").split()).lower()


def press_f11(page_title, dry=False):
    """Нажать F11, но только если впереди окно Firefox с этой самой страницей.

    Где нажать нечем (Linux без xdotool, Wayland, игровой режим Steam Deck), отвечаем 'unsupported':
    плеер всё равно растянут на всё окно, а в игровом режиме окно и так во весь экран.
    """
    backend = key_backend()
    if not backend:
        return {"ok": False, "reason": "unsupported"}
    exe, title = foreground_window()
    if backend == "xdotool" and not title:
        return {"ok": False, "reason": "unsupported"}     # xdotool не видит окна (например, Wayland)
    if exe not in BROWSERS:
        return {"ok": False, "reason": "not-foreground", "foreground": exe}
    want = norm(page_title)[:40]
    if not want or want not in norm(title):
        return {"ok": False, "reason": "other-tab", "foreground": exe}
    if not dry:
        if backend == "win":
            user32 = ctypes.WinDLL("user32")
            user32.keybd_event(0x7A, 0, 0, 0)        # VK_F11 нажать
            time.sleep(0.05)
            user32.keybd_event(0x7A, 0, 0x0002, 0)   # отпустить
        elif xdotool("key", "--clearmodifiers", "F11") is None:
            return {"ok": False, "reason": "unsupported"}
    return {"ok": True, "foreground": exe}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AnimeRemote/1.0"

    def log_message(self, *args):   # не шумим в консоли
        pass

    # -- ответы
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        try:
            self.send_response(code)
            origin = self.headers.get("Origin")
            if origin and ALLOWED_ORIGIN.match(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Anime-Remote")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Vary", "Origin")
            if body:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)
        except (ConnectionError, OSError):
            self.close_connection = True

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    # -- проверки
    def _from_lan(self):
        try:
            ip = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return ip.is_loopback or ip.is_private or ip.is_link_local

    def _api_ok(self):
        """API доступно только с заголовком X-Anime-Remote: чужой сайт его без разрешения не пошлёт."""
        if self.headers.get("X-Anime-Remote") != "1":
            self._json({"error": "missing X-Anime-Remote header"}, 403)
            return False
        return True

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            self._json({"error": "bad length"}, 413)
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._json({"error": "bad json"}, 400)
            return None
        if not isinstance(data, dict):
            self._json({"error": "object expected"}, 400)
            return None
        return data

    # -- методы
    def do_OPTIONS(self):
        if not self._from_lan():
            return self._send(403)
        self._send(204)

    def do_GET(self):
        if not self._from_lan():
            return self._send(403)
        url = urlparse(self.path)
        if url.path in STATIC:
            name, ctype = STATIC[url.path]
            try:
                with open(os.path.join(HERE, name), "rb") as f:
                    return self._send(200, f.read(), ctype)
            except OSError:
                return self._send(404, b"file missing", "text/plain; charset=utf-8")
        if url.path == "/api/state":
            return self._api_ok() and self._json(dict(HUB.get_state(), progress=PROGRESS.listing()))
        if url.path == "/api/poll":
            if not self._api_ok():
                return
            try:
                since = int(parse_qs(url.query).get("since", ["-1"])[0])
            except ValueError:
                since = -1
            return self._json(HUB.poll(since, POLL_WAIT))
        self._send(404, b"not found", "text/plain; charset=utf-8")

    do_HEAD = do_GET

    def do_POST(self):
        if not self._from_lan():
            return self._send(403)
        path = urlparse(self.path).path
        if path not in ("/api/cmd", "/api/state", "/api/key", "/api/hello", "/api/resume", "/api/forget"):
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        data = self._body()
        if data is None or not self._api_ok():
            return
        if path == "/api/hello":       # страница открылась: что по ней сохранено и просили ли вернуться
            url = data.get("url")
            if not (isinstance(url, str) and SITE_URL.match(url)):
                return self._json({"error": "bad url"}, 400)
            return self._json({"saved": PROGRESS.get(url), "resume": HUB.take_pending(url)})
        if path == "/api/forget":
            return self._json({"ok": PROGRESS.forget(str(data.get("key") or ""))})
        if path == "/api/resume":
            item = PROGRESS.get(str(data.get("key") or ""))
            if not item:
                return self._json({"error": "unknown title"}, 404)
            tab = HUB.get_state()
            tab_url = (tab["state"] or {}).get("url") if tab["online"] else None
            if tab_url == item["url"]:                 # эта страница уже открыта
                HUB.push({"action": "resume", "value": item})
                return self._json({"ok": True, "how": "tab"})
            HUB.expect(item["url"])
            if tab_url:                                # открыто другое аниме: переходим в той же вкладке
                HUB.push({"action": "open", "value": item["url"]})
                return self._json({"ok": True, "how": "navigate"})
            if not OPEN_PAGES:
                return self._json({"ok": True, "how": "skipped"})
            try:
                webbrowser.open(item["url"], new=2)    # браузер по умолчанию, новая вкладка
            except Exception as e:                     # noqa: BLE001 - любая ошибка запуска браузера
                return self._json({"ok": False, "how": "browser", "error": str(e)})
            return self._json({"ok": True, "how": "browser"})
        if path == "/api/cmd":
            action = data.get("action")
            if action not in ACTIONS:
                return self._json({"error": "unknown action"}, 400)
            value = data.get("value")
            if not isinstance(value, (int, float, bool, type(None))):
                return self._json({"error": "bad value"}, 400)
            return self._json({"ok": True, "seq": HUB.push({"action": action, "value": value})})
        if path == "/api/state":
            HUB.set_state(data)
            PROGRESS.track(data)
            return self._json({"ok": True})
        if data.get("key") != "f11":
            return self._json({"error": "unknown key"}, 400)
        self._json(press_f11(data.get("title"), dry=bool(data.get("dry"))))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return   # телефон закрыл соединение, это нормально
        super().handle_error(request, client_address)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))   # ничего не отправляет, только выбирает сетевой интерфейс
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    global PROGRESS, OPEN_PAGES
    try:
        sys.stdout.reconfigure(errors="replace", line_buffering=True)   # адрес виден и в journalctl
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Anime Remote: пульт для animevost.org с телефона")
    ap.add_argument("--port", type=int, default=int(os.environ.get("ANIME_REMOTE_PORT", "8765")))
    ap.add_argument("--data", default=os.path.join(HERE, "progress.json"), help="файл, где хранится, на чём остановился")
    ap.add_argument("--no-open", action="store_true", help="не открывать страницы в браузере (для тестов)")
    args = ap.parse_args()
    PORT = args.port
    PROGRESS = Progress(args.data)
    OPEN_PAGES = not args.no_open
    try:
        httpd = Server(("0.0.0.0", PORT), Handler)
    except OSError as e:
        print(f"Не удалось занять порт {PORT}: {e}")
        print("Скорее всего, пульт уже запущен в другом окне.")
        return 1
    print("Anime Remote запущен.")
    print()
    print(f"  Пульт с телефона (тот же Wi-Fi):  http://{lan_ip()}:{PORT}")
    print(f"  Скрипт для Firefox:               http://127.0.0.1:{PORT}/anime-remote.user.js")
    print(f"  Где остановился, хранится в:      {args.data}")
    print()
    print("Окно можно свернуть. Закрыть окно или Ctrl+C = выключить пульт.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
