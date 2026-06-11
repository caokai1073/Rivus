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


if __name__ == "__main__":
    # ── Windows single-instance check ────────────────────────────────────────
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

    def on_closing():
        """
        Red X button handler:
        - Windows: exit the entire process (uvicorn daemon thread exits with it)
        - macOS: hide the window (Dock icon click can restore it)
        """
        if sys.platform == "win32":
            os._exit(0)

        if _force_quit[0]:
            return True
        try:
            from AppKit import NSApp
            wins = NSApp.windows()
            print(f"[closing] NSApp.windows() count: {len(wins)}")
            if wins:
                _nswindow[0] = wins[0]
                _nswindow[0].orderOut_(None)
                print("[closing] Window hidden, reference saved")
                return False
        except Exception as e:
            print(f"[closing] orderOut_ failed: {e}")
        _window.hide()
        return False

    _window.events.closing += on_closing
    webview.start(func=_setup_macos)
