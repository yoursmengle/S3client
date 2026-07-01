#!/usr/bin/env python3
"""
R2 Manager – A GUI client for Cloudflare R2 object storage.
Based on the r2client library: https://github.com/fayharinn/R2-Client

Credentials are stored ONLY as Windows user environment variables (registry),
never written to disk files.
"""

import os
import sys
import hmac
import hashlib
import datetime
import threading
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote as _urlquote

# ─── Ensure dependencies are installed ───────────────────────────────────────
def _ensure_pkg(pkg, import_name=None):
    import importlib
    name = import_name or pkg
    try:
        importlib.import_module(name)
    except ImportError:
        print(f"[r2-manager] Installing {pkg}…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

_ensure_pkg("requests")
_ensure_pkg("r2client", "r2client")

import requests
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from r2client.R2Client import R2Client as _R2ClientLib
except ImportError:
    _R2ClientLib = None  # handled at runtime

# ─── Windows registry helpers (persistent env vars without files) ─────────────
try:
    import winreg
    _WINDOWS = True
except ImportError:
    _WINDOWS = False


def _reg_write(name: str, value: str) -> None:
    """Write a user-level environment variable to HKCU\\Environment (Windows)."""
    if not _WINDOWS:
        return
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
    )
    winreg.SetValueEx(key, name, 0, winreg.REG_EXPAND_SZ, value)
    winreg.CloseKey(key)
    # Broadcast WM_SETTINGCHANGE so new processes pick up the variable
    try:
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, "Environment", 2, 3000, None
        )
    except Exception:
        pass


def _reg_read(name: str) -> str:
    """Read a user-level environment variable from HKCU\\Environment."""
    if not _WINDOWS:
        return ""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ
        )
        val, _ = winreg.QueryValueEx(key, name)
        winreg.CloseKey(key)
        return val or ""
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def save_credentials(access_key: str, secret_key: str, endpoint: str) -> None:
    """Persist R2 credentials as user environment variables (no disk files)."""
    for name, value in [
        ("R2_ACCESS_KEY", access_key),
        ("R2_SECRET_KEY", secret_key),
        ("R2_ENDPOINT",   endpoint),
    ]:
        _reg_write(name, value)
        os.environ[name] = value


def load_credentials() -> dict:
    """Load credentials from the current process env or the registry."""
    result = {}
    for name in ("R2_ACCESS_KEY", "R2_SECRET_KEY", "R2_ENDPOINT"):
        val = os.environ.get(name) or _reg_read(name)
        result[name] = val or ""
        if val:
            os.environ[name] = val
    return result


def has_credentials() -> bool:
    c = load_credentials()
    return all(c[k] for k in ("R2_ACCESS_KEY", "R2_SECRET_KEY", "R2_ENDPOINT"))


_BUCKET_FILE = Path(__file__).parent / ".r2_bucket"


def save_last_bucket(bucket: str) -> None:
    """Persist the last-used bucket name to a local file."""
    try:
        _BUCKET_FILE.write_text(bucket.strip(), encoding="utf-8")
    except Exception:
        pass


def load_last_bucket() -> str:
    """Return the last-used bucket name, or empty string if none saved."""
    try:
        return _BUCKET_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


# ─── Color theme ──────────────────────────────────────────────────────────────
C = {
    "bg":        "#f0f7f4",   # 主背景：浅薄荷绿
    "sidebar":   "#dff0e8",   # 侧边栏：柔和绿
    "panel":     "#f7fdf9",   # 内容面板：近白
    "toolbar":   "#e8f5ee",   # 工具栏
    "selected":  "#b7dfc9",   # 选中行
    "hover":     "#cceedd",   # 悬停
    "fg":        "#1a3a2a",   # 主文字：深绿黑
    "fg2":       "#5a8a6a",   # 次要文字
    "accent":    "#2e9e6a",   # 强调色：中绿
    "accent2":   "#1b7a50",   # 强调色深
    "green":     "#27ae60",   # 操作绿
    "red":       "#e05c5c",   # 危险红
    "yellow":    "#d4870a",   # 文件夹黄
    "border":    "#b0d8c0",   # 边框
    "input_bg":  "#ffffff",   # 输入框
    "btn_bg":    "#d0eedd",   # 按钮背景
    "btn_hover": "#b0d8c0",   # 按钮悬停
    "progress":  "#2e9e6a",   # 进度条
    "hdr_bg":    "#2e9e6a",   # 标题栏渐变起始
    "row_alt":   "#edf8f2",   # 表格交替行
}

FONT   = ("Segoe UI", 9)
FONT_B = ("Segoe UI", 9, "bold")
FONT_L = ("Segoe UI", 13, "bold")
FONT_S = ("Segoe UI", 8)


