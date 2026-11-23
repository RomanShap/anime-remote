"""Diagnostic: how does an embedded player's episode list react to clicks from a script?

Opens a kinogo page in the "computer" Firefox with the extension, starts playback from the phone,
then tries click(), mouse events, pointer events and a click on the inner element on different
episode items, and reports whether the video changed. Same setup as ext_e2e.py.
"""
import json
import sys
import time

import ext_e2e as E

EP_DIAG = r"""
var re = /^\s*(?:(\d{1,4})\s*(?:-?(?:я|й|ая))?\s*(?:серия|серии|эпизод|эп\.?|episode|ep\.?)|(?:серия|эпизод|эп\.?|episode|ep\.?)\s*(\d{1,4}))\s*$/i;
var items = [].slice.call(document.querySelectorAll('li, div, span, a, button')).filter(function (e) { return e.children.length <= 3 && re.test(e.textContent); });
var v = document.querySelector('video');
var first = items[0];
var path = []; for (var e = first; e && path.length < 7; e = e.parentElement) { path.push(e.tagName + '.' + String(e.className).slice(0, 50) + (e.id ? '#' + e.id : '')); }
var sels = [].slice.call(document.querySelectorAll('select')).map(function (x) { return [x.className, x.options.length, x.selectedIndex, x.options[0] && x.options[0].textContent]; });
return { url: location.href.slice(0, 60), n: items.length, sels: sels, texts: items.slice(0, 4).map(function (e) { return e.tagName + ' ' + e.textContent.trim(); }), path: path,
         html: first ? first.parentElement.parentElement.outerHTML.slice(0, 1800) : null,
         video: v ? { t: v.currentTime, src: String(v.currentSrc).slice(0, 60), p: v.paused } : null };
"""

LABELS = r"""
var out = [];
[].slice.call(document.querySelectorAll('[aria-label], [title], [data-title], button')).forEach(function (e) {
  var t = (e.getAttribute('aria-label') || e.getAttribute('title') || e.getAttribute('data-title') || e.textContent || '').replace(/\s+/g, ' ').trim();
  if (t && t.length < 40) { out.push(e.tagName + '.' + String(e.className).slice(0, 30) + ' "' + t + '"'); }
});
var gen = document.querySelector('meta[name=generator]');
return { labels: out.slice(0, 40), scripts: [].slice.call(document.scripts).map(function (x) { return (x.src || 'inline').slice(-50); }).slice(0, 12), shadowHosts: [].slice.call(document.querySelectorAll('*')).filter(function (e) { return e.openOrClosedShadowRoot || e.shadowRoot; }).map(function (e) { return e.tagName; }).slice(0, 5), bodyStart: document.body.innerHTML.slice(0, 600) };
"""

PJS = r"""
var w = window.wrappedJSObject || window;
var found = [];
Object.getOwnPropertyNames(w).forEach(function (k) {
  try { var o = w[k]; if (o && typeof o === 'object' && typeof o.api === 'function') { found.push(k); } } catch (e) {}
});
var g = w.pljssglobal;
var info = { apiObjects: found, pljssglobal: g ? (g.length + ' items') : null, Playerjs: typeof w.Playerjs };
var p = found.length ? w[found[0]] : (g && g[0]);
if (p && p.api) {
  var q = function (x) { try { var r = p.api(x); return typeof r === 'object' ? JSON.stringify(r).slice(0, 300) : String(r).slice(0, 300); } catch (e) { return 'ERR ' + e; } };
  ['playlist_id', 'playlist', 'title', 'time', 'duration', 'file', 'id', 'season', 'serie', 'playing'].forEach(function (x) { info['api(' + x + ')'] = q(x); });
}
return info;
"""

TRY_CLICK = r"""
var re = /^\s*(?:(\d{1,4})\s*(?:-?(?:я|й|ая))?\s*(?:серия|серии|эпизод|эп\.?|episode|ep\.?)|(?:серия|эпизод|эп\.?|episode|ep\.?)\s*(\d{1,4}))\s*$/i;
var items = [].slice.call(document.querySelectorAll('li, div, span, a, button')).filter(function (e) { return e.children.length <= 3 && re.test(e.textContent); });
var it = items[arguments[0]];
var how = arguments[1];
var o = { bubbles: true, cancelable: true, view: window, button: 0 };
if (how === 'click') { it.click(); }
if (how === 'mouse') { it.dispatchEvent(new MouseEvent('mousedown', o)); it.dispatchEvent(new MouseEvent('mouseup', o)); it.dispatchEvent(new MouseEvent('click', o)); }
if (how === 'pointer') { it.dispatchEvent(new PointerEvent('pointerdown', o)); it.dispatchEvent(new MouseEvent('mousedown', o)); it.dispatchEvent(new PointerEvent('pointerup', o)); it.dispatchEvent(new MouseEvent('mouseup', o)); it.dispatchEvent(new MouseEvent('click', o)); }
if (how === 'inner') { var d = it.querySelector('*') || it; d.click(); }
if (how === 'parent') { it.parentElement.click(); }
return it.outerHTML.slice(0, 200);
"""


