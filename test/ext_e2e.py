"""End-to-end test of the Firefox extension and the phone page, in two throwaway headless Firefoxes.

  A = "computer": Firefox with the extension installed temporarily, opens a real streaming page.
  B = "phone":    Firefox opening the phone page from the pairing link.
Signalling goes through tools/mock_relay.py (a local ntfy stand-in), the phone page is served
from docs/ by a local static server, so the real ntfy.sh is not used.

    python tools/mock_relay.py --port 8777
    python -m http.server 8778 --directory docs
    python test/ext_e2e.py [animevost|relay|kinogo|all]
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from run_test import Marionette  # noqa: E402

FIREFOX = os.environ.get("FIREFOX") or (r"C:\Program Files\Mozilla Firefox\firefox.exe" if os.name == "nt" else "firefox")
EXT_DIR = os.path.join(ROOT, "extension")
RELAY = "http://127.0.0.1:8777"
PAGE = "http://127.0.0.1:8778/"
ANIMEVOST = "https://animevost.org/tip/tv/4064-hotel-inhumans-2.html"
KINOGO = "https://kinogo.pro/16326-chernyj-klever-smotret-onlajn-besplatno.html"

PREFS = """
user_pref("marionette.port", %d);
user_pref("media.volume_scale", "0.0");
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("extensions.autoDisableScopes", 0);
user_pref("xpinstall.signatures.required", false);
%s
"""

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(("PASS  " if ok else "FAIL  ") + name + ("  | " + str(detail)[:600] if detail != "" else ""), flush=True)


def launch(name, port, extra_prefs=""):
    prof = os.path.join(HERE, "profile-" + name)
    shutil.rmtree(prof, ignore_errors=True)
    os.makedirs(prof)
    with open(os.path.join(prof, "user.js"), "w", encoding="utf-8") as f:
        f.write(PREFS % (port, extra_prefs))
    env = dict(os.environ, MOZ_HEADLESS_WIDTH="1600", MOZ_HEADLESS_HEIGHT="900", MOZ_REMOTE_ALLOW_SYSTEM_ACCESS="1")
    subprocess.Popen([FIREFOX, "-no-remote", "-profile", prof, "-marionette", "-headless", "about:blank"],
                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    m = Marionette(port)
    m.send("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {"pageLoadStrategy": "eager"}}})
    m.send("WebDriver:SetTimeouts", {"pageLoad": 90000, "script": 30000})
    m.profile = prof
    return m


def quit_(m):
    try:
        m.send("Marionette:Quit", {"flags": ["eForceQuit"]})
    except Exception:  # noqa: BLE001
        pass


def relay_posts():
    with urllib.request.urlopen(RELAY + "/_stats", timeout=5) as r:
        return json.loads(r.read().decode())["posts"]


def wait(fn, timeout=30, every=0.5):
    """Poll fn() until it returns (True, value). Returns (ok, value, seconds)."""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            r = fn()
            if isinstance(r, tuple) and len(r) == 2:
                last = r[1]
                if r[0]:
                    return True, last, time.time() - t0
            elif r:
                return True, r, time.time() - t0
        except Exception as e:  # noqa: BLE001
            last = {"error": str(e)[:200]}
        time.sleep(every)
    return False, last if isinstance(last, dict) else {"last": last}, timeout


def click_at(m, x, y):
    m.send("WebDriver:PerformActions", {"actions": [{"type": "pointer", "id": "m1", "parameters": {"pointerType": "mouse"},
           "actions": [{"type": "pointerMove", "duration": 0, "x": int(x), "y": int(y)},
                       {"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0}]}]})
    m.send("WebDriver:ReleaseActions")


# ------------------------------------------------------------------ computer side

def install_extension(a):
    r = a.send("Addon:Install", {"path": EXT_DIR, "temporary": True})
    addon_id = r.get("value", r) if isinstance(r, dict) else r
    a.send("Marionette:SetContext", {"value": "chrome"})
    uuids = json.loads(a.js("return Services.prefs.getStringPref('extensions.webextensions.uuids', '{}');"))
    a.send("Marionette:SetContext", {"value": "content"})
    return addon_id, uuids.get(addon_id)


def configure(a, uuid):
    """Point the extension at the local relay and phone page through its own popup, then read the link."""
    a.send("WebDriver:Navigate", {"url": "moz-extension://%s/popup.html" % uuid})
    time.sleep(1.5)
    a.js("document.getElementById('page').value = arguments[0];"
         "document.getElementById('relay').value = arguments[1];"
         "document.getElementById('save').click();", [PAGE, RELAY])
    ok, r, t = wait(lambda: (lambda x: (x[0].startswith(PAGE) and x[1] == "Готов к подключению", x))(
        a.js("return [document.getElementById('linkText').value, document.getElementById('relayText').textContent];")), 20)
    vals = r if ok else None
    return (vals[0] if vals else None), (vals[1:] if vals else r)


def av_video(a):
    return a.js("""
      var f = document.querySelector('#player2 iframe'); if (!f) { return null; }
      var d = f.contentDocument, v = d && d.querySelector('video');
      var cur = [].slice.call(document.querySelectorAll('#items .epizode')).findIndex(function (e) { return e.classList.contains('active'); });
      return { src: f.getAttribute('src'), cur: cur, has: !!v, paused: v ? v.paused : true, t: v ? v.currentTime : -1,
               dur: v && isFinite(v.duration) ? v.duration : 0, vol: v ? v.volume : null, muted: v ? v.muted : null };
    """)


def theater_geometry(a):
    return a.js("""
      var f = document.querySelector('#player2 iframe'), r = f.getBoundingClientRect();
      var d = f.contentDocument, v = d.querySelector('video'), vr = v ? v.getBoundingClientRect() : {width: 0, height: 0};
      var pts = [[5, 5], [innerWidth / 2, innerHeight / 2], [innerWidth - 5, innerHeight - 5]];
      return { win: [innerWidth, innerHeight], frame: [r.left, r.top, r.width, r.height],
               top: pts.map(function (p) { return document.elementFromPoint(p[0], p[1]) === f; }),
               cls: document.documentElement.className, inner: d.documentElement.className,
               video: [Math.round(vr.width), Math.round(vr.height)] };
    """)


# ------------------------------------------------------------------ phone side

PHONE = """
var $ = function (id) { return document.getElementById(id); };
var eps = [].slice.call(document.querySelectorAll('#eps button'));
return { conn: $('connText').textContent, player: $('player').classList.contains('show'), title: $('title').textContent,
         ep: $('ep').textContent, time: $('time').textContent, note: $('note').textContent,
         playing: $('ticon').getAttribute('d').indexOf('M6') === 0, full: $('full').getAttribute('aria-pressed'),
         auto: $('auto').getAttribute('aria-pressed'), next: !$('next').disabled, prev: !$('prev').disabled,
         eps: eps.map(function (b) { return b.textContent; }), cur: eps.findIndex(function (b) { return b.classList.contains('cur'); }),
         saved: $('saved').textContent, cont: [].slice.call(document.querySelectorAll('#cont .item')).map(function (r) { return r.textContent; }),
         vol: $('vol').textContent };
