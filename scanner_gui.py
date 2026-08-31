#!/usr/bin/env python3
"""
Site Scanner — the window version.

Type a website address, press Scan, get a report. Nobody should need a terminal
to find out that a client's blog category links all 404.

    python scanner_gui.py

tkinter ships with Python on Windows and macOS, so this adds no dependency and
the whole thing still bundles into one .exe with PyInstaller:

    pyinstaller --onefile --windowed --name SiteScanner scanner_gui.py

The scan itself runs in a worker thread and reports progress through a queue.
Tkinter is not thread-safe: only the main thread ever touches a widget.
"""

import os
import queue
import sys
import threading
import traceback
import webbrowser

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from site_scan import (ISSUES, SEV_LABEL, SEV_ORDER, NoSitemap, Options,
                       blog_split, scan, write_reports)

APP_TITLE = "Site Scanner"
PAD = 10


def desktop_dir():
    """The user's real Desktop.

    On a OneDrive-backed Windows account there is no ~/Desktop at all — it lives
    under the OneDrive folder, and the registry knows where. Ask Windows before
    guessing, then fall back through the common shapes.
    """
    if sys.platform == "win32":
        try:
            import winreg
            key = (r"Software\Microsoft\Windows\CurrentVersion"
                   r"\Explorer\User Shell Folders")
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                raw, _ = winreg.QueryValueEx(k, "Desktop")
            path = os.path.expandvars(raw)
            if os.path.isdir(path):
                return path
        except Exception:
            pass

    home = os.path.expanduser("~")
    for candidate in (os.path.join(home, "Desktop"),
                      os.path.join(home, "OneDrive", "Desktop")):
        if os.path.isdir(candidate):
            return candidate
    return home


def default_output_dir():
    return os.path.join(desktop_dir(), "Site Scans")


