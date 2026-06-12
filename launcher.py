"""
launcher.py — Rivus Windows lightweight launcher
First run: creates a venv + pip install, shows a progress window
Subsequent runs: re-copies source files if version changed, then launches directly
"""
# ── Early startup log (runs before anything else) ─────────────────────────────
try:
    import os as _os, sys as _sys
    _log = _os.path.join(_os.environ.get("APPDATA", _os.path.expanduser("~")), "Rivus", "startup.log")
    _os.makedirs(_os.path.dirname(_log), exist_ok=True)
    with open(_log, "a") as _f:
        _f.write(f"\n--- launcher started (frozen={getattr(_sys,'frozen',False)}) ---\n")
except Exception:
    pass
# ─────────────────────────────────────────────────────────────────────────────
import sys
import os
import subprocess
import shutil
import threading
from pathlib import Path

def _early_log(msg):
    try:
        log = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Rivus", "startup.log")
        with open(log, "a") as f:
            f.write(msg + "\n")
    except Exception:
        pass

_early_log("imports ok: sys os subprocess shutil threading pathlib")

import tkinter as tk
_early_log("import ok: tkinter")
from tkinter import ttk, messagebox
_early_log("import ok: tkinter.ttk messagebox — all imports done")

# ── Version — must match config.py APP_VERSION ────────────────────────────────
APP_VERSION = "1.0.1"

# ── Paths ─────────────────────────────────────────────────────────────────────
APPDATA  = Path(os.environ.get("APPDATA", Path.home()))
APP_DIR  = APPDATA / "Rivus"
VENV_DIR = APP_DIR / "venv"
SRC_DIR  = APP_DIR / "app"
DATA_DIR = APP_DIR / "data"
PYTHON   = VENV_DIR / "Scripts" / "python.exe"
PIP      = VENV_DIR / "Scripts" / "pip.exe"
FLAG     = APP_DIR / ".installed"


