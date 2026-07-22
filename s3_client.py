#!/usr/bin/env python3
"""
S3 Client – A bilingual (中文/English) GUI client for S3-compatible object
storage: Cloudflare R2 and generic S3-compatible services (AWS S3, Backblaze
B2, MinIO, etc). Based on the r2client library: https://github.com/fayharinn/R2-Client

Multiple bucket credentials, across multiple platforms, can be configured on
the credentials page; each is auto-probed for validity and its accessible
buckets right after being added. Outside that page, every bucket from every
valid credential is fused into a single file view, and uploads are routed to
whichever bucket currently holds the least data.

Credentials are stored ONLY as Windows user environment variables (registry),
never written to disk files. The Access Key ID and Secret Access Key are
additionally encrypted at rest with Windows DPAPI.

UI language is auto-detected from the OS (Chinese Windows -> Chinese UI,
anything else -> English UI), and can be toggled at runtime from the button
in the bottom-right corner of the status bar; the manual choice is then
remembered for future launches.
"""

import os
import sys
import json
import uuid
import hmac
import time
import base64
import locale
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
        print(f"[s3-client] installing {pkg}…")
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


if _WINDOWS:
    import ctypes as _ctypes

    class _DATA_BLOB(_ctypes.Structure):
        _fields_ = [("cbData", _ctypes.c_ulong),
                    ("pbData", _ctypes.POINTER(_ctypes.c_char))]


def _dpapi_protect(plaintext: str) -> str:
    """Encrypt a string with Windows DPAPI, scoped to the current user account.

    Falls back to returning the plaintext unchanged if DPAPI is unavailable
    (non-Windows) or the call fails, so a credential is never silently lost.
    """
    if not _WINDOWS or not plaintext:
        return plaintext
    import ctypes
    data = plaintext.encode("utf-8")
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in  = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    try:
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        )
        if not ok:
            return plaintext
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return plaintext
    return "dpapi:" + base64.b64encode(raw).decode("ascii")


def _dpapi_unprotect(value: str) -> str:
    """Decrypt a value produced by _dpapi_protect(); passes plain values through
    unchanged (so credentials saved before encryption was added keep working)."""
    if not _WINDOWS or not value or not value.startswith("dpapi:"):
        return value
    import ctypes
    try:
        raw = base64.b64decode(value[len("dpapi:"):])
    except Exception:
        return value
    buf = ctypes.create_string_buffer(raw, len(raw))
    blob_in  = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    try:
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        )
        if not ok:
            return ""
        text = ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-8")
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return ""
    return text


def load_credentials() -> dict:
    """Load the legacy single-credential values (pre-multi-bucket) from the
    current process env or the registry. Used only to migrate old setups."""
    result = {}
    for name in ("R2_ACCESS_KEY", "R2_SECRET_KEY", "R2_ENDPOINT"):
        val = os.environ.get(name)
        if not val:
            raw = _reg_read(name)
            val = _dpapi_unprotect(raw) if name != "R2_ENDPOINT" else raw
        result[name] = val or ""
        if val:
            os.environ[name] = val
    return result


# ─── UI language: OS auto-detect + persisted manual override ─────────────────

_LANG_REG_NAME = "S3CLIENT_UI_LANG"


def _detect_os_language() -> str:
    """Return 'zh' if the OS UI language is Chinese, else 'en'."""
    name = ""
    try:
        if _WINDOWS:
            import ctypes
            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            name = locale.windows_locale.get(lcid, "")
        else:
            name = locale.getlocale()[0] or os.environ.get("LANG", "")
    except Exception:
        name = ""
    return "zh" if (name or "").lower().startswith("zh") else "en"


def get_ui_language() -> str:
    """Manual override (if the user has toggled the language before) wins;
    otherwise fall back to auto-detecting the OS UI language."""
    override = os.environ.get(_LANG_REG_NAME) or _reg_read(_LANG_REG_NAME)
    if override in ("zh", "en"):
        return override
    return _detect_os_language()


def set_ui_language_override(lang: str) -> None:
    _reg_write(_LANG_REG_NAME, lang)
    os.environ[_LANG_REG_NAME] = lang


# ─── Multi-credential persistence ─────────────────────────────────────────────

_CRED_LIST_ENV = "S3CLIENT_CREDENTIALS_JSON"


def save_credentials_list(entries: list) -> None:
    """Persist the full credential list (DPAPI-encrypting each secret) as a
    single JSON-encoded user environment variable."""
    serializable = [{
        "id":         e["id"],
        "platform":   e["platform"],
        "access_key": _dpapi_protect(e["access_key"]),
        "secret_key": _dpapi_protect(e["secret_key"]),
        "endpoint":   e["endpoint"],
        "region":     e.get("region", "auto"),
    } for e in entries]
    data = json.dumps(serializable, ensure_ascii=False)
    _reg_write(_CRED_LIST_ENV, data)
    os.environ[_CRED_LIST_ENV] = data


def load_credentials_list() -> list:
    """Load the credential list, decrypting secrets. Falls back to migrating
    a legacy single-credential (pre-multi-bucket) setup if no list exists."""
    raw = os.environ.get(_CRED_LIST_ENV) or _reg_read(_CRED_LIST_ENV)
    if raw:
        try:
            stored = json.loads(raw)
        except Exception:
            stored = []
        entries = [{
            "id":         e.get("id") or str(uuid.uuid4()),
            "platform":   e.get("platform", "r2"),
            "access_key": _dpapi_unprotect(e.get("access_key", "")),
            "secret_key": _dpapi_unprotect(e.get("secret_key", "")),
            "endpoint":   e.get("endpoint", ""),
            "region":     e.get("region", "auto"),
        } for e in stored]
        if entries:
            return entries

    legacy = load_credentials()
    if all(legacy.get(k) for k in ("R2_ACCESS_KEY", "R2_SECRET_KEY", "R2_ENDPOINT")):
        entry = {
            "id": str(uuid.uuid4()), "platform": "r2",
            "access_key": legacy["R2_ACCESS_KEY"],
            "secret_key": legacy["R2_SECRET_KEY"],
            "endpoint":   legacy["R2_ENDPOINT"],
            "region":     "auto",
        }
        save_credentials_list([entry])
        return [entry]
    return []


# ─── Translation table ────────────────────────────────────────────────────────

