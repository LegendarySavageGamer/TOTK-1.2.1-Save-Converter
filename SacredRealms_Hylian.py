"""SACRED ZONAI REALMS v7.30 — QR Diagnostics & Error Sharing.

Primary interface: local browser-rendered desktop UI (HTML/CSS/JS) using only the
Python standard library.  On Windows it opens Microsoft Edge in app mode when
available, giving us modern animations, layered artwork, sound, and real map
images without Tkinter's drawing limitations.

The proven classic Tkinter tools are preserved as a compatibility fallback and can
be launched from the modern interface while those complex panels are migrated.
"""
from __future__ import annotations

import json
import mimetypes
import os
import platform
import traceback
import shutil
from datetime import datetime
from collections import deque
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from totk_converter_core import parse_ktml, serialize_ktml, validate_ktml_types
from zonai_realms_backend import HOSTING
from wesley_zonai_save_converter import ktml_to_progress_sav, progress_sav_to_ktml

APP_NAME = "SACRED ZONAI REALMS"
APP_VERSION = "v7.30 — QR Diagnostics & Error Sharing"
HOST = "127.0.0.1"
APP_ERRORS = deque(maxlen=120)
APP_ERROR_EVENTS = deque(maxlen=120)
ERROR_LOCK = threading.RLock()
WEBVIEW_WINDOW = None
WEBVIEW_LOCK = threading.RLock()

# v7.30 hotfix: diagnostic report actions must stay responsive even when
# Docker Desktop is offline. The Hosting Diagnostics endpoint performs the
# real live preflight and refreshes this cache; report/QR actions only read it.
RUNTIME_DIAG_LOCK = threading.RLock()
RUNTIME_DIAG_CACHE = {}
RUNTIME_DIAG_CACHE_AT = 0.0

def _store_runtime_diagnostics(diag):
    global RUNTIME_DIAG_CACHE, RUNTIME_DIAG_CACHE_AT
    if isinstance(diag, dict):
        with RUNTIME_DIAG_LOCK:
            RUNTIME_DIAG_CACHE = dict(diag)
            RUNTIME_DIAG_CACHE_AT = time.time()

def _cached_runtime_diagnostics(snapshot=None):
    with RUNTIME_DIAG_LOCK:
        if RUNTIME_DIAG_CACHE:
            out = dict(RUNTIME_DIAG_CACHE)
            out["cached_for_report"] = True
            out["cache_age_seconds"] = max(0, int(time.time() - RUNTIME_DIAG_CACHE_AT))
            return out
    snap = snapshot if isinstance(snapshot, dict) else {}
    java = shutil.which("java")
    return {
        "docker_ok": None,
        "docker_detail": "Live Docker preflight was not run by this error-report action. Use Zonai Realms Hosting > Diagnostics for a live Docker check.",
        "java_found": bool(java),
        "java_path": java,
        "selected_ip": snap.get("advertised_ip"),
        "port": snap.get("port"),
        "port_free_when_stopped": None,
        "workspace_ok": bool(snap.get("workspace")),
        "runtime_ok": bool(snap.get("running") or snap.get("paused")),
        "radmin_detected": any(str(x.get("kind", "")).startswith("Radmin") for x in (snap.get("ips") or []) if isinstance(x, dict)),
        "cached_for_report": True,
        "cache_age_seconds": None,
    }


def record_app_error(message: str, *, source: str = "Application", operation: str = "", severity: str = "ERROR"):
    """Capture bounded, local-only structured diagnostic history for later export."""
    now = datetime.now().astimezone()
    raw = str(message)
    with ERROR_LOCK:
        APP_ERRORS.append(f"[{now.isoformat(timespec='seconds')}] {raw}")
        APP_ERROR_EVENTS.append({
            "timestamp": now.isoformat(timespec="seconds"),
            "time": now.strftime("%H:%M:%S"),
            "source": str(source or "Application")[:120],
            "operation": str(operation or "")[:160],
            "severity": str(severity or "ERROR")[:32],
            "message": raw,
        })


_SENSITIVE_KEY_RE = __import__('re').compile(
    r"(?:pass(?:word)?|passwd|secret|token|api[_-]?key|auth(?:orization)?|cookie|session|credential|private[_-]?key)",
    __import__('re').I,
)
_EMAIL_RE = __import__('re').compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_WIN_USER_RE = __import__('re').compile(r"(?i)([A-Z]:\\Users\\)[^\\/\r\n]+")
_POSIX_USER_RE = __import__('re').compile(r"(/(?:home|Users)/)[^/\r\n]+")
_BEARER_RE = __import__('re').compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_ASSIGN_SECRET_RE = __import__('re').compile(r"(?i)\b(password|passwd|token|secret|api[_-]?key|authorization)\s*([:=])\s*([^\s,;]+)")
_IPV4_RE = __import__('re').compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")


def _redact_string(value: object) -> str:
    """Best-effort privacy scrubber. It reduces exposure; it is not a guarantee."""
    text = str(value if value is not None else "")
    try:
        home = str(Path.home())
        if home and len(home) > 3:
            text = text.replace(home, "[HOME_REDACTED]")
    except Exception:
        pass
    text = _WIN_USER_RE.sub(r"\1[REDACTED]", text)
    text = _POSIX_USER_RE.sub(r"\1[REDACTED]", text)
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _ASSIGN_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)

    def redact_ip(match):
        ip = match.group(0)
        if ip.startswith("127.") or ip == "0.0.0.0":
            return ip
        try:
            octets = [int(x) for x in ip.split('.')]
            if len(octets) == 4 and all(0 <= x <= 255 for x in octets):
                return "[IP_REDACTED]"
        except Exception:
            pass
        return ip
    return _IPV4_RE.sub(redact_ip, text)


