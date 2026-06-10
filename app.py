"""
app.py — Rivus (问渠) 入口
运行: python app.py
"""
import os
import sys
import threading
import socket
import time
import urllib.request

# ── Windows 打包模式：把用户数据目录指向 %APPDATA%\Rivus ──────────────────────
# 仅在 PyInstaller 打包 + Windows 下生效，不影响 macOS / 开发模式
if sys.platform == "win32" and getattr(sys, "frozen", False):
    if not os.environ.get("MEMORYVAULT_DATA_DIR"):
        _win_data = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Rivus")
        os.makedirs(_win_data, exist_ok=True)
        os.environ["MEMORYVAULT_DATA_DIR"] = _win_data
    # 把打包内容根目录加入 PATH，确保 DLL 能被找到
    os.environ["PATH"] = sys._MEIPASS + os.pathsep + os.environ.get("PATH", "")

import uvicorn
import webview

from server import app as fastapi_app

_window      = None
_force_quit  = [False]  # True 时 on_closing 放行，允许真正退出
_nswindow    = [None]   # on_closing 时捕获 NSWindow 引用，供 reopen 使用
_app_delegate = [None]  # 持有 delegate 强引用，防止 Python GC 回收


def find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_server(port: int):
    uvicorn.run(fastapi_app, host="127.0.0.1", port=port, log_level="error")


class Api:
    """暴露给前端 JS 调用的原生接口"""

    def pick_pdf(self):
        result = _window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("PDF Files (*.pdf)",),
        )
        return result[0] if result else None

    def pick_docx(self):
        result = _window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Word Documents (*.docx)",),
        )
        return result[0] if result else None

    def pick_zip(self):
        result = _window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Rivus Backup (*.zip)",),
        )
        return result[0] if result else None

    def pick_save_path(self, filename: str):
        result = _window.create_file_dialog(
            webview.SAVE_DIALOG,
            save_filename=filename,
        )
        if not result:
            return None
        return result[0] if isinstance(result, (list, tuple)) else result

    def open_file(self, path: str):
        import subprocess
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif sys.platform == "win32":
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open", path])
            return True
        except Exception as e:
            print(f"[open_file] 错误: {e}")
            return False

    def open_url(self, url: str):
        import webbrowser
        try:
            webbrowser.open(url)
            return True
        except Exception as e:
            print(f"[open_url] 错误: {e}")
            return False

    def quit_app(self):
        _force_quit[0] = True
        _window.destroy()


def _setup_macos():
    """
    macOS 专属初始化，在 webview.start(func=...) 的后台线程中运行。
    关键：_app_delegate[0] 保持对 delegate 的强引用，防止 Python GC。
    """
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSObject
        from objc import objc_method

        # ── 1. 定义 App Delegate ──────────────────────────────────────────────
        class RivusDelegate(NSObject):

            @objc_method
            def applicationShouldHandleReopen_hasVisibleWindows_(self, app, hasVisibleWindows):
                """Dock 图标点击 → 恢复窗口"""
                from AppKit import NSApp
                if _nswindow[0] is not None:
                    _nswindow[0].makeKeyAndOrderFront_(None)
                    NSApp.activateIgnoringOtherApps_(True)
                    print("[dock] 窗口已恢复")
                else:
                    print("[dock] reopen: _nswindow[0] 为 None，尝试 NSApp.windows()")
                    for w in NSApp.windows():
                        w.makeKeyAndOrderFront_(None)
                    NSApp.activateIgnoringOtherApps_(True)
                return True

            @objc_method
            def applicationShouldTerminate_(self, sender):
                """Dock 右键 Quit / Cmd+Q → 真正退出"""
                print("[dock] applicationShouldTerminate_ 触发")
                _force_quit[0] = True
                try:
                    _window.destroy()
                except Exception:
                    pass
                threading.Timer(1.0, lambda: os._exit(0)).start()
                return 1  # NSTerminateNow

        # ── 2. 实例化，存入模块级列表防 GC ──────────────────────────────────
        delegate_instance = RivusDelegate.alloc().init()
        _app_delegate[0] = delegate_instance   # ← 关键：强引用保活

        # ── 3. 在主线程上设置 delegate ──────────────────────────────────────
        class _Installer(NSObject):
            @objc_method
            def run_(self, _arg):
                NSApplication.sharedApplication().setDelegate_(_app_delegate[0])
                print("[dock] NSApp delegate 已设置，类型:",
                      type(_app_delegate[0]).__name__)

        installer = _Installer.alloc().init()
        installer.performSelectorOnMainThread_withObject_waitUntilDone_(
            b"run:", None, True
        )

    except Exception as e:
        print(f"[dock] 初始化失败: {e}")
        import traceback; traceback.print_exc()


if __name__ == "__main__":
    # ── Windows 单实例检测 ────────────────────────────────────────────────────
    if sys.platform == "win32":
        import ctypes
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "Global\\RivusAppMutex")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            hwnd = ctypes.windll.user32.FindWindowW(None, "Rivus · 问渠")
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 9)       # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            sys.exit(0)

    PORT = find_free_port()

    t = threading.Thread(target=run_server, args=(PORT,), daemon=True)
    t.start()

    for _ in range(40):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}", timeout=1)
            break
        except Exception:
            time.sleep(0.3)

    api = Api()
    _window = webview.create_window(
        "Rivus · 问渠",
        f"http://127.0.0.1:{PORT}",
        width=1100,
        height=750,
        min_size=(800, 600),
        js_api=api,
    )

    def on_closing():
        """
        红色 X 点击：
        - Windows：直接退出整个进程（uvicorn daemon 线程随之结束）
        - macOS：隐藏窗口（Dock 图标点击可恢复）
        """
        if sys.platform == "win32":
            os._exit(0)

        if _force_quit[0]:
            return True
        try:
            from AppKit import NSApp
            wins = NSApp.windows()
            print(f"[closing] NSApp.windows() 数量: {len(wins)}")
            if wins:
                _nswindow[0] = wins[0]
                _nswindow[0].orderOut_(None)
                print("[closing] 窗口已隐藏，引用已保存")
                return False
        except Exception as e:
            print(f"[closing] orderOut_ 失败: {e}")
        _window.hide()
        return False

    _window.events.closing += on_closing
    webview.start(func=_setup_macos)