STRINGS = {
    "zh": {
        "app_name": "S3 客户端",

        "field_access_key_label": "访问密钥 ID（Access Key ID）",
        "field_secret_key_label": "机密访问密钥（Secret Access Key）",
        "field_endpoint_label":   "端点地址（Endpoint URL）",
        "field_platform_label":   "平台",
        "field_region_label":     "区域（Region）",
        "platform_r2_label":      "Cloudflare R2",
        "platform_s3_label":      "通用 S3 兼容（AWS S3 / Backblaze B2 / MinIO 等）",
        "btn_cancel":  "  取消  ",
        "btn_confirm": "  确定  ",
        "btn_add_confirm": "  添加  ",
        "btn_done":    "  完成  ",
        "warn_missing_info_title": "缺少信息",
        "warn_missing_info_body":  "请填写全部必填项。",
        "warn_bad_endpoint_title": "端点地址无效",
        "warn_bad_endpoint_body":  "端点地址必须以 http:// 或 https:// 开头。",

        "cred_dialog_title":    "S3 存储桶凭证管理",
        "cred_dialog_subtitle": "凭证以用户环境变量形式保存（密钥经 DPAPI 加密），不会写入任何磁盘文件。添加后将自动探测有效性与可访问的存储桶。",
        "cred_configured_label": "已配置的凭证",
        "cred_col_platform":    "平台",
        "cred_col_access_key":  "访问密钥 ID",
        "cred_col_secret_key":  "机密访问密钥",
        "cred_col_endpoint":    "端点地址",
        "cred_col_status":      "凭证状态",
        "ctx_recheck":          "🔄  重新验证",
        "ctx_delete_cred":      "🗑  删除",
        "confirm_delete_cred_title": "确认删除",
        "confirm_delete_cred_body":  "确定要删除这条凭证吗？\n\n{endpoint}",
        "add_cred_dialog_title": "添加存储桶凭证",

        "status_checking":         "验证中…",
        "cred_status_valid":       "✓ 有效 · {n} 个存储桶",
        "cred_status_valid_empty": "✓ 有效 · 未发现存储桶",
        "cred_status_invalid":     "✗ 无效：{err}",

        "conn_summary":      "🪣 {n_buckets} 个存储桶 · {n_valid}/{n_total} 凭证有效",
        "conn_summary_none": "未连接",

        "status_not_connected": "未连接  –  请在设置中添加存储桶凭证",

        "menu_file":              "文件",
        "menu_upload_files":      "上传文件…",
        "menu_upload_folder":     "上传文件夹…",
        "menu_download_selected": "下载所选项…",
        "menu_exit":              "退出",
        "menu_actions":           "操作",
        "menu_delete_selected":   "删除所选项",
        "menu_refresh":           "刷新",
        "menu_go_up":             "返回上级",
        "menu_settings":          "设置",
        "menu_credentials":       "S3 存储桶凭证…",

        "tb_upload":        "⬆  上传",
        "tb_upload_folder": "⬆📁  上传文件夹",
        "tb_download":      "⬇  下载",
        "tb_delete":        "🗑  删除",
        "tb_mkdir":         "📁  新建文件夹",
        "tb_go_up":         "↑  返回上级",
        "tb_refresh":       "🔄  刷新",

        "folder_panel_title": "  📂 目录",

        "col_name":     "  文件名",
        "col_size":     "大小",
        "col_type":     "类型",
        "col_modified": "修改时间",
        "type_folder":  "文件夹",
        "dash":         "—",

        "ctx_download": "⬇  下载",
        "ctx_delete":   "🗑  删除",
        "ctx_copy_key": "📋  复制完整对象键",

        "warn_no_target_title": "未连接存储桶",
        "warn_no_target_body":  "请在设置中添加至少一个有效的存储桶凭证。",

        "status_probing":  "正在验证 {n} 个凭证…",
        "status_loading_n": "正在加载 {n} 个存储桶…",
        "status_partial_errors": "部分存储桶加载失败：{errors}",

        "dlg_choose_upload_files": "选择要上传的文件",
        "status_uploading":       "正在上传 {key}…",
        "err_upload_title":       "上传失败",
        "status_upload_done_target": "上传完成 – 成功 {ok} 个，失败 {fail} 个（目标存储桶：{bucket}）",

        "dlg_choose_upload_folder":  "选择要上传的文件夹",
        "warn_empty_folder_title":  "空文件夹",
        "warn_empty_folder_body":   "所选文件夹中没有可上传的文件。",
        "status_upload_folder_done_target": "文件夹上传完成 – 成功 {ok} 个，失败 {fail} 个（目标存储桶：{bucket}）",

        "info_no_files_title":     "未选择文件",
        "info_no_files_body":      "请选择一个或多个要下载的文件。",
        "dlg_choose_download_dir": "选择下载文件夹",
        "status_downloading":     "正在下载 {key}…",
        "err_download_title":     "下载失败",
        "status_download_done":   "下载完成 – 成功 {ok} 个，失败 {fail} 个",

        "dlg_mkdir_title":     "新建文件夹",
        "dlg_mkdir_heading":   "📁  新建文件夹",
        "dlg_mkdir_name_label": "文件夹名称：",
        "warn_hint_title":     "提示",
        "warn_need_folder_name": "请输入文件夹名称。",
        "warn_bad_chars_title": "非法字符",
        "warn_bad_chars_body":  "文件夹名称包含非法字符。",
        "status_mkdir_creating_target": "创建文件夹 {key} → {bucket}…",
        "status_mkdir_done":     "文件夹 '{name}' 创建成功",
        "err_mkdir_title":       "创建失败",

        "info_no_items_title": "未选择项目",
        "info_no_items_body":  "请选择一个或多个要删除的文件或文件夹。",
        "delete_more_items":   "\n… 以及另外 {n} 项",
        "delete_folder_note":  "\n\n其中包含 {n} 个文件夹，共将删除 {m} 个对象。",
        "confirm_delete_title": "确认删除",
        "confirm_delete_body":  "确定要永久删除 {n} 项吗？\n\n{preview}{note}\n\n此操作无法撤销。",
        "status_deleting":     "正在删除 {key}…",
        "err_delete_title":    "删除失败",
        "status_delete_done":  "已删除 {ok} 个对象，失败 {fail} 个",

        "status_error":          "错误：{err}",
        "status_bucket_summary": "已融合 {n_buckets} 个存储桶  |  共 {count} 个对象，{size}  |  /{prefix} 中 {n} 项",
        "status_copied_key":     "已复制对象键：{key}",

        "action_list":     "列出对象",
        "action_upload":   "上传",
        "action_download": "下载",
        "action_delete":   "删除",
        "action_mkdir":    "新建文件夹",

        "err_403": (
            "{prefix}403 禁止访问 – {body}\n\n"
            "可能的原因：\n"
            "  • 访问密钥（Access Key）或机密访问密钥（Secret Access Key）不正确\n"
            "  • 端点地址（Endpoint URL）错误（应为：https://<account_id>.r2.cloudflarestorage.com）\n"
            "  • API 令牌缺少 R2 对象读取或写入权限"
        ),
        "err_404":    "{prefix}404 未找到 – {body}",
        "err_generic": "{prefix}HTTP {code} 错误 – {body}",
    },
    "en": {
        "app_name": "S3 Client",

        "field_access_key_label": "Access Key ID",
        "field_secret_key_label": "Secret Access Key",
        "field_endpoint_label":   "Endpoint URL",
        "field_platform_label":   "Platform",
        "field_region_label":     "Region",
        "platform_r2_label":      "Cloudflare R2",
        "platform_s3_label":      "Generic S3-Compatible (AWS S3 / Backblaze B2 / MinIO, etc.)",
        "btn_cancel":  "  Cancel  ",
        "btn_confirm": "  OK  ",
        "btn_add_confirm": "  Add  ",
        "btn_done":    "  Done  ",
        "warn_missing_info_title": "Missing information",
        "warn_missing_info_body":  "Please fill in all required fields.",
        "warn_bad_endpoint_title": "Invalid endpoint",
        "warn_bad_endpoint_body":  "The endpoint URL must start with http:// or https://.",

        "cred_dialog_title":    "S3 Bucket Credentials",
        "cred_dialog_subtitle": "Credentials are stored as user environment variables (keys DPAPI-encrypted) and never written to disk files. Each one is auto-probed for validity and accessible buckets after being added.",
        "cred_configured_label": "Configured credentials",
        "cred_col_platform":    "Platform",
        "cred_col_access_key":  "Access Key ID",
        "cred_col_secret_key":  "Secret Access Key",
        "cred_col_endpoint":    "Endpoint",
        "cred_col_status":      "Status",
        "ctx_recheck":          "🔄  Re-verify",
        "ctx_delete_cred":      "🗑  Delete",
        "confirm_delete_cred_title": "Confirm Delete",
        "confirm_delete_cred_body":  "Delete this credential?\n\n{endpoint}",
        "add_cred_dialog_title": "Add Bucket Credential",

        "status_checking":         "Verifying…",
        "cred_status_valid":       "✓ Valid · {n} bucket(s)",
        "cred_status_valid_empty": "✓ Valid · no buckets found",
        "cred_status_invalid":     "✗ Invalid: {err}",

        "conn_summary":      "🪣 {n_buckets} buckets · {n_valid}/{n_total} credentials valid",
        "conn_summary_none": "Not connected",

        "status_not_connected": "Not connected  –  add bucket credentials in Settings",

        "menu_file":              "File",
        "menu_upload_files":      "Upload File(s)…",
        "menu_upload_folder":     "Upload Folder…",
        "menu_download_selected": "Download Selected…",
        "menu_exit":              "Exit",
        "menu_actions":           "Actions",
        "menu_delete_selected":   "Delete Selected",
        "menu_refresh":           "Refresh",
        "menu_go_up":             "Go Up",
        "menu_settings":          "Settings",
        "menu_credentials":       "S3 Bucket Credentials…",

        "tb_upload":        "⬆  Upload",
        "tb_upload_folder": "⬆📁  Upload Folder",
        "tb_download":      "⬇  Download",
        "tb_delete":        "🗑  Delete",
        "tb_mkdir":         "📁  New Folder",
        "tb_go_up":         "↑  Go Up",
        "tb_refresh":       "🔄  Refresh",

        "folder_panel_title": "  📂 Folders",

        "col_name":     "  Name",
        "col_size":     "Size",
        "col_type":     "Type",
        "col_modified": "Modified",
        "type_folder":  "Folder",
        "dash":         "—",

        "ctx_download": "⬇  Download",
        "ctx_delete":   "🗑  Delete",
        "ctx_copy_key": "📋  Copy Full Object Key",

        "warn_no_target_title": "No bucket connected",
        "warn_no_target_body":  "Add at least one valid bucket credential in Settings.",

        "status_probing":  "Verifying {n} credential(s)…",
        "status_loading_n": "Loading {n} bucket(s)…",
        "status_partial_errors": "Some buckets failed to load: {errors}",

        "dlg_choose_upload_files": "Select files to upload",
        "status_uploading":       "Uploading {key}…",
        "err_upload_title":       "Upload failed",
        "status_upload_done_target": "Upload complete – {ok} succeeded, {fail} failed (target bucket: {bucket})",

        "dlg_choose_upload_folder":  "Select a folder to upload",
        "warn_empty_folder_title":  "Empty folder",
        "warn_empty_folder_body":   "The selected folder has no files to upload.",
        "status_upload_folder_done_target": "Folder upload complete – {ok} succeeded, {fail} failed (target bucket: {bucket})",

        "info_no_files_title":     "No files selected",
        "info_no_files_body":      "Select one or more files to download.",
        "dlg_choose_download_dir": "Select a download folder",
        "status_downloading":     "Downloading {key}…",
        "err_download_title":     "Download failed",
        "status_download_done":   "Download complete – {ok} succeeded, {fail} failed",

        "dlg_mkdir_title":     "New Folder",
        "dlg_mkdir_heading":   "📁  New Folder",
        "dlg_mkdir_name_label": "Folder name:",
        "warn_hint_title":     "Notice",
        "warn_need_folder_name": "Please enter a folder name.",
        "warn_bad_chars_title": "Invalid characters",
        "warn_bad_chars_body":  "The folder name contains invalid characters.",
        "status_mkdir_creating_target": "Creating folder {key} → {bucket}…",
        "status_mkdir_done":     "Folder '{name}' created successfully",
        "err_mkdir_title":       "Creation failed",

        "info_no_items_title": "No items selected",
        "info_no_items_body":  "Select one or more files or folders to delete.",
        "delete_more_items":   "\n… and {n} more",
        "delete_folder_note":  "\n\nIncludes {n} folder(s); {m} objects will be deleted in total.",
        "confirm_delete_title": "Confirm Delete",
        "confirm_delete_body":  "Permanently delete {n} item(s)?\n\n{preview}{note}\n\nThis action cannot be undone.",
        "status_deleting":     "Deleting {key}…",
        "err_delete_title":    "Delete failed",
        "status_delete_done":  "Deleted {ok} object(s), {fail} failed",

        "status_error":          "Error: {err}",
        "status_bucket_summary": "Fused {n_buckets} bucket(s)  |  {count} objects total, {size}  |  {n} items in /{prefix}",
        "status_copied_key":     "Copied object key: {key}",

        "action_list":     "List objects",
        "action_upload":   "Upload",
        "action_download": "Download",
        "action_delete":   "Delete",
        "action_mkdir":    "New folder",

        "err_403": (
            "{prefix}403 Forbidden – {body}\n\n"
            "Possible causes:\n"
            "  • The Access Key ID or Secret Access Key is incorrect\n"
            "  • The Endpoint URL is wrong (should be https://<account_id>.r2.cloudflarestorage.com)\n"
            "  • The API token lacks R2 object read/write permission"
        ),
        "err_404":    "{prefix}404 Not Found – {body}",
        "err_generic": "{prefix}HTTP {code} error – {body}",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    text = STRINGS.get(lang, STRINGS["en"]).get(key, key)
    return text.format(**kwargs) if kwargs else text


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


# ─── S3-compatible backend ─────────────────────────────────────────────────────
class S3Backend:
    """
    Talks to any S3-compatible storage service (Cloudflare R2, AWS S3,
    Backblaze B2, MinIO, ...) via hand-rolled AWS SigV4 signing, and adds a
    delete_file() method the r2client library this project started from
    doesn't provide.
    """

    def __init__(self, access_key: str, secret_key: str, endpoint: str,
                 region: str = "auto", lang: str = "en"):
        self.access_key = access_key
        self.secret_key = secret_key
        self.endpoint   = endpoint.rstrip("/")
        self.region     = region
        self.lang       = lang

    # ── SigV4 helpers ────────────────────────────────────────────────────────

    def _sign(self, key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def _signing_key(self, date_stamp: str) -> bytes:
        k = self._sign(("AWS4" + self.secret_key).encode("utf-8"), date_stamp)
        k = self._sign(k, self.region)   # "auto" for R2; a real region elsewhere
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

    def _auth_headers(self, method: str, bucket: str = "", key: str = "") -> dict:
        """Build minimal AWS SigV4 Authorization headers for the given request.

        An empty bucket targets the service root (e.g. ListBuckets).
        """
        host       = self.endpoint.split("://", 1)[-1]
        now        = datetime.datetime.now(datetime.timezone.utc)
        amz_date   = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        enc_key    = self._encode_key(key)
        if not bucket:
            uri = "/"
        else:
            uri = f"/{bucket}/{enc_key}" if key else f"/{bucket}/"
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
        cred_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
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

    # ── Retry helper ─────────────────────────────────────────────────────────

    def _http(self, method: str, url: str, *, attempts: int = 3,
              base_delay: float = 1.0, **kwargs) -> requests.Response:
        """requests call with a few retries (linear backoff) on transient
        network errors or 5xx responses. 4xx errors are not retried – they're
        deterministic (bad credentials, missing object) and won't heal on
        their own.
        """
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            resp = None
            try:
                resp = requests.request(method, url, **kwargs)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as exc:
                last_exc = exc
            if resp is not None:
                if resp.status_code < 500 or attempt == attempts:
                    return resp
                last_exc = RuntimeError(f"HTTP {resp.status_code}")
                resp.close()
            if attempt < attempts:
                time.sleep(base_delay * attempt)
        raise last_exc

    # ── Error helper ────────────────────────────────────────────────────────

    def _raise_for_status(self, resp: requests.Response, action_key: str = "") -> None:
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
        action = t(self.lang, f"action_{action_key}") if action_key else ""
        prefix = f"[{action}] " if action else ""
        if resp.status_code == 403:
            raise PermissionError(t(self.lang, "err_403", prefix=prefix, body=body))
        if resp.status_code == 404:
            raise FileNotFoundError(t(self.lang, "err_404", prefix=prefix, body=body))
        raise RuntimeError(t(self.lang, "err_generic", prefix=prefix,
                             code=resp.status_code, body=body))

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
        cred_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
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

    def list_buckets(self) -> list:
        """Return the names of all buckets this credential can access."""
        url      = f"{self.endpoint}/"
        headers  = self._auth_headers("GET", "")
        response = self._http("GET", url, headers=headers, timeout=30)
        self._raise_for_status(response, "list")
        ns    = "{http://s3.amazonaws.com/doc/2006-03-01/}"
        root  = ET.fromstring(response.content)
        names = []
        for item in root.findall(f"{ns}Buckets/{ns}Bucket"):
            name = item.findtext(f"{ns}Name")
            if name:
                names.append(name)
        return names

    def list_all_files(self, bucket: str) -> list:
        """Return a flat list of file metadata dicts: {key, size, last_modified}."""
        url      = f"{self.endpoint}/{bucket}/"
        headers  = self._auth_headers("GET", bucket)
        response = self._http("GET", url, headers=headers, timeout=30)
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
        resp    = self._http("PUT", url, headers=headers, data=data, timeout=120)
        self._raise_for_status(resp, "upload")

    def download_file(self, bucket: str, r2_key: str, local_path: str) -> None:
        url     = f"{self.endpoint}/{bucket}/{self._encode_key(r2_key)}"
        headers = self._auth_headers("GET", bucket, r2_key)
        resp    = self._http("GET", url, headers=headers, timeout=120, stream=True)
        self._raise_for_status(resp, "download")
        with open(local_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)

    def delete_file(self, bucket: str, r2_key: str) -> None:
        url     = f"{self.endpoint}/{bucket}/{self._encode_key(r2_key)}"
        headers = self._auth_headers("DELETE", bucket, r2_key)
        resp    = self._http("DELETE", url, headers=headers, timeout=30)
        self._raise_for_status(resp, "delete")

    def create_folder(self, bucket: str, folder_key: str) -> None:
        """Create a virtual folder by uploading a zero-byte placeholder object."""
        if not folder_key.endswith("/"):
            folder_key += "/"
        payload_hash = hashlib.sha256(b"").hexdigest()
        url     = f"{self.endpoint}/{bucket}/{self._encode_key(folder_key)}"
        headers = self._auth_headers_put(bucket, folder_key, payload_hash,
                                         "application/x-directory")
        resp    = self._http("PUT", url, headers=headers, data=b"", timeout=30)
        self._raise_for_status(resp, "mkdir")


# ─── Credential entries ────────────────────────────────────────────────────────

class CredEntry:
    """Runtime state for one configured bucket credential (may span multiple
    buckets once probed, since a single token can grant access to several)."""

    def __init__(self, cred_id: str, platform: str, access_key: str,
                 secret_key: str, endpoint: str, region: str, lang: str = "en"):
        self.id         = cred_id
        self.platform   = platform
        self.access_key = access_key
        self.secret_key = secret_key
        self.endpoint   = endpoint
        self.region     = region
        self.backend    = S3Backend(access_key, secret_key, endpoint, region=region, lang=lang)
        self.buckets: list = []
        self.status     = "checking"
        self.status_msg = t(lang, "status_checking")

    def to_dict(self) -> dict:
        return {
            "id": self.id, "platform": self.platform,
            "access_key": self.access_key, "secret_key": self.secret_key,
            "endpoint": self.endpoint, "region": self.region,
        }


def probe_credential(entry: "CredEntry", lang: str) -> None:
    """Attempt to list buckets reachable by this credential; updates status
    in place so the caller can just re-render after this returns."""
    try:
        buckets = entry.backend.list_buckets()
        entry.buckets = buckets
        entry.status = "valid"
        entry.status_msg = (t(lang, "cred_status_valid", n=len(buckets)) if buckets
                             else t(lang, "cred_status_valid_empty"))
    except Exception as exc:
        entry.buckets = []
        entry.status = "invalid"
        entry.status_msg = t(lang, "cred_status_invalid", err=str(exc)[:80])


def _mask(s: str) -> str:
    if len(s) <= 8:
        return "●" * max(len(s), 4)
    return s[:4] + "…" + s[-4:]


# ─── Add-credential dialog ─────────────────────────────────────────────────────
class AddCredentialDialog(tk.Toplevel):
    """Small modal form for entering one new bucket credential."""

    def __init__(self, parent, lang: str, on_submit):
        super().__init__(parent)
        self.lang      = lang
        self._on_submit = on_submit
        self.title(t(lang, "add_cred_dialog_title"))
        self.resizable(False, False)
        self.configure(bg=C["bg"])
        self.grab_set()
        self._build()
        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width()  // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"+{pw - self.winfo_width()//2}+{ph - self.winfo_height()//2}")
        parent.wait_window(self)

    def _build(self):
        lang = self.lang
        tk.Frame(self, bg=C["accent"], height=4).pack(fill="x")
        tk.Label(self, text=t(lang, "add_cred_dialog_title"),
                 bg=C["bg"], fg=C["accent2"], font=FONT_B).pack(pady=(14, 6))

        frm = tk.Frame(self, bg=C["bg"])
        frm.pack(padx=28, fill="x")

        tk.Label(frm, text=t(lang, "field_platform_label"), bg=C["bg"], fg=C["fg"],
                 font=FONT_B, anchor="w").pack(fill="x", pady=(10, 2))
        self._platform_map = {
            t(lang, "platform_r2_label"): "r2",
            t(lang, "platform_s3_label"): "s3_compatible",
        }
        self._platform_combo = ttk.Combobox(
            frm, values=list(self._platform_map.keys()), state="readonly", font=FONT,
        )
        self._platform_combo.current(0)
        self._platform_combo.pack(fill="x", ipady=4)

        fields = [
            (t(lang, "field_access_key_label"), "ak", False),
            (t(lang, "field_secret_key_label"), "sk", True),
            (t(lang, "field_endpoint_label"),   "ep", False),
        ]
        self._vars: dict[str, tk.StringVar] = {}
        for label, key, secret in fields:
            tk.Label(frm, text=label, bg=C["bg"], fg=C["fg"], font=FONT_B,
                     anchor="w").pack(fill="x", pady=(10, 2))
            var = tk.StringVar()
            ent = tk.Entry(
                frm, textvariable=var, show="●" if secret else "",
                bg=C["input_bg"], fg=C["fg"], insertbackground=C["fg"],
                relief="flat", font=FONT, highlightthickness=1,
                highlightcolor=C["accent"], highlightbackground=C["border"],
            )
            ent.pack(fill="x", ipady=7)
            self._vars[key] = var

        tk.Label(frm, text=t(lang, "field_region_label"), bg=C["bg"], fg=C["fg"],
                 font=FONT_B, anchor="w").pack(fill="x", pady=(10, 2))
        self._region_var = tk.StringVar(value="auto")
        self._region_entry = tk.Entry(
            frm, textvariable=self._region_var,
            bg=C["input_bg"], fg=C["fg"], insertbackground=C["fg"],
            relief="flat", font=FONT, highlightthickness=1,
            highlightcolor=C["accent"], highlightbackground=C["border"],
            state="disabled",
        )
        self._region_entry.pack(fill="x", ipady=7)

        def _on_platform_change(_evt=None):
            platform = self._platform_map[self._platform_combo.get()]
            if platform == "r2":
                self._region_var.set("auto")
                self._region_entry.configure(state="disabled")
            else:
                if self._region_var.get() in ("", "auto"):
                    self._region_var.set("us-east-1")
                self._region_entry.configure(state="normal")
        self._platform_combo.bind("<<ComboboxSelected>>", _on_platform_change)

        btn_row = tk.Frame(self, bg=C["bg"])
        btn_row.pack(pady=20)
        tk.Button(btn_row, text=t(lang, "btn_add_confirm"), command=self._submit,
                  bg=C["accent"], fg="#ffffff", font=FONT_B,
                  relief="flat", cursor="hand2", padx=14, pady=7, bd=0,
                  activebackground=C["accent2"], activeforeground="#ffffff",
                  ).pack(side="left", padx=8)
        tk.Button(btn_row, text=t(lang, "btn_cancel"), command=self.destroy,
                  bg=C["btn_bg"], fg=C["fg2"], font=FONT,
                  relief="flat", cursor="hand2", padx=14, pady=7, bd=0,
                  ).pack(side="left", padx=8)

    def _submit(self):
        lang = self.lang
        platform = self._platform_map[self._platform_combo.get()]
        ak = self._vars["ak"].get().strip()
        sk = self._vars["sk"].get().strip()
        ep = self._vars["ep"].get().strip()
        region = self._region_var.get().strip() or ("auto" if platform == "r2" else "us-east-1")
        if not (ak and sk and ep):
            messagebox.showwarning(t(lang, "warn_missing_info_title"),
                                   t(lang, "warn_missing_info_body"), parent=self)
            return
        if not ep.startswith("http"):
            messagebox.showwarning(t(lang, "warn_bad_endpoint_title"),
                                   t(lang, "warn_bad_endpoint_body"), parent=self)
            return
        self.destroy()
        self._on_submit(platform, ak, sk, ep, region)


# ─── Credentials management dialog ─────────────────────────────────────────────
class CredentialsDialog(tk.Toplevel):
    """Modal dialog listing every configured bucket credential as a table,
    with a '+' button to add more and auto-probing after each addition."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self._app = app
        self.lang = app.lang
        self.title(t(self.lang, "cred_dialog_title"))
        self.resizable(False, False)
        self.configure(bg=C["bg"])
        self.grab_set()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.update_idletasks()
        pw = parent.winfo_rootx() + parent.winfo_width()  // 2
        ph = parent.winfo_rooty() + parent.winfo_height() // 2
        self.geometry(f"+{pw - self.winfo_width()//2}+{ph - self.winfo_height()//2}")
        parent.wait_window(self)

    def _build(self):
        lang = self.lang
        top = tk.Frame(self, bg=C["accent"], height=56)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(
            top, text="☁  " + t(lang, "cred_dialog_title"),
            bg=C["accent"], fg="#ffffff", font=("Segoe UI", 14, "bold"),
        ).pack(pady=14)

        tk.Label(
            self, text=t(lang, "cred_dialog_subtitle"),
            bg=C["bg"], fg=C["fg2"], font=FONT_S, justify="center", wraplength=580,
        ).pack(pady=(14, 4))

        hdr_row = tk.Frame(self, bg=C["bg"])
        hdr_row.pack(fill="x", padx=24, pady=(10, 4))
        tk.Label(hdr_row, text=t(lang, "cred_configured_label"),
                 bg=C["bg"], fg=C["fg"], font=FONT_B).pack(side="left")
        tk.Button(
            hdr_row, text=" + ", command=self._add_credential,
            bg=C["accent"], fg="#ffffff", font=FONT_B,
            relief="flat", cursor="hand2", padx=10, pady=2, bd=0,
            activebackground=C["accent2"], activeforeground="#ffffff",
        ).pack(side="right")

        table_frame = tk.Frame(self, bg=C["bg"])
        table_frame.pack(padx=24, fill="both", expand=True)

        cols = ("platform", "access_key", "secret_key", "endpoint", "status")
        self._tree = ttk.Treeview(
            table_frame, columns=cols, show="headings", selectmode="browse", height=6,
        )
        for col, heading, width in [
            ("platform",   t(lang, "cred_col_platform"),   90),
            ("access_key", t(lang, "cred_col_access_key"), 130),
            ("secret_key", t(lang, "cred_col_secret_key"), 110),
            ("endpoint",   t(lang, "cred_col_endpoint"),   210),
            ("status",     t(lang, "cred_col_status"),     170),
        ]:
            self._tree.heading(col, text=heading)
            self._tree.column(col, width=width, minwidth=60, anchor="w")
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._tree.pack(fill="both", expand=True, pady=(4, 8))
        self._tree.bind("<Button-3>", self._show_row_menu)
        self._tree.bind("<Delete>", lambda _: self._delete_selected())

        mk = {
            "bg": C["panel"], "fg": C["fg"],
            "activebackground": C["selected"], "activeforeground": C["accent2"],
            "relief": "flat",
        }
        self._ctx = tk.Menu(self, tearoff=0, **mk)
        self._ctx.add_command(label=t(lang, "ctx_recheck"), command=self._recheck_selected)
        self._ctx.add_command(label=t(lang, "ctx_delete_cred"), command=self._delete_selected)

        btn_frame = tk.Frame(self, bg=C["bg"])
        btn_frame.pack(pady=(4, 20))
        tk.Button(
            btn_frame, text=t(lang, "btn_done"), command=self.destroy,
            bg=C["accent"], fg="#ffffff", font=FONT_B,
            relief="flat", cursor="hand2", padx=16, pady=8, bd=0,
            activebackground=C["accent2"], activeforeground="#ffffff",
        ).pack()

        self._refresh_table()

    def _platform_label(self, platform: str) -> str:
        return t(self.lang, "platform_r2_label") if platform == "r2" else t(self.lang, "platform_s3_label")

    def _refresh_table(self):
        self._tree.delete(*self._tree.get_children())
        for cred in self._app.creds:
            self._tree.insert(
                "", "end", iid=cred.id,
                values=(self._platform_label(cred.platform), _mask(cred.access_key),
                        "●" * 8, cred.endpoint, cred.status_msg),
            )

    def _add_credential(self):
        AddCredentialDialog(self, self.lang, self._on_new_credential)

    def _on_new_credential(self, platform, ak, sk, ep, region):
        entry = CredEntry(str(uuid.uuid4()), platform, ak, sk, ep, region, lang=self._app.lang)
        self._app.creds.append(entry)
        self._app.persist_credentials()
        self._refresh_table()
        self._probe(entry)

    def _probe(self, entry: CredEntry):
        entry.status = "checking"
        entry.status_msg = t(self.lang, "status_checking")
        self._refresh_table()

        def _run():
            probe_credential(entry, self.lang)
            self.after(0, self._on_probe_done)

        threading.Thread(target=_run, daemon=True).start()

    def _on_probe_done(self):
        try:
            self._refresh_table()
        except tk.TclError:
            pass  # dialog already closed
        self._app.on_credentials_changed()

    def _selected_entry(self):
        sel = self._tree.selection()
        if not sel:
            return None
        cred_id = sel[0]
        for c in self._app.creds:
            if c.id == cred_id:
                return c
        return None

    def _show_row_menu(self, event):
        iid = self._tree.identify_row(event.y)
        if iid:
            self._tree.selection_set(iid)
            self._ctx.post(event.x_root, event.y_root)

    def _recheck_selected(self):
        entry = self._selected_entry()
        if entry:
            self._probe(entry)

    def _delete_selected(self):
        entry = self._selected_entry()
        if not entry:
            return
        if not messagebox.askyesno(
            t(self.lang, "confirm_delete_cred_title"),
            t(self.lang, "confirm_delete_cred_body", endpoint=entry.endpoint),
            parent=self,
        ):
            return
        self._app.creds = [c for c in self._app.creds if c.id != entry.id]
        self._app.persist_credentials()
        self._refresh_table()
        self._app.on_credentials_changed()


# ─── Main Application Window ──────────────────────────────────────────────────
class S3ClientApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.lang = get_ui_language()
        self.geometry("1100x680")
        self.minsize(800, 500)
        self.configure(bg=C["bg"])

        # Set window icon (cloud symbol via window title emoji workaround)
        try:
            self.iconbitmap(default="")
        except Exception:
            pass

        # ── State ────────────────────────────────────────────────────────────
        self.creds:             list = []   # CredEntry list, one per configured credential
        self._current_prefix   = ""          # current "folder" path e.g. "imgs/"
        self._all_files:       list = []     # fused across every valid (cred, bucket) pair
        self._file_index:      dict = {}     # file-list row iid -> file dict
        self._dir_index:       dict = {}     # file-list row iid -> folder prefix
        self._row_seq          = 0
        self._status_text      = tk.StringVar(value=t(self.lang, "status_not_connected"))
        self._conn_summary_var = tk.StringVar(value=t(self.lang, "conn_summary_none"))
        self._sort_reverse:    dict = {}

        # ── Build UI ─────────────────────────────────────────────────────────
        self._build_ui()

        # ── Connect on start ─────────────────────────────────────────────────
        self.after(120, self._auto_connect)

    # ── UI (re)build ─────────────────────────────────────────────────────────

    def _build_ui(self):
        self.title(t(self.lang, "app_name"))
        self._apply_styles()
        self._build_menubar()
        self._build_header()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

    def _rebuild_ui(self):
        """Tear down and rebuild every widget after a language switch, keeping
        in-memory state (credentials, loaded files, current prefix)."""
        self.config(menu="")
        for w in self.winfo_children():
            w.destroy()
        self._build_ui()
        self._update_conn_summary()
        if self._all_files or self._valid_targets():
            self._populate_folder_tree()
            self._populate_file_list()
        else:
            self._set_status(t(self.lang, "status_not_connected"))

    def _toggle_language(self):
        self.lang = "en" if self.lang == "zh" else "zh"
        set_ui_language_override(self.lang)
        for cred in self.creds:
            cred.backend.lang = self.lang
        self._rebuild_ui()

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
        lang = self.lang
        mk = {
            "bg": C["panel"], "fg": C["fg"],
            "activebackground": C["selected"], "activeforeground": C["accent2"],
            "relief": "flat",
        }
        mb = tk.Menu(self, **mk)
        self.config(menu=mb)

        fm = tk.Menu(mb, tearoff=0, **mk)
        fm.add_command(label=t(lang, "menu_upload_files"),      command=self._do_upload)
        fm.add_command(label=t(lang, "menu_upload_folder"),     command=self._do_upload_folder)
        fm.add_command(label=t(lang, "menu_download_selected"), command=self._do_download)
        fm.add_separator()
        fm.add_command(label=t(lang, "menu_exit"), command=self.quit)
        mb.add_cascade(label=t(lang, "menu_file"), menu=fm)

        em = tk.Menu(mb, tearoff=0, **mk)
        em.add_command(label=t(lang, "menu_delete_selected"), command=self._do_delete)
        em.add_command(label=t(lang, "menu_refresh"),         command=self._do_refresh_all)
        em.add_command(label=t(lang, "menu_go_up"),           command=self._go_up)
        mb.add_cascade(label=t(lang, "menu_actions"), menu=em)

        sm = tk.Menu(mb, tearoff=0, **mk)
        sm.add_command(label=t(lang, "menu_credentials"), command=self._open_settings)
        mb.add_cascade(label=t(lang, "menu_settings"), menu=sm)

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self):
        lang = self.lang
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
            logo_frm, text=t(lang, "app_name"),
            bg=C["hdr_bg"], fg="#ffffff", font=("Segoe UI", 14, "bold"),
        ).pack(side="left")

        right = tk.Frame(hdr, bg=C["hdr_bg"])
        right.pack(side="right", padx=18)

        tk.Label(right, textvariable=self._conn_summary_var, bg=C["hdr_bg"],
                 fg="#d0f0e0", font=FONT).pack(side="left", padx=(0, 10))

        tk.Button(
            right, text="⚙", command=self._open_settings,
            bg=C["hdr_bg"], fg="#d0f0e0", font=("Segoe UI", 14),
            relief="flat", cursor="hand2", bd=0,
            activebackground=C["accent2"], activeforeground="#ffffff",
        ).pack(side="left", padx=(12, 0))

    # ── Toolbar ───────────────────────────────────────────────────────────────

    def _build_toolbar(self):
        lang = self.lang
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
            (t(lang, "tb_upload"),        self._do_upload,        "#2e9e6a", "#ffffff"),
            (t(lang, "tb_upload_folder"), self._do_upload_folder, "#2e9e6a", "#ffffff"),
            (t(lang, "tb_download"),      self._do_download,      "#27ae60", "#ffffff"),
            (t(lang, "tb_delete"),        self._do_delete,        "#e05c5c", "#ffffff"),
            None,
            (t(lang, "tb_mkdir"),  self._do_mkdir,  C["btn_bg"],  C["fg"]),
            (t(lang, "tb_go_up"),  self._go_up,     C["btn_bg"],  C["fg"]),
            (t(lang, "tb_refresh"), self._do_refresh_all, C["btn_bg"],  C["fg"]),
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
        lang = self.lang
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
            folder_hdr, text=t(lang, "folder_panel_title"), bg=C["accent"], fg="#ffffff",
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
            ("name",     t(lang, "col_name"),     360, "w"),
            ("size",     t(lang, "col_size"),      90, "e"),
            ("type",     t(lang, "col_type"),      70, "center"),
            ("modified", t(lang, "col_modified"), 190, "w"),
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
        self._ctx.add_command(label=t(lang, "ctx_download"), command=self._do_download)
        self._ctx.add_command(label=t(lang, "ctx_delete"),   command=self._do_delete)
        self._ctx.add_separator()
        self._ctx.add_command(label=t(lang, "ctx_copy_key"), command=self._copy_key)

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

        # Language toggle – bottom-right corner, shows the *other* language.
        other_label = "EN" if self.lang == "zh" else "中文"
        tk.Button(
            bar, text=f"🌐 {other_label}", command=self._toggle_language,
            bg=C["sidebar"], fg=C["accent2"], font=FONT_S,
            relief="flat", cursor="hand2", bd=0, padx=8,
            activebackground=C["selected"], activeforeground=C["accent2"],
        ).pack(side="right", padx=10)

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
        entries = load_credentials_list()
        if not entries:
            CredentialsDialog(self, self)
            self._update_conn_summary()
            return
        self.creds = [
            CredEntry(e["id"], e["platform"], e["access_key"], e["secret_key"],
                      e["endpoint"], e["region"], lang=self.lang)
            for e in entries
        ]
        self._probe_all_creds()

    def _probe_all_creds(self):
        self._set_status(t(self.lang, "status_probing", n=len(self.creds)))

        def _run():
            for cred in self.creds:
                probe_credential(cred, self.lang)
            self.after(0, self._on_all_probed)

        threading.Thread(target=_run, daemon=True).start()

    def _on_all_probed(self):
        self._update_conn_summary()
        if self._valid_targets():
            self._do_refresh_all()
        else:
            self._set_status(t(self.lang, "status_not_connected"))

    def _open_settings(self):
        CredentialsDialog(self, self)
        self._update_conn_summary()

    def persist_credentials(self):
        save_credentials_list([c.to_dict() for c in self.creds])

    def on_credentials_changed(self):
        """Called by CredentialsDialog after any add/delete/re-verify."""
        self._update_conn_summary()
        self._do_refresh_all()

    def _update_conn_summary(self):
        if not self.creds:
            self._conn_summary_var.set(t(self.lang, "conn_summary_none"))
            return
        valid = [c for c in self.creds if c.status == "valid"]
        n_buckets = sum(len(c.buckets) for c in valid)
        self._conn_summary_var.set(t(self.lang, "conn_summary", n_buckets=n_buckets,
                                     n_valid=len(valid), n_total=len(self.creds)))

    def _valid_targets(self) -> list:
        """Return every (CredEntry, bucket) pair currently usable."""
        return [(c, b) for c in self.creds if c.status == "valid" for b in c.buckets]

    def _need_target(self) -> bool:
        if not self._valid_targets():
            messagebox.showwarning(t(self.lang, "warn_no_target_title"),
                                   t(self.lang, "warn_no_target_body"), parent=self)
            return False
        return True

    def _least_used_target(self):
        """Pick the (CredEntry, bucket) pair with the smallest known total size."""
        targets = self._valid_targets()
        if not targets:
            messagebox.showwarning(t(self.lang, "warn_no_target_title"),
                                   t(self.lang, "warn_no_target_body"), parent=self)
            return None
        usage = {(c.id, b): 0 for c, b in targets}
        for f in self._all_files:
            k = (f["_cred"].id, f["_bucket"])
            if k in usage:
                usage[k] += f["size"]
        best_key = min(usage, key=usage.get)
        for c, b in targets:
            if (c.id, b) == best_key:
                return c, b
        return targets[0]

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
        lang = self.lang
        self._file_list.delete(*self._file_list.get_children())
        self._file_index.clear()
        self._dir_index.clear()
        prefix   = self._current_prefix
        sub_dirs = set()
        root_entries = []

        for f in self._all_files:
            key = f["key"]
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix):]
            if "/" in rest:
                sub_dirs.add(rest.split("/")[0])
            else:
                root_entries.append(f)

        row = 0
        # Sub-folders first
        for sub in sorted(sub_dirs):
            tag = ("folder", "even" if row % 2 == 0 else "odd")
            iid = f"__d{self._row_seq}"
            self._row_seq += 1
            self._dir_index[iid] = prefix + sub + "/"
            self._file_list.insert(
                "", "end",
                values=(f"📁   {sub}/", t(lang, "dash"), t(lang, "type_folder"), t(lang, "dash")),
                iid=iid,
                tags=tag,
            )
            row += 1

        # Files
        for f in sorted(root_entries, key=lambda x: x["key"]):
            key  = f["key"]
            name = key.split("/")[-1]
            ext  = name.rsplit(".", 1)[-1].lower() if "." in name else t(lang, "dash")
            size = _fmt_size(f["size"])
            mtime = f["last_modified"][:19].replace("T", " ") if f["last_modified"] else t(lang, "dash")
            icon  = _file_icon(ext)
            tag   = ("even" if row % 2 == 0 else "odd",)
            iid = f"__f{self._row_seq}"
            self._row_seq += 1
            self._file_index[iid] = f
            self._file_list.insert(
                "", "end",
                values=(f"{icon}   {name}", size, ext, mtime),
                iid=iid,
                tags=tag,
            )
            row += 1

        total = len(root_entries) + len(sub_dirs)
        n_buckets = len({(f["_cred"].id, f["_bucket"]) for f in self._all_files})
        total_size = _fmt_size(sum(f["size"] for f in self._all_files))
        self._set_status(t(lang, "status_bucket_summary",
                           n_buckets=n_buckets, count=len(self._all_files),
                           size=total_size, prefix=prefix, n=total))
        self._path_var.set("/" + prefix)

    def _on_file_double_click(self, _event=None):
        sel = self._file_list.selection()
        if not sel:
            return
        iid = sel[0]
        if iid in self._dir_index:
            # Navigate into sub-directory
            new_prefix = self._dir_index[iid]
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
        f = self._file_index.get(sel[0])
        if f:
            self.clipboard_clear()
            self.clipboard_append(f["key"])
            self._set_status(t(self.lang, "status_copied_key", key=f["key"]))

    # ── Column sort ───────────────────────────────────────────────────────────

    def _toggle_sort(self, col: str):
        rev = self._sort_reverse.get(col, False)
        items = [
            (self._file_list.set(k, col), k)
            for k in self._file_list.get_children("")
        ]
        # Folders always on top
        folders = [(v, k) for v, k in items if k in self._dir_index]
        files   = [(v, k) for v, k in items if k not in self._dir_index]
        files.sort(key=lambda x: x[0].lower(), reverse=rev)
        for idx, (_, k) in enumerate(folders + files):
            self._file_list.move(k, "", idx)
        self._sort_reverse[col] = not rev

    # ── Selection helpers ────────────────────────────────────────────────────

    def _selected_file_entries(self) -> list:
        """Return selected file entries (dicts with key/_cred/_bucket), excluding folders."""
        out = []
        for iid in self._file_list.selection():
            f = self._file_index.get(iid)
            if f:
                out.append(f)
        return out

    def _selected_delete_targets(self):
        """Return (file_entries, folder_prefixes) for the current selection."""
        file_entries, folder_prefixes = [], []
        for iid in self._file_list.selection():
            if iid in self._dir_index:
                folder_prefixes.append(self._dir_index[iid])
            else:
                f = self._file_index.get(iid)
                if f:
                    file_entries.append(f)
        return file_entries, folder_prefixes

    # ── Operations ───────────────────────────────────────────────────────────

    def _do_refresh_all(self):
        targets = self._valid_targets()
        if not targets:
            self._set_status(t(self.lang, "status_not_connected"))
            return
        self._set_status(t(self.lang, "status_loading_n", n=len(targets)))
        self._show_progress(True)
        self._progress_var.set(0)

        def _run():
            all_files = []
            errors = []
            for i, (cred, bucket) in enumerate(targets, 1):
                try:
                    files = cred.backend.list_all_files(bucket)
                    for f in files:
                        f["_cred"] = cred
                        f["_bucket"] = bucket
                    all_files.extend(files)
                except Exception as exc:
                    errors.append(f"{bucket}: {exc}")
                self.after(0, lambda v=i/len(targets)*100: self._progress_var.set(v))
            self.after(0, lambda: self._on_refresh_all_done(all_files, errors))

        threading.Thread(target=_run, daemon=True).start()

    def _on_refresh_all_done(self, files: list, errors: list):
        self._all_files = files
        self._populate_folder_tree()
        self._populate_file_list()
        self._show_progress(False)
        if errors:
            self._set_status(t(self.lang, "status_partial_errors", errors="; ".join(errors[:2])))

    def _do_upload(self):
        if not self._need_target():
            return
        paths = filedialog.askopenfilenames(
            parent=self, title=t(self.lang, "dlg_choose_upload_files")
        )
        if not paths:
            return
        target = self._least_used_target()
        if target is None:
            return
        cred, bucket = target
        prefix = self._current_prefix
        total  = len(paths)
        self._show_progress(True)

        def _run():
            ok = fail = 0
            for i, local_path in enumerate(paths, 1):
                fname  = Path(local_path).name
                r2_key = prefix + fname
                self.after(0, lambda k=r2_key: self._set_status(t(self.lang, "status_uploading", key=k)))
                self.after(0, lambda v=i/total*100: self._progress_var.set(v))
                try:
                    cred.backend.upload_file(bucket, local_path, r2_key)
                    ok += 1
                except Exception as exc:
                    fail += 1
                    self.after(0, lambda e=exc: messagebox.showerror(
                        t(self.lang, "err_upload_title"), str(e), parent=self))
            self.after(0, lambda: self._set_status(
                t(self.lang, "status_upload_done_target", ok=ok, fail=fail, bucket=bucket)))
            self.after(0, lambda: self._show_progress(False))
            self.after(0, self._do_refresh_all)

        threading.Thread(target=_run, daemon=True).start()

    def _do_upload_folder(self):
        if not self._need_target():
            return
        folder = filedialog.askdirectory(parent=self, title=t(self.lang, "dlg_choose_upload_folder"))
        if not folder:
            return
        base = Path(folder)
        local_paths = [p for p in base.rglob("*") if p.is_file()]
        if not local_paths:
            messagebox.showinfo(t(self.lang, "warn_empty_folder_title"),
                                t(self.lang, "warn_empty_folder_body"), parent=self)
            return
        target = self._least_used_target()
        if target is None:
            return
        cred, bucket = target
        prefix = self._current_prefix + base.name + "/"
        total  = len(local_paths)
        self._show_progress(True)

        def _run():
            ok = fail = 0
            for i, local_path in enumerate(local_paths, 1):
                rel    = local_path.relative_to(base).as_posix()
                r2_key = prefix + rel
                self.after(0, lambda k=r2_key: self._set_status(t(self.lang, "status_uploading", key=k)))
                self.after(0, lambda v=i/total*100: self._progress_var.set(v))
                try:
                    cred.backend.upload_file(bucket, str(local_path), r2_key)
                    ok += 1
                except Exception as exc:
                    fail += 1
                    self.after(0, lambda e=exc: messagebox.showerror(
                        t(self.lang, "err_upload_title"), str(e), parent=self))
            self.after(0, lambda: self._set_status(
                t(self.lang, "status_upload_folder_done_target", ok=ok, fail=fail, bucket=bucket)))
            self.after(0, lambda: self._show_progress(False))
            self.after(0, self._do_refresh_all)

        threading.Thread(target=_run, daemon=True).start()

    def _do_download(self):
        entries = self._selected_file_entries()
        if not entries:
            messagebox.showinfo(t(self.lang, "info_no_files_title"),
                                t(self.lang, "info_no_files_body"), parent=self)
            return
        dest_dir = filedialog.askdirectory(parent=self, title=t(self.lang, "dlg_choose_download_dir"))
        if not dest_dir:
            return
        total = len(entries)
        self._show_progress(True)

        def _run():
            ok = fail = 0
            for i, f in enumerate(entries, 1):
                key   = f["key"]
                fname = key.split("/")[-1]
                dest  = str(Path(dest_dir) / fname)
                self.after(0, lambda k=key: self._set_status(t(self.lang, "status_downloading", key=k)))
                self.after(0, lambda v=i/total*100: self._progress_var.set(v))
                try:
                    f["_cred"].backend.download_file(f["_bucket"], key, dest)
                    ok += 1
                except Exception as exc:
                    fail += 1
                    self.after(0, lambda e=exc: messagebox.showerror(
                        t(self.lang, "err_download_title"), str(e), parent=self))
            self.after(0, lambda: self._set_status(
                t(self.lang, "status_download_done", ok=ok, fail=fail)))
            self.after(0, lambda: self._show_progress(False))

        threading.Thread(target=_run, daemon=True).start()

    def _do_mkdir(self):
        if not self._need_target():
            return
        lang = self.lang

        # Modal input dialog
        dlg = tk.Toplevel(self)
        dlg.title(t(lang, "dlg_mkdir_title"))
        dlg.resizable(False, False)
        dlg.configure(bg=C["bg"])
        dlg.grab_set()
        dlg.update_idletasks()
        pw = self.winfo_rootx() + self.winfo_width()  // 2
        ph = self.winfo_rooty() + self.winfo_height() // 2
        dlg.geometry(f"+{pw - 160}+{ph - 80}")

        tk.Frame(dlg, bg=C["accent"], height=4).pack(fill="x")
        tk.Label(dlg, text=t(lang, "dlg_mkdir_heading"),
                 bg=C["bg"], fg=C["accent2"], font=FONT_B).pack(pady=(14, 6))
        tk.Label(dlg, text=t(lang, "dlg_mkdir_name_label"),
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
                messagebox.showwarning(t(lang, "warn_hint_title"),
                                       t(lang, "warn_need_folder_name"), parent=dlg)
                return
            if any(ch in raw for ch in ('\\', '?', '*', ':', '"', '<', '>', '|')):
                messagebox.showwarning(t(lang, "warn_bad_chars_title"),
                                       t(lang, "warn_bad_chars_body"), parent=dlg)
                return
            target = self._least_used_target()
            if target is None:
                return
            cred, bucket = target
            dlg.destroy()
            key = self._current_prefix + raw + "/"
            self._set_status(t(lang, "status_mkdir_creating_target", key=key, bucket=bucket))
            def _run():
                try:
                    cred.backend.create_folder(bucket, key)
                    self.after(0, lambda: self._set_status(t(lang, "status_mkdir_done", name=raw)))
                    self.after(0, self._do_refresh_all)
                except Exception as exc:
                    self.after(0, lambda e=exc: self._on_error(e, title=t(lang, "err_mkdir_title")))
            threading.Thread(target=_run, daemon=True).start()

        ent.bind("<Return>", lambda _: _confirm())
        btn_row = tk.Frame(dlg, bg=C["bg"])
        btn_row.pack(pady=16)
        tk.Button(btn_row, text=t(lang, "btn_confirm"), command=_confirm,
                  bg=C["accent"], fg="#ffffff", font=FONT_B,
                  relief="flat", cursor="hand2", padx=12, pady=6, bd=0,
                  activebackground=C["accent2"], activeforeground="#ffffff",
                  ).pack(side="left", padx=8)
        tk.Button(btn_row, text=t(lang, "btn_cancel"), command=dlg.destroy,
                  bg=C["btn_bg"], fg=C["fg2"], font=FONT,
                  relief="flat", cursor="hand2", padx=12, pady=6, bd=0,
                  ).pack(side="left", padx=8)
        self.wait_window(dlg)

    def _do_delete(self):
        lang = self.lang
        file_entries, folder_prefixes = self._selected_delete_targets()
        if not file_entries and not folder_prefixes:
            messagebox.showinfo(t(lang, "info_no_items_title"),
                                t(lang, "info_no_items_body"), parent=self)
            return

        # Expand each selected folder (across every fused bucket) into the
        # full set of (cred, bucket, key) triples it contains, including its
        # own placeholder object if any, so the whole subtree is removed.
        seen = set()
        targets = []
        for f in file_entries:
            sig = (f["_cred"].id, f["_bucket"], f["key"])
            if sig not in seen:
                seen.add(sig)
                targets.append((f["_cred"], f["_bucket"], f["key"]))
        for prefix in folder_prefixes:
            for f in self._all_files:
                if f["key"].startswith(prefix):
                    sig = (f["_cred"].id, f["_bucket"], f["key"])
                    if sig not in seen:
                        seen.add(sig)
                        targets.append((f["_cred"], f["_bucket"], f["key"]))
        if not targets:
            return

        names = [k.split("/")[-1] for _, _, k in targets if not k.endswith("/")]
        names += [p.rstrip("/").split("/")[-1] + "/" for p in folder_prefixes]
        preview = "\n".join(names[:6])
        if len(names) > 6:
            preview += t(lang, "delete_more_items", n=len(names) - 6)
        folder_note = ""
        if folder_prefixes:
            folder_note = t(lang, "delete_folder_note", n=len(folder_prefixes), m=len(targets))

        if not messagebox.askyesno(
            t(lang, "confirm_delete_title"),
            t(lang, "confirm_delete_body", n=len(names), preview=preview, note=folder_note),
            parent=self,
        ):
            return
        total = len(targets)
        self._show_progress(True)

        def _run():
            ok = fail = 0
            for i, (cred, bucket, key) in enumerate(targets, 1):
                self.after(0, lambda k=key: self._set_status(t(lang, "status_deleting", key=k)))
                self.after(0, lambda v=i/total*100: self._progress_var.set(v))
                try:
                    cred.backend.delete_file(bucket, key)
                    ok += 1
                except FileNotFoundError:
                    # Implicit folder had no placeholder object – nothing to do.
                    ok += 1
                except Exception as exc:
                    fail += 1
                    self.after(0, lambda e=exc: messagebox.showerror(
                        t(lang, "err_delete_title"), str(e), parent=self))
            self.after(0, lambda: self._set_status(
                t(lang, "status_delete_done", ok=ok, fail=fail)))
            self.after(0, lambda: self._show_progress(False))
            self.after(0, self._do_refresh_all)

        threading.Thread(target=_run, daemon=True).start()

    def _on_error(self, exc: Exception, title: str | None = None):
        self._show_progress(False)
        self._set_status(t(self.lang, "status_error", err=exc))
        messagebox.showerror(title or t(self.lang, "err_upload_title"), str(exc), parent=self)


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
    app = S3ClientApp()
    app.mainloop()
