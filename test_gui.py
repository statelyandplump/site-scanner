#!/usr/bin/env python3
"""
Smoke test for the window.

"It imports" proves nothing about a tkinter app — the widget tree is built at
runtime and a bad grid or a missing style blows up only when it renders. This
builds the real window, pushes fake worker events through the same queue the
scan uses, and tears it down. No network.

    python test_gui.py

Skips cleanly with exit 0 where there is no display, so it is safe in CI.
"""

import os
import sys
import tkinter as tk

FAILS = []


def check(name, condition, detail=""):
    if condition:
        print("  ok    %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        FAILS.append(name)


class FakeResult(object):
    """Enough of a ScanResult for the done-handler to render."""

    def __init__(self, findings, elapsed=12.0, pages=42):
        self.findings = findings
        self.elapsed = elapsed
        self._pages = pages

    def rows(self, key):
        return self.findings.get(key) or []

    def evidence(self):
        return {"crawl": {"pages_parsed": self._pages}}


def live(root, app, G, domain="example.com", limit=15, budget=180):
    """Drive a real scan through the real worker thread and event queue.

    Off by default because it hits the network. This is the only check that
    proves the threading, the queue, and report writing work together — the rest
    of the file only proves the widgets do.

    Pass a real domain you are allowed to crawl:
        python test_gui.py --live yourdomain.com
    """
    if "--live" in sys.argv:
        pos = sys.argv.index("--live")
        if pos + 1 < len(sys.argv):
            domain = sys.argv[pos + 1]
    import tempfile
    import time as _t

    print("\nlive scan (--live)")
    app.url.set(domain)
    app.limit.set(str(limit))
    app.external.set(False)
    app.outdir.set(tempfile.mkdtemp(prefix="scan-gui-"))
    app.paths = None
    opened = []
    app.open_report = lambda: opened.append(True)

    app.start()
    check("worker thread started", app.worker is not None and app.worker.is_alive())

    deadline = _t.time() + budget
    while _t.time() < deadline and app.paths is None:
        root.update()                    # pumps _drain via after()
        _t.sleep(0.05)

    check("scan finished inside %ds" % budget, app.paths is not None,
          app.status.get())
    if not app.paths:
        return
    check("html report written", os.path.isfile(app.paths["html"]),
          app.paths["html"])
    check("csv written", os.path.isfile(app.paths["csv"]), app.paths["csv"])
    check("evidence json written", os.path.isfile(app.paths.get("json", "")),
          app.paths.get("json"))
    check("report is not empty", os.path.getsize(app.paths["html"]) > 2000,
          os.path.getsize(app.paths["html"]))
    check("the report was opened for the user", opened == [True])
    check("findings rendered", len(app.tree.get_children()) > 0)
    check("progress bar finished", app.bar["value"] == 100, app.bar["value"])
    check("button returned to Scan", app.scan_btn["text"] == "Scan")
    print("  -> %s" % app.status.get())
    print("  -> %s" % app.paths["html"])


def main():
    try:
        root = tk.Tk()
    except tk.TclError as e:
        print("no display available, skipping GUI smoke test (%s)" % e)
        return 0

    import scanner_gui as G

    # Every messagebox is MODAL: it blocks in mainloop until a human clicks it,
    # which in a test means it hangs forever with no output. Stub them all before
    # touching anything that can raise one, and record what was shown.
    shown = []
    for name in ("showinfo", "showwarning", "showerror"):
        setattr(G.messagebox, name,
                lambda *a, **k: shown.append(a[1] if len(a) > 1 else ""))

    root.withdraw()                     # build it, don't flash it on screen
    app = G.ScannerApp(root)
    root.update()                        # force a real layout pass
    print("gui smoke test")

    check("window builds and lays out", app.tree is not None)
    check("scan button starts enabled", str(app.scan_btn["state"]) == "normal")
    check("report buttons start disabled",
          str(app.open_btn["state"]) == "disabled"
          and str(app.folder_btn["state"]) == "disabled")
    check("output dir defaults somewhere writable",
          os.path.isdir(os.path.dirname(app.outdir.get()) or "."),
          app.outdir.get())
    check("desktop resolves to a real directory",
          os.path.isdir(G.desktop_dir()), G.desktop_dir())
    check("external links default to on", app.external.get() is True)
    check("blog exclusion defaults to off", app.exclude_blog.get() is False)

    # Empty address must not start a scan.
    app.url.set("")
    app.start()
    check("empty address does not start a worker", app.worker is None)

    # A non-numeric page limit must be rejected before any network call.
    app.url.set("example.com")
    app.limit.set("twenty")
    app.start()
    check("bad page limit does not start a worker", app.worker is None)
    app.limit.set("")

    # Progress events must move the bar and not raise.
    app._on_progress("crawl", 5, 50)
    root.update()
    check("progress updates the bar", app.bar["value"] > 0, app.bar["value"])
    check("progress switches off indeterminate mode",
          str(app.bar["mode"]) == "determinate", app.bar["mode"])
    check("progress text names the stage", "Reading pages" in app.status.get(),
          app.status.get())
    app._on_progress("verify", 30, 60)
    root.update()
    check("verify stage renders too", "Checking links" in app.status.get(),
          app.status.get())

    # The done handler, with a blog-segmented finding and a plain one.
    findings = {
        "link_broken": [{"url": "https://ex.com/dead", "detail": "HTTP 404"}],
        "title_long": [{"url": "https://ex.com/blog/a", "detail": "104 chars",
                        "blog": True},
                       {"url": "https://ex.com/anxiety", "detail": "88 chars",
                        "blog": False}],
    }
    app.paths = None
    app.open_report = lambda: None      # don't launch a browser from a test
    app._on_done(FakeResult(findings), {"html": os.path.abspath("nope.html"),
                                        "csv": os.path.abspath("nope.csv")})
    root.update()

    rows = [app.tree.item(i)["values"] for i in app.tree.get_children()]
    check("findings render as rows", len(rows) == 2, rows)
    check("severity ordering puts High first", rows[0][0] == "High", rows)
    counts = " ".join(str(r[2]) for r in rows)
    check("blog split shows in the count column", "on blog" in counts, counts)
    check("status line reports the total", "3 findings" in app.status.get(),
          app.status.get())
    check("report buttons enable after a scan",
          str(app.open_btn["state"]) == "normal")
    check("scan button re-enables", str(app.scan_btn["state"]) == "normal"
          and app.scan_btn["text"] == "Scan")

    # A clean site must say so rather than showing an empty table.
    app._on_done(FakeResult({}), {"html": "x.html", "csv": "x.csv"})
    root.update()
    labels = [app.tree.item(i)["values"][1] for i in app.tree.get_children()]
    check("clean result states it explicitly",
          any("Clean" in str(l) for l in labels), labels)

    # Failure paths must not leave the button stuck on "Scanning...".
    app.scan_btn.configure(state="disabled", text="Scanning...")
    app._on_nositemap("example.com")
    check("no-sitemap re-enables the scan button",
          str(app.scan_btn["state"]) == "normal", app.scan_btn["state"])
    app.scan_btn.configure(state="disabled", text="Scanning...")
    app._on_error("Traceback...\nValueError: something broke\n")
    check("an error re-enables the scan button",
          str(app.scan_btn["state"]) == "normal", app.scan_btn["state"])
    check("failures actually told the user something", len(shown) >= 4, shown)

    if "--live" in sys.argv:
        live(root, app, G)

    root.destroy()
    print("")
    if FAILS:
        print("%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("All GUI controls passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