def _sanitize_value(value, key: str = ""):
    if _SENSITIVE_KEY_RE.search(str(key or "")):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _sanitize_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_value(v) for v in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _extract_error_details(errors):
    import re
    raw = errors[-1] if errors else ""
    clean = _redact_string(raw)
    detail = {
        "captured": bool(raw),
        "category": "Application Error" if raw else "No captured error",
        "exception_type": "",
        "message": clean if raw else "No captured application errors this session.",
        "traceback": clean if raw else "",
        "source_file": "",
        "function": "",
        "line": None,
    }
    if not raw:
        return detail
    type_matches = list(re.finditer(r"(?m)([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)):\s*([^\r\n]+)", raw))
    if type_matches:
        m = type_matches[-1]
        detail["exception_type"] = m.group(1)
        detail["message"] = _redact_string(m.group(2).strip())
    frame_matches = list(re.finditer(r'File\s+["\']([^"\']+)["\'],\s+line\s+(\d+),\s+in\s+([^\r\n]+)', raw))
    if frame_matches:
        m = frame_matches[-1]
        detail["source_file"] = Path(m.group(1)).name
        detail["line"] = int(m.group(2))
        detail["function"] = m.group(3).strip()
    elif "Browser UI:" in raw:
        m = re.search(r"@\s*([^\s:]+):(\d+)", raw)
        if m:
            detail["source_file"] = Path(m.group(1)).name
            detail["line"] = int(m.group(2))
            detail["function"] = "browser event handler"
    return detail


def _request_error_context(path: str):
    path = str(path or "")
    if "/api/convert/sav-to-ktml" in path:
        return "Save Converter", "SAV → KTML"
    if "/api/convert/ktml-to-sav" in path:
        return "Save Converter", "KTML → SAV"
    if "/api/hosting/replace-player" in path:
        return "Zonai Realms Hosting", "Player Save Upload / Replace"
    if "/api/hosting/replace-main-save" in path:
        return "Zonai Realms Hosting", "Main Realm Save Upload / Replace"
    if "/api/hosting/" in path:
        return "Zonai Realms Hosting", "Realm / Server Operation"
    if "/api/load-ktml" in path or "/api/apply-position" in path:
        return "Client Save / Recovery", "KTML Operation"
    return "Application", path[:160]


def _diagnostic_context(user_input=None):
    """Single sanitized diagnostic data model used by preview/TXT/JSON/developer/QR outputs."""
    import hashlib
    user_input = user_input if isinstance(user_input, dict) else {}
    snap = HOSTING.snapshot()
    # Avoid a synchronous Docker CLI timeout during Copy/TXT/JSON/QR actions.
    diag = _cached_runtime_diagnostics(snap)
    with ERROR_LOCK:
        errors = list(APP_ERRORS)
        app_events = [dict(x) for x in APP_ERROR_EVENTS]
    realm_events = HOSTING.recent_error_events(80) if hasattr(HOSTING, "recent_error_events") else []
    latest = _extract_error_details(errors)
    safe_snap = {k: v for k, v in snap.items() if k not in {"players"}}
    players = [
        {"name": "[PLAYER_REDACTED]", "uid": "[UID_REDACTED]", "online": p.get("online"), "game_ready": p.get("game_ready")}
        for p in snap.get("players", [])
    ]
    category = str(user_input.get("category") or latest.get("category") or "Other")[:120]
    description = str(user_input.get("description") or "")[:8000]
    steps = str(user_input.get("steps") or "")[:12000]
    current_page = str(user_input.get("current_page") or "Diagnostics & Error Reports")[:120]
    privacy_checked = bool(user_input.get("privacy_checked"))
    combined_events = sorted(
        [*app_events[-80:], *realm_events[-80:]],
        key=lambda x: str(x.get("timestamp") or ""),
    )[-120:]
    seed = "|".join(str(x.get("timestamp", "")) + str(x.get("message", "")) for x in combined_events[-12:])
    diagnostic_id = hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()[:12].upper() if seed else "NO-ERRORS"
    report = {
        "schema": "sacred-zonai-realms-diagnostic-v2",
        "application": {
            "name": "TOTK All-In-One Save Converter / Sacred Zonai Realms",
            "version": APP_VERSION,
            "build": "7.30",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "diagnostic_id": diagnostic_id,
        },
        "user_report": {
            "category": category,
            "what_user_was_doing": description,
            "steps_to_reproduce": steps,
            "current_page": current_page,
            "privacy_checkbox_confirmed": privacy_checked,
        },
        "error": latest,
        "operation": {
            "category": category,
            "conversion_direction": "SAV → KTML" if "SAV" in category.upper() and "KTML" in category.upper() and category.upper().find("SAV") < category.upper().find("KTML") else ("KTML → SAV" if "KTML" in category.upper() and "SAV" in category.upper() else "Not specified"),
            "source_file_type": ".sav" if "SAV" in category.upper() else (".ktml" if "KTML" in category.upper() else "Not specified"),
            "destination_file_type": ".ktml" if "SAV" in category.upper() and "KTML" in category.upper() else (".sav" if "KTML" in category.upper() and "SAV" in category.upper() else "Not specified"),
            "template_type": "Not captured unless supplied by the operation",
            "validation_result": "See captured application error / user description when applicable",
            "result": "Failed" if (latest.get("captured") or realm_events) else "No captured failure",
        },
        "runtime": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "operating_system": platform.platform(),
            "windows_version": platform.win32_ver()[0] + " " + platform.win32_ver()[1] if os.name == "nt" else "Not running on Windows",
            "frozen_executable": bool(getattr(sys, "frozen", False)),
        },
        "configuration": {
            "hosting": safe_snap,
            "runtime_diagnostics": diag,
            "enabled_component": category,
        },
        "player_summary": players,
        "converter_application_errors": app_events[-30:],
        "realm_errors": realm_events[-30:],
        "recent_error_history": combined_events[-40:],
        # Backward-compatible key retained, but intentionally contains ERROR-ONLY realm data in v7.30.
        "recent_realm_console": "\n".join(f"[{x.get('time','')}] {x.get('severity','ERROR')} {x.get('message','')}" for x in realm_events[-30:]) or "No captured realm errors.",
        "privacy": {
            "automatic_sanitization": True,
            "notice": "Best-effort redaction was applied. Users must still review reports, QR content, and screenshots before sharing.",
            "never_intentionally_collected": ["passwords", "authentication tokens", "browser cookies", "Discord tokens", "GitHub credentials", "API keys", "private keys", "session tokens"],
        },
    }
    return _sanitize_value(report)


