"""
remote.py — SSH tunnel management (paramiko-based)

Supports both password and key-based authentication.
Establishes local port forwarding to expose a remote Ollama instance on a local port,
making it transparently accessible to query.py.

Architecture:
  local localhost:{local_port}  ←→  SSH tunnel  ←→  remote localhost:{remote_port} (Ollama)
"""
import os
import select
import socket
import threading
import time
import requests

# ── Global state ──────────────────────────────────────────────────────────────

_ssh_client = None          # paramiko.SSHClient
_forward_server = None      # _LocalForwardServer thread
_lock = threading.Lock()
_cfg: dict = {}


# ── Local port forwarding implementation ──────────────────────────────────────

class _ChannelBridge(threading.Thread):
    """Bidirectionally forwards data between a (local_socket, SSH channel) pair."""

    def __init__(self, chan, sock):
        super().__init__(daemon=True)
        self.chan = chan
        self.sock = sock

    def run(self):
        self.sock.settimeout(1.0)
        self.chan.settimeout(1.0)
        try:
            while True:
                try:
                    r, _, _ = select.select([self.sock, self.chan], [], [], 1.0)
                except Exception:
                    break
                if self.sock in r:
                    try:
                        data = self.sock.recv(4096)
                    except Exception:
                        break
                    if not data:
                        break
                    try:
                        self.chan.sendall(data)
                    except Exception:
                        break
                if self.chan in r:
                    try:
                        data = self.chan.recv(4096)
                    except Exception:
                        break
                    if not data:
                        break
                    try:
                        self.sock.sendall(data)
                    except Exception:
                        break
        finally:
            try:
                self.chan.close()
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass


