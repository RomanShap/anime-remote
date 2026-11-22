"""End-to-end check of anime-remote in a throwaway Firefox profile, driven over Marionette.

The userscript is injected into the real animevost page. GM_xmlhttpRequest is bridged to
the local server by this script, so the shipped transport branch is the one exercised.
Commands go in the way the phone sends them: POST /api/cmd on the running server.
"""
import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "profile")
FIREFOX = os.environ.get("FIREFOX") or (r"C:\Program Files\Mozilla Firefox\firefox.exe" if os.name == "nt" else "firefox")
USERSCRIPT = os.path.join(os.path.dirname(HERE), "anime-remote.user.js")
PORT = os.environ.get("AR_PORT", "8765")     # AR_PORT=8766 to test against a separate server instance
SERVER = "http://127.0.0.1:" + PORT
PAGE = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].startswith("http") else "https://animevost.org/tip/tv/4064-hotel-inhumans-2.html"


def script_text():
    """The userscript as shipped, pointed at the server under test."""
    with open(USERSCRIPT, encoding="utf-8") as f:
        return f.read().replace("127.0.0.1:8765", "127.0.0.1:" + PORT) + "\nreturn true;"


HEADLESS = "--visible" not in sys.argv
MPORT = 2829

PREFS = """
user_pref("marionette.port", %d);
user_pref("media.volume_scale", "0.0");
user_pref("remote.prefs.recommended", false);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("datareporting.healthreport.uploadEnabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("startup.homepage_welcome_url", "");
user_pref("startup.homepage_welcome_url.additional", "");
user_pref("app.update.auto", false);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.tabs.warnOnClose", false);
""" % MPORT


class Marionette:
    def __init__(self, port):
        deadline = time.time() + 60
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        self.sock.settimeout(120)
        self.lock = threading.Lock()
        self.n = 0
        self._read()   # hello

    def _read(self):
        size = b""
        while True:
            ch = self.sock.recv(1)
            if not ch:
                raise ConnectionError("marionette closed")
            if ch == b":":
                break
            size += ch
        need = int(size)
        buf = b""
        while len(buf) < need:
            part = self.sock.recv(need - len(buf))
            if not part:
                raise ConnectionError("marionette closed")
            buf += part
        return json.loads(buf.decode("utf-8"))

    def send(self, name, params=None):
        with self.lock:
            self.n += 1
            raw = json.dumps([0, self.n, name, params or {}]).encode("utf-8")
            self.sock.sendall(str(len(raw)).encode() + b":" + raw)
            _, _, err, res = self._read()
        if err:
            raise RuntimeError("%s: %s" % (name, err.get("message", err)))
        return res

    def js(self, script, args=None, timeout=30000):
        res = self.send("WebDriver:ExecuteScript", {
            "script": script, "args": args or [], "newSandbox": False,
            "sandbox": "default", "scriptTimeout": timeout})
        return res.get("value") if isinstance(res, dict) else res


