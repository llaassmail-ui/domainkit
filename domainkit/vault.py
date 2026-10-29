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
import tempfile
import uuid
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
        "AWS_ACCESS_KEY_ID",
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
PROFILE_KEYS = {name: tuple(k for k in CREDENTIAL_KEYS if k.startswith(name.upper() + "_"))
                for name in ("aws", "godaddy", "spaceship", "gitlab")}


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
    store = load_profiles(path=target)
    for category, keys in PROFILE_KEYS.items():
        current = profile_current(store, category)
        if any(k in values for k in keys) and (current or any(str(values.get(k) or '').strip() for k in keys)):
            merged = dict(current['values']) if current else {}
            for key in keys:
                if key in values:
                    value = str(values.get(key) or '').strip()
                    if value:
                        merged[key] = value
            put_profile(store, category, current['name'] if current else '默认凭证', merged,
                        profile_id=current['id'] if current else None)
    save_profiles(store, path=target)
    return target


def empty_profiles():
    return {'version': 2, 'profiles': {k: [] for k in PROFILE_KEYS},
            'selected': {k: None for k in PROFILE_KEYS}}


def profile_current(store, category):
    return next((p for p in store['profiles'][category]
                 if p['id'] == store['selected'][category]), None)


def selected_values(store):
    values = {}
    for category in PROFILE_KEYS:
        current = profile_current(store, category)
        if current:
            values.update(current['values'])
    return values


def validate_profiles(store):
    try:
        if store['version'] != 2:
            raise ValueError()
        for category, keys in PROFILE_KEYS.items():
            ids, names = set(), set()
            if not isinstance(store['profiles'][category], list):
                raise ValueError()
            for p in store['profiles'][category]:
                if (not isinstance(p['id'], str) or not p['id'] or p['id'] in ids or
                        not isinstance(p['name'], str) or not p['name'].strip() or
                        p['name'].casefold() in names or not isinstance(p['values'], dict) or
                        any(k not in keys or not isinstance(v, str) for k, v in p['values'].items())):
                    raise ValueError()
                ids.add(p['id'])
                names.add(p['name'].casefold())
            if store['selected'][category] is not None and store['selected'][category] not in ids:
                raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ConfigError('凭证配置格式无效，原文件已保留') from exc


def load_profiles(*, path):
    payload = _read_payload(path)
    if not payload:
        return empty_profiles()
    if 'version' in payload:
        validate_profiles(payload)
        return payload
    store = empty_profiles()
    for category, keys in PROFILE_KEYS.items():
        if any(payload.get(k) for k in keys):
            put_profile(store, category, '默认凭证', payload)
    return store


def put_profile(store, category, name, values, *, profile_id=None):
    name = name.strip()
    if not name:
        raise ConfigError('凭证名称不能为空')
    profiles = store['profiles'][category]
    if any(p['name'].casefold() == name.casefold() and p['id'] != profile_id for p in profiles):
        raise ConfigError('该类别已有同名凭证')
    item = next((p for p in profiles if p['id'] == profile_id), None)
    if profile_id is not None and item is None:
        raise ConfigError('凭证不存在')
    if item is None:
        item = {'id': uuid.uuid4().hex}
        profiles.append(item)
    item.update(name=name, values={k: str(values.get(k) or '').strip() for k in PROFILE_KEYS[category]
                                   if str(values.get(k) or '').strip()})
    store['selected'][category] = item['id']
    return item['id']


def remove_profile(store, category, profile_id):
    store['profiles'][category] = [p for p in store['profiles'][category] if p['id'] != profile_id]
    if store['selected'][category] == profile_id:
        store['selected'][category] = None


def save_profiles(store, *, path):
    validate_profiles(store)
    envelope = {'magic': _MAGIC}
    envelope.update(_encrypt(json.dumps(store, ensure_ascii=False).encode('utf-8')))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old = _read_payload(path)
        if 'version' in old:
            validate_profiles(old)
        else:
            backup = path.with_name(path.name + '.v1.bak')
            if not backup.exists():
                fd, backup_temp = tempfile.mkstemp(prefix=backup.name + '.', dir=path.parent)
                try:
                    if not _dpapi_available():
                        os.chmod(backup_temp, 0o600)
                    with os.fdopen(fd, 'wb') as handle:
                        handle.write(path.read_bytes())
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(backup_temp, backup)
                finally:
                    if os.path.exists(backup_temp):
                        os.unlink(backup_temp)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        if not _dpapi_available():
            os.chmod(temporary, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(envelope, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if not _dpapi_available():
            os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_credentials(
    *,
    path: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Read stored credentials, or an empty dict when nothing is saved yet."""
    return selected_values(load_profiles(path=path or store_path(env)))


def _read_payload(target):
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
    return values


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