class _LocalForwardServer(threading.Thread):
    """
    Listens on a local TCP port and forwards each connection to a remote port via an SSH channel.
    Equivalent to: ssh -L local_port:remote_host:remote_port
    """

    def __init__(self, ssh_client, remote_host: str, remote_port: int, local_port: int):
        super().__init__(daemon=True)
        self._ssh   = ssh_client
        self._rhost = remote_host
        self._rport = remote_port
        self._lport = local_port
        self._stop  = threading.Event()
        self._sock  = None

    def run(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.bind(("127.0.0.1", self._lport))
            self._sock.listen(10)
            self._sock.settimeout(1.0)
        except Exception:
            return

        while not self._stop.is_set():
            try:
                client_sock, _ = self._sock.accept()
            except socket.timeout:
                continue
            except Exception:
                break

            try:
                transport = self._ssh.get_transport()
                if transport is None or not transport.is_active():
                    client_sock.close()
                    break
                chan = transport.open_channel(
                    "direct-tcpip",
                    (self._rhost, self._rport),
                    ("127.0.0.1", 0),
                )
                bridge = _ChannelBridge(chan, client_sock)
                bridge.start()
            except Exception:
                try:
                    client_sock.close()
                except Exception:
                    pass

        try:
            self._sock.close()
        except Exception:
            pass

    def stop(self):
        self._stop.set()
        try:
            # Unblock accept() by making a dummy connection
            socket.create_connection(("127.0.0.1", self._lport), timeout=0.1).close()
        except Exception:
            pass


# ── Internal utilities ────────────────────────────────────────────────────────

def _ollama_ok(base: str, timeout: float = 4.0) -> bool:
    try:
        return requests.get(f"{base}/api/tags", timeout=timeout).ok
    except Exception:
        return False


def _cleanup():
    """Close the SSH connection and port-forward server."""
    global _ssh_client, _forward_server, _cfg
    with _lock:
        if _forward_server:
            try:
                _forward_server.stop()
            except Exception:
                pass
            _forward_server = None
        if _ssh_client:
            try:
                _ssh_client.close()
            except Exception:
                pass
            _ssh_client = None
        _cfg = {}


# ── Public API ────────────────────────────────────────────────────────────────

def list_remote_models() -> list[str]:
    """
    List Ollama models on the remote server (when tunnel is active).
    Returns an empty list if not connected or Ollama is unresponsive.
    """
    base = get_base()
    if not base:
        return []
    try:
        r = requests.get(f"{base}/api/tags", timeout=4)
        return [m["name"] for m in r.json().get("models", [])] if r.ok else []
    except Exception:
        return []

def get_base() -> str | None:
    """
    Returns the local forwarding URL when the tunnel is active (e.g. http://localhost:11435),
    or None if not connected. Called by query.py's _ollama_base().
    """
    with _lock:
        if _ssh_client is None:
            return None
        t = _ssh_client.get_transport()
        if t is None or not t.is_active():
            return None
        return f"http://127.0.0.1:{_cfg.get('local_port', 11435)}"


def connect(
    host: str,
    user: str,
    ssh_port: int,
    auth_mode: str,        # "key" | "password"
    key_path: str = "",
    password: str = "",
    remote_port: int = 11434,
    local_port: int = 11435,
) -> dict:
    """
    Establish an SSH connection and local port forwarding.
    Returns {"ok": True} or {"ok": False, "error": "..."}.
    """
    global _ssh_client, _forward_server, _cfg

    if not host.strip():
        return {"ok": False, "error": "Host cannot be empty"}
    if not user.strip():
        return {"ok": False, "error": "Username cannot be empty"}

    # Disconnect any existing connection first
    disconnect()

    try:
        import paramiko
    except ImportError:
        return {
            "ok": False,
            "error": (
                "Missing dependency: paramiko. Run: pip install paramiko\n"
                "(Windows users: run this in the Command Prompt in the Rivus directory)"
            ),
        }

    client = paramiko.SSHClient()
    # Auto-accept host keys for new hosts (equivalent to StrictHostKeyChecking=accept-new)
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict = dict(
        hostname=host,
        port=ssh_port,
        username=user,
        timeout=15,
        banner_timeout=15,
        auth_timeout=20,
    )

    if auth_mode == "password":
        if not password:
            return {"ok": False, "error": "Password cannot be empty"}
        connect_kwargs["password"] = password
        connect_kwargs["look_for_keys"] = False
        connect_kwargs["allow_agent"] = False
    else:
        # Key-based authentication
        key_expanded = os.path.expanduser(key_path) if key_path else ""
        if key_expanded and os.path.isfile(key_expanded):
            connect_kwargs["key_filename"] = key_expanded
        else:
            # Let paramiko auto-search ~/.ssh/id_rsa, id_ed25519, etc.
            connect_kwargs["look_for_keys"] = True
        connect_kwargs["allow_agent"] = True

    try:
        client.connect(**connect_kwargs)
    except paramiko.AuthenticationException:
        return {"ok": False, "error": "SSH authentication failed. Check your username and password/key."}
    except paramiko.SSHException as e:
        return {"ok": False, "error": f"SSH error: {e}"}
    except socket.timeout:
        return {"ok": False, "error": f"Connection timed out. Check the host address and port ({host}:{ssh_port})."}
    except Exception as e:
        return {"ok": False, "error": f"Connection failed: {e}"}

    # Start local port forwarding
    fwd = _LocalForwardServer(client, "127.0.0.1", remote_port, local_port)
    fwd.start()

    # Wait up to 8 seconds for Ollama to respond
    base = f"http://127.0.0.1:{local_port}"
    deadline = time.time() + 8
    while time.time() < deadline:
        if _ollama_ok(base, timeout=2.0):
            break
        time.sleep(1)
    else:
        fwd.stop()
        client.close()
        return {
            "ok": False,
            "error": (
                "SSH connected, but remote Ollama is not responding.\n"
                "Please make sure Ollama is running on the remote server (ollama serve)."
            ),
        }

    with _lock:
        _ssh_client    = client
        _forward_server = fwd
        _cfg = {
            "host":        host,
            "user":        user,
            "ssh_port":    ssh_port,
            "auth_mode":   auth_mode,
            "remote_port": remote_port,
            "local_port":  local_port,
        }

    return {"ok": True}


def disconnect():
    """Disconnect the SSH tunnel."""
    _cleanup()


def status() -> dict:
    """
    Returns connection status dict:
    {
      "connected": bool,
      "host": str,
      "local_port": int,
      "models": [...],
      "error": str   # optional
    }
    """
    with _lock:
        if _ssh_client is None:
            return {"connected": False}
        t = _ssh_client.get_transport()
        alive = t is not None and t.is_active()
        cfg_copy = dict(_cfg)

    if not alive:
        return {"connected": False}

    base = f"http://127.0.0.1:{cfg_copy.get('local_port', 11435)}"
    try:
        r = requests.get(f"{base}/api/tags", timeout=4)
        models = [m["name"] for m in r.json().get("models", [])] if r.ok else []
        return {
            "connected": True,
            "host":       cfg_copy.get("host", ""),
            "local_port": cfg_copy.get("local_port"),
            "models":     models,
        }
    except Exception as e:
        return {
            "connected": True,
            "host":       cfg_copy.get("host", ""),
            "local_port": cfg_copy.get("local_port"),
            "models":     [],
            "error":      str(e),
        }
