"""Screenshots of the popup and the phone page during a real session (for checking the layout)."""
import base64
import os
import shutil
import time

import ext_e2e as E


def shot(m, name):
    r = m.send("WebDriver:TakeScreenshot", {"full": True})
    data = r["value"] if isinstance(r, dict) else r
    path = os.path.join(E.HERE, name)
    with open(path, "wb") as f:
        f.write(base64.b64decode(data))
    print("saved", path)


def main():
    a = E.launch("pc", 2833)
    b = None
    try:
        _, uuid = E.install_extension(a)
        link, _ = E.configure(a, uuid)
        a.send("WebDriver:Navigate", {"url": E.ANIMEVOST})
        E.wait(lambda: (lambda x: (x and x["has"], x))(E.av_video(a)), 40)
        b = E.launch("phone", 2834)
        b.send("WebDriver:SetWindowRect", {"width": 400, "height": 860})
        b.send("WebDriver:Navigate", {"url": link})
        E.wait(lambda: (lambda p: (p["conn"] == "Связь напрямую" and p["player"], p))(E.phone(b)), 40)
        E.tap(b, "#next")
        E.wait(lambda: (lambda p: (p["playing"] and E.secs(p["time"]) >= 2, p))(E.phone(b)), 60)
        E.tap(b, "document.querySelector('[data-seek=\"90\"]').click()")
        time.sleep(4)
        shot(b, "shot-phone.png")
        a.send("WebDriver:NewWindow", {"type": "tab"})
        hs = a.send("WebDriver:GetWindowHandles")
        hs = hs["value"] if isinstance(hs, dict) else hs
        a.send("WebDriver:SwitchToWindow", {"handle": hs[-1]})
        a.send("WebDriver:SetWindowRect", {"width": 420, "height": 760})
        a.send("WebDriver:Navigate", {"url": "moz-extension://%s/popup.html" % uuid})
        time.sleep(2.5)
        shot(a, "shot-popup.png")
    finally:
        for m in (b, a):
            if m:
                E.quit_(m)
        time.sleep(1.5)
        for name in ("pc", "phone"):
            shutil.rmtree(os.path.join(E.HERE, "profile-" + name), ignore_errors=True)


if __name__ == "__main__":
    main()
