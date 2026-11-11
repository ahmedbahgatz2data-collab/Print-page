import os
import re
import json
import base64
import sys
import subprocess
import time
import random
import queue
import datetime
import threading
import zipfile
import hashlib
import shutil
import tempfile
import urllib.request
import urllib.parse
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog

# استخدام محرك Playwright المستقر بدلاً من Selenium لتجنب التعليق
from playwright.sync_api import sync_playwright

# Palette
NAVY = "#323A4E"
NAVY_HOVER = "#414A62"
BLUE = "#1EA5FF"
BLUE_DARK = "#0E8EE6"
BG = "#FFFFFF"
TAB = "#EBEBEB"
BORDER = "#D9D9D9"
TEXT = "#2B2B2B"
GREEN_BG = "#E3F4D7"
GREEN = "#62C93F"

TEXT_OPTS = dict(
    relief="flat", borderwidth=0, highlightthickness=1,
    highlightbackground=BORDER, highlightcolor=BLUE,
    bg="white", fg=TEXT, insertbackground=TEXT,
    selectbackground=BLUE, selectforeground="white",
)
LIST_OPTS = dict(
    relief="flat", borderwidth=0, highlightthickness=1,
    highlightbackground=BORDER, highlightcolor=BLUE,
    bg="white", fg=TEXT, selectbackground=BLUE, selectforeground="white",
)

PAGE_SIZES = {
    "A0": ("ISO_A0", 841000, 1189000),
    "A1": ("ISO_A1", 594000, 841000),
    "A2": ("ISO_A2", 420000, 594000),
    "A3": ("ISO_A3", 297000, 420000),
    "A4": ("ISO_A4", 210000, 297000),
    "A5": ("ISO_A5", 148000, 210000),
    "A6": ("ISO_A6", 105000, 148000),
    "B4": ("ISO_B4", 250000, 353000),
    "B5": ("ISO_B5", 176000, 250000),
    "Letter": ("NA_LETTER", 215900, 279400),
    "Legal": ("NA_LEGAL", 215900, 355600),
    "Tabloid": ("NA_LEDGER", 279400, 431800),
}
CUSTOM_SIZE = "Custom (mm)"

def safe_name(text, max_len=80):
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text[:max_len].strip(" .")
    return text or "page"

def open_file(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass

def num(var, default):
    try:
        return var.get()
    except tk.TclError:
        return default

ALL_SITES = "*"
ALL_SITES_LABEL = "All sites (default)"
STATUS_HEADER = ["Link", "Page Link", "Filename", "Date&Time", "Status", "Notes"]
APP_VERSION = "1.4.0 (Playwright Edition)"
DEFAULT_UPDATE_SOURCE = "https://api.github.com/repos/ahmedbahgatz2data-collab/Print-page/releases/latest"

LAYOUT_PAGED = "Normal pages"
LAYOUT_LONG = "Single long page"
LAYOUTS = (LAYOUT_PAGED, LAYOUT_LONG)
SITE_KEYS = ("page_size", "custom_w", "custom_h", "landscape", "scale", "layout",
             "load_wait", "after_steps", "step_delay", "step_timeout")

COMMON_HIDE = """\
#onetrust-consent-sdk
#onetrust-banner-sdk
#CybotCookiebotDialog
#cookie-law-info-bar
.cc-window
.cookie-banner
.cookie-notice
.cookie-consent
[id*="cookie-banner" i]
[class*="cookie-banner" i]
[id*="cookiebanner" i]
[class*="cookiebanner" i]
[class*="consent-banner" i]
#hubspot-messages-iframe-container
#intercom-container
.intercom-lightweight-app
.drift-frame-controller
iframe#launcher
#tidio-chat
.tawk-min-container
.zopim
"""

def parse_hide_list(text):
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith("//") or "{" in s or "}" in s:
            continue
        out.append(s)
    return out

def settings_file():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "WebToPDF", "settings.json")