# ─── R2 Backend ───────────────────────────────────────────────────────────────
class R2Manager:
    """
    Wraps r2client and adds a delete_file() method using AWS SigV4 signing
    (the same mechanism used internally by r2client).
    """

    def __init__(self, access_key: str, secret_key: str, endpoint: str):
        self.access_key = access_key
        self.secret_key = secret_key
        self.endpoint   = endpoint.rstrip("/")

    # ── SigV4 helpers ────────────────────────────────────────────────────────

    def _sign(self, key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def _signing_key(self, date_stamp: str) -> bytes:
        k = self._sign(("AWS4" + self.secret_key).encode("utf-8"), date_stamp)
        k = self._sign(k, "auto")   # Cloudflare R2 region
        k = self._sign(k, "s3")
        return self._sign(k, "aws4_request")

    @staticmethod
    def _encode_key(key: str) -> str:
        """
        Percent-encode an object key for use in both the canonical URI and the
        request URL.  Each path segment is encoded per RFC 3986 (unreserved chars
        A-Z a-z 0-9 - _ . ~ are kept; '/' is preserved as the segment separator).
        This is required by AWS SigV4 for non-ASCII characters such as Chinese.
        """
        return "/".join(_urlquote(seg, safe="") for seg in key.split("/"))

    def _auth_headers(self, method: str, bucket: str, key: str = "") -> dict:
        """Build minimal AWS SigV4 Authorization headers for the given request."""
        host       = self.endpoint.split("://", 1)[-1]
        now        = datetime.datetime.now(datetime.timezone.utc)
        amz_date   = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        enc_key    = self._encode_key(key)
        uri        = f"/{bucket}/{enc_key}" if key else f"/{bucket}/"
        ph         = hashlib.sha256(b"").hexdigest()  # empty payload

        canonical_headers = (
            f"host:{host}\n"
            f"x-amz-content-sha256:{ph}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = (
            f"{method}\n{uri}\n\n"
            f"{canonical_headers}\n{signed_headers}\n{ph}"
        )

        algo       = "AWS4-HMAC-SHA256"
        cred_scope = f"{date_stamp}/auto/s3/aws4_request"
        sts        = (
            f"{algo}\n{amz_date}\n{cred_scope}\n"
            + hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        )
        sig = hmac.new(
            self._signing_key(date_stamp),
            sts.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        auth = (
            f"{algo} Credential={self.access_key}/{cred_scope}, "
            f"SignedHeaders={signed_headers}, Signature={sig}"
        )
        return {
            "x-amz-date":          amz_date,
            "x-amz-content-sha256": ph,
            "Authorization":       auth,
        }

    # ── Error helper ────────────────────────────────────────────────────────

    def _raise_for_status(self, resp: requests.Response, action: str = "") -> None:
        """Raise a descriptive error that includes R2's XML error body."""
        if resp.status_code < 400:
            return
        # Try to extract Code/Message from R2 XML error response
        body = resp.text[:600].strip()
        try:
            root = ET.fromstring(resp.content)
            code = (root.findtext("{http://s3.amazonaws.com/doc/2006-03-01/}Code")
                    or root.findtext("Code") or "")
            msg  = (root.findtext("{http://s3.amazonaws.com/doc/2006-03-01/}Message")
                    or root.findtext("Message") or body)
            body = f"{code}: {msg}" if code else msg
        except Exception:
            pass
        prefix = f"[{action}] " if action else ""
        if resp.status_code == 403:
            raise PermissionError(
                f"{prefix}403 Forbidden – {body}\n\n"
                "Possible causes:\n"
                "  • Access Key or Secret Key is incorrect\n"
                "  • Endpoint URL is wrong (must be: https://<account_id>.r2.cloudflarestorage.com)\n"
                "  • The API token lacks R2 read/write permissions"
            )
        if resp.status_code == 404:
            raise FileNotFoundError(f"{prefix}404 Not Found – {body}")
        raise RuntimeError(f"{prefix}HTTP {resp.status_code} – {body}")

    # ── SigV4 upload helpers ─────────────────────────────────────────────────

    def _auth_headers_put(self, bucket: str, key: str,
                          payload_hash: str, content_type: str) -> dict:
        """SigV4 headers for PUT (upload) – content hash must be signed."""
        host       = self.endpoint.split("://", 1)[-1]
        now        = datetime.datetime.now(datetime.timezone.utc)
        amz_date   = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        enc_key    = self._encode_key(key)
        uri        = f"/{bucket}/{enc_key}"

        canonical_headers = (
            f"content-type:{content_type}\n"
            f"host:{host}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "content-type;host;x-amz-content-sha256;x-amz-date"
        canonical_request = (
            f"PUT\n{uri}\n\n"
            f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )

        algo       = "AWS4-HMAC-SHA256"
        cred_scope = f"{date_stamp}/auto/s3/aws4_request"
        sts        = (
            f"{algo}\n{amz_date}\n{cred_scope}\n"
            + hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
        )
        sig = hmac.new(
            self._signing_key(date_stamp),
            sts.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "Content-Type":          content_type,
            "x-amz-date":            amz_date,
            "x-amz-content-sha256":  payload_hash,
            "Authorization": (
                f"{algo} Credential={self.access_key}/{cred_scope}, "
                f"SignedHeaders={signed_headers}, Signature={sig}"
            ),
        }

    # ── Public API ───────────────────────────────────────────────────────────

    def list_all_files(self, bucket: str) -> list:
        """Return a flat list of file metadata dicts: {key, size, last_modified}."""
        url      = f"{self.endpoint}/{bucket}/"
        headers  = self._auth_headers("GET", bucket)
        response = requests.get(url, headers=headers, timeout=30)
        self._raise_for_status(response, "list")
        ns    = "{http://s3.amazonaws.com/doc/2006-03-01/}"
        root  = ET.fromstring(response.content)
        files = []
        for item in root.findall(f"{ns}Contents"):
            key   = item.findtext(f"{ns}Key")          or ""
            size  = item.findtext(f"{ns}Size")         or "0"
            mtime = item.findtext(f"{ns}LastModified") or ""
            if key:
                files.append({
                    "key":           key,
                    "size":          int(size),
                    "last_modified": mtime,
                })
        return files

    def upload_file(self, bucket: str, local_path: str, r2_key: str) -> None:
        with open(local_path, "rb") as fh:
            data = fh.read()
        payload_hash = hashlib.sha256(data).hexdigest()
        # Derive MIME type via r2client helper; fall back to octet-stream
        try:
            from r2client.mime_types import get_content_type
            content_type = get_content_type(local_path)
        except Exception:
            content_type = "application/octet-stream"
        url     = f"{self.endpoint}/{bucket}/{self._encode_key(r2_key)}"
        headers = self._auth_headers_put(bucket, r2_key, payload_hash, content_type)
        resp    = requests.put(url, headers=headers, data=data, timeout=120)
        self._raise_for_status(resp, "upload")

    def download_file(self, bucket: str, r2_key: str, local_path: str) -> None:
        url     = f"{self.endpoint}/{bucket}/{self._encode_key(r2_key)}"
        headers = self._auth_headers("GET", bucket, r2_key)
        resp    = requests.get(url, headers=headers, timeout=120, stream=True)
        self._raise_for_status(resp, "download")
        with open(local_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)

    def delete_file(self, bucket: str, r2_key: str) -> None:
        url     = f"{self.endpoint}/{bucket}/{self._encode_key(r2_key)}"
        headers = self._auth_headers("DELETE", bucket, r2_key)
        resp    = requests.delete(url, headers=headers, timeout=30)
        self._raise_for_status(resp, "delete")

    def create_folder(self, bucket: str, folder_key: str) -> None:
        """Create a virtual folder by uploading a zero-byte placeholder object."""
        if not folder_key.endswith("/"):
            folder_key += "/"
        payload_hash = hashlib.sha256(b"").hexdigest()
        url     = f"{self.endpoint}/{bucket}/{self._encode_key(folder_key)}"
        headers = self._auth_headers_put(bucket, folder_key, payload_hash,
                                         "application/x-directory")
        resp    = requests.put(url, headers=headers, data=b"", timeout=30)
        self._raise_for_status(resp, "mkdir")


# ─── Setup / Credentials Dialog ───────────────────────────────────────────────
class SetupDialog(tk.Toplevel):
    """Modal dialog shown on first launch or when editing credentials."""

    def __init__(self, parent, on_save_callback):
        super().__init__(parent)
        self._on_save = on_save_callback
        self.title("R2 Manager – Connect to Cloudflare R2")
        self.resizable(False, False)
        self.configure(bg=C["bg"])
        self.grab_set()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        # Centre over parent
        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width()  // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"+{pw - self.winfo_width()//2}+{ph - self.winfo_height()//2}")
        parent.wait_window(self)

    def _build(self):
        # Gradient-style top bar
        top = tk.Frame(self, bg=C["accent"], height=56)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(
            top, text="☁  连接 Cloudflare R2",
            bg=C["accent"], fg="#ffffff", font=("Segoe UI", 14, "bold"),
        ).pack(pady=14)

        tk.Label(
            self,
            text="凭证以用户环境变量形式保存，不会写入任何磁盘文件。",
            bg=C["bg"], fg=C["fg2"], font=FONT_S, justify="center",
        ).pack(pady=(14, 4))

        frm = tk.Frame(self, bg=C["bg"])
        frm.pack(padx=36, fill="x")

        existing = load_credentials()
        fields = [
            ("Access Key ID",     "R2_ACCESS_KEY", False,
             "Cloudflare R2 Access Key ID"),
            ("Secret Access Key", "R2_SECRET_KEY", True,
             "Cloudflare R2 Secret Access Key"),
            ("Endpoint URL",      "R2_ENDPOINT",   False,
             "https://<account_id>.r2.cloudflarestorage.com"),
        ]
        self._vars: dict[str, tk.StringVar] = {}

        for label, env_key, secret, placeholder in fields:
            tk.Label(
                frm, text=label, bg=C["bg"], fg=C["fg"], font=FONT_B, anchor="w"
            ).pack(fill="x", pady=(10, 2))
            var = tk.StringVar(value=existing.get(env_key, ""))
            ent = tk.Entry(
                frm, textvariable=var,
                show="●" if secret else "",
                bg=C["input_bg"], fg=C["fg"],
                insertbackground=C["fg"],
                relief="flat", font=FONT,
                highlightthickness=1,
                highlightcolor=C["accent"],
                highlightbackground=C["border"],
            )
            ent.pack(fill="x", ipady=7)
            self._vars[env_key] = var

        # Buttons
        btn_frame = tk.Frame(self, bg=C["bg"])
        btn_frame.pack(pady=24)
        tk.Button(
            btn_frame, text="  ✔  连接  ", command=self._save,
            bg=C["accent"], fg="#ffffff", font=FONT_B,
            relief="flat", cursor="hand2", padx=16, pady=8,
            activebackground=C["accent2"], activeforeground="#ffffff",
            bd=0,
        ).pack(side="left", padx=8)
        tk.Button(
            btn_frame, text="  取消  ", command=self.destroy,
            bg=C["btn_bg"], fg=C["fg2"], font=FONT,
            relief="flat", cursor="hand2", padx=16, pady=8,
            activebackground=C["btn_hover"], activeforeground=C["fg"],
            bd=0,
        ).pack(side="left", padx=8)


    def _save(self):
        ak = self._vars["R2_ACCESS_KEY"].get().strip()
        sk = self._vars["R2_SECRET_KEY"].get().strip()
        ep = self._vars["R2_ENDPOINT"].get().strip()
        if not (ak and sk and ep):
            messagebox.showwarning("Missing Fields",
                                   "All three fields are required.", parent=self)
            return
        if not ep.startswith("http"):
            messagebox.showwarning("Invalid Endpoint",
                                   "Endpoint must start with https://", parent=self)
            return
        save_credentials(ak, sk, ep)
        self._on_save(ak, sk, ep)
        self.destroy()


# ─── Main Application Window ──────────────────────────────────────────────────
class R2ManagerApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("R2 Manager")
        self.geometry("1100x680")
        self.minsize(800, 500)
        self.configure(bg=C["bg"])

        # Set window icon (cloud symbol via window title emoji workaround)
        try:
            self.iconbitmap(default="")
        except Exception:
            pass

        # ── State ────────────────────────────────────────────────────────────
        self._r2:              R2Manager | None = None
        self._current_bucket   = tk.StringVar()
        self._current_prefix   = ""          # current "folder" path e.g. "imgs/"
        self._all_files:       list = []
        self._status_text      = tk.StringVar(value="Not connected  –  please add credentials via Settings")
        self._sort_reverse:    dict = {}

        # ── Build UI ─────────────────────────────────────────────────────────
        self._apply_styles()
        self._build_menubar()
        self._build_header()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

        # ── Connect on start ─────────────────────────────────────────────────
        self.after(120, self._auto_connect)

    # ── TTK / widget styles ──────────────────────────────────────────────────

    def _apply_styles(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".",
            background=C["bg"], foreground=C["fg"], font=FONT,
            troughcolor=C["border"], borderwidth=0,
        )
        st.configure("Treeview",
            background=C["panel"], foreground=C["fg"],
            fieldbackground=C["panel"], rowheight=28,
            borderwidth=0, relief="flat",
        )
        st.configure("Treeview.Heading",
            background=C["sidebar"], foreground=C["accent2"],
            font=FONT_B, relief="flat", padding=(8, 5),
        )
        st.map("Treeview",
            background=[("selected", C["selected"])],
            foreground=[("selected", C["accent2"])],
        )
        st.configure("Vertical.TScrollbar",
            background=C["btn_bg"], troughcolor=C["bg"],
            arrowcolor=C["fg2"], borderwidth=0, width=8,
        )
        st.configure("Horizontal.TScrollbar",
            background=C["btn_bg"], troughcolor=C["bg"],
            arrowcolor=C["fg2"], borderwidth=0, height=8,
        )
        st.configure("TCombobox",
            background=C["input_bg"], foreground=C["fg"],
            selectbackground=C["selected"],
            fieldbackground=C["input_bg"],
            arrowcolor=C["accent"],
        )
        st.configure("TProgressbar",
            background=C["progress"], troughcolor=C["border"],
        )
        st.configure("TSeparator", background=C["border"])

    # ── Menu bar ─────────────────────────────────────────────────────────────

    def _build_menubar(self):
        mk = {
            "bg": C["panel"], "fg": C["fg"],
            "activebackground": C["selected"], "activeforeground": C["accent2"],
            "relief": "flat",
        }
        mb = tk.Menu(self, **mk)
        self.config(menu=mb)

        fm = tk.Menu(mb, tearoff=0, **mk)
        fm.add_command(label="Upload File(s)…",   command=self._do_upload)
        fm.add_command(label="Download Selected…", command=self._do_download)
        fm.add_separator()
        fm.add_command(label="Exit",               command=self.quit)
        mb.add_cascade(label="File", menu=fm)

        em = tk.Menu(mb, tearoff=0, **mk)
        em.add_command(label="Delete Selected",  command=self._do_delete)
        em.add_command(label="Refresh",          command=self._do_refresh)
        em.add_command(label="Go Up",            command=self._go_up)
        mb.add_cascade(label="Edit", menu=em)

        sm = tk.Menu(mb, tearoff=0, **mk)
        sm.add_command(label="API Credentials…", command=self._open_settings)
        mb.add_cascade(label="Settings", menu=sm)

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self):
        hdr = tk.Frame(self, bg=C["hdr_bg"], height=58)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        # Logo + title
        logo_frm = tk.Frame(hdr, bg=C["hdr_bg"])
        logo_frm.pack(side="left", padx=(18, 0))
        tk.Label(
            logo_frm, text="☁",
            bg=C["hdr_bg"], fg="#ffffff", font=("Segoe UI", 20),
        ).pack(side="left", padx=(0, 6))
        tk.Label(
            logo_frm, text="R2 Manager",
            bg=C["hdr_bg"], fg="#ffffff", font=("Segoe UI", 14, "bold"),
        ).pack(side="left")

        right = tk.Frame(hdr, bg=C["hdr_bg"])
        right.pack(side="right", padx=18)

        tk.Label(right, text="Bucket:", bg=C["hdr_bg"],
                 fg="#d0f0e0", font=FONT).pack(side="left", padx=(0, 6))

        # Combobox with rounded look via Frame border
        cb_wrap = tk.Frame(right, bg="#ffffff", padx=1, pady=1)
        cb_wrap.pack(side="left")
        self._bucket_entry = ttk.Combobox(
            cb_wrap, textvariable=self._current_bucket, width=24, font=FONT,
        )
        self._bucket_entry.pack()
        self._bucket_entry.bind("<Return>",             lambda _: self._do_refresh())
        self._bucket_entry.bind("<<ComboboxSelected>>", lambda _: self._do_refresh())

        tk.Button(
            right, text="⚙", command=self._open_settings,
            bg=C["hdr_bg"], fg="#d0f0e0", font=("Segoe UI", 14),
            relief="flat", cursor="hand2", bd=0,
            activebackground=C["accent2"], activeforeground="#ffffff",
        ).pack(side="left", padx=(12, 0))

    # ── Toolbar ───────────────────────────────────────────────────────────────

    def _build_toolbar(self):
        bar = tk.Frame(self, bg=C["toolbar"], height=48)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        # Bottom border
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x")

        def _tb_btn(text, cmd, bg_color, fg_color):
            """Pill-style toolbar button."""
            f = tk.Frame(bar, bg=bg_color, padx=0, pady=0)
            b = tk.Button(
                f, text=text, command=cmd,
                bg=bg_color, fg=fg_color, font=FONT_B,
                relief="flat", cursor="hand2", padx=14, pady=4, bd=0,
                activebackground=C["btn_hover"], activeforeground=fg_color,
            )
            b.pack()
            return f

        items = [
            ("⬆  上传",  self._do_upload,   "#2e9e6a", "#ffffff"),
            ("⬇  下载",  self._do_download, "#27ae60", "#ffffff"),
            ("🗑  删除",  self._do_delete,   "#e05c5c", "#ffffff"),
            None,
            ("📁  新建文件夹", self._do_mkdir,  C["btn_bg"],  C["fg"]),
            ("↑  返回上级", self._go_up,       C["btn_bg"],  C["fg"]),
            ("🔄  刷新",   self._do_refresh,  C["btn_bg"],  C["fg"]),
        ]
        for item in items:
            if item is None:
                tk.Frame(bar, bg=C["border"], width=1).pack(
                    side="left", fill="y", padx=6, pady=10)
                continue
            text, cmd, bg, fg = item
            _tb_btn(text, cmd, bg, fg).pack(side="left", padx=4, pady=8)

        # Progress bar (hidden by default)
        self._progress_var = tk.DoubleVar()
        prog_wrap = tk.Frame(bar, bg=C["toolbar"])
        prog_wrap.pack(side="right", padx=14)
        self._progress = ttk.Progressbar(
            prog_wrap, variable=self._progress_var, maximum=100,
            style="TProgressbar", length=160,
        )
        self._progress.pack(pady=14)
        self._progress.pack_forget()

    # ── Body ──────────────────────────────────────────────────────────────────

    def _build_body(self):
        body = tk.Frame(self, bg=C["bg"])
        body.pack(fill="both", expand=True)

        # ── Left panel: folder tree ──────────────────────────────────────────
        left = tk.Frame(body, bg=C["sidebar"], width=210)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        # Folder panel header
        folder_hdr = tk.Frame(left, bg=C["accent"], height=30)
        folder_hdr.pack(fill="x")
        folder_hdr.pack_propagate(False)
        tk.Label(
            folder_hdr, text="  📂 目录", bg=C["accent"], fg="#ffffff",
            font=FONT_B, anchor="w",
        ).pack(fill="x", padx=8, pady=4)

        tree_frame = tk.Frame(left, bg=C["sidebar"])
        tree_frame.pack(fill="both", expand=True, padx=4, pady=(4, 6))

        self._folder_tree = ttk.Treeview(tree_frame, show="tree", selectmode="browse")
        fsb = ttk.Scrollbar(tree_frame, orient="vertical",
                            command=self._folder_tree.yview)
        self._folder_tree.configure(yscrollcommand=fsb.set)
        fsb.pack(side="right", fill="y")
        self._folder_tree.pack(fill="both", expand=True)
        self._folder_tree.bind("<<TreeviewSelect>>", self._on_folder_select)

        # ── Separator ────────────────────────────────────────────────────────
        tk.Frame(body, bg=C["border"], width=1).pack(side="left", fill="y")

        # ── Right panel: file list ────────────────────────────────────────────
        right = tk.Frame(body, bg=C["panel"])
        right.pack(side="left", fill="both", expand=True)

        # Breadcrumb path bar
        crumb = tk.Frame(right, bg=C["sidebar"])
        crumb.pack(fill="x")
        tk.Label(
            crumb, text="📍", bg=C["sidebar"], fg=C["accent"], font=FONT,
        ).pack(side="left", padx=(12, 2), pady=5)
        self._path_var = tk.StringVar(value="/")
        tk.Label(
            crumb, textvariable=self._path_var,
            bg=C["sidebar"], fg=C["accent2"], font=("Segoe UI", 9, "bold"), anchor="w",
        ).pack(side="left", pady=5)
        tk.Frame(right, bg=C["border"], height=1).pack(fill="x")

        # File treeview
        cols = ("name", "size", "type", "modified")
        self._file_list = ttk.Treeview(
            right, columns=cols, show="headings", selectmode="extended",
        )
        for col, heading, width, anchor in [
            ("name",     "  文件名",       360, "w"),
            ("size",     "大小",            90, "e"),
            ("type",     "类型",            70, "center"),
            ("modified", "修改时间",        190, "w"),
        ]:
            self._file_list.heading(
                col, text=heading, anchor=anchor,
                command=lambda c=col: self._toggle_sort(c),
            )
            self._file_list.column(col, width=width, minwidth=50, anchor=anchor)

        vsb = ttk.Scrollbar(right, orient="vertical",   command=self._file_list.yview)
        hsb = ttk.Scrollbar(right, orient="horizontal", command=self._file_list.xview)
        self._file_list.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right",  fill="y")
        hsb.pack(side="bottom", fill="x")
        self._file_list.pack(fill="both", expand=True)

        self._file_list.tag_configure("folder", foreground=C["yellow"], font=FONT_B)
        self._file_list.tag_configure("even",   background=C["panel"])
        self._file_list.tag_configure("odd",    background=C["row_alt"])

        self._file_list.bind("<Double-1>",  self._on_file_double_click)
        self._file_list.bind("<Button-3>",  self._show_context_menu)
        self._file_list.bind("<Delete>",    lambda _: self._do_delete())
        self._file_list.bind("<BackSpace>", lambda _: self._go_up())

        # Context menu
        mk = {
            "bg": C["panel"], "fg": C["fg"],
            "activebackground": C["selected"], "activeforeground": C["accent2"],
            "relief": "flat",
        }
        self._ctx = tk.Menu(self, tearoff=0, **mk)
        self._ctx.add_command(label="⬇  Download",      command=self._do_download)
        self._ctx.add_command(label="🗑  Delete",        command=self._do_delete)
        self._ctx.add_separator()
        self._ctx.add_command(label="📋  Copy Full Key", command=self._copy_key)

    # ── Status bar ────────────────────────────────────────────────────────────

    def _build_statusbar(self):
        tk.Frame(self, bg=C["border"], height=1).pack(fill="x", side="bottom")
        bar = tk.Frame(self, bg=C["sidebar"], height=28)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        # Green left indicator strip
        tk.Frame(bar, bg=C["accent"], width=4).pack(side="left", fill="y")
        tk.Label(
            bar, textvariable=self._status_text,
            bg=C["sidebar"], fg=C["fg2"], font=FONT_S, anchor="w",
        ).pack(side="left", padx=10)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _mk_icon_btn(parent, text, cmd, fg=None, font=None, **kw) -> tk.Button:
        return tk.Button(
            parent, text=text, command=cmd,
            bg=C["hdr_bg"], fg=fg or C["fg2"],
            font=font or FONT,
            relief="flat", cursor="hand2", bd=0,
            activebackground=C["accent2"],
            activeforeground="#ffffff",
            **kw,
        )

    def _set_status(self, msg: str):
        self._status_text.set(msg)

    def _show_progress(self, show: bool):
        if show:
            self._progress.pack(side="right", padx=14, pady=12)
        else:
            self._progress.pack_forget()

    # ── Connection ────────────────────────────────────────────────────────────

    def _auto_connect(self):
        if has_credentials():
            c = load_credentials()
            self._connect(c["R2_ACCESS_KEY"], c["R2_SECRET_KEY"], c["R2_ENDPOINT"])
        else:
            SetupDialog(self, self._connect)

    def _connect(self, ak: str, sk: str, ep: str):
        self._r2 = R2Manager(ak, sk, ep)
        host = ep.split("//", 1)[-1].split("/")[0]
        self._set_status(f"✓ Connected  –  {host}")
        # Restore last-used bucket if none already set in the UI
        if not self._current_bucket.get().strip():
            saved = load_last_bucket()
            if saved:
                self._current_bucket.set(saved)
        if self._current_bucket.get().strip():
            self._do_refresh()

    def _open_settings(self):
        SetupDialog(self, self._connect)

    # ── Folder tree population ────────────────────────────────────────────────

    def _populate_folder_tree(self):
        self._folder_tree.delete(*self._folder_tree.get_children())
        root_id = self._folder_tree.insert(
            "", "end", text="📁  /", iid="__root__", open=True
        )
        # Collect unique top-level folder names
        top_folders = sorted({
            f["key"].split("/")[0]
            for f in self._all_files
            if "/" in f["key"]
        })
        for folder in top_folders:
            self._folder_tree.insert(
                root_id, "end", text=f"📁  {folder}", iid=f"__tl__{folder}"
            )

    def _on_folder_select(self, _event=None):
        sel = self._folder_tree.selection()
        if not sel:
            return
        iid = sel[0]
        if iid == "__root__":
            self._current_prefix = ""
        elif iid.startswith("__tl__"):
            self._current_prefix = iid[len("__tl__"):] + "/"
        self._path_var.set("/" + self._current_prefix)
        self._populate_file_list()

    # ── File list population ──────────────────────────────────────────────────

    def _populate_file_list(self):
        self._file_list.delete(*self._file_list.get_children())
        prefix   = self._current_prefix
        sub_dirs = set()
        root_keys = []

        for f in self._all_files:
            key = f["key"]
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            if "/" in rest:
                sub_dirs.add(rest.split("/")[0])
            else:
                root_keys.append(f)

        row = 0
        # Sub-folders first
        for sub in sorted(sub_dirs):
            tag = ("folder", "even" if row % 2 == 0 else "odd")
            self._file_list.insert(
                "", "end",
                values=(f"📁   {sub}/", "—", "Folder", "—"),
                iid=f"__dir__{prefix}{sub}",
                tags=tag,
            )
            row += 1

        # Files
        for f in sorted(root_keys, key=lambda x: x["key"]):
            key  = f["key"]
            name = key.split("/")[-1]
            ext  = name.rsplit(".", 1)[-1].lower() if "." in name else "—"
            size = _fmt_size(f["size"])
            mtime = f["last_modified"][:19].replace("T", " ") if f["last_modified"] else "—"
            icon  = _file_icon(ext)
            tag   = ("even" if row % 2 == 0 else "odd",)
            self._file_list.insert(
                "", "end",
                values=(f"{icon}   {name}", size, ext, mtime),
                iid=key,
                tags=tag,
            )
            row += 1

        total = len(root_keys) + len(sub_dirs)
        bucket = self._current_bucket.get()
        total_size = _fmt_size(sum(f["size"] for f in self._all_files))
        self._set_status(
            f"Bucket: {bucket}  |  {len(self._all_files)} objects  {total_size}  "
            f"|  {total} items in  /{prefix}"
        )
        self._path_var.set("/" + prefix)

    def _on_file_double_click(self, _event=None):
        sel = self._file_list.selection()
        if not sel:
            return
        iid = sel[0]
        if iid.startswith("__dir__"):
            # Navigate into sub-directory
            new_prefix = iid[len("__dir__"):] + "/"
            self._current_prefix = new_prefix
            self._path_var.set("/" + new_prefix)
            self._populate_file_list()
        else:
            self._do_download()

    def _go_up(self):
        """Navigate to parent folder."""
        if not self._current_prefix:
            return
        parts = self._current_prefix.rstrip("/").split("/")
        self._current_prefix = "/".join(parts[:-1])
        if self._current_prefix:
            self._current_prefix += "/"
        self._path_var.set("/" + self._current_prefix)
        self._populate_file_list()
        # Update folder tree selection
        if not self._current_prefix:
            try:
                self._folder_tree.selection_set("__root__")
            except Exception:
                pass

    def _show_context_menu(self, event):
        iid = self._file_list.identify_row(event.y)
        if iid:
            self._file_list.selection_set(iid)
            self._ctx.post(event.x_root, event.y_root)

    def _copy_key(self):
        sel = self._file_list.selection()
        if not sel:
            return
        key = sel[0]
        if not key.startswith("__dir__"):
            self.clipboard_clear()
            self.clipboard_append(key)
            self._set_status(f"Copied key: {key}")

    # ── Column sort ───────────────────────────────────────────────────────────

    def _toggle_sort(self, col: str):
        rev = self._sort_reverse.get(col, False)
        items = [
            (self._file_list.set(k, col), k)
            for k in self._file_list.get_children("")
        ]
        # Folders always on top
        folders = [(v, k) for v, k in items if k.startswith("__dir__")]
        files   = [(v, k) for v, k in items if not k.startswith("__dir__")]
        files.sort(key=lambda x: x[0].lower(), reverse=rev)
        for idx, (_, k) in enumerate(folders + files):
            self._file_list.move(k, "", idx)
        self._sort_reverse[col] = not rev

    # ── Guard helpers ────────────────────────────────────────────────────────

    def _need_connection(self) -> bool:
        if not self._r2:
            messagebox.showwarning("Not Connected",
                                   "Please configure R2 credentials via Settings.", parent=self)
            return False
        return True

    def _need_bucket(self) -> bool:
        if not self._current_bucket.get().strip():
            messagebox.showwarning("No Bucket",
                                   "Enter a bucket name in the header bar and press Enter.", parent=self)
            return False
        return True

    def _selected_file_keys(self) -> list[str]:
        """Return selected file keys, excluding folder rows."""
        return [
            iid for iid in self._file_list.selection()
            if not iid.startswith("__dir__")
        ]

    def _selected_delete_targets(self) -> tuple[list[str], list[str]]:
        """Return (file_keys, folder_prefixes) for the current selection."""
        file_keys, folder_prefixes = [], []
        for iid in self._file_list.selection():
            if iid.startswith("__dir__"):
                folder_prefixes.append(iid[len("__dir__"):] + "/")
            else:
                file_keys.append(iid)
        return file_keys, folder_prefixes

    # ── Operations ───────────────────────────────────────────────────────────

    def _do_refresh(self):
        if not self._need_connection() or not self._need_bucket():
            return
        bucket = self._current_bucket.get().strip()
        self._set_status(f"Loading {bucket}…")
        self._show_progress(True)
        self._progress_var.set(0)

        def _run():
            try:
                files = self._r2.list_all_files(bucket)
                self.after(0, lambda: self._on_refresh_done(files, bucket))
            except Exception as exc:
                self.after(0, lambda e=exc: self._on_error("Refresh Error", e))

        threading.Thread(target=_run, daemon=True).start()

    def _on_refresh_done(self, files: list, bucket: str):
        save_last_bucket(bucket)   # persist on successful load
        self._all_files = files
        self._populate_folder_tree()
        self._populate_file_list()
        self._show_progress(False)

    def _do_upload(self):
        if not self._need_connection() or not self._need_bucket():
            return
        paths = filedialog.askopenfilenames(
            parent=self, title="Select files to upload"
        )
        if not paths:
            return
        bucket = self._current_bucket.get().strip()
        prefix = self._current_prefix
        total  = len(paths)
        self._show_progress(True)

        def _run():
            ok = fail = 0
            for i, local_path in enumerate(paths, 1):
                fname  = Path(local_path).name
                r2_key = prefix + fname
                self.after(0, lambda k=r2_key: self._set_status(f"Uploading {k}…"))
                self.after(0, lambda v=i/total*100: self._progress_var.set(v))
                try:
                    self._r2.upload_file(bucket, local_path, r2_key)
                    ok += 1
                except Exception as exc:
                    fail += 1
                    self.after(0, lambda e=exc: messagebox.showerror(
                        "Upload Error", str(e), parent=self))
            self.after(0, lambda: self._set_status(
                f"Upload complete – {ok} succeeded, {fail} failed"))
            self.after(0, lambda: self._show_progress(False))
            self.after(0, self._do_refresh)

        threading.Thread(target=_run, daemon=True).start()

    def _do_download(self):
        if not self._need_connection():
            return
        keys = self._selected_file_keys()
        if not keys:
            messagebox.showinfo("No Selection",
                                "Select one or more files to download.", parent=self)
            return
        dest_dir = filedialog.askdirectory(parent=self, title="Choose download folder")
        if not dest_dir:
            return
        bucket = self._current_bucket.get().strip()
        total  = len(keys)
        self._show_progress(True)

        def _run():
            ok = fail = 0
            for i, key in enumerate(keys, 1):
                fname = key.split("/")[-1]
                dest  = str(Path(dest_dir) / fname)
                self.after(0, lambda k=key: self._set_status(f"Downloading {k}…"))
                self.after(0, lambda v=i/total*100: self._progress_var.set(v))
                try:
                    self._r2.download_file(bucket, key, dest)
                    ok += 1
                except Exception as exc:
                    fail += 1
                    self.after(0, lambda e=exc: messagebox.showerror(
                        "Download Error", str(e), parent=self))
            self.after(0, lambda: self._set_status(
                f"Download complete – {ok} succeeded, {fail} failed"))
            self.after(0, lambda: self._show_progress(False))

        threading.Thread(target=_run, daemon=True).start()

    def _do_mkdir(self):
        if not self._need_connection() or not self._need_bucket():
            return

        # Modal input dialog
        dlg = tk.Toplevel(self)
        dlg.title("新建文件夹")
        dlg.resizable(False, False)
        dlg.configure(bg=C["bg"])
        dlg.grab_set()
        dlg.update_idletasks()
        pw = self.winfo_rootx() + self.winfo_width()  // 2
        ph = self.winfo_rooty() + self.winfo_height() // 2
        dlg.geometry(f"+{pw - 160}+{ph - 80}")

        tk.Frame(dlg, bg=C["accent"], height=4).pack(fill="x")
        tk.Label(dlg, text="📁  新建文件夹",
                 bg=C["bg"], fg=C["accent2"], font=FONT_B).pack(pady=(14, 6))
        tk.Label(dlg, text="文件夹名称：",
                 bg=C["bg"], fg=C["fg"], font=FONT, anchor="w").pack(padx=24, fill="x")

        name_var = tk.StringVar()
        ent = tk.Entry(
            dlg, textvariable=name_var,
            bg=C["input_bg"], fg=C["fg"],
            insertbackground=C["fg"],
            relief="flat", font=FONT,
            highlightthickness=1,
            highlightcolor=C["accent"],
            highlightbackground=C["border"],
        )
        ent.pack(padx=24, pady=(4, 0), fill="x", ipady=6)
        ent.focus_set()

        def _confirm():
            raw = name_var.get().strip().strip("/")
            if not raw:
                messagebox.showwarning("提示", "请输入文件夹名称。", parent=dlg)
                return
            if any(ch in raw for ch in ('\\', '?', '*', ':', '"', '<', '>', '|')):
                messagebox.showwarning("非法字符", "文件夹名称包含非法字符。", parent=dlg)
                return
            dlg.destroy()
            bucket  = self._current_bucket.get().strip()
            key     = self._current_prefix + raw + "/"
            self._set_status(f"创建文件夹 {key}…")
            def _run():
                try:
                    self._r2.create_folder(bucket, key)
                    self.after(0, lambda: self._set_status(f"文件夹 '{raw}' 创建成功"))
                    self.after(0, self._do_refresh)
                except Exception as exc:
                    self.after(0, lambda e=exc: self._on_error("创建失败", e))
            threading.Thread(target=_run, daemon=True).start()

        ent.bind("<Return>", lambda _: _confirm())
        btn_row = tk.Frame(dlg, bg=C["bg"])
        btn_row.pack(pady=16)
        tk.Button(btn_row, text="  确定  ", command=_confirm,
                  bg=C["accent"], fg="#ffffff", font=FONT_B,
                  relief="flat", cursor="hand2", padx=12, pady=6, bd=0,
                  activebackground=C["accent2"], activeforeground="#ffffff",
                  ).pack(side="left", padx=8)
        tk.Button(btn_row, text="  取消  ", command=dlg.destroy,
                  bg=C["btn_bg"], fg=C["fg2"], font=FONT,
                  relief="flat", cursor="hand2", padx=12, pady=6, bd=0,
                  ).pack(side="left", padx=8)
        self.wait_window(dlg)

    def _do_delete(self):
        if not self._need_connection():
            return
        file_keys, folder_prefixes = self._selected_delete_targets()
        if not file_keys and not folder_prefixes:
            messagebox.showinfo("No Selection",
                                "Select one or more files or folders to delete.", parent=self)
            return

        # Expand each selected folder into the full set of object keys it
        # contains (including its own placeholder object, if any) so the
        # whole subtree is removed, not just the folder row.
        all_keys = set(file_keys)
        for prefix in folder_prefixes:
            all_keys.add(prefix)
            for f in self._all_files:
                if f["key"].startswith(prefix):
                    all_keys.add(f["key"])
        keys = sorted(all_keys)
        if not keys:
            return

        names = [k.split("/")[-1] for k in file_keys]
        names += [p.rstrip("/").split("/")[-1] + "/" for p in folder_prefixes]
        preview = "\n".join(names[:6])
        if len(names) > 6:
            preview += f"\n… and {len(names) - 6} more"
        folder_note = ""
        if folder_prefixes:
            folder_note = (
                f"\n\nThis includes {len(folder_prefixes)} folder(s), "
                f"totalling {len(keys)} object(s) to be removed."
            )

        if not messagebox.askyesno(
            "Confirm Delete",
            f"Permanently delete {len(names)} item(s)?\n\n{preview}{folder_note}\n\n"
            "This action cannot be undone.",
            parent=self,
        ):
            return
        bucket = self._current_bucket.get().strip()
        total  = len(keys)
        self._show_progress(True)

        def _run():
            ok = fail = 0
            for i, key in enumerate(keys, 1):
                self.after(0, lambda k=key: self._set_status(f"Deleting {k}…"))
                self.after(0, lambda v=i/total*100: self._progress_var.set(v))
                try:
                    self._r2.delete_file(bucket, key)
                    ok += 1
                except FileNotFoundError:
                    # Implicit folder had no placeholder object – nothing to do.
                    ok += 1
                except Exception as exc:
                    fail += 1
                    self.after(0, lambda e=exc: messagebox.showerror(
                        "Delete Error", str(e), parent=self))
            self.after(0, lambda: self._set_status(
                f"Deleted {ok} object(s), {fail} failed"))
            self.after(0, lambda: self._show_progress(False))
            self.after(0, self._do_refresh)

        threading.Thread(target=_run, daemon=True).start()

    def _on_error(self, title: str, exc: Exception):
        self._show_progress(False)
        self._set_status(f"Error: {exc}")
        messagebox.showerror(title, str(exc), parent=self)


# ─── Utilities ────────────────────────────────────────────────────────────────

def _fmt_size(n: int) -> str:
    if n < 1_024:            return f"{n} B"
    if n < 1_024 ** 2:       return f"{n / 1_024:.1f} KB"
    if n < 1_024 ** 3:       return f"{n / 1_024**2:.1f} MB"
    return                          f"{n / 1_024**3:.2f} GB"


def _file_icon(ext: str) -> str:
    images = {"jpg", "jpeg", "png", "gif", "bmp", "svg", "webp", "ico", "tiff", "avif"}
    videos = {"mp4", "mkv", "mov", "avi", "wmv", "flv", "webm", "m4v"}
    audio  = {"mp3", "wav", "flac", "ogg", "aac", "m4a", "wma", "opus"}
    docs   = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods"}
    code   = {"py", "js", "ts", "html", "htm", "css", "json", "yaml", "yml",
              "xml", "sh", "bat", "ps1", "go", "rs", "java", "cpp", "c", "h",
              "rb", "php", "swift", "kt", "toml", "ini", "cfg", "env"}
    archives = {"zip", "tar", "gz", "bz2", "xz", "7z", "rar"}
    if ext in images:   return "🖼 "
    if ext in videos:   return "🎬"
    if ext in audio:    return "🎵"
    if ext in docs:     return "📄"
    if ext in code:     return "📝"
    if ext in archives: return "📦"
    return "📄"


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = R2ManagerApp()
    app.mainloop()