"""


def phone(b):
    return b.js(PHONE)


def tap(b, what):
    if what.startswith("#"):
        b.js("document.querySelector(arguments[0]).click();", [what])
    else:
        b.js(what)


# ------------------------------------------------------------------ scenarios

def scenario_animevost(a, b, link):
    a.send("WebDriver:Navigate", {"url": ANIMEVOST})
    time.sleep(3)
    ok, v, t = wait(lambda: (lambda x: (x and x["has"], x))(av_video(a)), 40)
    check("A: animevost page and player loaded", ok, v)
    # one real click on the page: Firefox needs it before it plays with sound
    rect = a.js("var h = document.querySelector('.shortstoryHead h1'); h.scrollIntoView({block: 'center'});"
                "var r = h.getBoundingClientRect(); return [r.left + 20, r.top + r.height / 2];")
    click_at(a, rect[0], rect[1])

    b.send("WebDriver:Navigate", {"url": link})
    ok, s, t = wait(lambda: (lambda p: (p["conn"] == "Связь напрямую" and p["player"], p))(phone(b)), 40)
    check("B: phone connects directly (WebRTC) and sees the video", ok, "%.1fs %s" % (t, s))
    if not ok:
        return
    check("B: title and episode list from animevost", "Отель для нелюдей" in s["title"] and len(s["eps"]) >= 7, s)
    start_posts = relay_posts()

    start = av_video(a)["cur"]
    tap(b, "#next")
    ok, v, t = wait(lambda: (lambda x: (x["cur"] == start + 1 and x["has"] and not x["paused"] and x["t"] > 1, x))(av_video(a)), 60)
    check("next: A switches %d -> %d and plays" % (start, start + 1), ok, "%.0fs %s" % (t, v))
    ok, s, t = wait(lambda: (lambda p: (p["cur"] == start + 1 and p["playing"], p))(phone(b)), 10)
    check("B shows the new episode playing", ok, s)

    tap(b, "#toggle")
    ok, v, t = wait(lambda: (lambda x: (x["paused"], x))(av_video(a)), 10)
    check("toggle: pauses", ok, v)
    tap(b, "#toggle")
    ok, v, t = wait(lambda: (lambda x: (not x["paused"], x))(av_video(a)), 10)
    check("toggle: resumes", ok, v)
    t0 = av_video(a)["t"]
    tap(b, "document.querySelector('[data-seek=\"90\"]').click()")
    ok, v, t = wait(lambda: (lambda x: (x["t"] >= t0 + 85, x))(av_video(a)), 20)
    check("seek +90 s", ok, "from %.0f: %s" % (t0, v))
    vol0 = av_video(a)["vol"]
    tap(b, "document.querySelector('[data-vol=\"-0.1\"]').click()")
    ok, v, t = wait(lambda: (lambda x: (abs(x["vol"] - (vol0 - 0.1)) < 0.011, x))(av_video(a)), 10)
    check("volume down", ok, "from %s: %s" % (vol0, v))
    tap(b, "#mute")
    ok, v, t = wait(lambda: (lambda x: (x["muted"], x))(av_video(a)), 10)
    check("mute on", ok, v)
    tap(b, "#mute")
    ok, v, t = wait(lambda: (lambda x: (not x["muted"] and not x["paused"], x))(av_video(a)), 10)
    check("mute off, still playing", ok, v)

    # full screen: the player fills the window through the frame chain
    tap(b, "#full")
    time.sleep(2)
    g = theater_geometry(a)
    covers = g["frame"][:2] == [0, 0] and abs(g["frame"][2] - g["win"][0]) < 2 and abs(g["frame"][3] - g["win"][1]) < 2 and all(g["top"])
    check("full: player frame covers the whole window", covers and "ar-theater" in g["inner"], g)
    ok, s, t = wait(lambda: (lambda p: (p["full"] == "true", p))(phone(b)), 5)
    check("B shows full screen on", ok, s and s.get("full"))
    a.send("Marionette:SetContext", {"value": "chrome"})
    winstate = a.js("return window.windowState + ' fullScreen=' + window.fullScreen;")
    a.send("Marionette:SetContext", {"value": "content"})
    print("      window state after full:", winstate)

    # pick an episode while full: stays full on the new player
    target = 3
    old = av_video(a)["src"]
    tap(b, "document.querySelectorAll('#eps button')[%d].click()" % target)
    ok, v, t = wait(lambda: (lambda x: (x["cur"] == target and x["src"] != old and not x["paused"] and x["t"] >= 1, x))(av_video(a)), 60)
    check("pick episode %d from the grid: switches and plays" % (target + 1), ok, "%.0fs %s" % (t, v))
    time.sleep(2)
    g = theater_geometry(a)
    covers = g["frame"][:2] == [0, 0] and abs(g["frame"][2] - g["win"][0]) < 2 and all(g["top"])
    check("full: still covers the window on the new episode", covers and "ar-theater" in g["inner"], g)
    tap(b, "#full")
    time.sleep(2)
    g = theater_geometry(a)
    check("full off: player back to normal size", g["frame"][2] < g["win"][0] - 100 and "ar-theater" not in g["cls"], g)

    # where I stopped
    tap(b, "document.querySelector('[data-seek=\"90\"]').click()")
    ok, s, t = wait(lambda: (lambda p: (any("4 серия" in c for c in p["cont"]), p))(phone(b)), 30)
    check("where I stopped: episode 4 appears in the continue list", ok, s and s.get("cont"))
    saved_t = av_video(a)["t"]
    tap(b, "#toggle")
    time.sleep(1.5)
    old = av_video(a)["src"]
    tap(b, "document.querySelectorAll('#eps button')[0].click()")
    wait(lambda: (lambda x: (x["cur"] == 0 and x["src"] != old and x["has"], x))(av_video(a)), 40)
    time.sleep(4)
    tap(b, "#toggle")
    time.sleep(1)
    ok, s, t = wait(lambda: (lambda p: (p["saved"].startswith("Запомнено: 4 серия") and p["saved"].endswith("вернуться"), p))(phone(b)), 15)
    check("B offers to go back to episode 4 while episode 1 is open", ok, s and s.get("saved"))
    tap(b, "document.getElementById('saved').click()")
    ok, v, t = wait(lambda: (lambda x: (x["cur"] == 3 and not x["paused"] and abs(x["t"] - saved_t) < 25, x))(av_video(a)), 60)
    check("resume: back on episode 4 near %.0f s" % saved_t, ok, "%.0fs %s" % (t, v))

    # auto-next
    tap(b, "#auto")
    ok, s, t = wait(lambda: (lambda p: (p["auto"] == "true", p))(phone(b)), 5)
    check("auto-next on", ok)
    time.sleep(3)
    a.js("var v = document.querySelector('#player2 iframe').contentDocument.querySelector('video'); v.currentTime = v.duration - 4;")
    time.sleep(2)
    print("      after jumping to the end:", av_video(a))
    ok, v, t = wait(lambda: (lambda x: (x["cur"] == 4 and not x["paused"] and x["t"] >= 1, x))(av_video(a)), 90)
    check("auto-next: end of episode 4 starts episode 5", ok, "%.0fs %s" % (t, v))
    tap(b, "#auto")

    used = relay_posts() - start_posts
    print("      phone connection log:", b.js("return window.arLog;"))
    check("direct mode: relay used only for reconnects (%d messages)" % used, used <= 2, used)


def scenario_relay(a, link):
    """Phone with WebRTC switched off: everything has to go through the relay."""
    c = launch("phone2", 2835, 'user_pref("media.peerconnection.enabled", false);')
    try:
        before = relay_posts()
        c.send("WebDriver:Navigate", {"url": link})
        ok, s, t = wait(lambda: (lambda p: (p["conn"].startswith("Связь через интернет") and p["player"], p))(phone(c)), 40)
        check("relay: phone without WebRTC connects through the relay", ok, "%.1fs %s" % (t, s))
        if not ok:
            return
        cur = av_video(a)["cur"]
        tap(c, "#toggle")
        ok, v, t = wait(lambda: (lambda x: (x["paused"], x))(av_video(a)), 15)
        check("relay: pause works", ok, "%.1fs %s" % (t, v))
        tap(c, "#toggle")
        ok, v, t = wait(lambda: (lambda x: (not x["paused"], x))(av_video(a)), 15)
        check("relay: resume works", ok, "%.1fs" % t)
        tap(c, "#prev")
        ok, v, t = wait(lambda: (lambda x: (x["cur"] == cur - 1 and not x["paused"] and x["t"] > 1, x))(av_video(a)), 60)
        check("relay: previous episode", ok, "%.0fs %s" % (t, v))
        ok, s, t = wait(lambda: (lambda p: (p["cur"] == cur - 1, p))(phone(c)), 15)
        check("relay: phone sees the new episode", ok, s and s.get("ep"))
        time.sleep(20)            # playing for 20 s must not flood the relay
        used = relay_posts() - before
        check("relay: messages stay few (%d in ~45 s)" % used, used <= 25, used)
    finally:
        quit_(c)
        time.sleep(1)
        shutil.rmtree(c.profile, ignore_errors=True)


def secs(text):
    """'1:23 / 23:39' -> 83 (seconds of the current position)."""
    try:
        part = text.split("/")[0].strip()
        n = 0
        for x in part.split(":"):
            n = n * 60 + int(x)
        return n
    except Exception:  # noqa: BLE001
        return -1


def scenario_kinogo(a, b):
    a.send("WebDriver:Navigate", {"url": KINOGO})
    time.sleep(4)
    # bring the player into view, as a viewer scrolling down would; the page's lazy loader starts it
    a.js("var f = document.querySelector('iframe[data-src], iframe'); if (f) { f.scrollIntoView({block: 'center'}); }")
    time.sleep(6)
    rect = a.js("var h = document.querySelector('h1') || document.body; h.scrollIntoView({block: 'center'}); var r = h.getBoundingClientRect(); return [r.left + 10, r.top + r.height / 2];")
    click_at(a, rect[0], rect[1])          # one real click on the page, like a viewer would
    a.js("var f = document.querySelector('iframe[data-src], iframe'); if (f) { f.scrollIntoView({block: 'center'}); }")
    ok, s, t = wait(lambda: (lambda p: (p["player"] and "клевер" in p["title"].lower(), p))(phone(b)), 60)
    check("kinogo: phone sees the page and the video", ok, "%.0fs %s" % (t, s))
    if not ok:
        return
    tap(b, "#toggle")
    ok, s, t = wait(lambda: (lambda p: (p["playing"] and secs(p["time"]) >= 2 and "23:" in p["time"] and "серия" in p["ep"], p))(phone(b)), 40)
    check("kinogo: play from the phone starts the episode with sound, episode shown", ok and not s.get("note"), "%.0fs %s" % (t, s))
    if not ok:
        return
    check("kinogo: next/prev available (player playlist)", s["next"] and s["prev"], (s["next"], s["prev"]))
    ep0 = s["ep"]
    t0 = secs(s["time"])
    tap(b, "document.querySelector('[data-seek=\"90\"]').click()")
    ok, s2, t = wait(lambda: (lambda p: (secs(p["time"]) >= t0 + 85, p))(phone(b)), 20)
    check("kinogo: seek +90 s", ok, (t0, s2.get("time")))

    tap(b, "#full")
    time.sleep(2)
    g = a.js("""
      var f = document.querySelector('.ar-stretch');
      var r = f ? f.getBoundingClientRect() : null;
      return { stretched: f ? f.tagName : null, rect: r ? [r.left, r.top, r.width, r.height] : null, win: [innerWidth, innerHeight],
               onTop: f ? document.elementFromPoint(innerWidth / 2, innerHeight / 2) === f : false };
    """)
    check("kinogo: full stretches the player iframe over the window", g["rect"] and g["rect"][:2] == [0, 0] and g["onTop"], g)

    tap(b, "#prev")
    ok, s3, t = wait(lambda: (lambda p: (p["ep"] != ep0 and "серия" in p["ep"] and p["playing"] and 1 <= secs(p["time"]) < 60, p))(phone(b)), 60)
    check("kinogo: previous episode from the phone (%s -> %s)" % (ep0, s3.get("ep")), ok, "%.0fs %s" % (t, s3))
    ep1 = s3.get("ep")
    g = a.js("var f = document.querySelector('.ar-stretch'); var r = f ? f.getBoundingClientRect() : null; return r ? [r.left, r.top, r.width, r.height, innerWidth, innerHeight] : null;")
    check("kinogo: still full screen after switching episodes", g and g[:2] == [0, 0] and g[2] == g[4], g)
    tap(b, "#full")

    tap(b, "document.querySelector('[data-seek=\"90\"]').click()")
    ok, s4, t = wait(lambda: (lambda p: (any(ep1 in c for c in p["cont"]), p))(phone(b)), 30)
    check("kinogo: where I stopped remembers %s" % ep1, ok, s4.get("cont"))
    saved = secs(s4.get("time", "0"))
    tap(b, "#toggle")
    time.sleep(1)

    # close the page, then continue from the phone: the page reopens on that episode and place
    a.send("WebDriver:Navigate", {"url": "about:blank"})
    ok, s5, t = wait(lambda: (lambda p: (not p["player"], p))(phone(b)), 20)
    check("kinogo: phone sees that no video is open", ok, s5.get("note"))
    tap(b, "document.querySelector('#cont .item .go').click()")
    ok, s6, t = wait(lambda: (lambda p: (p["player"] and p["ep"] == ep1 and p["playing"] and abs(secs(p["time"]) - saved) < 30, p))(phone(b)), 90)
    check("kinogo: continue reopens the page on %s near %d s" % (ep1, saved), ok, "%.0fs %s" % (t, s6))
    tap(b, "#toggle")


FRAME_DUMP = r"""
return { url: location.href.slice(0, 70), cls: document.documentElement.className,
  vids: [].slice.call(document.querySelectorAll('video')).map(function (v) { var r = v.getBoundingClientRect();
    return { t: Math.round(v.currentTime * 10) / 10, d: v.duration, p: v.paused, m: v.muted, rs: v.readyState, w: Math.round(r.width), h: Math.round(r.height), src: String(v.currentSrc || v.src).slice(0, 50) }; }),
  n: document.querySelectorAll('iframe').length };