def _diagnostic_text(report: dict) -> str:
    a, u, e, op, rt = report["application"], report["user_report"], report["error"], report["operation"], report["runtime"]
    lines = [
        "TOTK ALL-IN-ONE SAVE CONVERTER / SACRED ZONAI REALMS",
        "COMBINED DIAGNOSTICS & ERROR REPORT",
        "=" * 72,
        f"Application Version: {a['version']}",
        f"Diagnostic ID: {a.get('diagnostic_id', '—')}",
        f"Generated: {a['generated_at']}",
        f"Category: {u['category']}",
        f"Current Page: {u['current_page']}",
        f"Operation: {op['conversion_direction'] if op['conversion_direction'] != 'Not specified' else u['category']}",
        f"Result: {op['result']}", "",
        "WHAT THE USER WAS DOING", "-" * 72,
        u['what_user_was_doing'] or "Not provided.", "",
        "STEPS TO REPRODUCE", "-" * 72,
        u['steps_to_reproduce'] or "Not provided.", "",
        "CONVERTER / APPLICATION ERROR", "-" * 72,
        f"Error Type: {e['exception_type'] or 'Not identified'}",
        f"Error Message: {e['message']}",
        f"Affected Module/File: {e['source_file'] or 'Not identified'}",
        f"Affected Function: {e['function'] or 'Not identified'}",
        f"Approximate Line: {e['line'] if e['line'] is not None else 'Not identified'}", "",
        "TRACEBACK / CAPTURED ERROR", "-" * 72,
        e['traceback'] or "No application traceback captured.", "",
        "ZONAI REALMS HOSTING ERRORS (ERROR-ONLY)", "-" * 72,
    ]
    realm = report.get("realm_errors") or []
    if realm:
        for item in realm:
            lines.append(f"[{item.get('time') or item.get('timestamp','')}] {item.get('severity','ERROR')} — {item.get('message','')}")
    else:
        lines.append("No captured Realm errors.")
    lines += ["", "RECENT ERROR HISTORY", "-" * 72]
    history = report.get("recent_error_history") or []
    if history:
        for item in history:
            lines.append(f"[{item.get('time') or item.get('timestamp','')}] {item.get('source','Application')} — {item.get('operation') or item.get('severity','ERROR')} — {item.get('message','')}")
    else:
        lines.append("No captured errors this session.")
    lines += [
        "", "SYSTEM / RUNTIME", "-" * 72,
        f"Python: {rt['python']} ({rt['python_implementation']})",
        f"OS: {rt['operating_system']}",
        f"Windows: {rt['windows_version']}",
        f"Frozen EXE: {rt['frozen_executable']}", "",
        "SANITIZED CONFIGURATION / INTERNAL DIAGNOSTICS", "-" * 72,
        json.dumps(report['configuration'], indent=2, ensure_ascii=False, default=str), "",
        "PRIVACY NOTICE", "-" * 72,
        report['privacy']['notice'],
        f"User privacy checkbox confirmed: {'YES' if u['privacy_checkbox_confirmed'] else 'NO'}", "",
        "HOW TO REPORT", "-" * 72,
        "1. Review this report and QR content for personal/private information before sharing.",
        "2. Explain exactly what you were doing when the problem occurred.",
        "3. If sharing a screenshot, inspect it for usernames, email addresses, tokens, private server details, and personal folders.",
        "4. Do not share passwords, authentication tokens, API keys, private keys, account credentials, session tokens, or cookies.",
        "5. Attach this report manually in KirbyMimi's Caravan Discord or on Legendary Savage Gamer's GitHub support channel.",
        "6. Sacred Zonai Realms does not automatically upload this report, QR payload, or any save file.",
    ]
    return "\n".join(str(x) for x in lines)


def _compact_qr_text(report: dict, max_bytes: int = 800) -> str:
    """Create a mobile-friendly error-only payload that stays within practical QR limits."""
    a, u, e, op = report["application"], report["user_report"], report["error"], report["operation"]
    traceback_lines = [x.strip() for x in str(e.get("traceback") or "").splitlines() if x.strip()]
    relevant_trace = traceback_lines[-8:]
    realm = (report.get("realm_errors") or [])[-5:]
    history = (report.get("recent_error_history") or [])[-6:]
    parts = [
        "TOTK ALL-IN-ONE SAVE CONVERTER v7.30",
        "MOBILE DIAGNOSTIC REPORT",
        f"ID: {a.get('diagnostic_id','—')}",
        f"Generated: {a.get('generated_at','—')}",
        f"Category: {u.get('category','Other')}",
        f"Operation: {op.get('conversion_direction','Not specified')}",
        f"Error Type: {e.get('exception_type') or 'Not identified'}",
        f"Error: {e.get('message') or 'No application error captured'}",
        f"Module: {e.get('source_file') or 'Not identified'}",
        f"Function: {e.get('function') or 'Not identified'}",
    ]
    if relevant_trace:
        parts += ["Traceback (relevant):", *relevant_trace]
    if realm:
        parts.append("Realm Errors:")
        parts.extend(f"[{x.get('time','')}] {x.get('severity','ERROR')}: {x.get('message','')}" for x in realm)
    if history:
        parts.append("Recent Errors:")
        parts.extend(f"[{x.get('time','')}] {x.get('source','App')}: {x.get('message','')}" for x in history)
    steps = str(u.get("steps_to_reproduce") or "").strip()
    if steps:
        parts.append("Steps: " + steps[:500])
    parts += [
        "Privacy: best-effort redaction applied; review before sharing.",
        "Never share passwords, tokens, API keys, private keys, session credentials, or cookies.",
    ]
    text = _redact_string("\n".join(parts))
    # QR scanners are most interoperable in byte/alphanumeric-friendly text.
    # Keep the full Unicode report in TXT/JSON; normalize only the compact mobile QR payload.
    import unicodedata
    text = text.replace("→", "->").replace("—", "-").replace("•", "-").replace("←", "<-")
    text = unicodedata.normalize("NFKD", text).encode("ascii", errors="replace").decode("ascii")
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    # Preserve the beginning and privacy footer; shrink by bytes without breaking UTF-8.
    footer = "\n[QR COMPACTED - full details remain in TXT/JSON]\nPrivacy: review before sharing."
    allowance = max(256, max_bytes - len(footer.encode("utf-8")))
    clipped = data[:allowance]
    while True:
        try:
            body = clipped.decode("utf-8")
            break
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return body.rstrip() + footer


