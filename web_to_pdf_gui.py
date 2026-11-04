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
import urllib.parse
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException

# Free Download Manager inspired palette
NAVY = "#323A4E"        # dark toolbar
NAVY_HOVER = "#414A62"
BLUE = "#1EA5FF"        # accent
BLUE_DARK = "#0E8EE6"
BG = "#FFFFFF"
TAB = "#EBEBEB"         # light grey "tabs" / buttons
BORDER = "#D9D9D9"
TEXT = "#2B2B2B"
GREEN_BG = "#E3F4D7"    # status bar
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


# JavaScript injected into the recording browser. Every click is reported
# through console.log with the prefix __REC__ and read back by Python.
REC_JS = r"""
(function () {
  if (window.__recInstalled) return;
  window.__recInstalled = true;

  function uniq(sel) {
    try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; }
  }

  function buildSelector(el) {
    var tag = el.tagName.toLowerCase();
    if (el.id && uniq('#' + CSS.escape(el.id))) return '#' + CSS.escape(el.id);

    var attrs = ['data-testid', 'data-test', 'data-id', 'name', 'aria-label', 'title'];
    for (var i = 0; i < attrs.length; i++) {
      var v = el.getAttribute(attrs[i]);
      if (v && v.length < 80) {
        var s = tag + '[' + attrs[i] + '="' + v.replace(/"/g, '\\"') + '"]';
        if (uniq(s)) return s;
      }
    }

    var classes = Array.from(el.classList).filter(function (c) {
      return c.length < 40 && !/\d{3,}/.test(c);
    });
    for (var j = 0; j < classes.length; j++) {
      var s1 = tag + '.' + CSS.escape(classes[j]);
      if (uniq(s1)) return s1;
    }
    if (classes.length > 1) {
      var s2 = tag + '.' + classes.map(function (c) { return CSS.escape(c); }).join('.');
      if (uniq(s2)) return s2;
    }

    var parts = [];
    var cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.documentElement) {
      var part = cur.tagName.toLowerCase();
      if (cur.id && uniq('#' + CSS.escape(cur.id))) {
        parts.unshift('#' + CSS.escape(cur.id));
        break;
      }
      var parent = cur.parentElement;
      if (parent) {
        var sibs = Array.from(parent.children).filter(function (c) {
          return c.tagName === cur.tagName;
        });
        if (sibs.length > 1) part += ':nth-of-type(' + (sibs.indexOf(cur) + 1) + ')';
      }
      parts.unshift(part);
      cur = parent;
    }
    return parts.join(' > ');
  }

  document.addEventListener('click', function (e) {
    try {
      var t = e.target.closest('a,button,input,select,label,summary,[role=button],[role=tab],[onclick]') || e.target;
      var d = (t.innerText || t.value || t.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 40);
      var data = { selector: buildSelector(t), desc: t.tagName.toLowerCase() + (d ? ': ' + d : '') };
      console.log('__REC__' + encodeURIComponent(JSON.stringify(data)));
    } catch (err) {}
  }, true);
})();
"""


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
    "Executive": ("NA_EXECUTIVE", 184150, 266700),
    "Statement": ("NA_INVOICE", 139700, 215900),
    "Folio": ("OM_FOLIO", 210000, 330000),
}
CUSTOM_SIZE = "Custom (mm)"


def get_page_dims(cfg):
    """Returns (name, width_microns, height_microns) or None for the browser default."""
    size = cfg["page_size"]
    if size in PAGE_SIZES:
        return PAGE_SIZES[size]
    if size == CUSTOM_SIZE:
        return ("CUSTOM", int(cfg["custom_w"] * 1000), int(cfg["custom_h"] * 1000))
    return None


def pdf_page_size(path):
    """Read the first page size (in points) from a PDF file, or None if not readable."""
    try:
        with open(path, "rb") as f:
            data = f.read(3_000_000)
    except OSError:
        return None
    m = re.search(rb"/MediaBox\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]", data)
    if not m:
        return None
    x0, y0, x1, y1 = (float(v) for v in m.groups())
    return abs(x1 - x0), abs(y1 - y0)


def safe_name(text, max_len=80):
    """Make a title safe as a Windows file name, so Chrome does not need to change it.
    Windows does not allow names that end with a dot or a space."""
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text[:max_len].strip(" .")
    return text or "page"


def xpath_literal(s):
    if "'" not in s:
        return "'" + s + "'"
    if '"' not in s:
        return '"' + s + '"'
    parts = s.split("'")
    return "concat(" + ", \"'\", ".join("'" + p + "'" for p in parts) + ")"


def parse_selector(sel):
    """CSS by default. Prefix with text= or xpath= for other modes."""
    sel = sel.strip()
    low = sel.lower()
    if low.startswith("xpath="):
        return By.XPATH, sel[6:].strip()
    if low.startswith("text="):
        lit = xpath_literal(sel[5:].strip())
        return By.XPATH, f"//*[text()[contains(normalize-space(.), {lit})]]"
    return By.CSS_SELECTOR, sel


STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
"""


def apply_stealth_options(options):
    """Remove the most obvious automation signals (basic, not a guarantee)."""
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--lang=en-US")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])


def apply_stealth_driver(driver):
    """Hide navigator.webdriver, the 'HeadlessChrome' marker and set a normal Accept-Language."""
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
    except Exception:
        pass
    try:
        ua = driver.execute_script("return navigator.userAgent")
        driver.execute_cdp_cmd(
            "Network.setUserAgentOverride",
            {"userAgent": ua.replace("HeadlessChrome", "Chrome"), "acceptLanguage": "en-US,en;q=0.9"},
        )
    except Exception:
        pass


def open_file(path):
    """Open a file with the default viewer (used to show the test PDF)."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def make_service(path):
    return Service(executable_path=path) if path else Service()


