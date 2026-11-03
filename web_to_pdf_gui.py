import os
import re
import json
import time
import queue
import datetime
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

from selenium import webdriver
from selenium.webdriver.chrome.service import Service


def safe_name(text, max_len=80):
    text = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", text).strip()
    return text[:max_len] or "page"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Web Pages to PDF")
        self.geometry("820x700")
        self.minsize(720, 620)

        self.msg_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None

        self.var_folder = tk.StringVar()
        self.var_driver = tk.StringVar()
        self.var_scale = tk.IntVar(value=100)
        self.var_wait = tk.IntVar(value=5)
        self.var_load_wait = tk.IntVar(value=5)
        self.var_use_title = tk.BooleanVar(value=True)

        self.build_ui()
        self.after(100, self.poll_queue)

    # ---------------- UI ----------------
    def build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # Settings
        box = ttk.LabelFrame(self, text="الإعدادات / Settings")
        box.pack(fill="x", padx=10, pady=8)
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="مجلد الحفظ:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(box, textvariable=self.var_folder).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(box, text="استعراض...", command=self.pick_folder).grid(row=0, column=2, **pad)

        ttk.Label(box, text="chromedriver (اختياري):").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(box, textvariable=self.var_driver).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(box, text="استعراض...", command=self.pick_driver).grid(row=1, column=2, **pad)

        opts = ttk.Frame(box)
        opts.grid(row=2, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(opts, text="Scale %:").pack(side="left")
        ttk.Spinbox(opts, from_=10, to=200, width=6, textvariable=self.var_scale).pack(side="left", padx=(4, 16))
        ttk.Label(opts, text="انتظار تحميل الصفحة (ث):").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=120, width=6, textvariable=self.var_load_wait).pack(side="left", padx=(4, 16))
        ttk.Label(opts, text="انتظار حفظ PDF (ث):").pack(side="left")
        ttk.Spinbox(opts, from_=1, to=120, width=6, textvariable=self.var_wait).pack(side="left", padx=(4, 16))

        ttk.Checkbutton(
            box, text="استخدام عنوان الصفحة كاسم للملف",
            variable=self.var_use_title,
        ).grid(row=3, column=0, columnspan=3, sticky="w", **pad)

        # Links
        links = ttk.LabelFrame(self, text="الروابط (رابط في كل سطر) / Links")
        links.pack(fill="both", expand=True, padx=10, pady=4)
        self.txt_links = scrolledtext.ScrolledText(links, height=8, wrap="none")
        self.txt_links.pack(fill="both", expand=True, padx=6, pady=6)
        bar = ttk.Frame(links)
        bar.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(bar, text="تحميل من ملف txt", command=self.load_links_file).pack(side="left")
        ttk.Button(bar, text="مسح", command=lambda: self.txt_links.delete("1.0", "end")).pack(side="left", padx=6)
        self.lbl_count = ttk.Label(bar, text="")
        self.lbl_count.pack(side="right")
        self.txt_links.bind("<<Modified>>", self.on_links_modified)

        # Controls
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", padx=10, pady=6)
        self.btn_start = ttk.Button(ctrl, text="▶ ابدأ", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(ctrl, text="■ إيقاف", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.progress = ttk.Progressbar(ctrl, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.lbl_progress = ttk.Label(ctrl, text="0 / 0")
        self.lbl_progress.pack(side="left")

        # Log
        logf = ttk.LabelFrame(self, text="السجل / Log")
        logf.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.txt_log = scrolledtext.ScrolledText(logf, height=10, state="disabled")
        self.txt_log.pack(fill="both", expand=True, padx=6, pady=6)

    # ---------------- helpers ----------------
    def on_links_modified(self, _event=None):
        if self.txt_links.edit_modified():
            self.lbl_count.config(text=f"{len(self.get_links())} رابط")
            self.txt_links.edit_modified(False)

    def get_links(self):
        raw = self.txt_links.get("1.0", "end").splitlines()
        return [x.strip() for x in raw if x.strip()]

    def pick_folder(self):
        d = filedialog.askdirectory(title="اختر مجلد الحفظ")
        if d:
            self.var_folder.set(d)

    def pick_driver(self):
        f = filedialog.askopenfilename(
            title="اختر chromedriver",
            filetypes=[("chromedriver", "*.exe"), ("All files", "*.*")],
        )
        if f:
            self.var_driver.set(f)

    def load_links_file(self):
        f = filedialog.askopenfilename(
            title="اختر ملف الروابط",
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
        except queue.Empty:
            pass
        self.after(100, self.poll_queue)

    # ---------------- actions ----------------
    def start(self):
        folder = self.var_folder.get().strip()
        links = self.get_links()
        if not folder:
            messagebox.showwarning("تنبيه", "اختر مجلد الحفظ أولاً.")
            return
        if not os.path.isdir(folder):
            try:
                os.makedirs(folder)
            except OSError as e:
                messagebox.showerror("خطأ", f"تعذر إنشاء المجلد:\n{e}")
                return
        if not links:
            messagebox.showwarning("تنبيه", "أضف رابطاً واحداً على الأقل.")
            return

        cfg = {
            "folder": os.path.abspath(folder),
            "driver": self.var_driver.get().strip(),
            "scale": self.var_scale.get(),
            "wait": self.var_wait.get(),
            "load_wait": self.var_load_wait.get(),
            "use_title": self.var_use_title.get(),
        }
        self.stop_event.clear()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.msg_queue.put(("progress", (0, len(links))))
        self.worker = threading.Thread(target=self.run_job, args=(links, cfg), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.msg_queue.put(("log", "جاري الإيقاف بعد الرابط الحالي..."))

    # ---------------- worker ----------------
    def make_options(self, cfg):
        settings = {
            "recentDestinations": [{"id": "Save as PDF", "origin": "local", "account": ""}],
            "selectedDestinationId": "Save as PDF",
            "version": 2,
            "isHeaderFooterEnabled": False,
            "isLandscapeEnabled": False,
            "scalingType": 3,
            "scaling": str(cfg["scale"]),
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

    def run_job(self, links, cfg):
        q = self.msg_queue
        total = len(links)
        status_path = os.path.join(cfg["folder"], "Output Status.txt")
        new_file = not os.path.exists(status_path)

        try:
            with open(status_path, "a", encoding="utf-8") as out:
                if new_file:
                    out.write("Link\tPage Link\tFilename\tDate&Time\n")

                for i, link in enumerate(links):
                    if self.stop_event.is_set():
                        q.put(("log", "تم الإيقاف."))
                        break

                    q.put(("log", f"[{i + 1}/{total}] {link}"))
                    stamp = time.strftime("%H-%M-%S")
                    filename = f"filename{i}{stamp}"
                    try:
                        service = Service(executable_path=cfg["driver"]) if cfg["driver"] else Service()
                        with webdriver.Chrome(service=service, options=self.make_options(cfg)) as driver:
                            driver.get(link)
                            time.sleep(cfg["load_wait"])
                            driver.execute_script("window.stop();")

                            if cfg["use_title"]:
                                title = safe_name(driver.title)
                                filename = f"{i + 1:03d} - {title}"

                            driver.execute_script(f"document.title = {json.dumps(filename)};")
                            driver.execute_script("window.print();")
                            time.sleep(cfg["wait"])
                            row = f"{link}\t{driver.current_url}\t{filename}.pdf\t{datetime.datetime.now()}\n"
                        q.put(("log", f"   تم الحفظ: {filename}.pdf"))
                    except Exception as e:
                        short = str(e).splitlines()[0] if str(e) else type(e).__name__
                        row = f"{link}\tError: {short}\tError\t{datetime.datetime.now()}\n"
                        q.put(("log", f"   خطأ: {short}"))

                    out.write(row)
                    out.flush()
                    q.put(("progress", (i + 1, total)))
        except Exception as e:
            q.put(("log", f"خطأ عام: {e}"))

        q.put(("log", f"انتهى. سجل النتائج: {status_path}"))
        q.put(("done", None))


if __name__ == "__main__":
    App().mainloop()
