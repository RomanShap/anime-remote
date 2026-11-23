"""Smoke test against the real public relay (ntfy.sh): pairing and one command, a handful of messages.

The phone page is still served locally (docs/ on port 8778). Uses ~3 relay messages.
"""
import os
import shutil
import time

import ext_e2e as E

E.RELAY = "https://ntfy.sh"


def main():
    a = E.launch("pc", 2833)
    b = None
    try:
        _, uuid = E.install_extension(a)
        link, status = E.configure(a, uuid)
        E.check("real relay: extension connects to ntfy.sh", status and status[0] == "Готов к подключению", status)
        E.check("real relay: link has no relay parameter (ntfy.sh is the default)", link and "&r=" not in link, link and link[:50])
        a.send("WebDriver:Navigate", {"url": E.ANIMEVOST})
        E.wait(lambda: (lambda x: (x and x["has"], x))(E.av_video(a)), 40)
        b = E.launch("phone", 2834)
        b.send("WebDriver:Navigate", {"url": link})
        ok, s, t = E.wait(lambda: (lambda p: (p["conn"] == "Связь напрямую" and p["player"], p))(E.phone(b)), 40)
        E.check("real relay: phone pairs through ntfy.sh and connects directly", ok, "%.1fs %s" % (t, s and s.get("conn")))
        if ok:
            E.tap(b, "document.querySelector('[data-vol=\"-0.1\"]').click()")
            ok, v, t = E.wait(lambda: (lambda x: (abs(x["vol"] - 0.6) < 0.011, x))(E.av_video(a)), 10)
            E.check("real relay: a command reaches the computer", ok, v)
    finally:
        for m in (b, a):
            if m:
                E.quit_(m)
        time.sleep(1.5)
        for name in ("pc", "phone"):
            shutil.rmtree(os.path.join(E.HERE, "profile-" + name), ignore_errors=True)
        bad = [n for n, ok in E.RESULTS if not ok]
        print("\n%d checks, %d failed" % (len(E.RESULTS), len(bad)))


if __name__ == "__main__":
    main()
