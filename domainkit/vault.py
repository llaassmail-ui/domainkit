"""Local credential storage for the GUI.

Secrets are encrypted with Windows DPAPI (CryptProtectData) so only the current
Windows user on this machine can read them back. On other platforms the file
falls back to owner-only permissions, which is weaker but still keeps the
secrets out of shell history and out of the profile JSON.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional

from .errors import ConfigError

CREDENTIAL_KEYS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GODADDY_API_KEY",
    "GODADDY_API_SECRET",
    "SPACESHIP_API_KEY",
    "SPACESHIP_API_SECRET",
    "SPACESHIP_API_ENDPOINT",
    "GITLAB_URL",
    "GITLAB_PROJECT_ID",
    "GITLAB_TOKEN",
)

SECRET_KEYS = frozenset(
    {
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GODADDY_API_KEY",
        "GODADDY_API_SECRET",
        "SPACESHIP_API_KEY",
        "SPACESHIP_API_SECRET",
        "GITLAB_TOKEN",
    }
)

_STORE_ENV = "DOMAINKIT_CREDENTIAL_STORE"
_STORE_NAME = "credentials.json"
_MAGIC = "domainkit-credentials-v1"


def store_path(env: Optional[Mapping[str, str]] = None) -> Path:
    """Where the encrypted credential file lives."""
    env = os.environ if env is None else env
    override = (env.get(_STORE_ENV) or "").strip()
    if override:
        return Path(override)
    base = (env.get("LOCALAPPDATA") or env.get("XDG_CONFIG_HOME") or "").strip()
    root = Path(base) if base else Path.home() / ".config"
    return root / "domainkit" / _STORE_NAME


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _dpapi(func_name: str, payload: bytes) -> bytes:
    """Call CryptProtectData / CryptUnprotectData on a byte string."""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(payload, len(payload))
    source = Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    func = getattr(crypt32, func_name)
    ok = func(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result))
    if not ok:
        raise ConfigError(
            "Windows DPAPI {0} failed (error {1})".format(func_name, ctypes.get_last_error())
        )
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


def _encrypt(plaintext: bytes) -> Dict[str, str]:
    if _dpapi_available():
        sealed = _dpapi("CryptProtectData", plaintext)
        return {"scheme": "dpapi", "data": base64.b64encode(sealed).decode("ascii")}
    return {"scheme": "plain", "data": base64.b64encode(plaintext).decode("ascii")}


def _decrypt(scheme: str, data: str) -> bytes:
    try:
        raw = base64.b64decode(data.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ConfigError("stored credentials are not valid base64: {0}".format(exc)) from exc
    if scheme == "dpapi":
        if not _dpapi_available():
            raise ConfigError("this credential file was encrypted with Windows DPAPI and needs Windows to read")
        return _dpapi("CryptUnprotectData", raw)
    if scheme == "plain":
        return raw
    raise ConfigError("unknown credential encryption scheme: {0}".format(scheme))


def save_credentials(
    values: Mapping[str, str],
    *,
    path: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Path:
    """Write the non-empty credential values to the encrypted store."""
    target = path or store_path(env)
    payload = {
        key: str(values.get(key) or "").strip()
        for key in CREDENTIAL_KEYS
        if str(values.get(key) or "").strip()
    }
    envelope: Dict[str, str] = {"magic": _MAGIC}
    envelope.update(_encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8")))

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    if not _dpapi_available():
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    return target


def load_credentials(
    *,
    path: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Read stored credentials, or an empty dict when nothing is saved yet."""
    target = path or store_path(env)
    if not target.is_file():
        return {}
    try:
        envelope = json.loads(target.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ConfigError("credential file {0} is not valid JSON: {1}".format(target, exc)) from exc
    if not isinstance(envelope, dict) or envelope.get("magic") != _MAGIC:
        raise ConfigError("credential file {0} is not a domainkit credential store".format(target))

    decoded = _decrypt(str(envelope.get("scheme") or ""), str(envelope.get("data") or ""))
    try:
        values = json.loads(decoded.decode("utf-8"))
    except ValueError as exc:
        raise ConfigError("stored credentials are corrupted: {0}".format(exc)) from exc
    if not isinstance(values, dict):
        raise ConfigError("stored credentials must be a JSON object")
    return {str(k): str(v) for k, v in values.items() if str(k) in CREDENTIAL_KEYS}


def delete_credentials(
    *,
    path: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """Remove the credential file. Returns True when a file was deleted."""
    target = path or store_path(env)
    if not target.is_file():
        return False
    target.unlink()
    return True


def mask(value: str, *, keep: int = 4) -> str:
    """Render a secret as a short fingerprint, never the full value."""
    text = (value or "").strip()
    if not text:
        return "(未设置)"
    if len(text) <= keep:
        return "*" * len(text)
    return "{0}{1} (长度 {2})".format(text[:keep], "*" * min(8, len(text) - keep), len(text))


def describe(values: Mapping[str, str], keys: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """Build a display-safe summary: secrets masked, plain fields shown as-is."""
    summary: Dict[str, str] = {}
    for key in keys or CREDENTIAL_KEYS:
        raw = str(values.get(key) or "").strip()
        summary[key] = mask(raw) if key in SECRET_KEYS else (raw or "(未设置)")
    return summary


def apply_to_env(
    env: Dict[str, str],
    values: Mapping[str, str],
    keys: Optional[Iterable[str]] = None,
) -> None:
    """Set non-empty values and drop keys that are blank.

    Writing an empty string is worse than not writing at all: a blank
    GITLAB_URL is treated as unset and falls back to https://gitlab.com, which
    silently aims a private-instance token at the public server.
    """
    for key in keys or CREDENTIAL_KEYS:
        value = str(values.get(key) or "").strip()
        if value:
            env[key] = value
        else:
            env.pop(key, None)
