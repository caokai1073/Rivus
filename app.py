"""
app.py — Rivus (问渠) entry point
Usage: python app.py
"""
import os
import sys
import threading
import socket
import time
import urllib.request
from pathlib import Path

# ── Windows packaged mode: redirect user data to %APPDATA%\Rivus ─────────────
# Only applies when running as a PyInstaller bundle on Windows; no effect on macOS / dev mode
if sys.platform == "win32" and getattr(sys, "frozen", False):
    if not os.environ.get("MEMORYVAULT_DATA_DIR"):
        _win_data = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Rivus")
        os.makedirs(_win_data, exist_ok=True)
        os.environ["MEMORYVAULT_DATA_DIR"] = _win_data
    # Add bundle root to PATH so DLLs can be found
    os.environ["PATH"] = sys._MEIPASS + os.pathsep + os.environ.get("PATH", "")

import uvicorn
import webview

from server import app as fastapi_app

_window      = None
_port        = [None]   # set once server starts; used by download_doc
_force_quit  = [False]  # set to True to allow the window to actually close
_nswindow    = [None]   # NSWindow reference captured on close, used for reopen
_app_delegate = [None]  # strong reference to delegate, prevents Python GC
_hwnd        = [None]   # Win32 HWND captured after window is shown
_tray        = [None]   # pystray Icon instance


def find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_server(port: int):
    uvicorn.run(fastapi_app, host="127.0.0.1", port=port, log_level="error")


class Api:
    """Native interface exposed to frontend JS"""

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

    def pick_excel(self):
        result = _window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Excel Files (*.xlsx *.xls)",),
        )
        return result[0] if result else None

    def pick_md(self):
        result = _window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Markdown Files (*.md)",),
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

    def download_doc(self, doc_id: int, suggested_filename: str):
        """Download a document: show system Save dialog, then write file content from the server."""
        save_path = self.pick_save_path(suggested_filename)
        if not save_path:
            return {"ok": False, "reason": "cancelled"}
        try:
            url = f"http://127.0.0.1:{_port[0]}/api/docs/{doc_id}/file"
            with urllib.request.urlopen(url) as resp:
                data = resp.read()
            with open(save_path, "wb") as f:
                f.write(data)
            return {"ok": True, "path": save_path}
        except Exception as e:
            return {"ok": False, "reason": str(e)}

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
            print(f"[open_file] Error: {e}")
            return False

    def open_url(self, url: str):
        import webbrowser
        try:
            webbrowser.open(url)
            return True
        except Exception as e:
            print(f"[open_url] Error: {e}")
            return False

    def quit_app(self):
        _force_quit[0] = True
        if _tray[0]:
            try:
                _tray[0].stop()
            except Exception:
                pass
        _window.destroy()


def _setup_macos():
    """
    macOS-specific initialization, runs in the background thread started by webview.start(func=...).
    Key: _app_delegate[0] holds a strong reference to the delegate to prevent Python GC.
    """
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSObject
        from objc import objc_method

        # ── 1. Define App Delegate ────────────────────────────────────────────
        class RivusDelegate(NSObject):

            @objc_method
            def applicationShouldHandleReopen_hasVisibleWindows_(self, app, hasVisibleWindows):
                """Dock icon click → restore window"""
                from AppKit import NSApp
                if _nswindow[0] is not None:
                    _nswindow[0].makeKeyAndOrderFront_(None)
                    NSApp.activateIgnoringOtherApps_(True)
                    print("[dock] Window restored")
                else:
                    print("[dock] reopen: _nswindow[0] is None, trying NSApp.windows()")
                    for w in NSApp.windows():
                        w.makeKeyAndOrderFront_(None)
                    NSApp.activateIgnoringOtherApps_(True)
                return True

            @objc_method
            def applicationShouldTerminate_(self, sender):
                """Dock right-click Quit / Cmd+Q → actually quit"""
                print("[dock] applicationShouldTerminate_ triggered")
                _force_quit[0] = True
                try:
                    _window.destroy()
                except Exception:
                    pass
                threading.Timer(1.0, lambda: os._exit(0)).start()
                return 1  # NSTerminateNow

        # ── 2. Instantiate and store in module-level list to prevent GC ──────
        delegate_instance = RivusDelegate.alloc().init()
        _app_delegate[0] = delegate_instance   # ← critical: keep strong reference alive

        # ── 3. Set delegate on the main thread ───────────────────────────────
        class _Installer(NSObject):
            @objc_method
            def run_(self, _arg):
                NSApplication.sharedApplication().setDelegate_(_app_delegate[0])
                print("[dock] NSApp delegate set, type:",
                      type(_app_delegate[0]).__name__)

        installer = _Installer.alloc().init()
        installer.performSelectorOnMainThread_withObject_waitUntilDone_(
            b"run:", None, True
        )

    except Exception as e:
        print(f"[dock] Initialization failed: {e}")
        import traceback; traceback.print_exc()