def host_of(url):
    try:
        h = urllib.parse.urlparse(url if "//" in url else "//" + url).hostname or ""
    except ValueError:
        h = ""
    h = h.lower()
    return h[4:] if h.startswith("www.") else h

def host_matches(host, key):
    key = (key or "").lower()
    if key.startswith("www."):
        key = key[4:]
    return bool(key) and (host == key or host.endswith("." + key))

def valid_step(s):
    return isinstance(s, dict) and (
        (s.get("type") == "click" and s.get("selector"))
        or (s.get("type") == "wait" and isinstance(s.get("seconds"), (int, float)))
    )

def steps_for_url(step_sets, url):
    host = host_of(url)
    best = None
    for key in step_sets:
        if key != ALL_SITES and host_matches(host, key):
            if best is None or len(key) > len(best):
                best = key
    if best is not None and step_sets[best]:
        return step_sets[best]
    return step_sets.get(ALL_SITES, [])

def tsv(row):
    return "\t".join(re.sub(r"[\t\r\n]+", " ", str(x)) for x in row) + "\n"

def load_status(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return rows
    for n, line in enumerate(lines):
        if not line.strip():
            continue
        parts = line.split("\t")
        if n == 0 and parts[0].strip().lower() == "link":
            continue
        parts += [""] * (6 - len(parts))
        rows.append(parts[:6])
    return rows

def write_status(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\t".join(STATUS_HEADER) + "\n")
        for r in rows:
            f.write(tsv(r))

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Web Pages to PDF - Playwright Engine  v{APP_VERSION}")
        self.geometry("1020x780")
        self.minsize(900, 640)

        self.msg_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.opt_visible = False
        self.step_sets = {ALL_SITES: []}
        self.current_set = ALL_SITES
        self.hide_text = ""
        self.saved_links = ""
        self.site_settings = {}
        self._drag_idx = None

        self.var_folder = tk.StringVar()
        self.var_scale = tk.IntVar(value=100)
        self.var_load_wait = tk.IntVar(value=3)
        self.var_use_title = tk.BooleanVar(value=True)
        self.var_page_size = tk.StringVar(value="A4")
        self.var_landscape = tk.BooleanVar(value=False)
        self.var_dedupe = tk.BooleanVar(value=True)
        self.var_custom_w = tk.DoubleVar(value=210)
        self.var_custom_h = tk.DoubleVar(value=297)
        self.var_url_delay = tk.DoubleVar(value=0)
        self.var_step_timeout = tk.DoubleVar(value=10)
        self.var_step_delay = tk.DoubleVar(value=1)
        self.var_after_steps = tk.DoubleVar(value=2)
        self.var_continue = tk.BooleanVar(value=True)
        self.var_retries = tk.IntVar(value=2)
        self.var_workers = tk.IntVar(value=1)
        self.var_resume = tk.BooleanVar(value=True)
        self.var_unstick = tk.BooleanVar(value=True)
        self.var_layout = tk.StringVar(value=LAYOUT_PAGED)

        self.persist = {
            "folder": self.var_folder, "scale": self.var_scale,
            "load_wait": self.var_load_wait, "use_title": self.var_use_title,
            "page_size": self.var_page_size, "landscape": self.var_landscape,
            "dedupe": self.var_dedupe, "custom_w": self.var_custom_w, "custom_h": self.var_custom_h,
            "url_delay": self.var_url_delay, "step_timeout": self.var_step_timeout,
            "step_delay": self.var_step_delay, "after_steps": self.var_after_steps,
            "continue": self.var_continue, "retries": self.var_retries,
            "workers": self.var_workers, "resume": self.var_resume,
            "unstick": self.var_unstick, "layout": self.var_layout,
        }
        self.load_settings()
        self.build_ui()

        if self.saved_links:
            self.txt_links.insert("1.0", self.saved_links)
            self.update_link_count()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.poll_queue)

    @property
    def steps(self):
        return self.step_sets.setdefault(self.current_set, [])

    @steps.setter
    def steps(self, value):
        self.step_sets[self.current_set] = value

    def load_settings(self):
        try:
            with open(settings_file(), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for key, var in self.persist.items():
            if key in data:
                try:
                    var.set(data[key])
                except Exception:
                    pass
        sets = data.get("step_sets")
        if isinstance(sets, dict):
            clean = {k: [s for s in v if valid_step(s)] for k, v in sets.items() if isinstance(v, list)}
            clean.setdefault(ALL_SITES, [])
            self.step_sets = clean
            if data.get("current_set") in clean:
                self.current_set = data["current_set"]
        if isinstance(data.get("hide_text"), str):
            self.hide_text = data["hide_text"]
        if isinstance(data.get("links"), str):
            self.saved_links = data["links"]

    def save_settings(self):
        data = {key: var.get() for key, var in self.persist.items()}
        data["step_sets"] = self.step_sets
        data["current_set"] = self.current_set
        data["hide_text"] = self.hide_text
        data["links"] = self.txt_links.get("1.0", "end").strip()
        try:
            path = settings_file()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except OSError:
            pass

    def build_ui(self):
        self.apply_theme()
        pad = {"padx": 8, "pady": 4}

        self.toolbar = ttk.Frame(self, style="Toolbar.TFrame")
        self.toolbar.pack(side="top", fill="x")
        tb = ttk.Frame(self.toolbar, style="Toolbar.TFrame")
        tb.pack(fill="x", padx=12, pady=10)
        self.btn_start = ttk.Button(tb, text="\u25B6  Start Batch", style="Accent.TButton", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(tb, text="\u25A0  Stop", style="Toolbar.TButton", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.btn_test = ttk.Button(tb, text="Test First Link", style="Toolbar.TButton", command=self.test_first)
        self.btn_test.pack(side="left", padx=(8, 0))
        self.btn_options = ttk.Button(tb, text="", style="Toolbar.TButton", command=self.toggle_options)
        self.btn_options.pack(side="right")
        self.btn_folder = ttk.Button(tb, text="Open Folder", style="Toolbar.TButton", command=self.open_folder)
        self.btn_folder.pack(side="right", padx=(0, 8))

        status_line = ttk.Frame(self, style="StatusLine.TFrame", height=4)
        status_line.pack(side="bottom", fill="x")
        status = ttk.Frame(self, style="Status.TFrame")
        status.pack(side="bottom", fill="x")
        self.lbl_summary = ttk.Label(status, text="", style="Status.TLabel")
        self.lbl_summary.pack(side="left", padx=12, pady=8)
        self.lbl_progress = ttk.Label(status, text="0 / 0", style="Status.TLabel")
        self.lbl_progress.pack(side="right", padx=12)
        self.progress = ttk.Progressbar(status, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 6))

        box = ttk.LabelFrame(self, text="Options (Playwright Engine)")
        self.opt_box = box
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Save folder:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(box, textvariable=self.var_folder).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(box, text="Browse...", command=self.pick_folder).grid(row=0, column=2, **pad)

        opts = ttk.Frame(box)
        opts.grid(row=1, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(opts, text="Scale %:").pack(side="left")
        ttk.Spinbox(opts, from_=10, to=200, width=6, textvariable=self.var_scale).pack(side="left", padx=(4, 16))
        ttk.Label(opts, text="Page Load Delay (s):").pack(side="left")
        ttk.Spinbox(opts, from_=0, to=30, width=6, textvariable=self.var_load_wait).pack(side="left", padx=(4, 16))
        ttk.Label(opts, text="Delay Between Links (s):").pack(side="left")
        ttk.Spinbox(opts, from_=0, to=60, increment=0.5, width=6, textvariable=self.var_url_delay).pack(side="left", padx=(4, 16))

        pg = ttk.Frame(box)
        pg.grid(row=2, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(pg, text="Page Size:").pack(side="left")
        ttk.Combobox(pg, textvariable=self.var_page_size, state="readonly", width=14, values=list(PAGE_SIZES.keys())).pack(side="left", padx=(4, 12))
        ttk.Checkbutton(pg, text="Landscape", variable=self.var_landscape).pack(side="left", padx=(0, 16))
        ttk.Label(pg, text="Layout:").pack(side="left")
        ttk.Combobox(pg, textvariable=self.var_layout, state="readonly", width=16, values=list(LAYOUTS)).pack(side="left", padx=(4, 0))

        checks = ttk.Frame(box)
        checks.grid(row=3, column=0, columnspan=3, sticky="w", **pad)
        ttk.Checkbutton(checks, text="Use Title as Filename", variable=self.var_use_title).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(checks, text="Skip Already Saved (Resume)", variable=self.var_resume).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(checks, text="Unstick Floating Bars", variable=self.var_unstick).pack(side="left", padx=(0, 16))
        ttk.Button(checks, text="Hide CSS Elements...", command=self.edit_hide).pack(side="left")

        pane_opts = dict(sashwidth=8, sashrelief="flat", bg="#E9ECF1", bd=0, opaqueresize=True)
        main_pane = tk.PanedWindow(self, orient="vertical", **pane_opts)
        main_pane.pack(fill="both", expand=True, padx=10, pady=4)
        mid = tk.PanedWindow(main_pane, orient="horizontal", **pane_opts)
        main_pane.add(mid, stretch="always", minsize=200)

        links = ttk.LabelFrame(mid, text="Links (one per line)")
        mid.add(links, stretch="always", minsize=260)
        self.txt_links = scrolledtext.ScrolledText(links, height=10, width=40, wrap="none", **TEXT_OPTS)
        self.txt_links.pack(fill="both", expand=True, padx=6, pady=6)
        bar = ttk.Frame(links)
        bar.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(bar, text="Clear Links", command=lambda: self.txt_links.delete("1.0", "end")).pack(side="left")
        self.lbl_count = ttk.Label(bar, text="0 links")
        self.lbl_count.pack(side="right")
        self.txt_links.bind("<<Modified>>", self.on_links_modified)

        steps = ttk.LabelFrame(mid, text="Click Steps & Automation")
        mid.add(steps, stretch="always", minsize=410)

        lf = ttk.Frame(steps)
        lf.pack(fill="both", expand=True, padx=6, pady=(6, 2))
        self.lst_steps = tk.Listbox(lf, height=8, activestyle="none", **LIST_OPTS)
        self.lst_steps.pack(fill="both", expand=True)

        r1 = ttk.Frame(steps)
        r1.pack(fill="x", padx=6, pady=2)
        ttk.Button(r1, text="Add Click Step...", command=self.add_click).pack(side="left")
        ttk.Button(r1, text="Add Wait Step...", command=self.add_wait).pack(side="left", padx=4)
        ttk.Button(r1, text="Delete Step", command=self.delete_step).pack(side="left", padx=4)

        logf = ttk.LabelFrame(main_pane, text="Execution Log")
        main_pane.add(logf, stretch="always", minsize=90)
        self.txt_log = scrolledtext.ScrolledText(logf, height=7, state="disabled", **TEXT_OPTS)
        self.txt_log.pack(fill="both", expand=True, padx=6, pady=6)

        self.set_options(False)
        self.update_summary()

    def apply_theme(self):
        self.configure(bg=BG)
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, fieldbackground=BG, bordercolor=BORDER)
        s.configure("Toolbar.TFrame", background=NAVY)
        s.configure("Status.TFrame", background=GREEN_BG)
        s.configure("StatusLine.TFrame", background=GREEN)
        s.configure("Status.TLabel", background=GREEN_BG, foreground="#34502A")
        
        def flat_button(name, bg, fg, hover, bold=False):
            s.configure(name, background=bg, foreground=fg, padding=(10, 5), relief="flat")
            s.map(name, background=[("active", hover)])

        flat_button("TButton", TAB, "#333333", "#DDDDDD")
        flat_button("Accent.TButton", BLUE, "white", BLUE_DARK, bold=True)
        flat_button("Toolbar.TButton", NAVY, "#E6EAF2", NAVY_HOVER)

    def toggle_options(self):
        self.set_options(not self.opt_visible)

    def set_options(self, show):
        self.opt_visible = show
        if show:
            self.opt_box.pack(fill="x", padx=10, pady=(8, 0), after=self.toolbar)
        else:
            self.opt_box.pack_forget()
        self.btn_options.config(text=f"\u2630 Options {'\u25BE' if show else '\u25B8'}")

    def update_summary(self):
        parts = [
            f"Size: {self.var_page_size.get()}",
            f"Scale: {num(self.var_scale, 100)}%",
            "Engine: Playwright Chromium",
            f"Layout: {self.var_layout.get()}"
        ]
        self.lbl_summary.config(text="   \u00B7   ".join(parts))

    def update_link_count(self):
        links = self.get_links()
        self.lbl_count.config(text=f"{len(links)} links")

    def on_links_modified(self, _event=None):
        if self.txt_links.edit_modified():
            self.update_link_count()
            self.txt_links.edit_modified(False)

    def get_links(self):
        raw = self.txt_links.get("1.0", "end").splitlines()
        links = [x.strip() for x in raw if x.strip()]
        return list(dict.fromkeys(links)) if self.var_dedupe.get() else links

    def pick_folder(self):
        d = filedialog.askdirectory(title="Choose save folder")
        if d:
            self.var_folder.set(d)

    def open_folder(self):
        folder = self.var_folder.get().strip()
        if folder and os.path.isdir(folder):
            open_file(folder)

    def edit_hide(self):
        dlg = tk.Toplevel(self)
        dlg.title("Hide Elements CSS")
        txt = scrolledtext.ScrolledText(dlg, width=60, height=12)
        txt.pack(fill="both", expand=True, padx=10, pady=10)
        txt.insert("1.0", self.hide_text)
        def ok():
            self.hide_text = txt.get("1.0", "end").strip()
            dlg.destroy()
        ttk.Button(dlg, text="Save", command=ok).pack(pady=5)

    def add_wait(self):
        sec = simpledialog.askfloat("Add Wait", "Seconds to wait:", minvalue=0.5, maxvalue=60, parent=self)
        if sec:
            self.steps.append({"type": "wait", "seconds": sec})
            self.refresh_steps()

    def add_click(self):
        sel = simpledialog.askstring("Add Click", "Enter CSS selector:", parent=self)
        if sel:
            self.steps.append({"type": "click", "selector": sel})
            self.refresh_steps()

    def delete_step(self):
        sel = self.lst_steps.curselection()
        if sel:
            del self.steps[sel[0]]
            self.refresh_steps()

    def refresh_steps(self):
        self.lst_steps.delete(0, "end")
        for i, st in enumerate(self.steps, 1):
            if st["type"] == "wait":
                self.lst_steps.insert("end", f"{i}. Wait {st['seconds']}s")
            else:
                self.lst_steps.insert("end", f"{i}. Click {st['selector']}")

    def log(self, text):
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", text + "\n")
        self.txt_log.see("end")
        self.txt_log.config(state="disabled")

    def poll_queue(self):
        try:
            while True:
                kind, data = self.msg_queue.get_nowait()
                if kind == "log":
                    self.log(data)
                elif kind == "progress":
                    done, total = data
                    self.progress["maximum"] = max(total, 1)
                    self.progress["value"] = done
                    self.lbl_progress.config(text=f"{done} / {total}")
                elif kind == "done":
                    self.btn_start.config(state="normal")
                    self.btn_stop.config(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self.poll_queue)

    def on_close(self):
        self.save_settings()
        self.stop_event.set()
        self.destroy()

    def start(self):
        folder = self.var_folder.get().strip()
        links = self.get_links()
        if not folder or not links:
            messagebox.showwarning("Warning", "Select folder and input links first.")
            return

        cfg = {
            "folder": folder, "scale": num(self.var_scale, 100),
            "load_wait": num(self.var_load_wait, 3), "use_title": self.var_use_title.get(),
            "page_size": self.var_page_size.get(), "landscape": self.var_landscape.get(),
            "resume": self.var_resume.get(), "unstick": self.var_unstick.get(),
            "layout": self.var_layout.get(), "hide": parse_hide_list(self.hide_text),
            "url_delay": num(self.var_url_delay, 0), "steps": self.steps
        }

        self.save_settings()
        self.stop_event.clear()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")

        self.worker = threading.Thread(target=self.run_playwright_batch, args=(links, cfg), daemon=True)
        self.worker.start()

    def test_first(self):
        links = self.get_links()
        if links:
            self.start()

    def stop(self):
        self.stop_event.set()
        self.log("Stopping after current link...")

    # ----- Playwright Engine Engine Execution -----
    def run_playwright_batch(self, links, cfg):
        q = self.msg_queue
        total = len(links)
        folder = cfg["folder"]
        os.makedirs(folder, exist_ok=True)
        status_path = os.path.join(folder, "Output Status.txt")
        
        q.put(("log", f"Starting Playwright Engine... Processing {total} links."))

        with sync_playwright() as p:
            # تشغيل محرك Chromium بدون فتح نافذة وبدون الاعتماد على chromedriver المعلق
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1920, "height": 1080})

            done_cnt = 0
            for i, link in enumerate(links, 1):
                if self.stop_event.is_set():
                    break

                q.put(("log", f"[{i}/{total}] Navigating: {link}"))
                page = context.new_page()

                try:
                    # مهلة زمنية حاطمة (30 ثانية) تمنع تعليق الصفحة للأبد
                    page.goto(link, wait_until="load", timeout=30000)
                    
                    if cfg["load_wait"] > 0:
                        time.sleep(cfg["load_wait"])

                    # إخفاء العناصر المطلوبة عبر CSS
                    if cfg["hide"] or cfg["unstick"]:
                        css_rules = ""
                        if cfg["hide"]:
                            css_rules += ", ".join(cfg["hide"]) + " { display: none !important; }\n"
                        if cfg["unstick"]:
                            css_rules += "* { position: static !important; }\n"
                        css_rules += "* { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }"
                        page.add_style_tag(content=css_rules)

                    # تنفيذ خطوات الضغط المؤتمتة
                    for st in cfg["steps"]:
                        if st["type"] == "wait":
                            time.sleep(st["seconds"])
                        elif st["type"] == "click":
                            try:
                                page.click(st["selector"], timeout=5000)
                            except Exception:
                                pass

                    # تحديد اسم الملف الحقيقي
                    title = page.title()
                    file_name = f"{i:03d} - {safe_name(title)}" if cfg["use_title"] and title else f"page_{i}_{int(time.time())}"
                    pdf_path = os.path.join(folder, file_name + ".pdf")

                    # طباعة الـ PDF عبر أداة Playwright المدمجة والدقيقة
                    is_full_page = (cfg["layout"] == LAYOUT_LONG)
                    page.pdf(
                        path=pdf_path,
                        format=cfg["page_size"],
                        landscape=cfg["landscape"],
                        print_background=True, # طباعة ألوان وخلفيات الصفحة بنسبة 100%
                        scale=cfg["scale"] / 100.0,
                        full_page=is_full_page,
                        margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"}
                    )

                    q.put(("log", f"   Saved: {file_name}.pdf"))
                    done_cnt += 1

                except Exception as e:
                    err_msg = str(e).splitlines()[0] if str(e) else type(e).__name__
                    q.put(("log", f"   Error on {link}: {err_msg}"))
                finally:
                    page.close()

                q.put(("progress", (i, total)))
                
                if cfg["url_delay"] > 0:
                    time.sleep(cfg["url_delay"])

            browser.close()

        q.put(("log", f"Batch Finished. Successfully saved {done_cnt} / {total} files."))
        q.put(("done", None))

if __name__ == "__main__":
    App().mainloop()
