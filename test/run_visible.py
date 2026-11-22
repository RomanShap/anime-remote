"""Visible-window check of the real F11 path (needs a real window in the foreground)."""
import os
import subprocess
import threading
import time

import run_test as T
from run_test import Marionette, api, cmd, state, wait_for, check, brief, wait_new_playing, frame_src

WIN = ("return { fs: window.fullScreen, inner: [innerWidth, innerHeight], screen: [screen.width, screen.height],"
       " domFs: !!document.fullscreenElement, theater: document.documentElement.classList.contains('ar-theater'),"
       " hidden: document.hidden };")


class Lost(Exception):
    pass


def win(m):
    w = m.js(WIN)
    if w["hidden"]:
        raise Lost("the test window got covered or minimized, so the page stopped taking commands")
    return w


def covers(w):
    return w["fs"] is True and w["inner"] == w["screen"]


def click(m, x, y):
    m.send("WebDriver:PerformActions", {"actions": [{"type": "pointer", "id": "m1", "parameters": {"pointerType": "mouse"},
           "actions": [{"type": "pointerMove", "duration": 0, "x": int(x), "y": int(y)},
                       {"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0}]}]})
    m.send("WebDriver:ReleaseActions")


def main():
    os.makedirs(T.PROFILE, exist_ok=True)
    with open(os.path.join(T.PROFILE, "user.js"), "w", encoding="utf-8") as f:
        f.write(T.PREFS)
    subprocess.Popen([T.FIREFOX, "-no-remote", "-profile", T.PROFILE, "-marionette", "about:blank"],
                     env=dict(os.environ, MOZ_REMOTE_ALLOW_SYSTEM_ACCESS="1"),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    m = Marionette(T.MPORT)
    stop = threading.Event()
    try:
        try:
            m.send("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {"pageLoadStrategy": "eager"}}})
        except RuntimeError:
            m.send("WebDriver:NewSession", {})
        m.send("WebDriver:SetTimeouts", {"pageLoad": 90000, "script": 30000})
        m.send("WebDriver:Navigate", {"url": T.PAGE})
        time.sleep(3)
        m.js(T.BRIDGE)
        threading.Thread(target=T.relay, args=(m, stop), daemon=True).start()
        m.js(T.script_text())
        ok, s, t = wait_for("online", lambda s: s.get("video"), 60)
        check("page online in a visible window", ok, brief(s))
        if not ok:
            return

        title = m.js("return document.title;")
        fg = {}
        for _ in range(4):
            fg = api("/api/key", {"key": "f11", "title": title, "dry": True})
            if fg.get("ok"):
                break
            time.sleep(2)
        check("server sees the Firefox window with this page in front", bool(fg.get("ok")), fg)
        if not fg.get("ok"):
            print("      -> test window is not in the foreground, F11 cannot be exercised here")
            return

        rect = m.js("var h = document.querySelector('.shortstoryHead h1'); h.scrollIntoView({block: 'center'});"
                    "var r = h.getBoundingClientRect(); return [r.left + 20, r.top + r.height / 2];")
        click(m, rect[0], rect[1])
        # a real click puts keyboard focus into the page, a synthetic one leaves it in the address bar
        m.send("Marionette:SetContext", {"value": "chrome"})
        m.js("gBrowser.selectedBrowser.focus();")
        m.send("Marionette:SetContext", {"value": "content"})

        src = frame_src(m)
        cmd("ep", 2)                                   # away from the last episode
        ok, i, t = wait_new_playing(m, src, 40)
        check("start: episode 3 playing", ok, "%.0fs %s" % (t, brief(state())))

        # A. full screen from the phone
        cmd("full")
        wait_for("full", lambda s: s.get("full"), 10)
        time.sleep(2.5)
        w = win(m)
        s = state()
        check("full from the phone: Firefox really goes full screen (F11)", covers(w) and w["theater"] and s.get("hint") == "", "%s %s" % (w, brief(s)))

        src = frame_src(m)
        cmd("next")
        ok, i, t = wait_new_playing(m, src, 40)
        time.sleep(1.5)
        w = win(m)
        check("next while full: still full screen and playing", ok and covers(w) and w["theater"] and state().get("current") == 3, "%.0fs %s" % (t, w))

        cmd("full")
        wait_for("unfull", lambda s: s.get("full") is False, 10)
        time.sleep(2.5)
        w = win(m)
        check("full off from the phone: Firefox leaves full screen", w["fs"] is False and not w["theater"], w)

        # B. viewer used the player's own full-screen button, then switches episode from the phone
        pos = m.js("""
          var f = document.querySelector('#player2 iframe'); f.scrollIntoView({block: 'center'});
          var r = f.getBoundingClientRect(), b = f.contentDocument.getElementById('fullscreen');
          f.contentDocument.getElementById('controls').style.display = 'block';
          var br = b.getBoundingClientRect();
          return [r.left + br.left + br.width / 2, r.top + br.top + br.height / 2];
        """)
        click(m, pos[0], pos[1])
        time.sleep(2)
        w = win(m)
        check("player's own full-screen button works in the test window", w["domFs"] is True, w)
        if w["domFs"]:
            src = frame_src(m)
            cmd("prev")
            ok, i, t = wait_new_playing(m, src, 40)
            time.sleep(3)
            w = win(m)
            s = state()
            check("switching episode from the site's own full screen: comes back full screen and plays",
                  ok and s.get("current") == 2 and not w["domFs"] and covers(w) and w["theater"] and s.get("hint") == "",
                  "%.0fs %s %s" % (t, w, brief(s)))
            cmd("full")
            wait_for("unfull", lambda s: s.get("full") is False, 10)
            time.sleep(2.5)
            w = win(m)
            check("full off afterwards: back to a normal window", w["fs"] is False and not w["theater"], w)
        cmd("pause")
        time.sleep(1)
    except Lost as e:
        print("ABORTED  " + str(e), flush=True)
    finally:
        stop.set()
        time.sleep(0.3)
        try:
            m.send("Marionette:Quit", {"flags": ["eForceQuit"]})
        except Exception as e:
            print("quit:", e)
        bad = [n for n, ok in T.RESULTS if not ok]
        print("\n%d checks, %d failed%s" % (len(T.RESULTS), len(bad), (": " + "; ".join(bad)) if bad else ""), flush=True)


if __name__ == "__main__":
    main()