def find_system_python() -> str:
    """Find a real system-installed Python, avoiding sys.executable (which is Rivus.exe when packaged)"""
    for candidate in ["python", "python3", "py"]:
        path = shutil.which(candidate)
        if path and os.path.normcase(path) != os.path.normcase(sys.executable):
            try:
                result = subprocess.run(
                    [path, "--version"], capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and "Python 3" in (result.stdout + result.stderr):
                    return path
            except Exception:
                continue
    return None


def bundled_src() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "app"
    return Path(__file__).parent


def installed_version() -> str:
    """Return the version string stored in the FLAG file, or '' if not present."""
    try:
        return FLAG.read_text().strip()
    except Exception:
        return ""


def is_venv_ready() -> bool:
    return PYTHON.exists() and (SRC_DIR / "app.py").exists()


def needs_source_update() -> bool:
    """True if the venv is ready but source files are from an older version."""
    return is_venv_ready() and installed_version() != APP_VERSION


def update_source_files():
    """Re-copy source files and install any new dependencies."""
    shutil.copytree(bundled_src(), SRC_DIR, dirs_exist_ok=True)

    # Re-run pip install so newly added packages (e.g. pystray) get installed.
    # --upgrade picks up new entries; cached wheels make this fast on repeat runs.
    reqs = SRC_DIR / "requirements.txt"
    lines = [l for l in reqs.read_text().splitlines()
             if "pyobjc" not in l and l.strip() and not l.startswith("#")]
    reqs_win = APP_DIR / "requirements_win.txt"
    reqs_win.write_text("\n".join(lines))
    subprocess.run(
        [str(PIP), "install", "-r", str(reqs_win), "-q", "--upgrade",
         "--trusted-host", "pypi.org", "--trusted-host", "files.pythonhosted.org"],
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    FLAG.write_text(APP_VERSION)


def is_installed() -> bool:
    return FLAG.exists() and is_venv_ready() and installed_version() == APP_VERSION


def launch_app():
    env = os.environ.copy()
    env["MEMORYVAULT_DATA_DIR"] = str(DATA_DIR)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app_log = APP_DIR / "app.log"
    _early_log(f"launching: {PYTHON} {SRC_DIR / 'app.py'}  log→{app_log}")
    with open(app_log, "a") as log_fh:
        subprocess.Popen(
            [str(PYTHON), str(SRC_DIR / "app.py")],
            env=env,
            stdout=log_fh,
            stderr=log_fh,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


# ── Installation flow (background thread) ────────────────────────────────────

def run_install(sys_python, on_progress, on_done, on_error):
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)

        on_progress("Copying application files...", 5)
        shutil.copytree(bundled_src(), SRC_DIR, dirs_exist_ok=True)

        on_progress("Creating Python environment...", 15)
        if VENV_DIR.exists():
            shutil.rmtree(VENV_DIR)   # remove any incomplete venv from a previous interrupted run
        subprocess.run(
            [sys_python, "-m", "venv", str(VENV_DIR)],
            check=True, capture_output=True
        )

        on_progress("Upgrading pip...", 20)
        # Failure to upgrade pip won't block the rest of the install
        subprocess.run(
            [str(PIP), "install", "--upgrade", "pip", "-q",
             "--trusted-host", "pypi.org", "--trusted-host", "files.pythonhosted.org"],
            capture_output=True
        )

        # Filter out macOS-only dependencies
        reqs = SRC_DIR / "requirements.txt"
        lines = [l for l in reqs.read_text().splitlines()
                 if "pyobjc" not in l and l.strip() and not l.startswith("#")]
        reqs_win = APP_DIR / "requirements_win.txt"
        reqs_win.write_text("\n".join(lines))

        on_progress("Downloading and installing dependencies (first run may take 5-15 minutes)...", 25)
        proc = subprocess.Popen(
            [str(PIP), "install", "-r", str(reqs_win), "-q", "--progress-bar", "off",
             "--trusted-host", "pypi.org", "--trusted-host", "files.pythonhosted.org"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        installed = 0
        for line in proc.stdout:
            line = line.strip()
            if line:
                installed += 1
                pct = min(25 + installed * 2, 90)
                on_progress(f"Installing: {line[:60]}", pct)
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError("pip install failed. Please check your network connection and try again.")

        FLAG.write_text(APP_VERSION)
        on_progress("Installation complete! Launching...", 100)
        on_done()

    except Exception as e:
        on_error(str(e))


# ── Tkinter progress window ───────────────────────────────────────────────────

class SetupWindow:
    def __init__(self, sys_python):
        self.sys_python = sys_python
        self.root = tk.Tk()
        self.root.title("Rivus · First-time Setup")
        self.root.geometry("460x220")
        self.root.resizable(False, False)
        self.root.configure(bg="#1a1a2e")
        self.root.protocol("WM_DELETE_WINDOW", lambda: sys.exit(0))

        tk.Label(self.root, text="问渠  Rivus", font=("Segoe UI", 18, "bold"),
                 bg="#1a1a2e", fg="#ffffff").pack(pady=(28, 4))
        tk.Label(self.root, text="Initializing, please wait...", font=("Segoe UI", 10),
                 bg="#1a1a2e", fg="#aaaacc").pack()

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("G.Horizontal.TProgressbar",
                        troughcolor="#2e2e4e", background="#27ae60",
                        bordercolor="#1a1a2e", lightcolor="#27ae60", darkcolor="#27ae60")
        self.bar = ttk.Progressbar(self.root, style="G.Horizontal.TProgressbar",
                                   length=380, mode="determinate")
        self.bar.pack(pady=16)

        self.status_var = tk.StringVar(value="Preparing...")
        tk.Label(self.root, textvariable=self.status_var, font=("Segoe UI", 9),
                 bg="#1a1a2e", fg="#888899", wraplength=400).pack()

    def update(self, msg, pct):
        try:
            self.root.after(0, lambda m=msg, p=pct: [
                self.status_var.set(m),
                self.bar.configure(value=p),
            ])
        except Exception:
            pass

    def done(self):
        self.root.after(800, self._finish)

    def _finish(self):
        self.root.destroy()
        launch_app()

    def error(self, msg):
        self.root.after(0, lambda: self._show_error(msg))

    def _show_error(self, msg):
        self.status_var.set(f"❌ {msg}")
        tk.Button(self.root, text="Close", command=self.root.destroy,
                  bg="#c0392b", fg="white", relief="flat",
                  font=("Segoe UI", 10)).pack(pady=8)

    def run(self):
        threading.Thread(
            target=run_install,
            args=(self.sys_python, self.update, self.done, self.error),
            daemon=True
        ).start()
        self.root.mainloop()


# ── Entry point ───────────────────────────────────────────────────────────────

def _show_fatal(msg: str):
    """Show a messagebox with a fatal error — works even before any window exists."""
    # Always write to log first, in case the messagebox itself fails
    try:
        log_path = APP_DIR / "startup_error.log"
        APP_DIR.mkdir(parents=True, exist_ok=True)
        import datetime
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n=== {datetime.datetime.now()} ===\n{msg}\n")
    except Exception:
        pass
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Rivus — Startup Error", msg)
        root.destroy()
    except Exception:
        pass  # if even tkinter fails, nothing we can do


if __name__ == "__main__":
    _early_log("__main__ entered")
    try:
        _installed = is_installed()
        _early_log(f"is_installed={_installed}  FLAG.exists={FLAG.exists()}  venv_ready={is_venv_ready()}  stored_ver='{installed_version()}'  APP_VERSION='{APP_VERSION}'")
        if _installed:
            _early_log("path: launch_app()")
            launch_app()
            _early_log("launch_app() returned — launcher exiting normally")
        elif needs_source_update():
            _early_log("path: needs_source_update → update_source_files()")
            try:
                update_source_files()
                _early_log("update_source_files() done")
            except Exception as e:
                _early_log(f"update_source_files FAILED: {e}")
                _show_fatal(f"Failed to update application files:\n{e}\n\n"
                            f"Try deleting %APPDATA%\\Rivus\\.installed and restarting.")
                sys.exit(1)
            launch_app()
            _early_log("launch_app() returned after update")
        else:
            _early_log("path: first install — find_system_python()")
            sys_python = find_system_python()
            _early_log(f"sys_python={sys_python}")
            if not sys_python:
                _show_fatal(
                    "Python 3.10+ not found.\n\n"
                    "Please install it from https://www.python.org/downloads/\n"
                    "and check \"Add Python to PATH\" during installation."
                )
                sys.exit(1)
            _early_log("opening SetupWindow")
            win = SetupWindow(sys_python)
            win.run()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        _early_log(f"EXCEPTION in __main__: {tb}")
        _show_fatal(f"Unexpected error during startup:\n\n{tb}")
        # If running in a console (debug build), keep the window open
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            try:
                input("\n[Press Enter to exit]")
            except Exception:
                pass
