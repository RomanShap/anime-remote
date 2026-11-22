"""Headless check of "where I stopped" on the real site.

Run against a separate server instance so the real list is not touched:
    python server.py --port 8766 --no-open --data test/progress-test.json
    set AR_PORT=8766 && python test/run_progress.py
"""
import os
import subprocess
import threading
import time

import run_test as T
from run_test import Marionette, api, cmd, state, wait_for, check, brief, wait_new_playing, frame_src

TITLE_B = "https://animevost.org/tip/tv/4006-tensei-kizoku-kantei-skill-de-nariagaru-3rd-season.html"


def progress(url):
    for p in api("/api/state")["progress"]:
        if p["url"] == url:
            return p
    return None


def wait_saved(url, pred, timeout=40):
    t0 = time.time()
    p = None
    while time.time() - t0 < timeout:
        p = progress(url)
        try:
            if p and pred(p):
                return True, p
        except Exception:
            pass
        time.sleep(0.5)
    return False, p


def inject(m):
    m.js(T.BRIDGE)
    m.js(T.script_text())


def click_title(m):
    """One real click on neutral text: Firefox needs it before it lets a page start playback."""
    rect = m.js("var h = document.querySelector('.shortstoryHead h1'); h.scrollIntoView({block: 'center'});"
                "var r = h.getBoundingClientRect(); return [r.left + 20, r.top + r.height / 2];")
    m.send("WebDriver:PerformActions", {"actions": [{"type": "pointer", "id": "m1", "parameters": {"pointerType": "mouse"},
           "actions": [{"type": "pointerMove", "duration": 0, "x": int(rect[0]), "y": int(rect[1])},
                       {"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0}]}]})
    m.send("WebDriver:ReleaseActions")


def playing_at(ep, near=None, tol=25):
    def pred(s):
        ok = s.get("current") == ep and s.get("video") and not s.get("paused") and s.get("time", 0) >= 1
        return ok and (near is None or abs(s.get("time", 0) - near) <= tol)
    return pred


def main():
    for p in api("/api/state")["progress"]:
        api("/api/forget", {"key": p["url"]})
    os.makedirs(T.PROFILE, exist_ok=True)
    with open(os.path.join(T.PROFILE, "user.js"), "w", encoding="utf-8") as f:
        f.write(T.PREFS)
    env = dict(os.environ, MOZ_HEADLESS_WIDTH="1600", MOZ_HEADLESS_HEIGHT="900")
    subprocess.Popen([T.FIREFOX, "-no-remote", "-profile", T.PROFILE, "-marionette", "-headless", "about:blank"],
                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        threading.Thread(target=T.relay, args=(m, stop), daemon=True).start()
        inject(m)
        url = m.js("return location.origin + location.pathname;")
        ok, s, t = wait_for("online", lambda s: s.get("video") and s.get("url") == url, 60)
        check("page online and reports its address", ok, {"url": s.get("url"), **brief(s)})
        if not ok:
            return
        n = len(s["episodes"])
        click_title(m)

        # 1. an episode that really plays is remembered
        src = frame_src(m)
        cmd("ep", 1)
        ok, i, t = wait_new_playing(m, src, 60)
        check("episode 2 plays", ok, i)
        time.sleep(3)
        check("nothing remembered in the first seconds", progress(url) is None, progress(url))
        cmd("seek", 40)
        ok, p = wait_saved(url, lambda p: p["ep"] == 1 and p["time"] >= 35)
        check("after 30 s the episode and time are remembered", ok, p)

        # 2. switching: the old player's time must not be credited to the new episode
        src = frame_src(m)
        cmd("ep", 3)
        bad = []
        t0 = time.time()
        while time.time() - t0 < 6:
            p, s = progress(url), state()
            if p and p["ep"] == 3:
                bad.append(("saved", p["time"]))
            if s.get("current") == 3 and s.get("video") and s.get("time", 0) > 30:
                bad.append(("state", s.get("time")))
            time.sleep(0.2)
        check("switching does not carry the old episode's time over", not bad and progress(url)["ep"] == 1, bad[:3] or progress(url))
        ok, i, t = wait_new_playing(m, src, 60)
        cmd("seek", 40)
        ok, p = wait_saved(url, lambda p: p["ep"] == 3 and p["time"] >= 35)
        check("the new episode takes over once it has played 30 s", ok, p)

        # 3. an episode only glanced at does not take over
        src = frame_src(m)
        cmd("ep", 4)
        ok, i, t = wait_new_playing(m, src, 60)
        cmd("pause")
        time.sleep(6)
        p = progress(url)
        check("an episode glanced at for a few seconds does not take over", p["ep"] == 3, (p["ep"], p["time"], brief(state())))

        # 4. resume from the phone while the page is open
        want = progress(url)["time"]
        r = api("/api/resume", {"key": url})
        ok, s, t = wait_for("resume", playing_at(3, want), 60)
        check("resume: back on the remembered episode at the remembered time (%d s)" % want, r.get("how") == "tab" and ok, "%.0fs %s" % (t, brief(s)))

        # 5. same, but the site's own memory of the place is gone
        time.sleep(2)
        cmd("pause")
        want = wait_saved(url, lambda p: p["ep"] == 3, 5)[1]["time"]
        src = frame_src(m)
        cmd("ep", 5)
        ok, i, t = wait_new_playing(m, src, 60)
        cmd("pause")
        gone = m.js("""
          var e = document.querySelectorAll('#items .epizode')[3];
          var id = (e.getAttribute('onclick') || '').match(/\\((\\d+),/)[1];
          var had = localStorage.getItem('vost_pos_' + id) !== null;
          localStorage.removeItem('vost_pos_' + id);
          return [had, localStorage.getItem('vost_pos_' + id) === null];
        """)
        r = api("/api/resume", {"key": url})
        ok, s, t = wait_for("resume without site memory", playing_at(3, want), 60)
        check("resume when the site forgot the place: still lands at %d s" % want, gone[1] and ok, "site had it: %s, %.0fs %s" % (gone[0], t, brief(s)))

        # 6. a finished episode resumes at the next one
        m.js("var v = document.querySelector('#player2 iframe').contentDocument.querySelector('video'); v.currentTime = v.duration - 60;")
        ok, p = wait_saved(url, lambda p: p["ep"] == 3 and p["finished"], 30)
        check("an episode in its last 90 s is marked finished", ok, p)
        src = frame_src(m)
        cmd("ep", 0)
        wait_new_playing(m, src, 60)
        cmd("pause")
        time.sleep(1)
        r = api("/api/resume", {"key": url})
        ok, s, t = wait_for("resume after finished", playing_at(4), 60)
        check("resume after a finished episode starts the next one", ok and s.get("time", 999) < 120, "%.0fs %s" % (t, brief(s)))
        cmd("seek", 40)
        ok, p = wait_saved(url, lambda p: p["ep"] == 4 and p["time"] >= 35)
        check("...and that one is remembered in turn", ok, p)
        saved_a = p

        # 7. another title: the open tab is sent to its page and continues there
        api("/api/state", {"url": TITLE_B, "title": "B", "episodes": ["1 серия", "2 серия"], "current": 1, "video": True,
                           "paused": False, "time": 100, "duration": 1400})
        wait_for("real tab is the open one again", lambda s: s.get("url") == url, 15)
        r = api("/api/resume", {"key": TITLE_B})
        t0 = time.time()
        now = ""
        while time.time() - t0 < 25:
            try:
                now = m.js("return location.origin + location.pathname;")
            except Exception:
                now = ""
            if now == TITLE_B:
                break
            time.sleep(0.5)
        check("resume of another title: the tab navigates to its page", r.get("how") == "navigate" and now == TITLE_B, (r, now))
        if now == TITLE_B:
            time.sleep(4)
            inject(m)
            ok, s, t = wait_for("B online", lambda s: s.get("url") == TITLE_B and len(s.get("episodes", [])) > 0, 30)
            nb = len(s.get("episodes", []))
            target = min(1, nb - 1)
            ok, s, t = wait_for("B episode", lambda s: s.get("url") == TITLE_B and s.get("current") == target, 30)
            check("the opened page selects the remembered episode", ok, "%d episodes %s" % (nb, brief(s)))
            ok, s, t = wait_for("B blocked or playing", lambda s: s.get("hint") == "autoplay" or not s.get("paused"), 60)
            check("on a freshly opened page Firefox wants one click (hint) or just plays", ok, brief(s))
            click_title(m)
            cmd("play")
            ok, s, t = wait_for("B at place", lambda s: s.get("url") == TITLE_B and playing_at(target, 100)(s), 60)
            check("after the click it continues at the remembered time (100 s)", ok, "%.0fs %s" % (t, brief(s)))
            cmd("pause")

        # 8. the site forgot the episode (cookies gone): the page goes back to it, without starting playback
        m.send("WebDriver:Navigate", {"url": T.PAGE})
        time.sleep(2)
        m.send("WebDriver:DeleteAllCookies")
        m.send("WebDriver:Navigate", {"url": T.PAGE})
        time.sleep(3)
        first = m.js("return Array.from(document.querySelectorAll('#items .epizode')).findIndex(function (e) { return e.classList.contains('active'); });")
        inject(m)
        ok, s, t = wait_for("back to saved", lambda s: s.get("url") == url and s.get("current") == saved_a["ep"] and s.get("video"), 45)
        time.sleep(3)
        s = state()
        check("site forgot (opened episode %d): page returns to the remembered episode %d, paused, no hint" % (first + 1, saved_a["ep"] + 1),
              ok and first == 0 and s.get("paused") and s.get("hint") == "", brief(s))
        check("the remembered place survives the reload", progress(url)["ep"] == saved_a["ep"] and progress(url)["time"] >= saved_a["time"], progress(url))
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