def _setup_win_tray():
    """Create a system tray icon on Windows (runs in a daemon thread)."""
    if sys.platform != "win32":
        return
    try:
        import pystray
        from PIL import Image

        ico_path = Path(__file__).parent / "AppIcon.ico"
        if ico_path.exists():
            image = Image.open(str(ico_path))
        else:
            # Fallback: plain green square
            image = Image.new("RGB", (64, 64), color=(39, 174, 96))

        def _show(icon, item):
            hw = _hwnd[0]
            if hw:
                import ctypes
                ctypes.windll.user32.ShowWindow(hw, 9)   # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(hw)

        def _quit(icon, item):
            _force_quit[0] = True
            icon.stop()
            if _window:
                try:
                    _window.destroy()
                except Exception:
                    pass
            os._exit(0)

        menu = pystray.Menu(
            pystray.MenuItem("显示 Rivus", _show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", _quit),
        )
        icon = pystray.Icon("Rivus", image, "Rivus · 问渠", menu)
        _tray[0] = icon
        icon.run()          # blocks this thread; daemon=True so it exits with the process
    except Exception as e:
        print(f"[tray] {e}")


if __name__ == "__main__":
    # ── Windows single-instance check ────────────────────────────────────────
    if sys.platform == "win32":
        import ctypes
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "Global\\RivusAppMutex")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            # FindWindowW cannot find hidden windows; use EnumWindows instead
            _found = [0]
            _ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)
            def _enum_cb(hwnd, _):
                buf = ctypes.create_unicode_buffer(256)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
                if "Rivus" in buf.value:
                    _found[0] = hwnd
                    return False  # stop enumeration
                return True
            ctypes.windll.user32.EnumWindows(_ENUMPROC(_enum_cb), 0)
            if _found[0]:
                ctypes.windll.user32.ShowWindow(_found[0], 9)    # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(_found[0])
            sys.exit(0)

    PORT = find_free_port()
    _port[0] = PORT

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

    def on_shown():
        """Capture Win32 HWND and set app icon once the window is visible."""
        if sys.platform == "win32":
            import ctypes
            _hwnd[0] = ctypes.windll.user32.FindWindowW(None, "Rivus · 问渠")
            if _hwnd[0]:
                ico = str(Path(__file__).parent / "AppIcon.ico")
                if os.path.exists(ico):
                    LR_LOADFROMFILE = 0x0010
                    LR_DEFAULTSIZE  = 0x0040
                    IMAGE_ICON      = 1
                    WM_SETICON      = 0x0080
                    hicon = ctypes.windll.user32.LoadImageW(
                        0, ico, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
                    )
                    if hicon:
                        ctypes.windll.user32.SendMessageW(_hwnd[0], WM_SETICON, 0, hicon)  # ICON_SMALL
                        ctypes.windll.user32.SendMessageW(_hwnd[0], WM_SETICON, 1, hicon)  # ICON_BIG

    def on_closing():
        """
        Red X button handler — hide the window instead of quitting on both platforms.
        Actual quit goes through Api.quit_app() which sets _force_quit[0] = True first.
        """
        if _force_quit[0]:
            return True  # real quit

        if sys.platform == "win32":
            import ctypes
            hw = _hwnd[0]
            if hw:
                ctypes.windll.user32.ShowWindow(hw, 0)  # SW_HIDE → remove from taskbar, tray stays
            return False  # prevent close

        # macOS: hide via NSWindow
        try:
            from AppKit import NSApp
            wins = NSApp.windows()
            if wins:
                _nswindow[0] = wins[0]
                _nswindow[0].orderOut_(None)
                return False
        except Exception as e:
            print(f"[closing] orderOut_ failed: {e}")
        _window.hide()
        return False

    _window.events.shown += on_shown
    _window.events.closing += on_closing

    if sys.platform == "win32":
        threading.Thread(target=_setup_win_tray, daemon=True).start()

    webview.start(func=_setup_macos)