def is_displayed(el):
    try:
        return el.is_displayed()
    except Exception:
        return False


def click_element(driver, el):
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    except Exception:
        pass
    try:
        el.click()
    except Exception:
        driver.execute_script("arguments[0].click();", el)


def num(var, default):
    try:
        return var.get()
    except tk.TclError:
        return default


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Web Pages to PDF")
        self.geometry("1020x780")
        self.minsize(900, 640)

        self.msg_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.rec_stop = threading.Event()
        self.worker = None
        self.recording = False
        self.rec_driver = None
        self.steps = []

        self.var_folder = tk.StringVar()
        self.var_driver = tk.StringVar()
        self.var_scale = tk.IntVar(value=100)
        self.var_wait = tk.IntVar(value=5)
        self.var_load_wait = tk.IntVar(value=5)
        self.var_use_title = tk.BooleanVar(value=False)
        self.var_stop_loading = tk.BooleanVar(value=True)
        self.var_page_size = tk.StringVar(value="A4")
        self.var_landscape = tk.BooleanVar(value=False)
        self.var_dedupe = tk.BooleanVar(value=True)
        self.var_browser_mode = tk.StringVar(value="single")
        self.var_engine = tk.StringVar(value="direct")
        self.var_custom_w = tk.DoubleVar(value=210)
        self.var_custom_h = tk.DoubleVar(value=297)
        self.var_stealth = tk.BooleanVar(value=False)
        self.var_url_delay = tk.DoubleVar(value=0)
        self.var_step_timeout = tk.DoubleVar(value=10)
        self.var_step_delay = tk.DoubleVar(value=1)
        self.var_after_steps = tk.DoubleVar(value=2)
        self.var_continue = tk.BooleanVar(value=True)

        self.build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.poll_queue)

    # ---------------- UI ----------------
    def build_ui(self):
        self.apply_theme()
        pad = {"padx": 8, "pady": 4}

        # ----- Toolbar (top) -----
        self.toolbar = ttk.Frame(self, style="Toolbar.TFrame")
        self.toolbar.pack(side="top", fill="x")
        tb = ttk.Frame(self.toolbar, style="Toolbar.TFrame")
        tb.pack(fill="x", padx=12, pady=10)
        self.btn_start = ttk.Button(tb, text="\u25B6  Start", style="Accent.TButton", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(tb, text="\u25A0  Stop", style="Toolbar.TButton", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))
        self.btn_test = ttk.Button(tb, text="Test on first link", style="Toolbar.TButton", command=self.test_first)
        self.btn_test.pack(side="left", padx=(8, 0))
        self.btn_options = ttk.Button(tb, text="", style="Toolbar.TButton", command=self.toggle_options)
        self.btn_options.pack(side="right")

        # ----- Status bar (bottom, always visible) -----
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

        # ----- Options (collapsed by default, toggled from the toolbar) -----
        box = ttk.LabelFrame(self, text="Options")
        self.opt_box = box
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Save folder:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(box, textvariable=self.var_folder).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(box, text="Browse...", command=self.pick_folder).grid(row=0, column=2, **pad)

        ttk.Label(box, text="chromedriver (optional):").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(box, textvariable=self.var_driver).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(box, text="Browse...", command=self.pick_driver).grid(row=1, column=2, **pad)

        opts = ttk.Frame(box)
        opts.grid(row=2, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(opts, text="Scale %:").pack(side="left")
        ttk.Spinbox(opts, from_=10, to=200, width=6, textvariable=self.var_scale).pack(side="left", padx=(4, 16))
        ttk.Label(opts, text="Page load wait (s):").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=120, width=6, textvariable=self.var_load_wait).pack(side="left", padx=(4, 16))
        ttk.Label(opts, text="PDF save timeout (s):").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=300, width=6, textvariable=self.var_wait).pack(side="left", padx=(4, 16))
        ttk.Label(opts, text="Delay between URLs (s):").pack(side="left")
        ttk.Spinbox(opts, from_=0, to=300, increment=0.5, width=6, textvariable=self.var_url_delay).pack(side="left", padx=(4, 16))

        pg = ttk.Frame(box)
        pg.grid(row=3, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(pg, text="Page size:").pack(side="left")
        ttk.Combobox(
            pg, textvariable=self.var_page_size, state="readonly", width=16,
            values=["Browser default"] + list(PAGE_SIZES) + [CUSTOM_SIZE],
        ).pack(side="left", padx=(4, 12))
        ttk.Checkbutton(pg, text="Landscape", variable=self.var_landscape).pack(side="left", padx=(0, 16))
        ttk.Label(pg, text="Custom size (mm):").pack(side="left")
        ttk.Spinbox(pg, from_=20, to=2000, width=6, textvariable=self.var_custom_w).pack(side="left", padx=(4, 2))
        ttk.Label(pg, text="x").pack(side="left")
        ttk.Spinbox(pg, from_=20, to=2000, width=6, textvariable=self.var_custom_h).pack(side="left", padx=(2, 0))

        br = ttk.Frame(box)
        br.grid(row=4, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(br, text="Browser:").pack(side="left")
        ttk.Radiobutton(
            br, text="Close and reopen after every URL",
            variable=self.var_browser_mode, value="each",
        ).pack(side="left", padx=(4, 12))
        ttk.Radiobutton(
            br, text="One browser for all URLs",
            variable=self.var_browser_mode, value="single",
        ).pack(side="left")

        eng = ttk.Frame(box)
        eng.grid(row=5, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(eng, text="Print method:").pack(side="left")
        ttk.Radiobutton(
            eng, text="Direct PDF (recommended: exact size and scale)",
            variable=self.var_engine, value="direct",
        ).pack(side="left", padx=(4, 12))
        ttk.Radiobutton(
            eng, text="Chrome print dialog (fallback)",
            variable=self.var_engine, value="kiosk",
        ).pack(side="left")

        checks = ttk.Frame(box)
        checks.grid(row=6, column=0, columnspan=3, sticky="w", **pad)
        ttk.Checkbutton(checks, text="Use page title as file name", variable=self.var_use_title).pack(side="left", padx=(0, 20))
        ttk.Checkbutton(checks, text="Stop page loading before running steps", variable=self.var_stop_loading).pack(side="left", padx=(0, 20))
        ttk.Checkbutton(
            checks, text="Remove duplicate links automatically",
            variable=self.var_dedupe, command=self.update_link_count,
        ).pack(side="left", padx=(0, 20))
        ttk.Checkbutton(
            checks, text="Stealth mode (reduce automation signals)", variable=self.var_stealth,
        ).pack(side="left")

        # ----- Links | Steps | Log  (drag the dividers to resize) -----
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
        ttk.Button(bar, text="Load from txt file", command=self.load_links_file).pack(side="left")
        ttk.Button(bar, text="Clear", command=lambda: self.txt_links.delete("1.0", "end")).pack(side="left", padx=6)
        self.lbl_count = ttk.Label(bar, text="0 links")
        self.lbl_count.pack(side="right")
        self.txt_links.bind("<<Modified>>", self.on_links_modified)

        steps = ttk.LabelFrame(mid, text="Steps run on every link before saving")
        mid.add(steps, stretch="always", minsize=410)

        lf = ttk.Frame(steps)
        lf.pack(fill="both", expand=True, padx=6, pady=(6, 2))
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)
        self.lst_steps = tk.Listbox(lf, height=8, activestyle="none", exportselection=False, **LIST_OPTS)
        self.lst_steps.grid(row=0, column=0, sticky="nsew")
        sy = ttk.Scrollbar(lf, orient="vertical", command=self.lst_steps.yview)
        sy.grid(row=0, column=1, sticky="ns")
        sx = ttk.Scrollbar(lf, orient="horizontal", command=self.lst_steps.xview)
        sx.grid(row=1, column=0, sticky="ew")
        self.lst_steps.config(yscrollcommand=sy.set, xscrollcommand=sx.set)

        r1 = ttk.Frame(steps)
        r1.pack(fill="x", padx=6, pady=2)
        self.btn_record = ttk.Button(r1, text="\u25CF Record clicks", command=self.toggle_record)
        self.btn_record.pack(side="left")
        ttk.Button(r1, text="Add click...", command=self.add_click).pack(side="left", padx=4)
        ttk.Button(r1, text="Add wait...", command=self.add_wait).pack(side="left")

        r2 = ttk.Frame(steps)
        r2.pack(fill="x", padx=6, pady=2)
        ttk.Button(r2, text="Delete", command=self.delete_step).pack(side="left")
        ttk.Button(r2, text="Up", width=5, command=lambda: self.move_step(-1)).pack(side="left", padx=4)
        ttk.Button(r2, text="Down", width=6, command=lambda: self.move_step(1)).pack(side="left")
        ttk.Button(r2, text="Clear all", command=self.clear_steps).pack(side="left", padx=4)
        ttk.Button(r2, text="Save...", command=self.save_steps).pack(side="left")
        ttk.Button(r2, text="Load...", command=self.load_steps).pack(side="left", padx=4)

        sg = ttk.Frame(steps)
        sg.pack(fill="x", padx=6, pady=(4, 6))
        ttk.Label(sg, text="Element timeout (s):").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(sg, from_=1, to=120, increment=1, width=6, textvariable=self.var_step_timeout).grid(row=0, column=1, padx=(4, 12))
        ttk.Label(sg, text="Delay between steps (s):").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(sg, from_=0, to=60, increment=0.5, width=6, textvariable=self.var_step_delay).grid(row=0, column=3, padx=4)
        ttk.Label(sg, text="Wait after steps (s):").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Spinbox(sg, from_=0, to=120, increment=0.5, width=6, textvariable=self.var_after_steps).grid(row=1, column=1, padx=(4, 12), pady=(4, 0))
        ttk.Checkbutton(sg, text="Continue if an element is not found", variable=self.var_continue).grid(row=1, column=2, columnspan=2, sticky="w", pady=(4, 0))

        # ----- Log -----
        logf = ttk.LabelFrame(main_pane, text="Log")
        main_pane.add(logf, stretch="always", minsize=90)
        self.txt_log = scrolledtext.ScrolledText(logf, height=7, state="disabled", **TEXT_OPTS)
        self.txt_log.pack(fill="both", expand=True, padx=6, pady=6)

        for v in (self.var_page_size, self.var_landscape, self.var_scale, self.var_engine,
                  self.var_browser_mode, self.var_custom_w, self.var_custom_h, self.var_stealth):
            v.trace_add("write", self.update_summary)
        self.update_summary()
        self.set_options(False)

    # ---------------- theme and options panel ----------------
    def apply_theme(self):
        self.configure(bg=BG)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(family="Segoe UI", size=10)
            except tk.TclError:
                pass

        self.option_add("*TCombobox*Listbox.background", "white")
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", BLUE)
        self.option_add("*TCombobox*Listbox.selectForeground", "white")

        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, fieldbackground=BG, bordercolor=BORDER,
                    lightcolor=BG, darkcolor=BG, troughcolor="#F0F0F0", focuscolor=BG)
        s.configure("TFrame", background=BG)
        s.configure("TLabel", background=BG, foreground=TEXT)
        s.configure("TLabelframe", background=BG, bordercolor=BORDER, lightcolor=BG,
                    darkcolor=BG, relief="solid", borderwidth=1)
        s.configure("TLabelframe.Label", background=BG, foreground="#4A4A4A",
                    font=("Segoe UI", 10, "bold"))

        def flat_button(name, bg, fg, hover, pressed, dis_bg, dis_fg, padding, bold=False):
            s.configure(name, background=bg, foreground=fg, bordercolor=bg, lightcolor=bg,
                        darkcolor=bg, focuscolor=bg, padding=padding, relief="flat", borderwidth=1)
            if bold:
                s.configure(name, font=("Segoe UI", 10, "bold"))
            states = [("disabled", dis_bg), ("pressed", pressed), ("active", hover)]
            s.map(name, background=states, bordercolor=states, lightcolor=states, darkcolor=states,
                  foreground=[("disabled", dis_fg)])

        flat_button("TButton", TAB, "#333333", "#DDDDDD", "#D0D0D0", "#F4F4F4", "#A8A8A8", (10, 5))
        flat_button("Accent.TButton", BLUE, "white", BLUE_DARK, "#0A7DCC", "#A8D9FB", "white", (18, 8), bold=True)
        flat_button("Toolbar.TButton", NAVY, "#E6EAF2", NAVY_HOVER, "#2A3142", NAVY, "#7A859B", (12, 8))

        for w in ("TEntry", "TSpinbox", "TCombobox"):
            s.configure(w, fieldbackground="white", foreground=TEXT, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, padding=4, arrowcolor="#5A6478")
            s.map(w, bordercolor=[("focus", BLUE)], lightcolor=[("focus", BLUE)],
                  darkcolor=[("focus", BLUE)])
        s.configure("TSpinbox", background="#F3F3F3")
        s.configure("TCombobox", background="#F3F3F3")
        s.map("TCombobox", fieldbackground=[("readonly", "white")],
              foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", "white")],
              selectforeground=[("readonly", TEXT)])

        for w in ("TCheckbutton", "TRadiobutton"):
            s.configure(w, background=BG, foreground=TEXT, indicatorbackground="white",
                        indicatorforeground="white", upperbordercolor="#B5BCC8",
                        lowerbordercolor="#B5BCC8")
            s.map(w, background=[("active", BG)],
                  indicatorbackground=[("selected", BLUE), ("pressed", "#CFE9FD")],
                  indicatorforeground=[("selected", "white")],
                  upperbordercolor=[("selected", BLUE)], lowerbordercolor=[("selected", BLUE)])

        s.configure("Horizontal.TProgressbar", troughcolor="#D5DBE4", background=BLUE,
                    bordercolor="#D5DBE4", lightcolor=BLUE, darkcolor=BLUE, thickness=10)
        s.configure("TScrollbar", background="#C5CBD5", troughcolor="#F3F3F3", bordercolor="#F3F3F3",
                    arrowcolor="#5A6478", lightcolor="#C5CBD5", darkcolor="#C5CBD5")
        s.map("TScrollbar", background=[("active", "#AEB6C3")])

        s.configure("Toolbar.TFrame", background=NAVY)
        s.configure("Status.TFrame", background=GREEN_BG)
        s.configure("StatusLine.TFrame", background=GREEN)
        s.configure("Status.TLabel", background=GREEN_BG, foreground="#34502A")

    def toggle_options(self):
        self.set_options(not self.opt_visible)

    def set_options(self, show):
        self.opt_visible = show
        if show:
            self.opt_box.pack(fill="x", padx=10, pady=(8, 0), after=self.toolbar)
        else:
            self.opt_box.pack_forget()
        arrow = "\u25BE" if show else "\u25B8"
        self.btn_options.config(text=f"\u2630  Options  {arrow}")

    def update_summary(self, *_):
        size = self.var_page_size.get()
        direct = self.var_engine.get() == "direct"
        if size == CUSTOM_SIZE:
            size = f"{num(self.var_custom_w, 0):g} x {num(self.var_custom_h, 0):g} mm"
        elif size == "Browser default" and direct:
            size = "A4"
        if self.var_landscape.get():
            size += " landscape"
        parts = [
            size,
            f"{num(self.var_scale, 100)}%",
            "Direct PDF" if direct else "Print dialog",
            "one browser" if self.var_browser_mode.get() == "single" else "browser per URL",
        ]
        if self.var_stealth.get():
            parts.append("stealth")
        self.lbl_summary.config(text="   \u00B7   ".join(parts))

    # ---------------- helpers ----------------
    def update_link_count(self):
        all_links = self.get_links(dedupe=False)
        links = self.get_links()
        text = f"{len(links)} links"
        if len(all_links) != len(links):
            text += f" ({len(all_links) - len(links)} duplicates ignored)"
        self.lbl_count.config(text=text)

    def on_links_modified(self, _event=None):
        if self.txt_links.edit_modified():
            self.update_link_count()
            self.txt_links.edit_modified(False)

    def get_links(self, dedupe=True):
        raw = self.txt_links.get("1.0", "end").splitlines()
        links = [x.strip() for x in raw if x.strip()]
        if dedupe and self.var_dedupe.get():
            links = list(dict.fromkeys(links))  # keeps first occurrence and order
        return links

    def pick_folder(self):
        d = filedialog.askdirectory(title="Choose save folder")
        if d:
            self.var_folder.set(d)

    def pick_driver(self):
        f = filedialog.askopenfilename(
            title="Choose chromedriver",
            filetypes=[("chromedriver", "*.exe"), ("All files", "*.*")],
        )
        if f:
            self.var_driver.set(f)

    def load_links_file(self):
        f = filedialog.askopenfilename(
            title="Choose links file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if f:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                self.txt_links.delete("1.0", "end")
                self.txt_links.insert("1.0", fh.read())

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
                    self.btn_record.config(state="normal")
                    self.btn_test.config(state="normal")
                elif kind == "rec_step":
                    self.steps.append(data)
                    self.refresh_steps(select=len(self.steps) - 1)
                    self.log(f"Recorded: click {data['selector']}")
                elif kind == "rec_done":
                    self.recording = False
                    self.btn_record.config(text="\u25CF Record clicks")
                    self.btn_start.config(state="normal")
                    self.btn_test.config(state="normal")
                    self.log(f"Recording finished. {len(self.steps)} step(s) in the list.")
        except queue.Empty:
            pass
        self.after(100, self.poll_queue)

    def on_close(self):
        self.rec_stop.set()
        self.stop_event.set()
        d = self.rec_driver
        if d is not None:
            try:
                d.quit()
            except Exception:
                pass
        self.destroy()

    # ---------------- steps list ----------------
    def step_text(self, n, st):
        if st["type"] == "wait":
            return f"{n}. Wait {st['seconds']:g} s"
        prefix = "Click ALL" if st.get("all") else "Click"
        extra = f"   ({st['desc']})" if st.get("desc") else ""
        return f"{n}. {prefix}: {st['selector']}{extra}"

    def refresh_steps(self, select=None):
        self.lst_steps.delete(0, "end")
        for n, st in enumerate(self.steps, 1):
            self.lst_steps.insert("end", self.step_text(n, st))
        if select is not None and 0 <= select < len(self.steps):
            self.lst_steps.selection_set(select)
            self.lst_steps.see(select)

    def selected_index(self):
        sel = self.lst_steps.curselection()
        return sel[0] if sel else None

    def delete_step(self):
        i = self.selected_index()
        if i is None:
            return
        del self.steps[i]
        self.refresh_steps(select=min(i, len(self.steps) - 1))

    def move_step(self, delta):
        i = self.selected_index()
        if i is None:
            return
        j = i + delta
        if 0 <= j < len(self.steps):
            self.steps[i], self.steps[j] = self.steps[j], self.steps[i]
            self.refresh_steps(select=j)

    def clear_steps(self):
        if self.steps and messagebox.askyesno("Clear steps", "Remove all steps?"):
            self.steps = []
            self.refresh_steps()

    def add_wait(self):
        sec = simpledialog.askfloat("Add wait", "Seconds to wait:", minvalue=0.1, maxvalue=600, parent=self)
        if sec:
            self.steps.append({"type": "wait", "seconds": sec})
            self.refresh_steps(select=len(self.steps) - 1)

    def add_click(self):
        dlg = tk.Toplevel(self)
        dlg.title("Add click step")
        dlg.transient(self)
        dlg.configure(bg=BG)
        dlg.resizable(False, False)

        var = tk.StringVar()
        all_var = tk.BooleanVar(value=False)

        ttk.Label(dlg, text="Selector:").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 2))
        ent = ttk.Entry(dlg, textvariable=var, width=64)
        ent.grid(row=1, column=0, columnspan=2, padx=10)
        ttk.Label(
            dlg,
            text="CSS selector, or  text=Visible text,  or  xpath=//div[@id='x']",
            foreground="gray",
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=2)
        ttk.Checkbutton(dlg, text="Click ALL matching elements", variable=all_var).grid(
            row=3, column=0, columnspan=2, sticky="w", padx=10, pady=4
        )

        def ok():
            s = var.get().strip()
            if not s:
                return
            self.steps.append({"type": "click", "selector": s, "desc": "", "all": all_var.get()})
            self.refresh_steps(select=len(self.steps) - 1)
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", padx=10, pady=10)
        ttk.Button(btns, text="OK", command=ok).pack(side="left")
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="left", padx=6)
        dlg.bind("<Return>", lambda _e: ok())
        dlg.grab_set()
        ent.focus_set()

    def save_steps(self):
        if not self.steps:
            messagebox.showinfo("Save steps", "There are no steps to save.")
            return
        f = filedialog.asksaveasfilename(
            title="Save steps", defaultextension=".json", filetypes=[("JSON", "*.json")]
        )
        if f:
            with open(f, "w", encoding="utf-8") as fh:
                json.dump(self.steps, fh, indent=2, ensure_ascii=False)

    def load_steps(self):
        f = filedialog.askopenfilename(title="Load steps", filetypes=[("JSON", "*.json")])
        if not f:
            return
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
            ok = isinstance(data, list) and all(
                isinstance(s, dict)
                and (
                    (s.get("type") == "click" and s.get("selector"))
                    or (s.get("type") == "wait" and isinstance(s.get("seconds"), (int, float)))
                )
                for s in data
            )
            if not ok:
                raise ValueError("Invalid steps file.")
        except Exception as e:
            messagebox.showerror("Load steps", f"Could not load steps:\n{e}")
            return
        self.steps = data
        self.refresh_steps()

    # ---------------- recording ----------------
    def toggle_record(self):
        if self.recording:
            self.rec_stop.set()
            return
        if self.worker is not None and self.worker.is_alive():
            return

        links = self.get_links()
        url = links[0] if links else simpledialog.askstring(
            "Record clicks", "Enter a sample page URL to open:", parent=self
        )
        if not url:
            return
        url = url.strip()
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url

        self.recording = True
        self.rec_stop.clear()
        self.btn_record.config(text="\u25A0 Stop recording")
        self.btn_start.config(state="disabled")
        self.btn_test.config(state="disabled")
        self.log("Recording started. Click the elements you want in the browser window, "
                 "in order, then press 'Stop recording' here.")
        threading.Thread(
            target=self.record_worker,
            args=(url, self.var_driver.get().strip(), self.var_stealth.get()),
            daemon=True,
        ).start()

    def drain_logs(self, driver):
        for entry in driver.get_log("browser"):
            m = re.search(r"__REC__([^\"'\s]+)", entry.get("message", ""))
            if not m:
                continue
            try:
                data = json.loads(urllib.parse.unquote(m.group(1)))
                self.msg_queue.put(("rec_step", {
                    "type": "click",
                    "selector": data["selector"],
                    "desc": data.get("desc", ""),
                    "all": False,
                }))
            except Exception:
                pass

    def record_worker(self, url, driver_path, stealth=False):
        q = self.msg_queue
        driver = None
        try:
            options = webdriver.ChromeOptions()
            options.add_argument("--start-maximized")
            options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
            if stealth:
                apply_stealth_options(options)
            driver = webdriver.Chrome(service=make_service(driver_path), options=options)
            self.rec_driver = driver
            if stealth:
                apply_stealth_driver(driver)
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": REC_JS})
            driver.get(url)

            while not self.rec_stop.is_set():
                try:
                    self.drain_logs(driver)
                except Exception:
                    break  # browser was closed
                time.sleep(0.4)

            try:
                self.drain_logs(driver)
            except Exception:
                pass
        except Exception as e:
            short = str(e).splitlines()[0] if str(e) else type(e).__name__
            q.put(("log", f"Recording error: {short}"))
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
            self.rec_driver = None
            q.put(("rec_done", None))

    # ---------------- actions ----------------
    def build_cfg(self):
        """Validate the settings. Returns (links, cfg), or None if something is missing."""
        folder = self.var_folder.get().strip()
        links = self.get_links()
        if not folder:
            self.set_options(True)
            messagebox.showwarning("Warning", "Choose a save folder first (Options panel).")
            return None
        if not os.path.isdir(folder):
            try:
                os.makedirs(folder)
            except OSError as e:
                messagebox.showerror("Error", f"Could not create the folder:\n{e}")
                return None
        if not links:
            messagebox.showwarning("Warning", "Add at least one link.")
            return None

        cfg = {
            "folder": os.path.abspath(folder),
            "driver": self.var_driver.get().strip(),
            "scale": num(self.var_scale, 100),
            "wait": num(self.var_wait, 5),
            "load_wait": num(self.var_load_wait, 5),
            "use_title": self.var_use_title.get(),
            "stop_loading": self.var_stop_loading.get(),
            "step_timeout": num(self.var_step_timeout, 10),
            "step_delay": num(self.var_step_delay, 1),
            "after_steps": num(self.var_after_steps, 2),
            "continue_missing": self.var_continue.get(),
            "steps": json.loads(json.dumps(self.steps)),
            "page_size": self.var_page_size.get(),
            "landscape": self.var_landscape.get(),
            "new_browser": self.var_browser_mode.get() == "each",
            "engine": self.var_engine.get(),
            "custom_w": num(self.var_custom_w, 210),
            "custom_h": num(self.var_custom_h, 297),
            "stealth": self.var_stealth.get(),
            "url_delay": num(self.var_url_delay, 0),
        }
        return links, cfg

    def start(self):
        if self.recording:
            messagebox.showwarning("Warning", "Stop the recording first.")
            return
        built = self.build_cfg()
        if not built:
            return
        links, cfg = built

        self.stop_event.clear()
        self.btn_start.config(state="disabled")
        self.btn_record.config(state="disabled")
        self.btn_test.config(state="disabled")
        self.btn_stop.config(state="normal")
        dupes = len(self.get_links(dedupe=False)) - len(links)
        if dupes:
            self.msg_queue.put(("log", f"Ignored {dupes} duplicate link(s). Processing {len(links)} unique link(s)."))
        self.msg_queue.put(("progress", (0, len(links))))
        self.worker = threading.Thread(target=self.run_job, args=(links, cfg), daemon=True)
        self.worker.start()

    def test_first(self):
        """Run the current settings and steps on the first link only, then open the PDF."""
        if self.recording:
            messagebox.showwarning("Warning", "Stop the recording first.")
            return
        if self.worker is not None and self.worker.is_alive():
            return
        built = self.build_cfg()
        if not built:
            return
        links, cfg = built

        test_folder = os.path.join(cfg["folder"], "_test")
        try:
            os.makedirs(test_folder, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Error", f"Could not create the test folder:\n{e}")
            return
        cfg = dict(cfg, folder=test_folder, test=True, new_browser=True)

        self.stop_event.clear()
        self.btn_start.config(state="disabled")
        self.btn_test.config(state="disabled")
        self.btn_record.config(state="disabled")
        self.worker = threading.Thread(target=self.run_test, args=(links[0], cfg), daemon=True)
        self.worker.start()

    def run_test(self, link, cfg):
        q = self.msg_queue
        direct = cfg["engine"] == "direct"
        size = cfg["page_size"]
        if size == CUSTOM_SIZE:
            size = f"Custom {cfg['custom_w']:g} x {cfg['custom_h']:g} mm"
        elif size == "Browser default" and direct:
            size = "A4 (default)"
        orient = "landscape" if cfg["landscape"] else "portrait"
        method = "Direct PDF" if direct else "Chrome print dialog"

        q.put(("log", f"TEST on first link: {link}"))
        q.put(("log", f"   settings: {size}, {orient}, scale {cfg['scale']}%, {method}, {len(cfg['steps'])} step(s)"))
        driver = None
        try:
            driver = self.new_driver(cfg)
            saved, _url = self.process_url(driver, 0, link, cfg)
            q.put(("log", f"   test file: {saved}{self.describe_pdf(cfg, saved)}"))
            q.put(("log", f"   opening it (saved in: {cfg['folder']})"))
            open_file(os.path.join(cfg["folder"], saved))
        except Exception as e:
            short = str(e).splitlines()[0] if str(e) else type(e).__name__
            q.put(("log", f"   test error: {short}"))
        finally:
            self.close_driver(driver)
        q.put(("done", None))

    def stop(self):
        self.stop_event.set()
        self.msg_queue.put(("log", "Stopping after the current link..."))

    # ---------------- worker ----------------
    def make_options(self, cfg):
        settings = {
            "recentDestinations": [{"id": "Save as PDF", "origin": "local", "account": ""}],
            "selectedDestinationId": "Save as PDF",
            "version": 2,
            "isHeaderFooterEnabled": False,
            "isLandscapeEnabled": cfg["landscape"],
            "scalingType": 3,
            "scaling": str(cfg["scale"]),
        }
        dims = get_page_dims(cfg)
        if dims:
            name, w, h = dims
            settings["mediaSize"] = {
                "name": name,
                "width_microns": w,
                "height_microns": h,
                "custom_display_name": cfg["page_size"],
            }
        prefs = {
            "printing.print_preview_sticky_settings.appState": json.dumps(settings),
            "savefile.default_directory": cfg["folder"],
        }
        options = webdriver.ChromeOptions()
        options.page_load_strategy = "none"
        options.add_argument("--start-maximized")
        options.add_argument("--kiosk-printing")
        options.add_experimental_option("prefs", prefs)
        if cfg.get("stealth"):
            apply_stealth_options(options)
        return options

    def run_steps(self, driver, cfg):
        q = self.msg_queue
        for n, st in enumerate(cfg["steps"], 1):
            if st["type"] == "wait":
                q.put(("log", f"   step {n}: waiting {st['seconds']:g} s"))
                time.sleep(st["seconds"])
                continue

            by, val = parse_selector(st["selector"])
            try:
                els = WebDriverWait(driver, cfg["step_timeout"]).until(
                    lambda d: d.find_elements(by, val) or False
                )
            except TimeoutException:
                els = []

            if not els:
                msg = f"step {n}: element not found ({st['selector']})"
                if cfg["continue_missing"]:
                    q.put(("log", f"   warning: {msg}"))
                    continue
                raise RuntimeError(msg)

            visible = [e for e in els if is_displayed(e)] or els
            targets = visible if st.get("all") else visible[:1]
            clicked = 0
            for el in targets:
                try:
                    click_element(driver, el)
                    clicked += 1
                except Exception:
                    pass
            q.put(("log", f"   step {n}: clicked {clicked} element(s)"))
            time.sleep(cfg["step_delay"])

    @staticmethod
    def driver_alive(driver):
        try:
            _ = driver.window_handles
            return True
        except Exception:
            return False

    @staticmethod
    def close_driver(driver):
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    @staticmethod
    def pdf_names(folder):
        try:
            return {f for f in os.listdir(folder) if f.lower().endswith(".pdf")}
        except OSError:
            return set()

    def make_direct_options(self, cfg):
        options = webdriver.ChromeOptions()
        options.page_load_strategy = "none"
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")
        if cfg.get("stealth"):
            apply_stealth_options(options)
        return options

    def new_driver(self, cfg):
        if cfg["engine"] == "direct":
            driver = webdriver.Chrome(service=make_service(cfg["driver"]), options=self.make_direct_options(cfg))
            try:  # remove the "HeadlessChrome" marker that some sites block
                ua = driver.execute_script("return navigator.userAgent")
                driver.execute_cdp_cmd(
                    "Network.setUserAgentOverride",
                    {"userAgent": ua.replace("HeadlessChrome", "Chrome")},
                )
            except Exception:
                pass
        else:
            driver = webdriver.Chrome(service=make_service(cfg["driver"]), options=self.make_options(cfg))
        if cfg.get("stealth"):
            apply_stealth_driver(driver)
        return driver

    def print_direct(self, driver, cfg, filename):
        """Save the page with Chrome's PDF engine: exact paper size and scale, no dialog."""
        _name, w, h = get_page_dims(cfg) or PAGE_SIZES["A4"]
        params = {
            "landscape": cfg["landscape"],
            "printBackground": True,
            "displayHeaderFooter": False,
            "scale": min(2.0, max(0.1, cfg["scale"] / 100.0)),
            "paperWidth": w / 25400.0,   # microns -> inches
            "paperHeight": h / 25400.0,
        }
        result = driver.execute_cdp_cmd("Page.printToPDF", params)
        name = filename + ".pdf"
        with open(os.path.join(cfg["folder"], name), "wb") as f:
            f.write(base64.b64decode(result["data"]))
        return name

    @staticmethod
    def describe_pdf(cfg, name):
        """Read the real page size from the saved PDF and compare it with the selected size."""
        size = pdf_page_size(os.path.join(cfg["folder"], name))
        if not size:
            return ""
        w_pt, h_pt = size
        text = f"  [{w_pt / 72 * 25.4:.0f} x {h_pt / 72 * 25.4:.0f} mm]"
        dims = get_page_dims(cfg)
        if dims:
            _n, w, h = dims
            exp_w, exp_h = w / 25400 * 72, h / 25400 * 72
            if cfg["landscape"]:
                exp_w, exp_h = exp_h, exp_w
            if abs(exp_w - w_pt) < 3 and abs(exp_h - h_pt) < 3:
                text += "  size OK"
            else:
                text += f"  SIZE DIFFERS from {cfg['page_size']}"
        return text

    def wait_for_new_pdf(self, folder, before, timeout):
        """Wait for a new PDF to appear and finish writing. Returns its real file name."""
        deadline = time.time() + timeout
        sizes = {}
        while time.time() < deadline:
            for name in sorted(self.pdf_names(folder) - before):
                try:
                    size = os.path.getsize(os.path.join(folder, name))
                except OSError:
                    continue
                if size > 0 and sizes.get(name) == size:
                    return name
                sizes[name] = size
            time.sleep(0.4)
        return None

    def process_url(self, driver, i, link, cfg):
        driver.get(link)
        time.sleep(cfg["load_wait"])
        if cfg["stop_loading"]:
            driver.execute_script("window.stop();")

        if cfg["steps"]:
            self.run_steps(driver, cfg)
            time.sleep(cfg["after_steps"])

        stamp = time.strftime("%H-%M-%S")
        is_test = cfg.get("test", False)
        if cfg["use_title"]:
            prefix = "TEST" if is_test else f"{i + 1:03d}"
            filename = f"{prefix} - {safe_name(driver.title)}"
        else:
            filename = f"TEST {stamp}" if is_test else f"filename{i}{stamp}"
        if os.path.exists(os.path.join(cfg["folder"], filename + ".pdf")):
            filename += f" {stamp}"  # avoid Chrome adding " (1)" to the name

        if cfg["engine"] == "direct":
            saved = self.print_direct(driver, cfg, filename)
            return saved, driver.current_url

        before = self.pdf_names(cfg["folder"])
        driver.execute_script(f"document.title = {json.dumps(filename)};")
        driver.execute_script("window.print();")
        saved = self.wait_for_new_pdf(cfg["folder"], before, cfg["wait"])
        if not saved:
            raise RuntimeError(f"PDF file did not appear within {cfg['wait']} s")
        return saved, driver.current_url

    def run_job(self, links, cfg):
        q = self.msg_queue
        total = len(links)
        status_path = os.path.join(cfg["folder"], "Output Status.txt")
        new_file = not os.path.exists(status_path)
        driver = None

        try:
            with open(status_path, "a", encoding="utf-8") as out:
                if new_file:
                    out.write("Link\tPage Link\tFilename\tDate&Time\n")

                for i, link in enumerate(links):
                    if self.stop_event.is_set():
                        q.put(("log", "Stopped."))
                        break

                    if i > 0 and cfg["url_delay"] > 0:
                        delay = cfg["url_delay"] * (random.uniform(0.7, 1.5) if cfg["stealth"] else 1)
                        end = time.time() + delay
                        while time.time() < end and not self.stop_event.is_set():
                            time.sleep(0.2)
                        if self.stop_event.is_set():
                            q.put(("log", "Stopped."))
                            break

                    q.put(("log", f"[{i + 1}/{total}] {link}"))
                    try:
                        if driver is None:
                            driver = self.new_driver(cfg)
                        saved, current_url = self.process_url(driver, i, link, cfg)
                        row = f"{link}\t{current_url}\t{saved}\t{datetime.datetime.now()}\n"
                        q.put(("log", f"   saved: {saved}{self.describe_pdf(cfg, saved)}"))
                    except Exception as e:
                        short = str(e).splitlines()[0] if str(e) else type(e).__name__
                        row = f"{link}\tError: {short}\tError\t{datetime.datetime.now()}\n"
                        q.put(("log", f"   error: {short}"))
                    finally:
                        # Close after every URL, or when the shared browser died / was closed
                        if driver is not None and (cfg["new_browser"] or not self.driver_alive(driver)):
                            self.close_driver(driver)
                            driver = None

                    out.write(row)
                    out.flush()
                    q.put(("progress", (i + 1, total)))
        except Exception as e:
            q.put(("log", f"General error: {e}"))
        finally:
            self.close_driver(driver)

        q.put(("log", f"Finished. Results log: {status_path}"))
        q.put(("done", None))


if __name__ == "__main__":
    App().mainloop()