def find_video_frame(m):
    if m.js("var v = document.querySelector('video'); return !!(v && v.getBoundingClientRect().width > 300 && !v.paused);"):
        return True
    n = m.js("return document.querySelectorAll('iframe').length;")
    for i in range(n):
        try:
            m.send("WebDriver:SwitchToFrame", {"id": i})
        except Exception:  # noqa: BLE001
            continue
        if find_video_frame(m):
            return True
        m.send("WebDriver:SwitchToParentFrame")
    return False


def main():
    a = E.launch("pc", 2833)
    b = None
    try:
        _, uuid = E.install_extension(a)
        link, _ = E.configure(a, uuid)
        a.send("WebDriver:Navigate", {"url": "about:blank"})
        b = E.launch("phone", 2834)
        b.send("WebDriver:Navigate", {"url": link})
        E.wait(lambda: (lambda p: (p["conn"] == "Связь напрямую", p))(E.phone(b)), 40)
        a.send("WebDriver:Navigate", {"url": sys.argv[1] if len(sys.argv) > 1 else E.KINOGO})
        time.sleep(4)
        a.js("var f = document.querySelector('iframe[data-src], iframe'); if (f) { f.scrollIntoView({block: 'center'}); }")
        time.sleep(6)
        rect = a.js("var h = document.querySelector('h1') || document.body; h.scrollIntoView({block: 'center'}); var r = h.getBoundingClientRect(); return [r.left + 10, r.top + r.height / 2];")
        E.click_at(a, rect[0], rect[1])
        a.js("var f = document.querySelector('iframe[data-src], iframe'); if (f) { f.scrollIntoView({block: 'center'}); }")
        E.wait(lambda: (lambda p: (len(p["eps"]) > 1, p))(E.phone(b)), 40)
        E.tap(b, "#toggle")
        E.wait(lambda: (lambda p: (p["playing"] and E.secs(p["time"]) >= 3, p))(E.phone(b)), 40)
        print("phone:", {k: v for k, v in E.phone(b).items() if k in ("ep", "time", "eps", "cur")})
        if not find_video_frame(a):
            print("no video frame found")
            return
        print(json.dumps(a.js(EP_DIAG), ensure_ascii=False, indent=1)[:3000])
        print(json.dumps(a.js(PJS), ensure_ascii=False)[:3000])
        st = "var w = window.wrappedJSObject; var p = w.player; return [p.api('playlist_id'), Math.round(p.api('time')), Math.round(p.api('duration')), document.querySelector('video').paused];"
        for cmd in ("next", "next", "prev"):
            before = a.js(st)
            a.js("window.wrappedJSObject.player.api(arguments[0]);", [cmd])
            time.sleep(8)
            print("api(%s):" % cmd, before, "->", a.js(st), "| phone:", {k: v for k, v in E.phone(b).items() if k in ("ep", "time")})
        return
        for idx, how in ((0, "click"), (1, "mouse"), (2, "pointer"), (3, "inner"), (4, "parent")):
            before = a.js("var v = document.querySelector('video'); return [Math.round(v.currentTime), String(v.currentSrc).slice(-12)];")
            print("try", how, "on item", idx, ":", a.js(TRY_CLICK, [idx, how])[:160])
            time.sleep(7)
            after = a.js("var v = document.querySelector('video'); return [Math.round(v.currentTime), String(v.currentSrc).slice(-12)];")
            print("   video before", before, "after", after, "| phone:", {k: v for k, v in E.phone(b).items() if k in ("ep", "time", "cur")})
    finally:
        for m in (b, a):
            if m:
                E.quit_(m)
        time.sleep(1.5)
        import shutil
        import os
        for name in ("pc", "phone"):
            shutil.rmtree(os.path.join(E.HERE, "profile-" + name), ignore_errors=True)


if __name__ == "__main__":
    main()
