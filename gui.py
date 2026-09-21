"""
DWG Normalizer - простой интерфейс.
Запуск:  python gui.py
"""

import glob
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import dwg_normalizer as core

APP_DIR = os.path.dirname(os.path.abspath(__file__))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DWG Normalizer - нормализация шрифтов и размеров")
        self.geometry("860x640")
        self.minsize(700, 500)

        self.queue: queue.Queue = queue.Queue()
        self.busy = False

        self._build()
        self._load_profiles()
        self.after(100, self._poll)

    # ---------------------------------------------------------------- вёрстка
    def _build(self):
        pad = {"padx": 8, "pady": 4}

        # --- выбор файла ---
        frm_file = ttk.LabelFrame(self, text="Что обрабатываем")
        frm_file.pack(fill="x", **pad)

        self.var_path = tk.StringVar()
        self.var_batch = tk.BooleanVar(value=False)

        row = ttk.Frame(frm_file)
        row.pack(fill="x", padx=8, pady=6)
        ttk.Entry(row, textvariable=self.var_path).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="Файл...", width=10,
                   command=self._pick_file).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="Папка...", width=10,
                   command=self._pick_folder).pack(side="left", padx=(4, 0))

        ttk.Checkbutton(frm_file, text="Обработать все чертежи в папке (пакетный режим)",
                        variable=self.var_batch).pack(anchor="w", padx=8, pady=(0, 6))

        # --- профиль ---
        frm_prof = ttk.LabelFrame(self, text="Профиль стандарта")
        frm_prof.pack(fill="x", **pad)

        row2 = ttk.Frame(frm_prof)
        row2.pack(fill="x", padx=8, pady=6)
        self.cmb_profile = ttk.Combobox(row2, state="readonly")
        self.cmb_profile.pack(side="left", fill="x", expand=True)
        ttk.Button(row2, text="Обзор...", width=10,
                   command=self._pick_profile).pack(side="left", padx=(6, 0))

        # --- кнопки ---
        frm_btn = ttk.Frame(self)
        frm_btn.pack(fill="x", **pad)

        self.btn_scan = ttk.Button(frm_btn, text="Проверить (без изменений)",
                                   command=lambda: self._run(scan_only=True))
        self.btn_scan.pack(side="left")

        self.btn_run = ttk.Button(frm_btn, text="Нормализовать",
                                  command=lambda: self._run(scan_only=False))
        self.btn_run.pack(side="left", padx=(8, 0))

        ttk.Button(frm_btn, text="Очистить отчёт",
                   command=lambda: self.txt.delete("1.0", "end")).pack(side="left", padx=(8, 0))

        self.progress = ttk.Progressbar(frm_btn, mode="indeterminate", length=140)
        self.progress.pack(side="right")

        # --- отчёт ---
        frm_out = ttk.LabelFrame(self, text="Отчёт")
        frm_out.pack(fill="both", expand=True, **pad)

        self.txt = tk.Text(frm_out, wrap="none", font=("Consolas", 9),
                           background="#1e1e1e", foreground="#d4d4d4",
                           insertbackground="#d4d4d4")
        vsb = ttk.Scrollbar(frm_out, orient="vertical", command=self.txt.yview)
        hsb = ttk.Scrollbar(frm_out, orient="horizontal", command=self.txt.xview)
        self.txt.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.txt.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frm_out.rowconfigure(0, weight=1)
        frm_out.columnconfigure(0, weight=1)

        # --- статус ---
        self.var_status = tk.StringVar(value="Готов")
        ttk.Label(self, textvariable=self.var_status, relief="sunken",
                  anchor="w").pack(fill="x", side="bottom")

    # ---------------------------------------------------------------- профили
    def _load_profiles(self):
        files = sorted(glob.glob(os.path.join(APP_DIR, "*.json")))
        self.profiles = files
        names = [os.path.basename(f) for f in files]
        self.cmb_profile["values"] = names or ["(профилей нет - встроенный)"]
        self.cmb_profile.current(0)

    def _current_profile_path(self):
        if not self.profiles:
            return None
        idx = self.cmb_profile.current()
        return self.profiles[idx] if 0 <= idx < len(self.profiles) else None

    # ---------------------------------------------------------------- выбор
    def _pick_file(self):
        path = filedialog.askopenfilename(
            title="Выберите чертёж",
            filetypes=[("Чертежи", "*.dwg *.dxf"), ("DWG", "*.dwg"),
                       ("DXF", "*.dxf"), ("Все файлы", "*.*")])
        if path:
            self.var_path.set(path)
            self.var_batch.set(False)

    def _pick_folder(self):
        path = filedialog.askdirectory(title="Выберите папку с чертежами")
        if path:
            self.var_path.set(path)
            self.var_batch.set(True)

    def _pick_profile(self):
        path = filedialog.askopenfilename(
            title="Выберите профиль", filetypes=[("JSON", "*.json")])
        if path:
            if path not in self.profiles:
                self.profiles.append(path)
                self.cmb_profile["values"] = [os.path.basename(f) for f in self.profiles]
            self.cmb_profile.current(self.profiles.index(path))

    # ---------------------------------------------------------------- запуск
    def _run(self, scan_only: bool):
        if self.busy:
            return
        path = self.var_path.get().strip()
        if not path or not os.path.exists(path):
            messagebox.showwarning("Не выбран файл", "Укажите чертёж или папку.")
            return

        try:
            profile = core.load_profile(self._current_profile_path())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Ошибка профиля", str(exc))
            return

        self.busy = True
        self.btn_run.state(["disabled"])
        self.btn_scan.state(["disabled"])
        self.progress.start(12)
        self.var_status.set("Работаю...")

        threading.Thread(target=self._worker,
                         args=(path, profile, scan_only, self.var_batch.get()),
                         daemon=True).start()

    def _worker(self, path, profile, scan_only, batch):
        """Выполняется в фоновом потоке - интерфейс не подвисает."""
        try:
            if batch and os.path.isdir(path):
                files = [os.path.join(path, f) for f in sorted(os.listdir(path))
                         if f.lower().endswith((".dwg", ".dxf"))
                         and "_normalized" not in f.lower()]
                if not files:
                    self.queue.put(("done", "В папке нет .dwg/.dxf файлов", False))
                    return
                chunks, all_ok = [], True
                for i, f in enumerate(files, 1):
                    self.queue.put(("status", f"[{i}/{len(files)}] {os.path.basename(f)}"))
                    rep = core.process_file(f, profile, scan_only=scan_only)
                    chunks.append(rep.text())
                    all_ok = all_ok and rep.ok
                self.queue.put(("done", ("\n" + "-" * 70 + "\n").join(chunks), all_ok))
            else:
                rep = core.process_file(path, profile, scan_only=scan_only)
                self.queue.put(("done", rep.text(), rep.ok))
        except Exception as exc:  # noqa: BLE001
            self.queue.put(("done", f"✗ ОШИБКА: {exc}", False))

    def _poll(self):
        """Забирает результаты из фонового потока в главный поток tkinter."""
        try:
            while True:
                kind, *payload = self.queue.get_nowait()
                if kind == "status":
                    self.var_status.set(payload[0])
                elif kind == "done":
                    text, ok = payload
                    self.txt.insert("end", text + "\n\n")
                    self.txt.see("end")
                    self.progress.stop()
                    self.btn_run.state(["!disabled"])
                    self.btn_scan.state(["!disabled"])
                    self.busy = False
                    self.var_status.set("Готово" if ok else "Завершено с ошибками")
        except queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == "__main__":
    App().mainloop()