class ScannerApp(object):

    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.worker = None
        self.paths = None

        root.title(APP_TITLE)
        root.minsize(620, 500)
        try:
            root.call("tk", "scaling", 1.3)     # legible on high-DPI laptops
        except tk.TclError:
            pass

        self._build()
        self.root.after(100, self._drain)

    # ------------------------------------------------------------ layout

    def _build(self):
        outer = ttk.Frame(self.root, padding=PAD + 4)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        ttk.Label(outer, text="Website address",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w")

        entry_row = ttk.Frame(outer)
        entry_row.grid(row=1, column=0, sticky="ew", pady=(2, 2))
        entry_row.columnconfigure(0, weight=1)

        self.url = tk.StringVar()
        self.entry = ttk.Entry(entry_row, textvariable=self.url, font=("Segoe UI", 11))
        self.entry.grid(row=0, column=0, sticky="ew", ipady=4)
        self.entry.bind("<Return>", lambda e: self.start())
        self.entry.focus_set()

        self.scan_btn = ttk.Button(entry_row, text="Scan", command=self.start, width=12)
        self.scan_btn.grid(row=0, column=1, padx=(8, 0))

        ttk.Label(outer, text="e.g. example.com  —  the whole site is scanned "
                              "from its sitemap",
                  foreground="#6b7280").grid(row=2, column=0, sticky="w", pady=(0, PAD))

        # -- options
        opts = ttk.LabelFrame(outer, text="Options", padding=PAD)
        opts.grid(row=3, column=0, sticky="ew")
        opts.columnconfigure(1, weight=1)

        self.external = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Check links to other websites (slower)",
                        variable=self.external).grid(row=0, column=0, columnspan=3,
                                                     sticky="w")

        self.exclude_blog = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Leave blog posts out of the title and "
                                   "description findings",
                        variable=self.exclude_blog).grid(row=1, column=0, columnspan=3,
                                                         sticky="w", pady=(2, 6))

        ttk.Label(opts, text="Stop after").grid(row=2, column=0, sticky="w")
        self.limit = tk.StringVar(value="")
        ttk.Entry(opts, textvariable=self.limit, width=8).grid(row=2, column=1,
                                                               sticky="w", padx=(6, 6))
        ttk.Label(opts, text="pages   (leave empty to scan them all)",
                  foreground="#6b7280").grid(row=2, column=2, sticky="w")

        ttk.Label(opts, text="Save to").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.outdir = tk.StringVar(value=default_output_dir())
        ttk.Entry(opts, textvariable=self.outdir).grid(row=3, column=1, sticky="ew",
                                                       padx=(6, 6), pady=(6, 0))
        ttk.Button(opts, text="Change...", command=self.pick_dir).grid(row=3, column=2,
                                                                      pady=(6, 0))

        # -- progress
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(outer, textvariable=self.status).grid(row=4, column=0, sticky="w",
                                                        pady=(PAD, 2))
        self.bar = ttk.Progressbar(outer, mode="determinate", maximum=100)
        self.bar.grid(row=5, column=0, sticky="ew")

        # -- results
        self.results = ttk.LabelFrame(outer, text="Findings", padding=6)
        self.results.grid(row=6, column=0, sticky="nsew", pady=(PAD, 0))
        outer.rowconfigure(6, weight=1)

        cols = ("severity", "issue", "count")
        self.tree = ttk.Treeview(self.results, columns=cols, show="headings", height=10)
        for name, text, width, anchor in (
                ("severity", "", 74, "center"),
                ("issue", "Finding", 380, "w"),
                ("count", "Count", 110, "e")):
            self.tree.heading(name, text=text)
            self.tree.column(name, width=width, anchor=anchor,
                             stretch=(name == "issue"))
        scroll = ttk.Scrollbar(self.results, orient="vertical",
                               command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.tree.tag_configure("high", foreground="#b42318")
        self.tree.tag_configure("med", foreground="#b54708")
        self.tree.tag_configure("low", foreground="#175cd3")

        # -- footer
        footer = ttk.Frame(outer)
        footer.grid(row=7, column=0, sticky="ew", pady=(PAD, 0))
        self.open_btn = ttk.Button(footer, text="Open report", state="disabled",
                                   command=self.open_report)
        self.open_btn.pack(side="left")
        self.folder_btn = ttk.Button(footer, text="Open folder", state="disabled",
                                     command=self.open_folder)
        self.folder_btn.pack(side="left", padx=(8, 0))
        ttk.Label(footer, text="Reads the page source only — it does not run "
                               "JavaScript.", foreground="#6b7280").pack(side="right")

    # ------------------------------------------------------------ actions

    def pick_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.outdir.get() or "~")
        if chosen:
            self.outdir.set(chosen)

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        domain = self.url.get().strip()
        if not domain:
            messagebox.showinfo(APP_TITLE, "Type a website address first.")
            return

        limit = None
        raw = self.limit.get().strip()
        if raw:
            if not raw.isdigit() or int(raw) < 1:
                messagebox.showinfo(APP_TITLE,
                                    "'Stop after' needs to be a whole number of "
                                    "pages, or empty to scan the whole site.")
                return
            limit = int(raw)

        outdir = self.outdir.get().strip() or default_output_dir()
        try:
            if not os.path.isdir(outdir):
                os.makedirs(outdir)
        except OSError as e:
            messagebox.showerror(APP_TITLE, "Cannot write to that folder:\n\n%s" % e)
            return

        self.tree.delete(*self.tree.get_children())
        for btn in (self.open_btn, self.folder_btn):
            btn.configure(state="disabled")
        self.scan_btn.configure(state="disabled", text="Scanning...")
        self.bar.configure(mode="indeterminate")
        self.bar.start(12)
        self.status.set("Looking for the sitemap...")
        self.paths = None

        opts = Options(limit=limit, external=self.external.get(),
                       exclude_blog=self.exclude_blog.get(), workers=8)
        self.worker = threading.Thread(target=self._work,
                                       args=(domain, opts, outdir), daemon=True)
        self.worker.start()

    def _work(self, domain, opts, outdir):
        """Worker thread. Talks to the UI only through the queue."""
        def progress(stage, done, total):
            self.events.put(("progress", stage, done, total))
        try:
            result = scan(domain, opts, progress=progress)
            paths = write_reports(result, outdir, want_json=True)
            self.events.put(("done", result, paths))
        except NoSitemap:
            self.events.put(("nositemap", domain))
        except Exception:
            self.events.put(("error", traceback.format_exc()))

    def _drain(self):
        """Main thread: apply whatever the worker has posted."""
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "progress":
                    self._on_progress(*event[1:])
                elif kind == "done":
                    self._on_done(*event[1:])
                elif kind == "nositemap":
                    self._on_nositemap(event[1])
                elif kind == "error":
                    self._on_error(event[1])
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    # ------------------------------------------------------------ handlers

    def _on_progress(self, stage, done, total):
        if self.bar["mode"] == "indeterminate":
            self.bar.stop()
            self.bar.configure(mode="determinate")
        label = ("Reading pages" if stage == "crawl"
                 else "Checking links and images")
        self.bar["value"] = (done * 100.0 / total) if total else 0
        self.status.set("%s — %d of %d" % (label, done, total))

    def _finish(self):
        self.bar.stop()
        self.bar.configure(mode="determinate")
        self.scan_btn.configure(state="normal", text="Scan")

    def _on_done(self, result, paths):
        self._finish()
        self.bar["value"] = 100
        self.paths = paths

        total = 0
        for key, sev, label, _ in sorted(ISSUES, key=lambda i: SEV_ORDER[i[1]]):
            rows = result.rows(key)
            if not rows:
                continue
            count, on_blog = blog_split(rows)
            total += count
            shown = "%d" % count
            if on_blog:
                shown = "%d  (%d on blog)" % (count, on_blog)
            self.tree.insert("", "end", tags=(sev,),
                             values=(SEV_LABEL[sev], label, shown))

        pages = result.evidence()["crawl"]["pages_parsed"]
        if total:
            self.status.set("Done. %d finding%s across %d pages in %.0fs."
                            % (total, "" if total == 1 else "s", pages,
                               result.elapsed))
        else:
            self.status.set("Done. Nothing found across %d pages." % pages)
            self.tree.insert("", "end", values=("", "Clean — nothing found.", ""))

        for btn in (self.open_btn, self.folder_btn):
            btn.configure(state="normal")
        self.open_report()

    def _on_nositemap(self, domain):
        self._finish()
        self.bar["value"] = 0
        self.status.set("No sitemap found.")
        messagebox.showwarning(
            APP_TITLE,
            "Could not find a sitemap for %s.\n\n"
            "That is worth noting on its own — most sites should have one.\n\n"
            "Check the address is right, and that the site is reachable."
            % domain)

    def _on_error(self, trace):
        self._finish()
        self.bar["value"] = 0
        self.status.set("The scan stopped with an error.")
        # Show the last line, keep the rest available for whoever debugs it.
        last = [l for l in trace.strip().splitlines() if l.strip()][-1]
        messagebox.showerror(APP_TITLE, "The scan could not finish.\n\n%s" % last)
        sys.stderr.write(trace)

    def open_report(self):
        if self.paths:
            webbrowser.open("file:///" + self.paths["html"].replace("\\", "/"))

    def open_folder(self):
        if not self.paths:
            return
        folder = os.path.dirname(self.paths["html"])
        try:
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                os.system('open "%s"' % folder)
            else:
                os.system('xdg-open "%s"' % folder)
        except Exception:
            webbrowser.open("file:///" + folder.replace("\\", "/"))


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    ScannerApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
