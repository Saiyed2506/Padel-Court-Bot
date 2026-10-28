"""Protecting secrets on disk (Windows built-ins, no extra packages).

* protect()/unprotect(): Windows DPAPI encryption, tied to your Windows user
  account. A copied file (backup, cloud sync, another PC) can't be decrypted.
  Malware running as *you* still could - nothing on a normal PC prevents that.
* restrict(): file permissions so only your Windows user can read the file.
* cloud_synced(): warns when the bot folder is inside OneDrive/Dropbox/etc.

On other systems protect() is a no-op (stores plain text) and restrict() uses
chmod 600.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("padel_bot")

_WINDOWS = sys.platform == "win32"
_PREFIX = "dpapi:"


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    ui_forbidden = 0x1
    if encrypt:
        ok = crypt32.CryptProtectData(ctypes.byref(blob_in), ctypes.c_wchar_p("padel-bot"), None, None, None,
                                      ui_forbidden, ctypes.byref(blob_out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None,
                                        ui_forbidden, ctypes.byref(blob_out))
    if not ok:
        raise OSError("Windows could not " + ("encrypt" if encrypt else "decrypt") + " the data")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def protect(text: str) -> str:
    """Encrypt for this Windows user; returns text safe to write to a file."""
    if not _WINDOWS:
        return text
    try:
        return _PREFIX + base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")
    except Exception as exc:  # very unusual; keep working, but say so
        log.warning("Couldn't encrypt the login token (%s) - stored unencrypted.", exc)
        return text


def unprotect(stored: str) -> str:
    """Reverse of protect(). Plain (older) files are returned as they are."""
    if not stored.startswith(_PREFIX):
        return stored
    return _dpapi(base64.b64decode(stored[len(_PREFIX):]), False).decode("utf-8")


def is_protected(stored: str) -> bool:
    return stored.startswith(_PREFIX)


def restrict(path: Path) -> None:
    """Only your user account may read/write this file (best effort)."""
    if not path.exists():
        return
    try:
        if _WINDOWS:
            user = os.environ.get("USERNAME", "")
            domain = os.environ.get("USERDOMAIN", "")
            who = f"{domain}\\{user}" if domain else user
            if user:
                subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{who}:F"],
                               capture_output=True, timeout=15, check=False)
        else:
            os.chmod(path, 0o600)
    except Exception as exc:
        log.warning("Couldn't restrict access to %s: %s", path.name, exc)


def cloud_synced(path: Path) -> str | None:
    """Name of a cloud-sync folder the path is inside, if any."""
    lowered = str(path.resolve()).lower()
    for name in ("onedrive", "dropbox", "google drive", "googledrive", "icloud"):
        if name in lowered:
            return name.title()
    return None
