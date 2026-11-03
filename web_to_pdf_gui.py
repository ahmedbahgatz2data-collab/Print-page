import os
import re
import json
import time
import queue
import datetime
import threading
import urllib.parse
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException


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
    "A3": ("ISO_A3", 297000, 420000),
    "A4": ("ISO_A4", 210000, 297000),
    "A5": ("ISO_A5", 148000, 210000),
    "Letter": ("NA_LETTER", 215900, 279400),
    "Legal": ("NA_LEGAL", 215900, 355600),
    "Tabloid": ("NA_LEDGER", 279400, 431800),
}


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
        self.geometry("1020x870")
        self.minsize(900, 780)

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
        self.var_wait = tk.IntVar(value=20)
        self.var_load_wait = tk.IntVar(value=5)
        self.var_use_title = tk.BooleanVar(value=True)
        self.var_stop_loading = tk.BooleanVar(value=True)
        self.var_page_size = tk.StringVar(value="Browser default")
        self.var_landscape = tk.BooleanVar(value=False)
        self.var_dedupe = tk.BooleanVar(value=True)
        self.var_browser_mode = tk.StringVar(value="each")
        self.var_step_timeout = tk.DoubleVar(value=10)
        self.var_step_delay = tk.DoubleVar(value=1)
        self.var_after_steps = tk.DoubleVar(value=2)
        self.var_continue = tk.BooleanVar(value=True)

        self.build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.poll_queue)

    # ---------------- UI ----------------
    def build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # ----- Settings -----
        box = ttk.LabelFrame(self, text="Settings")
        box.pack(fill="x", padx=10, pady=8)
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

        pg = ttk.Frame(box)
        pg.grid(row=3, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(pg, text="Page size:").pack(side="left")
        ttk.Combobox(
            pg, textvariable=self.var_page_size, state="readonly", width=16,
            values=["Browser default"] + list(PAGE_SIZES),
        ).pack(side="left", padx=(4, 16))
        ttk.Checkbutton(pg, text="Landscape", variable=self.var_landscape).pack(side="left")

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

        checks = ttk.Frame(box)
        checks.grid(row=5, column=0, columnspan=3, sticky="w", **pad)
        ttk.Checkbutton(checks, text="Use page title as file name", variable=self.var_use_title).pack(side="left", padx=(0, 20))
        ttk.Checkbutton(checks, text="Stop page loading before running steps", variable=self.var_stop_loading).pack(side="left", padx=(0, 20))
        ttk.Checkbutton(
            checks, text="Remove duplicate links automatically",
            variable=self.var_dedupe, command=self.update_link_count,
        ).pack(side="left")

        # ----- Links + Steps -----
        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=10, pady=4)
        mid.columnconfigure(0, weight=1)
        mid.columnconfigure(1, weight=1)
        mid.rowconfigure(0, weight=1)

        links = ttk.LabelFrame(mid, text="Links (one per line)")
        links.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self.txt_links = scrolledtext.ScrolledText(links, height=10, width=40, wrap="none")
        self.txt_links.pack(fill="both", expand=True, padx=6, pady=6)
        bar = ttk.Frame(links)
        bar.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(bar, text="Load from txt file", command=self.load_links_file).pack(side="left")
        ttk.Button(bar, text="Clear", command=lambda: self.txt_links.delete("1.0", "end")).pack(side="left", padx=6)
        self.lbl_count = ttk.Label(bar, text="0 links")
        self.lbl_count.pack(side="right")
        self.txt_links.bind("<<Modified>>", self.on_links_modified)

        steps = ttk.LabelFrame(mid, text="Steps run on every link before saving")
        steps.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

        lf = ttk.Frame(steps)
        lf.pack(fill="both", expand=True, padx=6, pady=(6, 2))
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)
        self.lst_steps = tk.Listbox(lf, height=8, activestyle="dotbox", exportselection=False)
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

        # ----- Controls -----
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=10, pady=6)
        self.btn_start = ttk.Button(ctrl, text="\u25B6 Start", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(ctrl, text="\u25A0 Stop", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(ctrl, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.lbl_progress = ttk.Label(ctrl, text="0 / 0")
        self.lbl_progress.pack(side="left")

        # ----- Log -----
        logf = ttk.LabelFrame(self, text="Log")
        logf.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.txt_log = scrolledtext.ScrolledText(logf, height=9, state="disabled")
        self.txt_log.pack(fill="both", expand=True, padx=6, pady=6)

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
                elif kind == "rec_step":
                    self.steps.append(data)
                    self.refresh_steps(select=len(self.steps) - 1)
                    self.log(f"Recorded: click {data['selector']}")
                elif kind == "rec_done":
                    self.recording = False
                    self.btn_record.config(text="\u25CF Record clicks")
                    self.btn_start.config(state="normal")
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
        self.log("Recording started. Click the elements you want in the browser window, "
                 "in order, then press 'Stop recording' here.")
        threading.Thread(
            target=self.record_worker, args=(url, self.var_driver.get().strip()), daemon=True
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

    def record_worker(self, url, driver_path):
        q = self.msg_queue
        driver = None
        try:
            options = webdriver.ChromeOptions()
            options.add_argument("--start-maximized")
            options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
            driver = webdriver.Chrome(service=make_service(driver_path), options=options)
            self.rec_driver = driver
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
    def start(self):
        if self.recording:
            messagebox.showwarning("Warning", "Stop the recording first.")
            return
        folder = self.var_folder.get().strip()
        links = self.get_links()
        if not folder:
            messagebox.showwarning("Warning", "Choose a save folder first.")
            return
        if not os.path.isdir(folder):
            try:
                os.makedirs(folder)
            except OSError as e:
                messagebox.showerror("Error", f"Could not create the folder:\n{e}")
                return
        if not links:
            messagebox.showwarning("Warning", "Add at least one link.")
            return

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
        }
        self.stop_event.clear()
        self.btn_start.config(state="disabled")
        self.btn_record.config(state="disabled")
        self.btn_stop.config(state="normal")
        dupes = len(self.get_links(dedupe=False)) - len(links)
        if dupes:
            self.msg_queue.put(("log", f"Ignored {dupes} duplicate link(s). Processing {len(links)} unique link(s)."))
        self.msg_queue.put(("progress", (0, len(links))))
        self.worker = threading.Thread(target=self.run_job, args=(links, cfg), daemon=True)
        self.worker.start()

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
        if cfg["page_size"] in PAGE_SIZES:
            name, w, h = PAGE_SIZES[cfg["page_size"]]
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

    def new_driver(self, cfg):
        return webdriver.Chrome(service=make_service(cfg["driver"]), options=self.make_options(cfg))

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
        if cfg["use_title"]:
            filename = f"{i + 1:03d} - {safe_name(driver.title)}"
        else:
            filename = f"filename{i}{stamp}"
        if os.path.exists(os.path.join(cfg["folder"], filename + ".pdf")):
            filename += f" {stamp}"  # avoid Chrome adding " (1)" to the name

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

                    q.put(("log", f"[{i + 1}/{total}] {link}"))
                    try:
                        if driver is None:
                            driver = self.new_driver(cfg)
                        saved, current_url = self.process_url(driver, i, link, cfg)
                        row = f"{link}\t{current_url}\t{saved}\t{datetime.datetime.now()}\n"
                        q.put(("log", f"   saved: {saved}"))
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
