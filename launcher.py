"""
launcher.py — Rivus Windows 轻量启动器
首次运行：创建 venv + pip install，显示进度窗口
后续运行：直接用 venv 中的 Python 启动 app.py
"""
import sys
import os
import subprocess
import shutil
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

# ── 路径 ──────────────────────────────────────────────────────────────────────
APPDATA  = Path(os.environ.get("APPDATA", Path.home()))
APP_DIR  = APPDATA / "Rivus"
VENV_DIR = APP_DIR / "venv"
SRC_DIR  = APP_DIR / "app"
DATA_DIR = APP_DIR / "data"
PYTHON   = VENV_DIR / "Scripts" / "python.exe"
PIP      = VENV_DIR / "Scripts" / "pip.exe"
FLAG     = APP_DIR / ".installed"


def find_system_python() -> str:
    """找系统安装的真实 Python，避免用 sys.executable（打包后是 Rivus.exe 本身）"""
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


def is_installed() -> bool:
    return FLAG.exists() and PYTHON.exists() and (SRC_DIR / "app.py").exists()


def launch_app():
    env = os.environ.copy()
    env["MEMORYVAULT_DATA_DIR"] = str(DATA_DIR)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [str(PYTHON), str(SRC_DIR / "app.py")],
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


# ── 安装流程（后台线程）──────────────────────────────────────────────────────

def run_install(sys_python, on_progress, on_done, on_error):
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)

        on_progress("正在复制程序文件…", 5)
        shutil.copytree(bundled_src(), SRC_DIR, dirs_exist_ok=True)

        on_progress("正在创建 Python 环境…", 15)
        if VENV_DIR.exists():
            shutil.rmtree(VENV_DIR)   # 清除上次中断留下的残缺 venv
        subprocess.run(
            [sys_python, "-m", "venv", str(VENV_DIR)],
            check=True, capture_output=True
        )

        on_progress("正在升级 pip…", 20)
        # 升级 pip 失败不影响后续安装，直接跳过
        subprocess.run(
            [str(PIP), "install", "--upgrade", "pip", "-q",
             "--trusted-host", "pypi.org", "--trusted-host", "files.pythonhosted.org"],
            capture_output=True
        )

        # 过滤掉 macOS 专属依赖
        reqs = SRC_DIR / "requirements.txt"
        lines = [l for l in reqs.read_text().splitlines()
                 if "pyobjc" not in l and l.strip() and not l.startswith("#")]
        reqs_win = APP_DIR / "requirements_win.txt"
        reqs_win.write_text("\n".join(lines))

        on_progress("正在下载并安装依赖（首次约需 5-15 分钟）…", 25)
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
                on_progress(f"安装中：{line[:60]}", pct)
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError("pip install 失败，请检查网络连接后重试。")

        FLAG.write_text("ok")
        on_progress("安装完成！正在启动…", 100)
        on_done()

    except Exception as e:
        on_error(str(e))


# ── Tkinter 进度窗口 ──────────────────────────────────────────────────────────

class SetupWindow:
    def __init__(self, sys_python):
        self.sys_python = sys_python
        self.root = tk.Tk()
        self.root.title("Rivus · 首次安装")
        self.root.geometry("460x220")
        self.root.resizable(False, False)
        self.root.configure(bg="#1a1a2e")
        self.root.protocol("WM_DELETE_WINDOW", lambda: sys.exit(0))

        tk.Label(self.root, text="问渠  Rivus", font=("Segoe UI", 18, "bold"),
                 bg="#1a1a2e", fg="#ffffff").pack(pady=(28, 4))
        tk.Label(self.root, text="正在初始化，请稍候…", font=("Segoe UI", 10),
                 bg="#1a1a2e", fg="#aaaacc").pack()

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("G.Horizontal.TProgressbar",
                        troughcolor="#2e2e4e", background="#27ae60",
                        bordercolor="#1a1a2e", lightcolor="#27ae60", darkcolor="#27ae60")
        self.bar = ttk.Progressbar(self.root, style="G.Horizontal.TProgressbar",
                                   length=380, mode="determinate")
        self.bar.pack(pady=16)

        self.status_var = tk.StringVar(value="准备中…")
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
        tk.Button(self.root, text="关闭", command=self.root.destroy,
                  bg="#c0392b", fg="white", relief="flat",
                  font=("Segoe UI", 10)).pack(pady=8)

    def run(self):
        threading.Thread(
            target=run_install,
            args=(self.sys_python, self.update, self.done, self.error),
            daemon=True
        ).start()
        self.root.mainloop()


# ── 入口 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if is_installed():
        launch_app()
    else:
        sys_python = find_system_python()
        if not sys_python:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "未找到 Python",
                "请先安装 Python 3.10+\nhttps://www.python.org/downloads/\n\n"
                "安装时记得勾选 \"Add Python to PATH\""
            )
            sys.exit(1)
        win = SetupWindow(sys_python)
        win.run()