def api(path, body=None, timeout=35):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(SERVER + path, data=data, headers={
        "X-Anime-Remote": "1", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def cmd(action, value=None):
    return api("/api/cmd", {"action": action, "value": value})


def state():
    return api("/api/state").get("state") or {}


def wait_for(what, pred, timeout=45, every=0.5):
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        last = state()
        try:
            if pred(last):
                return True, last, time.time() - t0
        except Exception:
            pass
        time.sleep(every)
    return False, last, timeout


FRAME_INFO = """
  var f = document.querySelector('#player2 iframe'); if (!f) { return null; }
  var d = f.contentDocument, v = d && d.querySelector('video');
  return { src: f.contentWindow.location.href, t: v ? v.currentTime : -1, paused: v ? v.paused : true,
           dur: v && isFinite(v.duration) ? v.duration : 0,
           cls: d && d.documentElement ? d.documentElement.className : '' };
"""


def frame_src(m):
    i = m.js(FRAME_INFO)
    return i["src"] if i else None


def wait_new_playing(m, old_src, timeout=90):
    """Wait until a different player frame than old_src is really playing."""
    t0 = time.time()
    i = None
    while time.time() - t0 < timeout:
        i = m.js(FRAME_INFO)
        if i and i["src"] != old_src and not i["paused"] and i["t"] >= 1 and i["dur"] > 0:
            return True, i, time.time() - t0
        time.sleep(0.5)
    return False, i, timeout


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(("PASS  " if ok else "FAIL  ") + name + ("  | " + str(detail) if detail != "" else ""), flush=True)


def brief(s):
    keys = ("current", "video", "paused", "time", "duration", "volume", "muted", "full", "autonext", "hint")
    return {k: s.get(k) for k in keys}


BRIDGE = """
if (!globalThis.__gmq) {
  globalThis.__gmq = []; globalThis.__gmcb = {}; globalThis.__gmn = 0;
  globalThis.GM_xmlhttpRequest = function (d) {
    var id = ++globalThis.__gmn; globalThis.__gmcb[id] = d;
    globalThis.__gmq.push({ id: id, method: d.method, url: d.url, data: d.data || null, headers: d.headers });
  };
  globalThis.__gmdone = function (id, status, text) {
    var d = globalThis.__gmcb[id]; delete globalThis.__gmcb[id];
    if (!d) { return; }
    if (status) { d.onload && d.onload({ status: status, responseText: text }); }
    else { d.onerror && d.onerror({}); }
  };
}
return true;
"""


def relay(m, stop):
    """Carry GM_xmlhttpRequest calls from the page to the local server and back."""
    def one(r):
        try:
            req = urllib.request.Request(r["url"], data=r["data"].encode("utf-8") if r["data"] else None,
                                         headers=r["headers"], method=r["method"])
            with urllib.request.urlopen(req, timeout=35) as resp:
                status, text = resp.status, resp.read().decode("utf-8")
        except Exception:
            status, text = 0, ""
        if not stop.is_set():
            try:
                m.js("globalThis.__gmdone(arguments[0], arguments[1], arguments[2]);", [r["id"], status, text])
            except Exception:
                pass

    while not stop.is_set():
        try:
            batch = m.js("return globalThis.__gmq ? globalThis.__gmq.splice(0) : [];") or []
        except Exception:
            batch = []
        for r in batch:
            threading.Thread(target=one, args=(r,), daemon=True).start()
        time.sleep(0.15)


def main():
    os.makedirs(PROFILE, exist_ok=True)
    with open(os.path.join(PROFILE, "user.js"), "w", encoding="utf-8") as f:
        f.write(PREFS)
    env = dict(os.environ, MOZ_HEADLESS_WIDTH="1600", MOZ_HEADLESS_HEIGHT="900", MOZ_REMOTE_ALLOW_SYSTEM_ACCESS="1")
    args = [FIREFOX, "-no-remote", "-profile", PROFILE, "-marionette"]
    if HEADLESS:
        args.append("-headless")
    args.append("about:blank")
    subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    m = Marionette(MPORT)
    stop = threading.Event()
    try:
        try:
            m.send("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {"pageLoadStrategy": "eager"}}})
        except RuntimeError:
            m.send("WebDriver:NewSession", {})
        m.send("WebDriver:SetTimeouts", {"pageLoad": 90000, "script": 30000})

        m.send("Marionette:SetContext", {"value": "chrome"})
        prefs = m.js("return {autoplay: Services.prefs.getIntPref('media.autoplay.default'),"
                     " policy: Services.prefs.getIntPref('media.autoplay.blocking_policy'),"
                     " version: Services.appinfo.version};")
        m.send("Marionette:SetContext", {"value": "content"})
        print("firefox", prefs, "headless" if HEADLESS else "visible", flush=True)
        check("Firefox autoplay settings are the defaults (audio blocked)", prefs.get("autoplay") == 1, prefs)

        m.send("WebDriver:Navigate", {"url": PAGE})
        time.sleep(3)
        m.js(BRIDGE)
        threading.Thread(target=relay, args=(m, stop), daemon=True).start()
        m.js(script_text())

        ok, s, t = wait_for("tab online", lambda s: len(s.get("episodes", [])) > 0, 30)
        check("page reports itself to the server", ok, {"title": s.get("title"), "episodes": len(s.get("episodes", [])), **brief(s)})
        if not ok:
            return
        n_eps = len(s["episodes"])
        ok, s, t = wait_for("player ready", lambda s: s.get("video"), 60)
        check("site player loads its video element", ok, "%.0fs %s" % (t, brief(s)))

        # 1. no click on the page yet: Firefox should refuse to start playback
        cmd("toggle")
        ok, s, t = wait_for("autoplay hint or playing", lambda s: s.get("hint") == "autoplay" or not s.get("paused"), 15)
        blocked = s.get("hint") == "autoplay"
        check("before any click: play is either blocked with a hint, or works", ok, brief(s))
        print("      -> without a click Firefox %s" % ("BLOCKED playback (hint shown on the phone)" if blocked else "allowed playback"), flush=True)

        # 2. one real click on neutral text, then everything from the "phone"
        rect = m.js("var h = document.querySelector('.shortstoryHead h1'); h.scrollIntoView({block: 'center'});"
                    "var r = h.getBoundingClientRect(); return [r.left + 20, r.top + r.height / 2];")
        m.send("WebDriver:PerformActions", {"actions": [{"type": "pointer", "id": "m1", "parameters": {"pointerType": "mouse"},
               "actions": [{"type": "pointerMove", "duration": 0, "x": int(rect[0]), "y": int(rect[1])},
                           {"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0}]}]})
        m.send("WebDriver:ReleaseActions")
        start = state().get("current", 0)
        cmd("next")
        ok, s, t = wait_for("next", lambda s: s.get("current") == start + 1, 15)
        check("next: episode switches %d -> %d" % (start, start + 1), ok, brief(s))
        ok, s, t = wait_for("playing", lambda s: s.get("video") and not s.get("paused") and s.get("time", 0) >= 2, 75)
        check("next: new episode starts playing by itself after one earlier click", ok, "%.0fs %s" % (t, brief(s)))

        cmd("toggle")
        ok, s, t = wait_for("pause", lambda s: s.get("paused"), 10)
        check("toggle: pauses", ok, brief(s))
        cmd("toggle")
        ok, s, t = wait_for("resume", lambda s: not s.get("paused"), 10)
        check("toggle: resumes", ok, brief(s))

        before = state().get("time", 0)
        cmd("seek", 90)
        ok, s, t = wait_for("seek", lambda s: s.get("time", 0) >= before + 85, 30)
        check("seek +90 s (from %d s)" % before, ok, brief(s))

        vol = state().get("volume")
        cmd("vol", -0.1)
        ok, s, t = wait_for("vol", lambda s: s.get("volume") == vol - 10, 10)
        check("volume down %s -> %s" % (vol, vol - 10 if vol is not None else None), ok, brief(s))
        cmd("mute")
        ok, s, t = wait_for("mute", lambda s: s.get("muted"), 10)
        check("mute on", ok)
        cmd("mute")
        ok, s, t = wait_for("unmute", lambda s: not s.get("muted"), 10)
        check("mute off", ok)

        # 3. full screen: player must cover the whole window, nothing on top of it
        cmd("full")
        ok, s, t = wait_for("full", lambda s: s.get("full"), 10)
        check("full: state says full", ok, brief(s))
        time.sleep(1.5)
        geo = m.js("""
          var f = document.querySelector('#player2 iframe'), r = f.getBoundingClientRect();
          var pts = [[5, 5], [innerWidth / 2, innerHeight / 2], [innerWidth - 5, innerHeight - 5], [innerWidth - 5, 5], [5, innerHeight - 5]];
          var top = pts.map(function (p) { var e = document.elementFromPoint(p[0], p[1]); return e === f; });
          var d = f.contentDocument, k = d.getElementById('knpki'), v = d.querySelector('video'), vr = v.getBoundingClientRect();
          return { win: [innerWidth, innerHeight], frame: [r.left, r.top, r.width, r.height], frameOnTop: top,
                   bar: k ? d.defaultView.getComputedStyle(k).display : 'none', video: [vr.width, vr.height],
                   frameView: [d.defaultView.innerWidth, d.defaultView.innerHeight], scroll: document.documentElement.scrollHeight };
        """)
        full_ok = (geo["frame"][:2] == [0, 0] and abs(geo["frame"][2] - geo["win"][0]) < 2
                   and abs(geo["frame"][3] - geo["win"][1]) < 2 and all(geo["frameOnTop"])
                   and abs(geo["video"][0] - geo["win"][0]) < 2 and abs(geo["video"][1] - geo["win"][1]) < 2)
        check("full: player frame and video cover the whole window, nothing above them", full_ok, geo)
        shot = m.send("WebDriver:TakeScreenshot", {"full": False})
        with open(os.path.join(HERE, "full.png"), "wb") as f:
            f.write(base64.b64decode(shot["value"] if isinstance(shot, dict) else shot))

        target = min(n_eps - 2, 3)
        src = frame_src(m)
        cmd("ep", target)
        ok, i, t = wait_new_playing(m, src)
        s = state()
        check("pick episode %d while full: switches and plays" % (target + 1), ok and s.get("current") == target, "%.0fs %s %s" % (t, i, brief(s)))
        time.sleep(1)
        geo2 = m.js("var f = document.querySelector('#player2 iframe'), r = f.getBoundingClientRect();"
                    "return [r.left, r.top, r.width, r.height, innerWidth, innerHeight, f.contentDocument.documentElement.className];")
        check("full: still covers the window after switching", geo2[:2] == [0, 0] and abs(geo2[2] - geo2[4]) < 2
              and abs(geo2[3] - geo2[5]) < 2 and "ar-theater" in geo2[6] and state().get("full"), geo2)

        cmd("full")
        ok, s, t = wait_for("unfull", lambda s: not s.get("full"), 10)
        geo3 = m.js("var r = document.querySelector('#player2 iframe').getBoundingClientRect(); return [r.width, r.height, innerWidth];")
        check("full off: player back to its normal size", ok and geo3[0] < geo3[2] - 100, geo3)
        ok, s, t = wait_for("hints cleared", lambda s: s.get("hint") == "", 15)
        check("hints clear once things work", ok, brief(s))

        # 4. auto-next at the end of an episode
        cmd("autonext", True)
        ok, s, t = wait_for("autonext on", lambda s: s.get("autonext"), 10)
        check("autonext toggle on", ok)
        cur = state().get("current")
        src = frame_src(m)
        m.js("var v = document.querySelector('#player2 iframe').contentDocument.querySelector('video'); v.currentTime = v.duration - 4;")
        ok, i, t = wait_new_playing(m, src, 150)
        s = state()
        check("autonext: end of episode %d starts episode %d" % (cur + 1, cur + 2), ok and s.get("current") == cur + 1, "%.0fs %s %s" % (t, i, brief(s)))
        cmd("autonext", False)
        wait_for("autonext off", lambda s: not s.get("autonext"), 10)

        src = frame_src(m)
        cmd("prev")
        ok, i, t = wait_new_playing(m, src)
        s = state()
        check("prev: goes back one episode and plays", ok and s.get("current") == cur, "%.0fs %s" % (t, brief(s)))

        # 5. the site's second player (Playerjs)
        cmd("pause")
        m.js("var f = document.querySelector('#player2 iframe'); var id = f.contentWindow.location.search.match(/play=(\\d+)/)[1];"
             "f.contentWindow.location.href = '/frame5.php?play=' + id + '&old=1';")
        time.sleep(6)
        ok, s, t = wait_for("player2 video", lambda s: s.get("video"), 30)
        check("player no. 2: video element found", ok, brief(s))
        if ok:
            cmd("play")
            ok2, s, t = wait_for("player2 playing", lambda s: not s.get("paused") and s.get("time", 0) >= 1, 45)
            check("player no. 2: play from the phone works", ok2, "%.0fs %s" % (t, brief(s)))
            cmd("pause")
            ok3, s, t = wait_for("player2 pause", lambda s: s.get("paused"), 10)
            check("player no. 2: pause works", ok3, brief(s))
            src = frame_src(m)
            cmd("next")
            ok4, i, t = wait_new_playing(m, src)
            check("player no. 2: next episode switches and plays", ok4, "%.0fs %s %s" % (t, i, brief(state())))
    finally:
        stop.set()
        time.sleep(0.3)
        try:
            m.send("Marionette:Quit", {"flags": ["eForceQuit"]})
        except Exception as e:
            print("quit:", e)
        bad = [n for n, ok in RESULTS if not ok]
        print("\n%d checks, %d failed%s" % (len(RESULTS), len(bad), (": " + "; ".join(bad)) if bad else ""), flush=True)


if __name__ == "__main__":
    main()