"""


def dump_frames(m, depth=0):
    try:
        info = m.js(FRAME_DUMP)
    except Exception as e:  # noqa: BLE001
        print("  " * depth + "! " + str(e)[:100])
        return
    if info["vids"] or depth == 0:
        print("  " * depth + json.dumps(info, ensure_ascii=False)[:600])
    for i in range(info["n"]):
        try:
            m.send("WebDriver:SwitchToFrame", {"id": i})
        except Exception:  # noqa: BLE001
            continue
        dump_frames(m, depth + 1)
        m.send("WebDriver:SwitchToParentFrame")


def diag_kinogo(a, b, link):
    b.send("WebDriver:Navigate", {"url": link})
    wait(lambda: (lambda p: (p["conn"] == "Связь напрямую", p))(phone(b)), 40)
    a.send("WebDriver:Navigate", {"url": KINOGO})
    time.sleep(4)
    a.js("var f = document.querySelector('iframe[data-src], iframe'); if (f) { f.scrollIntoView({block: 'center'}); }")
    time.sleep(8)
    rect = a.js("var h = document.querySelector('h1') || document.body; h.scrollIntoView({block: 'center'}); var r = h.getBoundingClientRect(); return [r.left + 10, r.top + r.height / 2];")
    click_at(a, rect[0], rect[1])          # one real click on the page, like a viewer would
    a.js("var f = document.querySelector('iframe[data-src], iframe'); if (f) { f.scrollIntoView({block: 'center'}); }")
    print("--- before play:", phone(b))
    dump_frames(a)
    tap(b, "#toggle")
    for i in range(4):
        time.sleep(6)
        print("--- %ds after play:" % (6 * (i + 1)), {k: v for k, v in phone(b).items() if k in ("title", "ep", "time", "playing", "note", "next", "prev", "eps")})
    dump_frames(a)


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    a = launch("pc", 2833)
    b = None
    try:
        addon_id, uuid = install_extension(a)
        check("A: extension installs (temporary)", bool(uuid), (addon_id, uuid))
        link, status = configure(a, uuid)
        check("A: popup gives a pairing link to the local phone page", bool(link) and link.startswith(PAGE + "#k="), link and link[:60])
        check("A: extension is connected to the relay", status and status[0] == "Готов к подключению", status)
        a.send("WebDriver:Navigate", {"url": "about:blank"})
        if not link:
            return
        b = launch("phone", 2834)
        if which in ("animevost", "relay", "all"):
            scenario_animevost(a, b, link)
        if which in ("relay", "all"):
            scenario_relay(a, link)
        if which == "diag":
            diag_kinogo(a, b, link)
        if which in ("kinogo", "all"):
            if which == "kinogo":
                b.send("WebDriver:Navigate", {"url": link})
                wait(lambda: (lambda p: (p["conn"] == "Связь напрямую", p))(phone(b)), 40)
            scenario_kinogo(a, b)
    finally:
        for m in (b, a):
            if m:
                quit_(m)
        time.sleep(1.5)
        for name in ("pc", "phone", "phone2"):
            shutil.rmtree(os.path.join(HERE, "profile-" + name), ignore_errors=True)
        bad = [n for n, ok in RESULTS if not ok]
        print("\n%d checks, %d failed%s" % (len(RESULTS), len(bad), (": " + "; ".join(bad)) if bad else ""), flush=True)


if __name__ == "__main__":
    main()
