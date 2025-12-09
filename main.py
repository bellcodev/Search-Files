import os
import sys
import re
import time
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import SimpleQueue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime

def list_roots():
    if sys.platform.startswith("win"):
        import string
        from ctypes import windll
        drives = []
        bitmask = windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if bitmask & (1 << i):
                drive = f"{letter}:\\\\"
                drives.append(drive)
        return drives or ["C:\\\\"]
    else:
        roots = ["/"]
        for p in ("/mnt", "/media"):
            if os.path.exists(p):
                try:
                    for entry in os.listdir(p):
                        full = os.path.join(p, entry)
                        if os.path.ismount(full) or os.path.isdir(full):
                            roots.append(full)
                except Exception:
                    pass
        return roots

class DeepSearcher:
    def __init__(self, max_workers=8, follow_symlinks=False):
        self.max_workers = max_workers
        self.follow_symlinks = follow_symlinks
        self._stop_event = threading.Event()
        self._executor = None
        self._futures = []
        self._lock = threading.Lock()

    def stop(self):
        self._stop_event.set()

    def is_stopped(self):
        return self._stop_event.is_set()

    def _match(self, name, query, use_regex=False):
        if use_regex:
            return bool(query.search(name))
        else:
            return query in name.lower()

    def _scan_path(self, root, query, use_regex, result_queue, stats):
        stack = [root]
        while stack and not self.is_stopped():
            path = stack.pop()
            try:
                with os.scandir(path) as it:
                    for entry in it:
                        if self.is_stopped():
                            return
                        try:
                            name = entry.name
                        except Exception:
                            continue
                        stats['visited'] += 1

                        try:
                            if self._match(name, query, use_regex):
                                try:
                                    st = entry.stat(follow_symlinks=self.follow_symlinks)
                                    size = st.st_size if entry.is_file(follow_symlinks=self.follow_symlinks) else None
                                    mtime = st.st_mtime
                                except Exception:
                                    size = None
                                    mtime = None

                                result_queue.put({
                                    "name": name,
                                    "path": entry.path,
                                    "is_file": entry.is_file(follow_symlinks=self.follow_symlinks),
                                    "is_dir": entry.is_dir(follow_symlinks=self.follow_symlinks),
                                    "size": size,
                                    "mtime": mtime
                                })

                            try:
                                if entry.is_dir(follow_symlinks=self.follow_symlinks):
                                    stack.append(entry.path)
                            except Exception:
                                continue
                        except PermissionError:
                            continue
            except (PermissionError, FileNotFoundError, NotADirectoryError):
                continue
            except Exception:
                continue

    def run_search(self, raw_query, roots=None, use_regex=False, result_callback=None, progress_callback=None, done_callback=None):
        if not raw_query:
            raise ValueError("raw_query cannot be empty")

        self._stop_event.clear()
        result_queue = SimpleQueue()
        stats = {"visited": 0, "found": 0, "start_time": time.time()}

        if use_regex:
            pattern = re.compile(raw_query, re.IGNORECASE)
            query_obj = pattern
        else:
            query_obj = raw_query.lower()

        if roots is None or len(roots) == 0:
            roots = list_roots()

        self._executor = ThreadPoolExecutor(max_workers=min(self.max_workers, max(1, len(roots))))
        self._futures = []
        for r in roots:
            if self.is_stopped():
                break
            if not os.path.exists(r):
                continue
            try:
                fut = self._executor.submit(self._scan_path, r, query_obj, use_regex, result_queue, stats)
                self._futures.append(fut)
            except Exception:
                continue

        def consumer_loop():
            last_progress_time = 0
            while any(not f.done() for f in self._futures) and not self.is_stopped():
                while not result_queue.empty():
                    res = result_queue.get()
                    stats['found'] += 1
                    if result_callback:
                        try:
                            result_callback(res)
                        except Exception:
                            pass
                if progress_callback and (time.time() - last_progress_time) > 0.5:
                    try:
                        progress_callback({
                            "visited": stats['visited'],
                            "found": stats['found'],
                            "elapsed": time.time() - stats['start_time']
                        })
                    except Exception:
                        pass
                    last_progress_time = time.time()
                time.sleep(0.05)

            while not result_queue.empty():
                res = result_queue.get()
                stats['found'] += 1
                if result_callback:
                    try:
                        result_callback(res)
                    except Exception:
                        pass

            for f in as_completed(self._futures):
                if self.is_stopped():
                    break

            try:
                self._executor.shutdown(wait=False)
            except Exception:
                pass

            stats['elapsed'] = time.time() - stats['start_time']
            if done_callback:
                try:
                    done_callback(stats)
                except Exception:
                    pass

        t = threading.Thread(target=consumer_loop, daemon=True)
        t.start()
        return t

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Buscador de Archivos")
        self.geometry("1000x650")
        self.minsize(800, 400)
        self.configure(bg="#1e1e1e")

        self.searcher = DeepSearcher(max_workers=8)
        self.search_thread = None
        self.results = []

        self._create_widgets()

    def _create_widgets(self):
        top = tk.Frame(self, bg="#252526", pady=8)
        top.pack(fill="x", padx=8, pady=(8,0))

        tk.Label(top, text="🔎 Buscar:", bg="#252526", fg="white", font=("Segoe UI", 11, "bold")).pack(side="left", padx=(6,8))
        self.entry = tk.Entry(top, font=("Segoe UI", 11), width=50)
        self.entry.pack(side="left", padx=(0,8))
        self.entry.bind("<Return>", lambda e: self.start_search())

        self.regex_var = tk.BooleanVar(value=False)
        tk.Checkbutton(top, text="Regex", variable=self.regex_var, bg="#252526", fg="white", selectcolor="#2d2d30").pack(side="left", padx=(0,6))

        tk.Button(top, text="Examinar roots", command=self.choose_roots, bg="#007acc", fg="white").pack(side="left", padx=6)
        self.roots_label = tk.Label(top, text="", bg="#252526", fg="white")
        self.roots_label.pack(side="left", padx=6)

        tk.Button(top, text="Buscar", command=self.start_search, bg="#00a86b", fg="white").pack(side="right", padx=6)
        tk.Button(top, text="Detener", command=self.stop_search, bg="#d9534f", fg="white").pack(side="right", padx=(0,6))

        columns = ("type", "name", "path", "size", "modified")
        self.tree = ttk.Treeview(self, columns=columns, show="headings")
        self.tree.heading("type", text="T")
        self.tree.heading("name", text="Nombre")
        self.tree.heading("path", text="Ruta completa")
        self.tree.heading("size", text="Tamaño")
        self.tree.heading("modified", text="Modificado")
        self.tree.column("type", width=30, anchor="center")
        self.tree.column("name", width=200)
        self.tree.column("path", width=450)
        self.tree.column("size", width=100, anchor="e")
        self.tree.column("modified", width=150, anchor="center")

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", background="#2d2d30", foreground="white", fieldbackground="#2d2d30", rowheight=26)
        style.configure("Treeview.Heading", background="#007acc", foreground="white")

        self.tree.pack(fill="both", expand=True, padx=8, pady=8)
        self.tree.bind("<Double-1>", self._open_selected)

        bottom = tk.Frame(self, bg="#1e1e1e", height=40)
        bottom.pack(fill="x", padx=8, pady=(0,8))
        self.status_label = tk.Label(bottom, text="Listo", bg="#1e1e1e", fg="white")
        self.status_label.pack(side="left")
        tk.Button(bottom, text="Limpiar", command=self.clear_results, bg="#6c6c6c", fg="white").pack(side="right", padx=(0,6))

    def choose_roots(self):
        paths = filedialog.askdirectory(mustexist=True, title="Selecciona carpeta raíz (puedes repetir para añadir más)")
        if not paths:
            return
        current = self.roots_label.cget("text")
        roots = current.split(";") if current else []
        roots = [r for r in roots if r]
        roots.append(paths)
        self.roots_label.config(text=";".join(roots))

    def _collect_roots(self):
        text = self.roots_label.cget("text")
        if not text:
            return None
        return [p for p in text.split(";") if p]

    def start_search(self):
        query = self.entry.get().strip()
        if not query:
            messagebox.showwarning("Atención", "Escribe algo para buscar.")
            return
        self.clear_results()
        self.status_label.config(text="Iniciando búsqueda...")
        roots = self._collect_roots()
        use_regex = bool(self.regex_var.get())
        self.search_thread = self.searcher.run_search(
            raw_query=query,
            roots=roots,
            use_regex=use_regex,
            result_callback=self._on_result,
            progress_callback=self._on_progress,
            done_callback=self._on_done
        )

    def stop_search(self):
        if self.search_thread and self.search_thread.is_alive():
            self.searcher.stop()
            self.status_label.config(text="Cancelando búsqueda...")
        else:
            self.status_label.config(text="No hay búsqueda en curso.")

    def _on_result(self, res):
        self.results.append(res)
        self.after(0, lambda: self._insert_result_in_tree(res))

    def _insert_result_in_tree(self, res):
        typ = "F" if res.get("is_file") else "D"
        name = res.get("name") or ""
        path = res.get("path") or ""
        size = res.get("size")
        if size is None:
            size_str = ""
        else:
            for unit in ["B", "KB", "MB", "GB"]:
                if size < 1024:
                    size_str = f"{size:.0f}{unit}"
                    break
                size /= 1024.0
            else:
                size_str = f"{size:.0f}TB"
        mtime = res.get("mtime")
        if mtime:
            try:
                mtime_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            except Exception:
                mtime_str = ""
        else:
            mtime_str = ""
        self.tree.insert("", "end", values=(typ, name, path, size_str, mtime_str))

    def _on_progress(self, stats):
        text = f"Visited: {stats.get('visited',0)}  Found: {stats.get('found',0)}  Elapsed: {stats.get('elapsed',0):.1f}s"
        self.after(0, lambda: self.status_label.config(text=text))

    def _on_done(self, stats):
        text = f"Terminado. Visited: {stats.get('visited',0)}  Found: {stats.get('found',0)}  Time: {stats.get('elapsed',0):.1f}s"
        self.after(0, lambda: self.status_label.config(text=text))

    def _open_selected(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        path = self.tree.item(item, "values")[2]
        folder = os.path.dirname(path) if os.path.isfile(path) else path
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(f'explorer /select,"{path}"')
            elif sys.platform.startswith("darwin"):
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception:
            try:
                if sys.platform.startswith("win"):
                    subprocess.Popen(["explorer", folder])
                elif sys.platform.startswith("darwin"):
                    subprocess.Popen(["open", folder])
                else:
                    subprocess.Popen(["xdg-open", folder])
            except Exception:
                messagebox.showinfo("Abrir", f"No se pudo abrir la ubicación: {folder}")

    def clear_results(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        self.results = []
        self.status_label.config(text="Listo")

def main():
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()