def _qr_png_bytes(payload: str) -> bytes:
    """Generate a shareable QR image. PNG is preferred; SVG is a no-Pillow fallback."""
    import io
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=6, border=4)
    qr.add_data(payload)
    qr.make(fit=True)
    buf = io.BytesIO()
    try:
        image = qr.make_image(fill_color="black", back_color="white")
        image.save(buf, format="PNG")
        return buf.getvalue()
    except (ImportError, ModuleNotFoundError):
        # Running the source tree should still produce a QR even if Pillow was
        # not installed. qrcode's SVG factory is pure Python and browser-safe.
        from qrcode.image.svg import SvgPathImage
        buf = io.BytesIO()
        image = qr.make_image(image_factory=SvgPathImage)
        image.save(buf)
        return buf.getvalue()


def build_error_report(user_input=None) -> bytes:
    """Backward-compatible TXT report builder used by the existing endpoint."""
    return _diagnostic_text(_diagnostic_context(user_input)).encode("utf-8")


def resource_path(relative: str | Path) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def _find_value(doc, path, default=None):
    if not isinstance(doc, dict):
        return default
    for section in doc.values():
        if isinstance(section, dict) and path in section:
            return section[path]
    return default


def _locate(doc, path):
    if not isinstance(doc, dict):
        return None
    for sec, section in doc.items():
        if isinstance(section, dict) and path in section:
            return sec, section
    return None


def _set_or_add(doc, section_name, path, value):
    loc = _locate(doc, path)
    if loc:
        loc[1][path] = value
        return
    section = doc.setdefault(section_name, {})
    if not isinstance(section, dict):
        raise ValueError(f"KTML section {section_name!r} is not writable")
    section[path] = value


class AppState:
    def __init__(self):
        self.lock = threading.RLock()
        self.doc = None
        self.filename = None
        self.loaded_at = None

    def summary(self):
        with self.lock:
            pos = None
            if self.doc:
                pos = _find_value(self.doc, "World_PlayerPos", None) or _find_value(self.doc, "PlayerStatus.SavePos", None)
            return {
                "app": APP_NAME,
                "version": APP_VERSION,
                "loaded": bool(self.doc),
                "filename": self.filename,
                "position": pos if isinstance(pos, dict) else None,
            }


STATE = AppState()
TOKEN = secrets.token_urlsafe(24)


# ---------------------------------------------------------------------------
# Persistent personalization storage
# ---------------------------------------------------------------------------
WALLPAPER_SLOTS = {"app_header", "home", "converter", "client", "hosting", "recovery", "multiplayer", "multiplayer_hero", "credits", "support"}
SOUND_SLOTS = {"tab", "success", "warning", "error"}
WALLPAPER_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
SOUND_EXTS = {".mp3", ".wav", ".ogg"}
FIT_MODES = {"fill", "fit", "stretch", "center"}

def _user_data_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "SacredZonaiRealms"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sacred-zonai-realms"

class PersonalizationStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.root = _user_data_root()
        self.media_root = self.root / "customization"
        self.wallpaper_root = self.media_root / "wallpapers"
        self.sound_root = self.media_root / "sounds"
        self.config_path = self.root / "settings.json"
        self.root.mkdir(parents=True, exist_ok=True)
        self.wallpaper_root.mkdir(parents=True, exist_ok=True)
        self.sound_root.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    @staticmethod
    def defaults():
        return {
            "wallpapers": {},
            "wallpaper_fit": {slot: "fill" for slot in WALLPAPER_SLOTS},
            "sounds": {},
            "audio": {"volume": 28, "mute": False},
        }

    def _load(self):
        data = self.defaults()
        try:
            if self.config_path.is_file():
                saved = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    for key in ("wallpapers", "wallpaper_fit", "sounds", "audio"):
                        if isinstance(saved.get(key), dict):
                            data[key].update(saved[key])
        except Exception as exc:
            record_app_error(f"Could not load personalization settings: {exc}")
        return data

    def save(self):
        with self.lock:
            tmp = self.config_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.config_path)

    def _public_url(self, kind: str, name: str | None):
        if not name:
            return None
        return f"/user-media/{kind}/{urllib.parse.quote(name)}"

    def snapshot(self):
        with self.lock:
            wallpapers = {}
            for slot, name in list(self.data.get("wallpapers", {}).items()):
                path = self.wallpaper_root / Path(name).name
                if slot in WALLPAPER_SLOTS and path.is_file():
                    wallpapers[slot] = {"name": path.name, "url": self._public_url("wallpapers", path.name)}
            sounds = {}
            for slot, name in list(self.data.get("sounds", {}).items()):
                path = self.sound_root / Path(name).name
                if slot in SOUND_SLOTS and path.is_file():
                    sounds[slot] = {"name": path.name, "url": self._public_url("sounds", path.name)}
            fits = {slot: (self.data.get("wallpaper_fit", {}).get(slot) if self.data.get("wallpaper_fit", {}).get(slot) in FIT_MODES else "fill") for slot in WALLPAPER_SLOTS}
            audio = self.data.get("audio", {})
            return {
                "wallpapers": wallpapers,
                "wallpaper_fit": fits,
                "sounds": sounds,
                "audio": {
                    "volume": max(0, min(100, int(audio.get("volume", 28) or 0))),
                    "mute": bool(audio.get("mute", False)),
                },
                "config_path": str(self.config_path),
            }

    def save_media(self, kind: str, slot: str, filename: str, data: bytes):
        slot = str(slot).lower().strip()
        kind = str(kind).lower().strip()
        if kind == "wallpapers":
            allowed_slots, allowed_exts, root, max_bytes = WALLPAPER_SLOTS, WALLPAPER_EXTS, self.wallpaper_root, 64 * 1024 * 1024
        elif kind == "sounds":
            allowed_slots, allowed_exts, root, max_bytes = SOUND_SLOTS, SOUND_EXTS, self.sound_root, 32 * 1024 * 1024
        else:
            raise ValueError("Unknown personalization media type")
        if slot not in allowed_slots:
            raise ValueError(f"Unsupported {kind} slot: {slot}")
        ext = Path(filename or "").suffix.lower()
        if ext not in allowed_exts:
            raise ValueError(f"Unsupported file type {ext or '(none)'}")
        if not data:
            raise ValueError("The selected file is empty")
        if len(data) > max_bytes:
            raise ValueError(f"The selected file is too large ({len(data):,} bytes)")
        # One file per slot. Remove only older files belonging to this slot.
        for old in root.glob(slot + ".*"):
            try:
                old.unlink()
            except OSError:
                pass
        dest = root / f"{slot}{ext}"
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        with self.lock:
            self.data[kind][slot] = dest.name
            self.save()
        return self.snapshot()

    def set_preferences(self, payload: dict):
        with self.lock:
            if "volume" in payload:
                self.data["audio"]["volume"] = max(0, min(100, int(payload.get("volume", 28))))
            if "mute" in payload:
                self.data["audio"]["mute"] = bool(payload.get("mute"))
            slot = payload.get("wallpaper_slot")
            fit = payload.get("wallpaper_fit")
            if slot is not None or fit is not None:
                slot = str(slot or "").lower().strip()
                fit = str(fit or "").lower().strip()
                if slot not in WALLPAPER_SLOTS:
                    raise ValueError("Unknown wallpaper slot")
                if fit not in FIT_MODES:
                    raise ValueError("Unknown wallpaper fit mode")
                self.data["wallpaper_fit"][slot] = fit
            self.save()
        return self.snapshot()

    def reset(self, kind: str, slot: str | None = None):
        kind = str(kind).lower().strip()
        with self.lock:
            if kind == "wallpaper":
                slots = [slot] if slot else list(WALLPAPER_SLOTS)
                for s in slots:
                    if s not in WALLPAPER_SLOTS:
                        raise ValueError("Unknown wallpaper slot")
                    old_name = self.data["wallpapers"].pop(s, None)
                    if old_name:
                        try: (self.wallpaper_root / Path(old_name).name).unlink(missing_ok=True)
                        except Exception: pass
                    self.data["wallpaper_fit"][s] = "fill"
            elif kind == "sound":
                slots = [slot] if slot else list(SOUND_SLOTS)
                for s in slots:
                    if s not in SOUND_SLOTS:
                        raise ValueError("Unknown sound slot")
                    old_name = self.data["sounds"].pop(s, None)
                    if old_name:
                        try: (self.sound_root / Path(old_name).name).unlink(missing_ok=True)
                        except Exception: pass
            else:
                raise ValueError("Reset kind must be wallpaper or sound")
            self.save()
        return self.snapshot()

CUSTOMIZATION = PersonalizationStore()


class Handler(BaseHTTPRequestHandler):
    server_version = "SacredZonaiRealms/7.30"

    def log_message(self, fmt, *args):
        # Keep the console quiet; the UI is the primary interface.
        pass

    def _authorized(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        supplied = self.headers.get("X-Sacred-Token") or (query.get("token") or [""])[0]
        return secrets.compare_digest(str(supplied), TOKEN)

    def _send_json(self, payload, code=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, filename: str, content_type: str = "application/octet-stream", code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self, max_bytes=64 * 1024 * 1024):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > int(max_bytes):
            raise ValueError(f"Request is too large ({length:,} bytes; limit {int(max_bytes):,})")
        return self.rfile.read(length)

    def _stream_body_to_temp(self, suffix=".bin", max_bytes=1024 * 1024 * 1024):
        import tempfile
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > int(max_bytes):
            raise ValueError(f"Upload is too large ({length:,} bytes; limit {int(max_bytes):,})")
        fd, path = tempfile.mkstemp(prefix="sacred-realms-upload-", suffix=suffix)
        try:
            remaining = length
            with os.fdopen(fd, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("Upload ended before Content-Length bytes were received")
                    f.write(chunk)
                    remaining -= len(chunk)
            return Path(path)
        except Exception:
            try: os.close(fd)
            except Exception: pass
            try: os.remove(path)
            except OSError: pass
            raise

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            if not self._authorized():
                return self._send_json({"ok": False, "error": "Unauthorized"}, 403)
            if path == "/api/state":
                return self._send_json({"ok": True, **STATE.summary()})
            if path == "/api/hosting/state":
                return self._send_json({"ok": True, **HOSTING.snapshot()})
            if path == "/api/hosting/logs":
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    tail = int((query.get("tail") or [250])[0])
                except Exception:
                    tail = 250
                return self._send_json({"ok": True, "logs": HOSTING.recent_logs(tail), **HOSTING.snapshot()})
            if path == "/api/hosting/diagnostics":
                query = urllib.parse.parse_qs(parsed.query)
                selected = (query.get("ip") or [None])[0]
                live_diag = HOSTING.diagnostics(selected)
                _store_runtime_diagnostics(live_diag)
                return self._send_json({"ok": True, **live_diag})
            if path == "/api/customization/settings":
                return self._send_json({"ok": True, **CUSTOMIZATION.snapshot()})
            if path == "/api/hosting/download-main-save":
                query = urllib.parse.parse_qs(parsed.query)
                fmt = (query.get("format") or ["ktml"])[0]
                data, name, ctype = HOSTING.download_main_save(fmt)
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(data)
            if path == "/api/hosting/download-player":
                query = urllib.parse.parse_qs(parsed.query)
                uid = (query.get("uid") or [""])[0]
                fmt = (query.get("format") or ["ktml"])[0]
                data, name, ctype = HOSTING.download_player(uid, fmt)
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(data)
            if path == "/api/diagnostics/report":
                data = build_error_report()
                name = f"TOTK_Save_Converter_Error_v7.30_{datetime.now().strftime('%Y-%m-%d_%H%M')}.txt"
                return self._send_bytes(data, name, "text/plain; charset=utf-8")
            if path == "/api/diagnostics/history":
                report = _diagnostic_context({})
                return self._send_json({"ok": True, "history": report.get("recent_error_history", []), "diagnostic_id": report["application"].get("diagnostic_id")})
            if path == "/api/download-ktml":
                with STATE.lock:
                    if not STATE.doc:
                        return self._send_json({"ok": False, "error": "No KTML save loaded"}, 400)
                    validate_ktml_types(STATE.doc)
                    data = serialize_ktml(STATE.doc).encode("utf-8")
                    name = Path(STATE.filename or "client-save").stem + "-hylian-edited.ktml"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(data)
            return self._send_json({"ok": False, "error": "Unknown API endpoint"}, 404)

        # User-selected wallpaper/audio media lives outside the packaged EXE.
        if path.startswith("/user-media/"):
            parts = [urllib.parse.unquote(x) for x in path.split("/") if x]
            if len(parts) != 3 or parts[0] != "user-media" or parts[1] not in {"wallpapers", "sounds"}:
                self.send_error(404); return
            root = CUSTOMIZATION.wallpaper_root if parts[1] == "wallpapers" else CUSTOMIZATION.sound_root
            name = Path(parts[2]).name
            full = (root / name).resolve()
            try:
                full.relative_to(root.resolve())
            except Exception:
                self.send_error(403); return
            if not full.is_file():
                self.send_error(404); return
            ctype = mimetypes.guess_type(str(full))[0] or "application/octet-stream"
            data = full.read_bytes()
            self.send_response(200); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data); return

        # Static app files.
        if path in ("", "/"):
            path = "/webui/index.html"
        rel = path.lstrip("/")
        full = resource_path(rel).resolve()
        root = resource_path(".").resolve()
        try:
            full.relative_to(root)
        except Exception:
            self.send_error(403)
            return
        if not full.is_file():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(str(full))[0] or "application/octet-stream"
        data = full.read_bytes()
        # Inject token into the root HTML only.
        if full.name == "index.html" and "webui" in full.parts:
            text = data.decode("utf-8").replace("__SACRED_TOKEN__", TOKEN).replace("__APP_VERSION__", APP_VERSION)
            data = text.encode("utf-8")
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store" if full.suffix in {".html", ".js", ".css"} else "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._authorized():
            return self._send_json({"ok": False, "error": "Unauthorized"}, 403)
        try:
            if path == "/api/customization/upload":
                query = urllib.parse.parse_qs(parsed.query)
                kind = (query.get("kind") or [""])[0]
                slot = (query.get("slot") or [""])[0]
                filename = urllib.parse.unquote(self.headers.get("X-Filename") or "")
                limit = 64 * 1024 * 1024 if kind == "wallpapers" else 32 * 1024 * 1024
                snap = CUSTOMIZATION.save_media(kind, slot, filename, self._read_body(max_bytes=limit))
                return self._send_json({"ok": True, "message": f"Saved custom {kind[:-1] if kind.endswith('s') else kind} for {slot}.", **snap})
            if path == "/api/customization/preferences":
                payload = json.loads(self._read_body() or b"{}")
                return self._send_json({"ok": True, **CUSTOMIZATION.set_preferences(payload)})
            if path == "/api/customization/reset":
                payload = json.loads(self._read_body() or b"{}")
                return self._send_json({"ok": True, **CUSTOMIZATION.reset(payload.get("kind", ""), payload.get("slot"))})
            if path == "/api/convert/ktml-to-sav":
                raw = self._read_body(max_bytes=64 * 1024 * 1024)
                text = raw.decode("utf-8", errors="strict")
                data = ktml_to_progress_sav(text)
                return self._send_bytes(data, "progress.sav")
            if path == "/api/convert/sav-to-ktml":
                raw = self._read_body(max_bytes=64 * 1024 * 1024)
                text = progress_sav_to_ktml(raw)
                return self._send_bytes(text.encode("utf-8"), "converted-save.ktml", "text/plain; charset=utf-8")
            if path == "/api/hosting/repair-runtime":
                detail = HOSTING.repair_runtime()
                return self._send_json({"ok": True, "message": detail, **HOSTING.snapshot()})
            if path == "/api/window/resize":
                payload = json.loads(self._read_body() or b"{}")
                w, h = int(payload.get("width", 1480)), int(payload.get("height", 920))
                w=max(980,min(w,3840)); h=max(680,min(h,2160))
                with WEBVIEW_LOCK:
                    win = WEBVIEW_WINDOW
                if win is None:
                    return self._send_json({"ok": True, "supported": False, "message": "Window resize is available in the native WebView build. The browser/app-mode fallback controls its own window size."})
                win.resize(w, h)
                return self._send_json({"ok": True, "supported": True, "width": w, "height": h, "message": f"Window resized to {w} × {h}."})
            if path == "/api/load-ktml":
                raw = self._read_body()
                text = raw.decode("utf-8", errors="strict")
                doc = parse_ktml(text)
                validate_ktml_types(doc)
                filename = self.headers.get("X-Filename") or "client-save.ktml"
                with STATE.lock:
                    STATE.doc = doc
                    STATE.filename = Path(filename).name
                    STATE.loaded_at = time.time()
                return self._send_json({"ok": True, **STATE.summary()})

            if path == "/api/load-default":
                candidates = [resource_path("defaultClientSave.ktml"), resource_path("server_bundle/Resources/SaveServer/defaultClientSave.ktml")]
                p = next((x for x in candidates if x.exists()), None)
                if not p:
                    raise FileNotFoundError("Bundled defaultClientSave.ktml was not found")
                doc = parse_ktml(p.read_text(encoding="utf-8", errors="strict"))
                with STATE.lock:
                    STATE.doc = doc
                    STATE.filename = p.name
                    STATE.loaded_at = time.time()
                return self._send_json({"ok": True, **STATE.summary()})

            if path == "/api/apply-position":
                payload = json.loads(self._read_body() or b"{}")
                vec = {a: float(payload[a]) for a in ("x", "y", "z")}
                with STATE.lock:
                    if not STATE.doc:
                        raise ValueError("Load a KTML save first")
                    # Write both verified player position vectors so client and world state agree.
                    _set_or_add(STATE.doc, "Vector3", "World_PlayerPos", dict(vec))
                    _set_or_add(STATE.doc, "Vector3", "PlayerStatus.SavePos", dict(vec))
                    _set_or_add(STATE.doc, "Float", "PlayerStatus.SavePosRadY", float(payload.get("radY", 0.0)))
                    if payload.get("forceMainField", True):
                        _set_or_add(STATE.doc, "String64", "Sequence_CurrentBanc", "MainField")
                    validate_ktml_types(STATE.doc)
                return self._send_json({"ok": True, "position": vec, "message": "World_PlayerPos and PlayerStatus.SavePos synchronized in memory."})

            if path == "/api/hosting/start":
                payload = json.loads(self._read_body() or b"{}")
                snap = HOSTING.start(
                    engine=payload.get("engine", "docker"),
                    duration_hours=payload.get("duration_hours", 2),
                    pvp_enabled=payload.get("pvp_enabled", True),
                    upload_bps=payload.get("upload_bps", 1_000_000),
                    preferred_port=payload.get("preferred_port") or None,
                    advertised_ip=payload.get("advertised_ip") or None,
                )
                return self._send_json({"ok": True, **snap})
            if path == "/api/hosting/pause":
                return self._send_json({"ok": True, **HOSTING.pause()})
            if path == "/api/hosting/resume":
                return self._send_json({"ok": True, **HOSTING.resume()})
            if path == "/api/hosting/restart":
                return self._send_json({"ok": True, **HOSTING.restart()})
            if path == "/api/hosting/close":
                return self._send_json({"ok": True, **HOSTING.close()})
            if path == "/api/hosting/command":
                payload = json.loads(self._read_body() or b"{}")
                HOSTING.send_command(payload.get("command", ""))
                return self._send_json({"ok": True, "logs": HOSTING.recent_logs(250)})
            if path == "/api/hosting/firewall":
                name = HOSTING.allow_firewall_port()
                return self._send_json({"ok": True, "message": f"Windows Firewall elevation requested for {name}."})
            if path == "/api/hosting/open-folder":
                HOSTING.open_workspace()
                return self._send_json({"ok": True})
            if path == "/api/hosting/replace-main-save":
                filename = urllib.parse.unquote(self.headers.get("X-Filename") or "server-save.ktml")
                result = HOSTING.replace_main_save(self._read_body(), filename)
                return self._send_json({"ok": True, **result})
            if path == "/api/hosting/merge-romfs":
                filename = urllib.parse.unquote(self.headers.get("X-Filename") or "romfs.zip")
                temp = self._stream_body_to_temp(suffix=".zip", max_bytes=1024 * 1024 * 1024)
                try:
                    count = HOSTING.merge_romfs_file(temp, filename)
                finally:
                    try: temp.unlink(missing_ok=True)
                    except Exception: pass
                return self._send_json({"ok": True, "files_written": count})
            if path == "/api/hosting/reset-romfs":
                result = HOSTING.reset_romfs_to_original()
                return self._send_json({"ok": True, **result})
            if path == "/api/hosting/kick-player":
                payload = json.loads(self._read_body() or b"{}")
                HOSTING.kick_player(str(payload.get("uid", "")))
                return self._send_json({"ok": True})
            if path == "/api/hosting/ban-player":
                payload = json.loads(self._read_body() or b"{}")
                HOSTING.ban_player(str(payload.get("uid", "")), bool(payload.get("include_ip", True)))
                return self._send_json({"ok": True})
            if path == "/api/hosting/replace-player":
                query = urllib.parse.parse_qs(parsed.query)
                uid = (query.get("uid") or [""])[0]
                filename = urllib.parse.unquote(self.headers.get("X-Filename") or "player.ktml")
                result = HOSTING.replace_player(uid, self._read_body(), filename)
                return self._send_json({"ok": True, **result})
            if path == "/api/hosting/delete-player":
                payload = json.loads(self._read_body() or b"{}")
                deleted = HOSTING.delete_player_save(str(payload.get("uid", "")))
                return self._send_json({"ok": True, "deleted": deleted})
            if path == "/api/hosting/pull-image":
                detail = HOSTING.pull_latest_java_image()
                return self._send_json({"ok": True, "message": "Docker Java image refreshed.", "detail": detail[-2000:]})
            if path == "/api/diagnostics/build":
                payload = json.loads(self._read_body(max_bytes=512 * 1024) or b"{}")
                report = _diagnostic_context(payload)
                return self._send_json({"ok": True, "report": report, "text": _diagnostic_text(report)})
            if path == "/api/diagnostics/qr":
                import base64
                payload = json.loads(self._read_body(max_bytes=512 * 1024) or b"{}")
                report = _diagnostic_context(payload)
                qr_text = _compact_qr_text(report)
                image_bytes = _qr_png_bytes(qr_text)
                image_mime = "image/svg+xml" if image_bytes.lstrip().startswith(b"<") else "image/png"
                return self._send_json({
                    "ok": True,
                    "diagnostic_id": report["application"].get("diagnostic_id"),
                    "payload": qr_text,
                    "payload_bytes": len(qr_text.encode("utf-8")),
                    "image_base64": base64.b64encode(image_bytes).decode("ascii"),
                    "image_mime": image_mime,
                    "generated_at": report["application"].get("generated_at"),
                })
            if path == "/api/diagnostics/clear-history":
                with ERROR_LOCK:
                    APP_ERRORS.clear()
                    APP_ERROR_EVENTS.clear()
                if hasattr(HOSTING, "clear_error_history"):
                    HOSTING.clear_error_history()
                return self._send_json({"ok": True, "message": "Temporary diagnostic error history cleared. Save, realm, backup, and configuration data were not changed."})
            if path == "/api/diagnostics/export":
                payload = json.loads(self._read_body(max_bytes=512 * 1024) or b"{}")
                fmt = str(payload.pop("format", "txt")).lower()
                if fmt not in {"txt", "json"}:
                    raise ValueError("Diagnostic export format must be txt or json")
                report = _diagnostic_context(payload)
                stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
                if fmt == "json":
                    data = json.dumps(report, indent=2, ensure_ascii=False, default=str).encode("utf-8")
                    return self._send_bytes(data, f"TOTK_Save_Converter_Error_v7.30_{stamp}.json", "application/json; charset=utf-8")
                data = _diagnostic_text(report).encode("utf-8")
                return self._send_bytes(data, f"TOTK_Save_Converter_Error_v7.30_{stamp}.txt", "text/plain; charset=utf-8")
            if path == "/api/diagnostics/client-error":
                payload = json.loads(self._read_body() or b"{}")
                record_app_error("Browser UI: " + str(payload.get("message") or "Unknown UI error") + (f" @ {payload.get('source')}:{payload.get('line')}" if payload.get("source") else ""), source="Browser UI", operation="UI Event")
                return self._send_json({"ok": True})
            if path == "/api/launch-classic":
                launch_classic()
                return self._send_json({"ok": True, "message": "Classic functional tools launched."})

            if path == "/api/shutdown":
                self._send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return

            return self._send_json({"ok": False, "error": "Unknown API endpoint"}, 404)
        except Exception as exc:
            source, operation = _request_error_context(self.path)
            record_app_error(f"{self.command} {self.path}: {exc}\n{traceback.format_exc(limit=6)}", source=source, operation=operation)
            return self._send_json({"ok": False, "error": str(exc)}, 400)


def launch_classic():
    """Launch the proven classic Tkinter compatibility tools in a separate process."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--classic"]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--classic"]
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(cmd, cwd=str(resource_path(".")), **kwargs)


def run_classic():
    from totk_save_converter_all_in_one import App
    app = App()
    app.mainloop()


def _find_edge():
    if os.name != "nt":
        return None
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    return next((p for p in candidates if p.is_file()), None)


def open_app_window(url):
    global WEBVIEW_WINDOW
    """Open the modern UI in a native WebView2 window when available.

    pywebview is preferred over Tkinter and over a loose browser tab. On Windows
    it uses the Edge/WebView2 runtime already present on most current systems.
    If WebView2/pywebview cannot start, Sacred Realms falls back to Edge/Chrome
    app mode and finally to the user's default browser.

    Returns True only when a blocking pywebview window was used.
    """
    try:
        import webview
        # Marc Robledo's integrated save editor exports the edited progress.sav
        # through a normal browser download. pywebview disables downloads by
        # default, so explicitly allow them before the WebView2 window starts.
        try:
            webview.settings["ALLOW_DOWNLOADS"] = True
        except Exception:
            pass
        WEBVIEW_WINDOW = webview.create_window(
            f"{APP_NAME} — {APP_VERSION}",
            url,
            width=1600, height=960,
            min_size=(980, 680),
            background_color="#020807",
        )
        webview.start(debug=False)
        return True
    except Exception as exc:
        print(f"Native WebView unavailable, using browser app mode: {exc}")

    exe = _find_edge()
    if exe:
        try:
            subprocess.Popen([str(exe), f"--app={url}", "--window-size=1600,960", "--disable-features=Translate"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return False
        except Exception:
            pass
    webbrowser.open(url, new=1)
    return False

def main():
    if "--classic" in sys.argv:
        return run_classic()
    server = ThreadingHTTPServer((HOST, 0), Handler)
    port = server.server_address[1]
    url = f"http://{HOST}:{port}/?token={urllib.parse.quote(TOKEN)}"
    threading.Thread(target=server.serve_forever, name="SacredRealmsWeb", daemon=True).start()
    print(f"{APP_NAME} {APP_VERSION}")
    print(f"Modern Hylian UI: {url}")
    try:
        used_native_webview = open_app_window(url)
        if used_native_webview:
            return
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            HOSTING.shutdown()
        except Exception:
            pass
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
