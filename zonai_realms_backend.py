"""Headless hosting backend for Sacred Realms — Zonai Realms Hosting.

This module preserves Sacred Realms' proven bundled Kirbymimi runtime layout while
adopting several reliability ideas from Wesley Da Man's Zonai Hosting project:
per-realm lifecycle locking, persistent host state outside a one-file PyInstaller
bundle, authoritative live `list` roster polling, atomic player-save replacement,
port cooldown tracking, Docker preflight/recreate helpers, and explicit runtime
repair before every start.

It intentionally does not replace the existing Tkinter ServerManagerFrame.  The
classic console stays available as a compatibility/fallback UI; this backend is
used by the modern Hylian web interface.
"""
from __future__ import annotations

import json
import os
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from totk_converter_core import (
    convert_ktml_to_progress,
    convert_progress_to_ktml,
    parse_ktml,
    serialize_ktml,
    normalize_ktml_types,
    validate_ktml_types,
)
import wesley_zonai_save_converter as zonai_save_codec

PORT_MIN = 10000
PORT_MAX = 12000
DEFAULT_UPLOAD_BPS = 10_000_000
RECOMMENDED_UPLOAD_BPS = 1_000_000
DEFAULT_DOCKER_IMAGE = "wesleyhellewell/totk_online_multiplayer_server_test:latest"
WESLEY_CONTAINER_PORT = 10014
KNOWN_BAD_DOCKER_IMAGE = "wesleyhellewell/totk_online_multiplayer_server"
FRIENDLY_REALM_NAME = "Traveler's In Time"
ROOM_NUMBER_MIN = 1
ROOM_NUMBER_MAX = 1000

CONNECT_RE = re.compile(r"(?P<name>[^\[\]:]+?)\s+connected(?:\s+-\s+save UID:\s*(?P<uid>\S+))?\s*$", re.I)
LEAVE_RE = re.compile(r"(?P<name>[^\[\]:]+?)\s+(?:left|disconnected)\s*$", re.I)
LIST_LINE_RE = re.compile(r"^-\s*(?P<name>.+?)\s*\(save uid=(?P<uid>\S+)\)\s*$", re.I)
LIST_EMPTY_RE = re.compile(r"^No players connected\.\s*$", re.I)
MAINFIELD_RE = re.compile(r"Adding client .* to manager MainField", re.I)


def resource_path(relative: str | Path) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def app_data_root() -> Path:
    """Mutable data must never live inside PyInstaller's temporary _MEIPASS."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "SacredRealmsOfTheKingdom" / "ZonaiRealms"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "SacredRealmsOfTheKingdom" / "ZonaiRealms"


def _run_hidden(args, **kwargs):
    if os.name == "nt" and "creationflags" not in kwargs:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(args, **kwargs)


class ZonaiRealmBackend:
    def __init__(self):
        self.lock = threading.RLock()
        self.lifecycle_lock = threading.RLock()
        self.data_root = app_data_root()
        self.rooms_dir = self.data_root / "Rooms"
        self.backups_dir = self.data_root / "Backups"
        self.port_history_path = self.data_root / "port_history.json"
        self.session_path = self.data_root / "active_realm.json"
        self.room_counter_path = self.data_root / "room_counter.json"
        self.rooms_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)

        self.workspace: Path | None = None
        self.room_name: str | None = None
        self.realm_display_name: str | None = None
        self.room_number: int | None = None
        self.port: int | None = None
        self.engine = "docker"
        self.container_name: str | None = None
        self.docker_image = DEFAULT_DOCKER_IMAGE
        self.proc: subprocess.Popen | None = None
        self.log_proc: subprocess.Popen | None = None
        self.status = "offline"
        self.connection_state = "No realm open"
        self.deadline: datetime | None = None
        self.duration_mode = "timed"
        self.duration_hours: float | None = 2.0
        self.realm_started_at: datetime | None = None
        self.uptime_accumulated_seconds = 0
        self.paused_remaining_seconds: int | None = None
        self.realm_paused = False
        self.pvp_enabled = True
        self.upload_bps = RECOMMENDED_UPLOAD_BPS
        self.advertised_ip: str | None = None
        self.logs = deque(maxlen=900)
        self.error_events = deque(maxlen=160)
        self.players: dict[str, dict[str, Any]] = {}
        self.nickname_to_uid: dict[str, str] = {}
        self.game_ready_names: set[str] = set()
        self.pending_connect_name: str | None = None
        self.last_roster_poll = 0.0
        self._stop_event = threading.Event()
        self._reader_threads: list[threading.Thread] = []
        self._last_known_user_mtimes: dict[str, float] = {}

        self._load_session_metadata()
        self._maintenance_thread = threading.Thread(target=self._maintenance_loop, name="ZonaiRealmMaintenance", daemon=True)
        self._maintenance_thread.start()

    # ------------------------------ generic state / logging
    @staticmethod
    def _classify_error_line(text: str):
        """Return a diagnostic severity for confirmed failure/error console lines only."""
        clean = str(text or "").strip()
        low = clean.lower()
        if not clean:
            return None
        # Avoid common negative/benign phrases that contain the word "error".
        if any(x in low for x in ("0 errors", "no errors", "no error", "without error", "error count: 0")):
            return None
        if re.search(r"\b(fatal|critical)\b", low):
            return "FATAL" if "fatal" in low else "CRITICAL"
        if re.search(r"\b(error|exception|traceback|unhandled exception|runtime exception)\b", low):
            return "EXCEPTION" if "exception" in low or "traceback" in low else "ERROR"
        if re.search(r"\b(crash(?:ed|ing)?|failed|failure)\b", low):
            return "ERROR"
        if any(x in low for x in (
            "connection refused", "connection failure", "server failure", "container failure",
            "malformed save", "parsing failure", "parse failure", "could not start",
            "unable to start", "launch error", "save parsing"
        )):
            return "ERROR"
        return None

    def _log(self, text: str):
        clean = str(text).rstrip("\r\n")
        if not clean:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        severity = self._classify_error_line(clean)
        with self.lock:
            self.logs.append(f"[{stamp}] {clean}")
            if severity:
                self.error_events.append({
                    "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "time": stamp,
                    "source": "Zonai Realms Hosting",
                    "severity": severity,
                    "message": clean,
                })
        self._parse_log_line(clean)

    def recent_logs(self, tail: int = 250) -> str:
        with self.lock:
            return "\n".join(list(self.logs)[-max(1, min(int(tail), 900)):])

    def recent_error_events(self, tail: int = 80) -> list[dict[str, Any]]:
        """Return only error/crash-related realm console events for diagnostics."""
        with self.lock:
            return [dict(x) for x in list(self.error_events)[-max(1, min(int(tail), 160)):]]

    def clear_error_history(self):
        """Clear temporary diagnostics only; live console/server/save state is untouched."""
        with self.lock:
            self.error_events.clear()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            remaining = None
            if self.deadline:
                remaining = max(0, int((self.deadline - datetime.now()).total_seconds()))
            elif self.realm_paused and self.paused_remaining_seconds is not None:
                remaining = max(0, int(self.paused_remaining_seconds))
            uptime = None
            if self.duration_mode == "unlimited":
                uptime = int(self.uptime_accumulated_seconds)
                if self.realm_started_at and self.is_running():
                    uptime += max(0, int((datetime.now(timezone.utc) - self.realm_started_at).total_seconds()))
            players = sorted(self.players.values(), key=lambda p: (not bool(p.get("online")), str(p.get("name", "")).lower()))
            return {
                "status": self.status,
                "connection_state": self.connection_state,
                "room_name": self.room_name,
                "realm_display_name": self.realm_display_name or self.room_name,
                "room_number": self.room_number,
                "room_number_display": f"#{self.room_number:03d}" if self.room_number else None,
                "duration_mode": self.duration_mode,
                "duration_hours": self.duration_hours,
                "realm_started_at": self.realm_started_at.isoformat() if self.realm_started_at else None,
                "uptime_seconds": uptime,
                "auto_stop": self.duration_mode != "unlimited",
                "workspace": str(self.workspace) if self.workspace else None,
                "port": self.port,
                "engine": self.engine,
                "container_name": self.container_name,
                "paused": self.realm_paused,
                "running": self.is_running(),
                "remaining_seconds": remaining,
                "pvp_enabled": self.pvp_enabled,
                "upload_bps": self.upload_bps,
                "advertised_ip": self.advertised_ip,
                "players": players,
                "ips": self.detect_ipv4_addresses(),
                "recommended_upload_bps": RECOMMENDED_UPLOAD_BPS,
                "default_upload_bps": DEFAULT_UPLOAD_BPS,
                "docker_image": self.docker_image,
                "romfs_modified": bool(self.workspace and (self.workspace / ".sacred_romfs_modified").exists()),
                "data_root": str(self.data_root),
            }

    # ------------------------------ durable session metadata
    def _save_session_metadata(self):
        data = {
            "workspace": str(self.workspace) if self.workspace else None,
            "room_name": self.room_name,
            "realm_display_name": self.realm_display_name,
            "room_number": self.room_number,
            "duration_mode": self.duration_mode,
            "duration_hours": self.duration_hours,
            "realm_started_at": self.realm_started_at.isoformat() if self.realm_started_at else None,
            "uptime_accumulated_seconds": self.uptime_accumulated_seconds,
            "port": self.port,
            "engine": self.engine,
            "container_name": self.container_name,
            "pvp_enabled": self.pvp_enabled,
            "upload_bps": self.upload_bps,
            "advertised_ip": self.advertised_ip,
            "paused": self.realm_paused,
            "paused_remaining_seconds": self.paused_remaining_seconds,
            "deadline": self.deadline.isoformat() if self.deadline else None,
        }
        try:
            self.data_root.mkdir(parents=True, exist_ok=True)
            tmp = self.session_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self.session_path)
        except Exception:
            pass

    def _load_session_metadata(self):
        if not self.session_path.exists():
            return
        try:
            data = json.loads(self.session_path.read_text(encoding="utf-8"))
            ws = Path(data.get("workspace") or "")
            if not ws.is_dir():
                return
            self.workspace = ws
            self.room_name = data.get("room_name") or ws.name
            self.realm_display_name = data.get("realm_display_name") or self.room_name
            self.room_number = int(data["room_number"]) if data.get("room_number") else None
            self.port = int(data["port"]) if data.get("port") else None
            self.advertised_ip = data.get("advertised_ip") or None
            self.engine = data.get("engine") or "docker"
            self.container_name = data.get("container_name")
            self.pvp_enabled = bool(data.get("pvp_enabled", True))
            self.upload_bps = int(data.get("upload_bps") or RECOMMENDED_UPLOAD_BPS)
            self.duration_mode = data.get("duration_mode") or ("timed" if data.get("deadline") else "timed")
            self.duration_hours = data.get("duration_hours")
            self.uptime_accumulated_seconds = int(data.get("uptime_accumulated_seconds") or 0)
            started = data.get("realm_started_at")
            self.realm_started_at = datetime.fromisoformat(started) if started else None
            if self.realm_started_at and self.realm_started_at.tzinfo is None:
                self.realm_started_at = self.realm_started_at.replace(tzinfo=timezone.utc)
            dl = data.get("deadline")
            self.deadline = datetime.fromisoformat(dl) if dl else None
            paused = bool(data.get("paused"))
            self.paused_remaining_seconds = data.get("paused_remaining_seconds")
            if paused:
                self.realm_paused = True; self.status = "paused"
                self.connection_state = "Previous paused realm found • ready to resume"
                self.realm_started_at = None
            else:
                docker_still_running = False
                if self.engine == "docker" and self.container_name:
                    try:
                        docker_still_running = self._docker_running()
                    except Exception:
                        docker_still_running = False
                if docker_still_running:
                    self.realm_paused = False; self.status = "online"
                    self.connection_state = "Reconnected to existing Docker realm"
                    self._load_player_meta()
                    return
                self.realm_paused = True; self.status = "paused"
                self.connection_state = "Previous realm found • server is stopped"
                if self.duration_mode == "unlimited" and self.realm_started_at:
                    self.uptime_accumulated_seconds += max(0, int((datetime.now(timezone.utc)-self.realm_started_at).total_seconds()))
                    self.realm_started_at = None
            self._load_player_meta()
        except Exception as exc:
            self._log(f"[session recovery error] {exc}")

    def _used_room_numbers(self) -> set[int]:
        used = set()
        for meta in self.rooms_dir.glob("*/realm_meta.json"):
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                n = int(data.get("room_number") or 0)
                if ROOM_NUMBER_MIN <= n <= ROOM_NUMBER_MAX:
                    used.add(n)
            except Exception:
                continue
        return used

    def _allocate_room_number(self) -> int:
        next_n = ROOM_NUMBER_MIN
        try:
            data = json.loads(self.room_counter_path.read_text(encoding="utf-8"))
            next_n = int(data.get("next_room_number") or ROOM_NUMBER_MIN)
        except Exception:
            pass
        if not ROOM_NUMBER_MIN <= next_n <= ROOM_NUMBER_MAX:
            next_n = ROOM_NUMBER_MIN
        used = self._used_room_numbers()
        for offset in range(ROOM_NUMBER_MAX):
            n = ((next_n - 1 + offset) % ROOM_NUMBER_MAX) + 1
            if n not in used:
                return n
        raise RuntimeError("Unable to create a new realm. All 1,000 room numbers are currently in use. Delete or remove an unused realm before creating another.")

    def _commit_room_number(self, n: int):
        following = (int(n) % ROOM_NUMBER_MAX) + 1
        tmp = self.room_counter_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"next_room_number": following}, indent=2), encoding="utf-8")
        os.replace(tmp, self.room_counter_path)

    # ------------------------------ networking / port allocation
    @staticmethod
    def _port_free(port: int) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", int(port)))
            return True
        except OSError:
            return False
        finally:
            sock.close()

    def _load_port_history(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.port_history_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _record_released_port(self, port: int | None):
        if not port:
            return
        history = self._load_port_history()
        history.append({"port": int(port), "released_at": datetime.now(timezone.utc).isoformat()})
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        keep = []
        for row in history[-600:]:
            try:
                t = datetime.fromisoformat(row["released_at"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                if t >= cutoff:
                    keep.append(row)
            except Exception:
                continue
        try:
            self.port_history_path.write_text(json.dumps(keep, indent=2), encoding="utf-8")
        except Exception:
            pass

    def choose_port(self) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        cooling = set()
        for row in self._load_port_history():
            try:
                t = datetime.fromisoformat(row["released_at"])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                if t >= cutoff:
                    cooling.add(int(row["port"]))
            except Exception:
                pass
        candidates = [p for p in range(PORT_MIN, PORT_MAX + 1) if p not in cooling and self._port_free(p)]
        if not candidates:
            candidates = [p for p in range(PORT_MIN, PORT_MAX + 1) if self._port_free(p)]
        if not candidates:
            raise RuntimeError("No free realm ports are available between 10000 and 12000.")
        return random.choice(candidates)

    @staticmethod
    def _lan_ip() -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            s.close()

    def detect_ipv4_addresses(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        seen = set()
        if os.name == "nt":
            script = (
                "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -ne '0.0.0.0' } | "
                "ForEach-Object { $a=Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue; "
                "$p=Get-NetConnectionProfile -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue; "
                "[PSCustomObject]@{IPAddress=$_.IPAddress;InterfaceAlias=$_.InterfaceAlias;Description=$a.InterfaceDescription;Status=$a.Status;Profile=$p.Name} } | ConvertTo-Json -Compress"
            )
            try:
                r = _run_hidden(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=8)
                if r.returncode == 0 and r.stdout.strip():
                    data = json.loads(r.stdout)
                    if isinstance(data, dict):
                        data = [data]
                    for item in data or []:
                        ip = str(item.get("IPAddress") or "").strip()
                        if not ip or ip in seen:
                            continue
                        seen.add(ip)
                        alias = str(item.get("InterfaceAlias") or "Adapter")
                        desc = str(item.get("Description") or "")
                        profile = str(item.get("Profile") or "")
                        text = " ".join([alias, desc, profile, ip]).lower()
                        kind = "Radmin / VPN" if ("radmin" in text or ip.startswith("26.")) else ("VPN" if any(k in text for k in ("vpn", "hamachi", "tailscale", "zerotier", "wireguard")) else "Network")
                        rows.append({"ip": ip, "label": f"{ip} • {alias}" + (f" • {profile}" if profile else ""), "kind": kind})
            except Exception:
                pass
            # Radmin sometimes does not surface cleanly through Get-NetIPAddress/Profile.
            # ipconfig is a second independent discovery path; 26/8 is Radmin's common virtual range.
            try:
                r = _run_hidden(["ipconfig", "/all"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=8)
                current = ""
                for line in (r.stdout or "").splitlines():
                    if line and not line.startswith((" ", "\t")):
                        current = line.strip(" :")
                    m = re.search(r"IPv4 Address[^:]*:\s*([0-9.]+)", line, re.I)
                    if m:
                        ip = m.group(1).strip()
                        if ip.startswith("26.") and ip not in seen:
                            seen.add(ip); rows.append({"ip": ip, "label": f"{ip} • Radmin VPN • {current or 'Virtual adapter'}", "kind": "Radmin / VPN"})
            except Exception:
                pass
        lan = self._lan_ip()
        if lan not in seen:
            rows.append({"ip": lan, "label": f"{lan} • Primary LAN", "kind": "Network"})
            seen.add(lan)
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if ip.startswith("127.") or ip in seen:
                    continue
                rows.append({"ip": ip, "label": f"{ip} • Local adapter", "kind": "Network"})
                seen.add(ip)
        except Exception:
            pass
        rows.sort(key=lambda r: (0 if r["kind"].startswith("Radmin") else 1 if r["kind"] == "VPN" else 2, r["ip"]))
        return rows

    # ------------------------------ Docker/native lifecycle
    @staticmethod
    def docker_cli() -> str | None:
        return shutil.which("docker")

    def docker_preflight(self, *, wait_seconds: float = 0.0, poll_seconds: float = 1.5) -> tuple[bool, str]:
        """Check Docker Desktop and optionally wait for its Linux engine pipe.

        Docker Desktop can briefly remove ``dockerDesktopLinuxEngine`` while its
        backend is starting/restarting.  v7.25 treated that transient state as a
        permanent failure, which could strand a paused realm.  v7.26 retries the
        daemon check for a bounded period without changing the saved room/port.
        """
        docker = self.docker_cli()
        if not docker:
            return False, "Docker CLI was not found. Start/install Docker Desktop or use Native Java."
        deadline = time.monotonic() + max(0.0, float(wait_seconds or 0.0))
        last_detail = "Docker Engine is not responding."
        attempt = 0
        while True:
            attempt += 1
            try:
                r = _run_hidden(
                    [docker, "version", "--format", "{{.Server.Version}}"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=8,
                )
                if r.returncode == 0 and r.stdout.strip():
                    detail = f"Docker Engine {r.stdout.strip()} is available."
                    if attempt > 1:
                        detail += f" Ready after {attempt} checks."
                    return True, detail
                last_detail = (r.stderr.strip() or r.stdout.strip() or last_detail)
            except Exception as exc:
                last_detail = str(exc)

            if time.monotonic() >= deadline:
                return False, last_detail
            time.sleep(max(0.25, float(poll_seconds or 1.5)))

    def _ensure_docker_image(self):
        normalized = str(self.docker_image or "").split(":", 1)[0].strip().lower()
        if normalized == KNOWN_BAD_DOCKER_IMAGE.lower():
            raise RuntimeError(
                "Blocked old TOTK Docker image. Sacred Zonai Realms v7.30 requires "
                "wesleyhellewell/totk_online_multiplayer_server_test:latest."
            )
        docker = self.docker_cli()
        if not docker:
            raise RuntimeError("Docker CLI was not found.")
        r = _run_hidden([docker, "image", "inspect", self.docker_image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        if r.returncode == 0:
            return
        self._log(f"Docker image {self.docker_image} is not cached; downloading it now…")
        r = _run_hidden([docker, "pull", self.docker_image], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
        if r.stdout:
            for line in r.stdout.splitlines():
                self._log("[Docker] " + line)
        if r.returncode != 0:
            raise RuntimeError(f"Docker could not download the required TOTK server image: {self.docker_image}")

    def _docker_exists(self) -> bool:
        if not self.container_name or not self.docker_cli():
            return False
        r = _run_hidden([self.docker_cli(), "container", "inspect", self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0

    def _docker_running(self) -> bool:
        if not self._docker_exists():
            return False
        r = _run_hidden([self.docker_cli(), "inspect", "-f", "{{.State.Running}}", self.container_name], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        return r.returncode == 0 and r.stdout.strip().lower() == "true"

    def is_running(self) -> bool:
        if self.engine == "docker":
            try:
                return self._docker_running()
            except Exception:
                return False
        return bool(self.proc and self.proc.poll() is None)

    def _create_docker_container(self):
        if not self.workspace or not self.port:
            raise RuntimeError("Realm workspace/port is not ready.")
        self._ensure_docker_image()
        docker = self.docker_cli()
        safe = re.sub(r"[^a-z0-9_.-]+", "-", (self.room_name or "realm").lower()).strip("-")
        self.container_name = ("zonai-realms-" + safe)[-62:]
        if self._docker_exists():
            _run_hidden([docker, "rm", "-f", self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # v7.30 same-version Docker image hotfix: Wesley confirmed the _test image
        # is the good multiplayer-server image.  Unlike the old generic Temurin
        # container, this image owns its server runtime/entrypoint.  Sacred Zonai
        # Realms therefore mounts only the persistent realm workspace at /app/run,
        # maps the selected host port to the server's fixed internal 10014 port,
        # and deliberately does NOT override the image entrypoint with java -jar.
        volume = f"{self.workspace.resolve()}:/app/run"
        cmd = [
            docker, "create", "--name", self.container_name,
            "--init", "--interactive", "--tty", "--stop-timeout", "15",
            "--restart", "no",
            "-p", f"0.0.0.0:{self.port}:{WESLEY_CONTAINER_PORT}",
            "-v", volume,
            self.docker_image,
        ]
        r = _run_hidden(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if r.returncode != 0:
            # Wesley's code retries cleanly when a stale container races creation.
            if "conflict" in (r.stdout or "").lower() and self._docker_exists():
                _run_hidden([docker, "rm", "-f", self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                r = _run_hidden(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if r.returncode != 0:
                raise RuntimeError("Docker could not create the realm container:\n" + (r.stdout or "Unknown Docker error").strip())

    def _start_docker(self):
        if not self._docker_exists():
            self._create_docker_container()
        docker = self.docker_cli()
        r = _run_hidden([docker, "start", self.container_name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
        if r.returncode != 0:
            raise RuntimeError("Docker could not start the realm container:\n" + (r.stdout or "").strip())
        self.log_proc = subprocess.Popen(
            [docker, "logs", "-f", "--since", "0s", self.container_name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        t = threading.Thread(target=self._reader, args=(self.log_proc,), daemon=True)
        t.start(); self._reader_threads.append(t)

    def _start_native(self):
        if not self.workspace:
            raise RuntimeError("Realm workspace is not ready.")
        java = shutil.which("java")
        if not java:
            raise RuntimeError("Java was not found. Install 64-bit Java 17 or newer, or use Docker Desktop.")
        # stdin=PIPE is deliberate: Kirbymimi's CommandServer reads System.in.
        self.proc = subprocess.Popen(
            [java, "-jar", "server.jar"], cwd=self.workspace,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        t = threading.Thread(target=self._reader, args=(self.proc,), daemon=True)
        t.start(); self._reader_threads.append(t)

    def _reader(self, proc):
        try:
            if not proc or not proc.stdout:
                return
            for line in proc.stdout:
                self._log(line)
        except Exception as exc:
            self._log(f"[console reader] {exc}")

    def send_command(self, command: str):
        command = str(command or "").strip()
        if not command:
            raise ValueError("Command is empty.")
        if not self.is_running():
            raise RuntimeError("The realm is not running.")
        if self.engine == "docker":
            docker = self.docker_cli()
            # docker attach is the only reliable way to write to the kept-open stdin.
            p = subprocess.Popen(
                [docker, "attach", "--no-stdin=false", "--sig-proxy=false", self.container_name],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                text=True,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            try:
                p.stdin.write(command + "\n"); p.stdin.flush(); time.sleep(0.12)
            finally:
                try: p.stdin.close()
                except Exception: pass
                try: p.terminate()
                except Exception: pass
        else:
            if not self.proc or not self.proc.stdin:
                raise RuntimeError("Native Java console stdin is unavailable.")
            self.proc.stdin.write(command + "\n"); self.proc.stdin.flush()
        self._log(f"> {command}")

    def _stop_engine(self, remove_container=False):
        old_port = self.port
        if self.engine == "docker" and self.container_name:
            docker = self.docker_cli()
            if docker and self._docker_exists():
                r = _run_hidden([docker, "stop", "-t", "15", self.container_name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=35)
                if r.stdout.strip(): self._log("[Docker] " + r.stdout.strip())
                if self._docker_running():
                    _run_hidden([docker, "kill", self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if self.log_proc and self.log_proc.poll() is None:
                try: self.log_proc.terminate(); self.log_proc.wait(timeout=2)
                except Exception:
                    try: self.log_proc.kill()
                    except Exception: pass
            self.log_proc = None
            if remove_container and docker and self._docker_exists():
                _run_hidden([docker, "rm", "-f", self.container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif self.proc:
            p = self.proc
            try:
                if p.stdin:
                    try: p.stdin.write("stop\n"); p.stdin.flush(); time.sleep(0.35)
                    except Exception: pass
                p.terminate(); p.wait(timeout=8)
            except Exception:
                if os.name == "nt" and p.pid:
                    try: _run_hidden(["taskkill", "/PID", str(p.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
                    except Exception: pass
                try: p.kill()
                except Exception: pass
            self.proc = None
        if old_port:
            end = time.time() + 12
            while time.time() < end and not self._port_free(old_port):
                time.sleep(0.2)

    # ------------------------------ workspace creation / runtime repair
    @staticmethod
    def _copy_tree(src: Path, dst: Path):
        shutil.copytree(src, dst, dirs_exist_ok=True)

    @staticmethod
    def _link_or_copy(src: Path, dst: Path):
        try:
            os.link(src, dst)
        except Exception:
            shutil.copy2(src, dst)

    @staticmethod
    def _set_resource_upload_limit(path: Path, value: int):
        value = int(value)
        text = path.read_text(encoding="utf-8", errors="strict")
        if "serverMaxUploadPerSecond" in text:
            new, n = re.subn(r'("serverMaxUploadPerSecond"\s*:\s*)\d+', rf'\g<1>{value}', text, count=1)
        else:
            pattern = re.compile(r'("targetClass"\s*:\s*"ResourceServer"\s*\n\s*"isSyncedWithParent"\s*:\s*false)(\s*\})', re.S)
            replacement = r'\1\n\t\t"properties": {\n\t\t\t"serverMaxUploadPerSecond": ' + str(value) + r'\n\t\t}\2'
            new, n = pattern.subn(replacement, text, count=1)
        if n != 1:
            raise ValueError("Could not set serverMaxUploadPerSecond in serverCreator.ktml.")
        path.write_text(new, encoding="utf-8")

    @staticmethod
    def _set_pvp(path: Path, enabled: bool):
        text = path.read_text(encoding="utf-8", errors="strict")
        marker = '"PlayerPuppet": {'
        pos = text.find(marker)
        if pos < 0:
            raise ValueError("PlayerPuppet section was not found in actorCreators.ktml.")
        prefix, tail = text[:pos], text[pos:]
        block = re.compile(r'\n\s*\{\s*"targetClass"\s*:\s*"ActorCollisionSync"\s*\}\s*', re.S)
        has = bool(block.search(tail))
        if enabled and not has:
            # restore pristine actorCreators from bundled runtime if a prior room edit removed it
            pristine = resource_path("server_bundle/runtime/Resources/SyncedActorServer/actorCreators.ktml")
            if pristine.exists():
                base = pristine.read_text(encoding="utf-8", errors="strict")
                path.write_text(base, encoding="utf-8")
                text = base; pos = text.find(marker); prefix, tail = text[:pos], text[pos:]; has = bool(block.search(tail))
            if not has:
                raise ValueError("PvP requested but ActorCollisionSync is missing from PlayerPuppet.")
        elif not enabled and has:
            tail, n = block.subn("\n", tail, count=1)
            if n != 1:
                raise ValueError("Could not disable PlayerPuppet collision sync.")
            path.write_text(prefix + tail, encoding="utf-8")

    def _create_workspace(self, port: int, pvp: bool, upload_bps: int) -> tuple[Path, str, int]:
        bundle = resource_path("server_bundle")
        jar_src = bundle / "server.jar"
        runtime_src = bundle / "runtime"
        if not jar_src.is_file() or not runtime_src.is_dir():
            raise FileNotFoundError("Bundled server.jar/runtime resources were not found.")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        room_name = f"ZonaiRealm_{stamp}_{port}"
        room_number = self._allocate_room_number()
        ws = self.rooms_dir / room_name
        ws.mkdir(parents=True, exist_ok=False)
        self._link_or_copy(jar_src, ws / "server.jar")
        self._copy_tree(runtime_src, ws)
        (ws / "User" / "SaveServer" / "users").mkdir(parents=True, exist_ok=True)
        creator = ws / "Resources" / "TOTKServer" / "serverCreator.ktml"
        text = creator.read_text(encoding="utf-8", errors="strict")
        text, n = re.subn(r'("port"\s*:\s*)\d+', rf'\g<1>{port}', text, count=1)
        if n != 1:
            raise ValueError("Could not set the realm port.")
        creator.write_text(text, encoding="utf-8")
        if int(upload_bps) != DEFAULT_UPLOAD_BPS:
            self._set_resource_upload_limit(creator, upload_bps)
        self._set_pvp(ws / "Resources" / "SyncedActorServer" / "actorCreators.ktml", bool(pvp))
        (ws / "realm_meta.json").write_text(json.dumps({"realm_display_name": FRIENDLY_REALM_NAME, "room_number": room_number, "created_at": datetime.now(timezone.utc).isoformat()}, indent=2), encoding="utf-8")
        self._commit_room_number(room_number)
        return ws, room_name, room_number

    def _sanitize_users(self):
        if not self.workspace:
            return
        users = self.workspace / "User" / "SaveServer" / "users"
        users.mkdir(parents=True, exist_ok=True)
        # Historical Sacred Realms v4.x/v5.x backups inside users crash Kirbymimi SaveServer.
        legacy = users / "Backups"
        if legacy.is_dir():
            dest = self.workspace / "Backups" / "PlayerSaves" / "MigratedFromUsers"
            dest.mkdir(parents=True, exist_ok=True)
            for child in legacy.iterdir():
                target = dest / child.name
                if target.exists(): target = dest / f"{child.stem}_{int(time.time())}{child.suffix}"
                shutil.move(str(child), str(target))
            try: legacy.rmdir()
            except OSError: pass
            self._log("[repair] Moved legacy Backups out of User/SaveServer/users.")
        bad = [p.name for p in users.iterdir() if p.is_dir()]
        if bad:
            raise RuntimeError("User/SaveServer/users may contain player .ktml files only. Remove subfolders: " + ", ".join(bad))

    def _repair_runtime(self):
        if not self.workspace:
            raise RuntimeError("No realm workspace exists.")
        for d in [self.workspace / "User" / "SaveServer" / "users", self.workspace / "User" / "BanServer"]:
            d.mkdir(parents=True, exist_ok=True)
        self._sanitize_users()
        required = [
            "server.jar", "User/SaveServer/save.ktml", "Resources/SaveServer/defaultClientSave.ktml",
            "Resources/SaveServer/name2hash.ktml", "Resources/SaveServer/bitKeyMap.ktml",
            "Resources/TOTKServer/serverCreator.ktml", "Resources/TOTKServer/actorList.ktml",
            "Resources/SyncedActorServer/actorCreators.ktml", "Romfs/Sequence/Main.module.ainb",
        ]
        missing = [r for r in required if not (self.workspace / r).exists()]
        if missing:
            raise FileNotFoundError("Realm runtime is incomplete. Missing: " + ", ".join(missing))
        for d in [self.workspace / "User" / "SaveServer", self.workspace / "User" / "SaveServer" / "users"]:
            probe = d / ".zonai_realms_write_test"
            probe.write_text("ok", encoding="utf-8"); probe.unlink(missing_ok=True)

    # ------------------------------ public lifecycle
    def repair_runtime(self):
        """Public v7.00 recovery action: validate and repair the active realm runtime."""
        with self.lock:
            if self.workspace is None:
                raise RuntimeError("No realm workspace exists yet. Open a realm once before using runtime repair.")
            self._sanitize_users()
            self._repair_runtime()
            self._log("[recovery] Realm runtime integrity check completed successfully.")
            return "Realm runtime repaired and required files verified."


    def start(self, *, engine: str = "docker", duration_hours: float | None = 2, pvp_enabled: bool = True, upload_bps: int = RECOMMENDED_UPLOAD_BPS, preferred_port: int | None = None, advertised_ip: str | None = None):
        with self.lifecycle_lock:
            if self.is_running():
                raise RuntimeError("A realm is already running.")
            if self.realm_paused and self.workspace:
                raise RuntimeError("A paused realm already exists. Resume or close it before opening a new realm.")
            engine = "native" if str(engine).lower().startswith("native") else "docker"
            advertised_ip = str(advertised_ip or "").strip() or None
            detected = self.detect_ipv4_addresses()
            detected_ips = {r["ip"] for r in detected}
            if advertised_ip and advertised_ip not in detected_ips:
                raise RuntimeError(f"Selected connection address {advertised_ip} is no longer assigned to this PC. Refresh IPs and select it again.")
            if advertised_ip and engine == "native":
                # Prove Windows can bind the chosen virtual/LAN adapter before Java starts.
                probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    probe.bind((advertised_ip, 0))
                except OSError as exc:
                    raise RuntimeError(f"Windows cannot bind the selected adapter {advertised_ip}: {exc}")
                finally:
                    probe.close()
            if engine == "docker":
                self._log("Checking Docker Desktop engine readiness…")
                ok, detail = self.docker_preflight(wait_seconds=30)
                if not ok: raise RuntimeError(
                    "Docker Desktop's Linux engine did not become ready within 30 seconds. " + detail
                )
                self._log(detail)
            port = int(preferred_port) if preferred_port else self.choose_port()
            if port < PORT_MIN or port > PORT_MAX or not self._port_free(port):
                raise RuntimeError(f"Port {port} is unavailable.")
            upload_bps = max(50_000, min(int(upload_bps), 100_000_000))
            unlimited = duration_hours is None or str(duration_hours).lower() in {"unlimited", "infinite", "infinity"}
            hours = None if unlimited else max(1.0, min(float(duration_hours), 8.0))
            ws, room_name, room_number = self._create_workspace(port, bool(pvp_enabled), upload_bps)
            self.workspace, self.room_name, self.port = ws, room_name, port
            self.realm_display_name, self.room_number = FRIENDLY_REALM_NAME, room_number
            self.duration_mode = "unlimited" if unlimited else "timed"
            self.duration_hours = hours
            self.uptime_accumulated_seconds = 0
            self.realm_started_at = None
            self.engine, self.pvp_enabled, self.upload_bps = engine, bool(pvp_enabled), upload_bps
            self.advertised_ip = advertised_ip or (detected[0]["ip"] if detected else self._lan_ip())
            self.container_name = None; self.realm_paused = False; self.paused_remaining_seconds = None
            self.players = {}; self.nickname_to_uid = {}; self.game_ready_names.clear(); self.pending_connect_name = None
            self.logs.clear(); self._repair_runtime(); self._load_player_meta()
            self.status = "opening"; self.connection_state = "Waiting for server initialization"
            self._log(f"=== ZONAI REALM OPENING • {FRIENDLY_REALM_NAME} • Room - #{room_number:03d} ===")
            self._log(f"Engine: {'Docker Desktop' if engine == 'docker' else 'Native Java'} • Port {port} • Upload {upload_bps:,} B/s • PvP {'ON' if pvp_enabled else 'OFF'}")
            self._log(f"Advertised connection address: {self.advertised_ip}:{port}" + (" • Radmin Direct" if engine == "native" and str(self.advertised_ip).startswith("26.") else ""))
            try:
                if engine == "docker":
                    self._create_docker_container(); self._start_docker()
                else:
                    self._start_native()
            except Exception as exc:
                # Never strand a freshly-created workspace in an ambiguous
                # "opening" state. Keep it recoverable as a paused realm so
                # the user can inspect files, fix Docker/Java, or close it.
                self.deadline = None
                self.paused_remaining_seconds = None if unlimited else int(hours * 3600)
                self.realm_paused = True
                self.status = "error"
                self.connection_state = f"Launch failed • realm preserved: {exc}"
                self._save_session_metadata()
                self._log(f"[launch error] {exc}")
                raise
            self.deadline = None if unlimited else datetime.now() + timedelta(hours=hours)
            self.realm_started_at = datetime.now(timezone.utc) if unlimited else None
            self.status = "online"
            self.connection_state = "Waiting for player"
            self._save_session_metadata()
            return self.snapshot()

    def pause(self):
        with self.lifecycle_lock:
            if not self.workspace or not self.is_running(): raise RuntimeError("No running realm is available to pause.")
            self.paused_remaining_seconds = max(0, int((self.deadline - datetime.now()).total_seconds())) if self.deadline else None
            if self.duration_mode == "unlimited" and self.realm_started_at:
                self.uptime_accumulated_seconds += max(0, int((datetime.now(timezone.utc) - self.realm_started_at).total_seconds()))
                self.realm_started_at = None
            self._log("Pausing realm safely…")
            self._stop_engine(remove_container=False)
            self.deadline = None; self.realm_paused = True; self.status = "paused"
            self.connection_state = "Realm paused • save files unlocked"
            for p in self.players.values(): p["online"] = False
            self._save_player_meta(); self._save_session_metadata()
            self._log("=== ZONAI REALM PAUSED ===")
            return self.snapshot()

    def resume(self):
        with self.lifecycle_lock:
            if not self.realm_paused or not self.workspace or not self.port: raise RuntimeError("No paused realm is available to resume.")
            if not self._port_free(self.port): raise RuntimeError(f"Port {self.port} is still busy. Zonai Realms will not silently change the saved room endpoint.")
            self._repair_runtime()
            if self.engine == "docker":
                self.status = "resuming"
                self.connection_state = "Waiting for Docker Desktop engine"
                self._save_session_metadata()
                self._log("Resume requested • waiting for Docker Desktop Linux engine…")
                ok, detail = self.docker_preflight(wait_seconds=45)
                if not ok:
                    # Keep the realm paused/recoverable.  Never flip it online
                    # or create a new room/port when Docker itself is unavailable.
                    self.status = "paused"
                    self.connection_state = "Realm paused • Docker engine not ready"
                    self._save_session_metadata()
                    raise RuntimeError(
                        "Docker Desktop's Linux engine did not become ready within 45 seconds. "
                        "The realm is still safely paused and can be resumed again after Docker is ready. "
                        + detail
                    )
                self._log(detail)
                if not self._docker_exists():
                    self._log("Saved realm container is missing; recreating it with the SAME workspace and port…")
                    self._create_docker_container()
                self._start_docker()
            else:
                self._start_native()
            if self.duration_mode == "unlimited":
                self.deadline = None
                self.realm_started_at = datetime.now(timezone.utc)
            else:
                self.deadline = datetime.now() + timedelta(seconds=max(0, int(self.paused_remaining_seconds or 0)))
            self.realm_paused = False; self.status = "online"; self.connection_state = "Waiting for player"
            self._save_session_metadata(); self._log("=== ZONAI REALM RESUMED • same room / same saves / same port ===")
            return self.snapshot()

    def restart(self):
        with self.lifecycle_lock:
            if not self.is_running(): raise RuntimeError("Realm is not running.")
            remaining = max(0, int((self.deadline - datetime.now()).total_seconds())) if self.deadline else None
            if self.duration_mode == "unlimited" and self.realm_started_at:
                self.uptime_accumulated_seconds += max(0, int((datetime.now(timezone.utc) - self.realm_started_at).total_seconds()))
                self.realm_started_at = None
            self._stop_engine(remove_container=False); self._repair_runtime()
            if self.engine == "docker": self._start_docker()
            else: self._start_native()
            if self.duration_mode == "unlimited":
                self.deadline = None; self.realm_started_at = datetime.now(timezone.utc)
            else:
                self.deadline = datetime.now() + timedelta(seconds=max(0, int(remaining or 0)))
            self.status = "online"; self.connection_state = "Waiting for player"
            self._save_session_metadata(); self._log("=== ZONAI REALM RESTARTED ===")
            return self.snapshot()

    def close(self):
        with self.lifecycle_lock:
            if self.workspace:
                self._stop_engine(remove_container=True)
            self._record_released_port(self.port)
            if self.duration_mode == "unlimited" and self.realm_started_at:
                self.uptime_accumulated_seconds += max(0, int((datetime.now(timezone.utc) - self.realm_started_at).total_seconds()))
            self.realm_started_at = None
            self.deadline = None; self.paused_remaining_seconds = None; self.realm_paused = False
            self.status = "closed"; self.connection_state = "Realm closed"
            for p in self.players.values(): p["online"] = False
            self._save_player_meta(); self._log("=== ZONAI REALM CLOSED ===")
            self._save_session_metadata()
            return self.snapshot()

    def pull_latest_java_image(self):
        ok, detail = self.docker_preflight()
        if not ok: raise RuntimeError(detail)
        docker = self.docker_cli()
        r = _run_hidden([docker, "pull", self.docker_image], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
        if r.returncode != 0: raise RuntimeError((r.stdout or "Docker pull failed").strip())
        self._log("[Docker] Java image refreshed.")
        return r.stdout.strip()

    # ------------------------------ players / roster
    def _player_meta_path(self) -> Path | None:
        return self.workspace / "ZonaiRealmPlayers.json" if self.workspace else None

    def _load_player_meta(self):
        path = self._player_meta_path()
        if not path or not path.exists(): return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.players = data
                self.nickname_to_uid = {str(v.get("name")): uid for uid, v in data.items() if v.get("name")}
        except Exception: pass

    def _save_player_meta(self):
        path = self._player_meta_path()
        if not path: return
        try:
            tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(self.players, indent=2), encoding="utf-8"); os.replace(tmp, path)
        except Exception: pass

    def _users_dir(self) -> Path | None:
        return self.workspace / "User" / "SaveServer" / "users" if self.workspace else None

    def _scan_user_mtimes(self) -> dict[str, float]:
        out = {}; d = self._users_dir()
        if d and d.is_dir():
            for p in d.glob("*.ktml"):
                try: out[p.stem] = p.stat().st_mtime
                except OSError: pass
        return out

    def _correlate_uid(self, name: str, online=True) -> str | None:
        if name in self.nickname_to_uid:
            uid = self.nickname_to_uid[name]
        else:
            current = self._scan_user_mtimes(); changed = []
            for uid0, mt in current.items():
                old = self._last_known_user_mtimes.get(uid0)
                if old is None or mt > old + 0.0001: changed.append((mt, uid0))
            self._last_known_user_mtimes = current
            uid = sorted(changed, reverse=True)[0][1] if changed else None
        if uid:
            self.nickname_to_uid[name] = uid
            row = self.players.setdefault(uid, {"uid": uid})
            row.update({"uid": uid, "name": name, "online": bool(online), "last_seen": datetime.now().isoformat(timespec="seconds")})
            self._save_player_meta()
        return uid

    def _parse_log_line(self, line: str):
        stripped = line.strip(); low = stripped.lower()
        m = CONNECT_RE.search(stripped)
        if m:
            name, uid = m.group("name").strip(), m.group("uid")
            self.pending_connect_name = name
            if uid:
                self.nickname_to_uid[name] = uid
                row = self.players.setdefault(uid, {"uid": uid}); row.update({"uid": uid, "name": name, "online": True, "last_seen": datetime.now().isoformat(timespec="seconds")})
                self._save_player_meta()
            else:
                self._correlate_uid(name, True)
            self.connection_state = f"{name} connected • handshake received"
        if MAINFIELD_RE.search(stripped):
            name = self.pending_connect_name
            if name:
                self.game_ready_names.add(name); self.connection_state = f"{name} in game • MainField ready"; self.pending_connect_name = None
                uid = self.nickname_to_uid.get(name)
                if uid in self.players: self.players[uid]["game_ready"] = True; self._save_player_meta()
        m = LEAVE_RE.search(stripped)
        if m:
            name = m.group("name").strip(); self.game_ready_names.discard(name)
            uid = self.nickname_to_uid.get(name)
            if uid and uid in self.players:
                self.players[uid]["online"] = False; self.players[uid]["game_ready"] = False; self.players[uid]["last_seen"] = datetime.now().isoformat(timespec="seconds"); self._save_player_meta()
            self.connection_state = f"{name} disconnected • waiting for player"
        if any(k in low for k in ("server initialized successfully", "server started", "listening")):
            self.status = "online"

    def _poll_roster(self):
        if not self.is_running(): return
        try:
            before = len(self.logs)
            self.send_command("list")
            time.sleep(0.8)
            lines = self.recent_logs(100).splitlines()
            online: dict[str, str] = {}; saw = False
            for line in reversed(lines):
                # strip our [time] prefix
                body = re.sub(r"^\[\d\d:\d\d:\d\d\]\s*", "", line).strip()
                m = LIST_LINE_RE.match(body)
                if m:
                    online[m.group("uid")] = m.group("name"); saw = True; continue
                if LIST_EMPTY_RE.match(body): saw = True; break
                if saw: break
            if not saw: return
            now = datetime.now().isoformat(timespec="seconds")
            for uid, name in online.items():
                row = self.players.setdefault(uid, {"uid": uid}); row.update({"uid": uid, "name": name, "online": True, "last_seen": now})
                self.nickname_to_uid[name] = uid
            for uid, row in self.players.items():
                if uid not in online: row["online"] = False; row["game_ready"] = False
            if online:
                names = ", ".join(sorted(online.values())); self.connection_state = f"{len(online)} player(s) connected • {names}"
            else: self.connection_state = "No players connected • waiting"
            self._save_player_meta()
        except Exception as exc:
            self._log(f"[roster poll warning] {exc}")

    def _assert_save_upload_safe(self):
        """Require a real paused realm before touching live SaveServer files."""
        if not self.workspace:
            raise RuntimeError("Open a realm first.")
        if self.is_running():
            raise RuntimeError("Pause the realm before uploading or replacing a save.")
        if not self.realm_paused:
            raise RuntimeError("Realm must be paused before uploading or replacing a save.")

    def _default_client_save_path(self) -> Path:
        candidates = []
        if self.workspace:
            candidates.append(self.workspace / "Resources" / "SaveServer" / "defaultClientSave.ktml")
        candidates.extend([
            resource_path("server_bundle/runtime/Resources/SaveServer/defaultClientSave.ktml"),
            resource_path("defaultClientSave.ktml"),
        ])
        p = next((x for x in candidates if x.is_file()), None)
        if not p:
            raise FileNotFoundError("The server defaultClientSave.ktml required for player-save validation was not found.")
        return p

    @staticmethod
    def _map_field_count(maps: dict[str, Any]) -> int:
        return sum(len(v) for v in maps.values() if isinstance(v, dict))

    def _prepare_server_compatible_player_ktml(self, raw: bytes, filename: str) -> tuple[str, dict[str, Any]]:
        """Turn either a full progress.sav or KTML into the exact child-save shape
        Kirbymimi SaveServer expects in User/SaveServer/users/<uid>.ktml.

        The server does *not* store a complete progress.sav per player.  Its
        per-player file is normalized against defaultClientSave.ktml and then
        merged with the realm-wide save.ktml when a binary progress.sav is
        exported.  Writing a full SAV->KTML conversion directly into users/
        therefore passes our editor/parser but is not the server's native
        storage model.
        """
        ext = Path(filename).suffix.lower()
        if ext == ".sav":
            if len(raw) != zonai_save_codec.FILE_SIZE:
                raise ValueError(
                    f"SAV validation failed: expected exactly {zonai_save_codec.FILE_SIZE:,} bytes, got {len(raw):,}."
                )
            full_text = zonai_save_codec.progress_sav_to_ktml(raw)
            input_kind = "SAV"
        elif ext == ".ktml":
            full_text = raw.decode("utf-8", errors="strict")
            input_kind = "KTML"
        else:
            raise ValueError("Choose a .ktml or .sav file.")

        # Parse with the server-derived codec, not only the editor parser.
        incoming_maps = zonai_save_codec.parse_ktml(full_text)
        default_path = self._default_client_save_path()
        default_text = default_path.read_text(encoding="utf-8", errors="strict")
        default_maps = zonai_save_codec.parse_ktml(default_text)

        projected: dict[str, Any] = {}
        imported_values = 0
        defaulted_values = 0
        for type_name in zonai_save_codec.ALL_TYPES:
            schema_fields = default_maps.get(type_name, {})
            if not isinstance(schema_fields, dict) or not schema_fields:
                continue
            source_fields = incoming_maps.get(type_name, {})
            if not isinstance(source_fields, dict):
                source_fields = {}
            out = {}
            for key, default_value in schema_fields.items():
                if key in source_fields:
                    out[key] = source_fields[key]
                    imported_values += 1
                else:
                    out[key] = default_value
                    defaulted_values += 1
            projected[type_name] = out

        child_text = zonai_save_codec.maps_to_ktml_text(projected)
        # Both parsers must agree that the generated child KTML is structurally valid.
        parse_ktml(child_text)
        zonai_save_codec.parse_ktml(child_text)

        if not self.workspace:
            raise RuntimeError("Open a realm first.")
        base_path = self.workspace / "User" / "SaveServer" / "save.ktml"
        if not base_path.is_file():
            raise FileNotFoundError("The realm-wide User/SaveServer/save.ktml is missing.")
        base_text = base_path.read_text(encoding="utf-8", errors="strict")
        # This is the same base + child merge shape the server exporter uses.
        merged_sav = zonai_save_codec.ktml_to_progress_sav(base_text, child_text)
        if len(merged_sav) != zonai_save_codec.FILE_SIZE:
            raise ValueError("Server compatibility validation failed: merged SAV length is not standard.")
        # A second decode catches bad type markers, pointers, strings, arrays, etc.
        zonai_save_codec.progress_sav_to_ktml(merged_sav)

        return child_text, {
            "input_kind": input_kind,
            "input_bytes": len(raw),
            "incoming_fields": self._map_field_count(incoming_maps),
            "server_child_fields": self._map_field_count(projected),
            "imported_values": imported_values,
            "defaulted_values": defaulted_values,
            "verified_merged_sav_bytes": len(merged_sav),
            "storage_model": "server child KTML projected through defaultClientSave.ktml",
        }

    def _prepare_server_compatible_main_ktml(self, raw: bytes, filename: str) -> tuple[str, dict[str, Any]]:
        ext = Path(filename).suffix.lower()
        if ext == ".sav":
            if len(raw) != zonai_save_codec.FILE_SIZE:
                raise ValueError(
                    f"SAV validation failed: expected exactly {zonai_save_codec.FILE_SIZE:,} bytes, got {len(raw):,}."
                )
            text = zonai_save_codec.progress_sav_to_ktml(raw)
            input_kind = "SAV"
        elif ext == ".ktml":
            text = raw.decode("utf-8", errors="strict")
            input_kind = "KTML"
        else:
            raise ValueError("Choose a .ktml or .sav file.")
        parse_ktml(text)
        maps = zonai_save_codec.parse_ktml(text)
        # Re-serialize through the server-derived type normalizer.  This is
        # critical for Int64/UInt64 values: Java expects Long, not Double.
        text = zonai_save_codec.maps_to_ktml_text(maps)
        parse_ktml(text)
        maps = zonai_save_codec.parse_ktml(text)
        rebuilt = zonai_save_codec.ktml_to_progress_sav(text)
        if len(rebuilt) != zonai_save_codec.FILE_SIZE:
            raise ValueError("Server compatibility validation failed: rebuilt SAV length is not standard.")
        zonai_save_codec.progress_sav_to_ktml(rebuilt)
        return text, {
            "input_kind": input_kind,
            "input_bytes": len(raw),
            "server_fields": self._map_field_count(maps),
            "verified_sav_bytes": len(rebuilt),
            "storage_model": "realm-wide save.ktml",
        }

    def player_save_path(self, uid: str) -> Path | None:
        d = self._users_dir(); p = d / f"{uid}.ktml" if d else None
        return p if p and p.is_file() else None

    def download_player(self, uid: str, fmt: str = "ktml") -> tuple[bytes, str, str]:
        p = self.player_save_path(uid)
        if not p: raise FileNotFoundError("This player's KTML save is not available yet.")
        if fmt.lower() == "ktml": return p.read_bytes(), p.name, "text/plain; charset=utf-8"
        server_path = self.workspace / "User" / "SaveServer" / "save.ktml"
        if not server_path.is_file():
            raise FileNotFoundError("The server-wide save.ktml is missing, so this player cannot be merged into a complete progress.sav.")
        # Wesley's implementation mirrors Kirbymimi's exporter: the actual client
        # progress.sav is the server-wide base plus the player's per-UID overlay.
        sav_bytes = zonai_save_codec.ktml_to_progress_sav(
            server_path.read_text(encoding="utf-8", errors="strict"),
            p.read_text(encoding="utf-8", errors="strict"),
        )
        return sav_bytes, f"{uid}.sav", "application/octet-stream"

    def replace_player(self, uid: str, raw: bytes, filename: str):
        self._assert_save_upload_safe()
        target = self.player_save_path(uid)
        if not target:
            raise FileNotFoundError("Player save is not mapped yet.")
        backup = None
        incoming = None
        replaced = False
        steps = []
        try:
            self._log(f"[save-upload] Validating {Path(filename).suffix.upper().lstrip('.')} for player {uid}…")
            steps.append("Validated input format and server child-save schema.")
            text, validation = self._prepare_server_compatible_player_ktml(raw, filename)
            self._log(f"[save-upload] Server compatibility verified: {validation['server_child_fields']} child fields; merged SAV {validation['verified_merged_sav_bytes']:,} bytes.")
            steps.append("Converted/projected to server-compatible player KTML.")

            backup_dir = self.workspace / "Backups" / "PlayerSaves" / uid
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"backup_before_upload_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}_{uid}.ktml"
            shutil.copy2(target, backup)
            self._log(f"[save-upload] Backed up existing player save: {backup.name}")
            steps.append("Backed up existing player save.")

            temp_dir = self.workspace / "Temp"
            temp_dir.mkdir(parents=True, exist_ok=True)
            incoming = temp_dir / f"{uid}.incoming.ktml"
            with incoming.open("w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            parse_ktml(incoming.read_text(encoding="utf-8", errors="strict"))

            err = None
            for _ in range(24):
                try:
                    os.replace(incoming, target)
                    replaced = True
                    err = None
                    break
                except PermissionError as exc:
                    err = exc
                    time.sleep(.25)
            if err:
                raise PermissionError(f"Save remained locked after 6 seconds: {err}")

            # Verify bytes on disk after the atomic replacement, then re-run the
            # server merge/export test from the actual stored child KTML.
            stored_text = target.read_text(encoding="utf-8", errors="strict")
            parse_ktml(stored_text)
            base_text = (self.workspace / "User" / "SaveServer" / "save.ktml").read_text(encoding="utf-8", errors="strict")
            verify_sav = zonai_save_codec.ktml_to_progress_sav(base_text, stored_text)
            zonai_save_codec.progress_sav_to_ktml(verify_sav)
            if len(verify_sav) != zonai_save_codec.FILE_SIZE:
                raise ValueError("Post-write server compatibility verification failed.")
            steps.append("Verified replacement from disk with the server-derived exporter/loader.")
            self._log(f"Player save {uid} replaced safely; backup: {backup.name}")
            return {
                "backup": str(backup),
                "validation": validation,
                "steps": steps,
                "message": "Save uploaded successfully. Realm remains paused; resume only after reviewing this success result.",
            }
        except Exception as exc:
            rollback = False
            if replaced and backup and backup.is_file():
                try:
                    shutil.copy2(backup, target)
                    rollback = True
                    self._log(f"[save-upload] Rollback restored original player save for {uid}.")
                except Exception as rollback_exc:
                    self._log(f"[save upload error] Upload failed and rollback also failed for {uid}: {rollback_exc}")
            self._log(f"[save upload error] Player save upload failed for {uid} ({Path(filename).suffix.lower() or 'unknown'}): {exc}. Original {'restored' if rollback else 'preserved before replacement' if not replaced else 'may require manual restore' }.")
            raise
        finally:
            if incoming and incoming.exists():
                try: incoming.unlink()
                except OSError: pass

    def delete_player_save(self, uid: str):
        self._assert_save_upload_safe()
        target = self.player_save_path(uid)
        if not target: return False
        backup_dir = self.workspace / "Backups" / "DeletedPlayerSaves"; backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{uid}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.ktml"; shutil.copy2(target, backup); target.unlink()
        self._log(f"Deleted player save {uid}; backup retained at {backup.name}")
        return True

    def download_main_save(self, fmt: str = "ktml") -> tuple[bytes, str, str]:
        if not self.workspace:
            raise RuntimeError("No realm exists yet.")
        path = self.workspace / "User" / "SaveServer" / "save.ktml"
        if not path.is_file():
            raise FileNotFoundError("No server save.ktml exists yet.")
        if fmt.lower() == "ktml":
            return path.read_bytes(), f"{self.room_name or 'zonai-realm'}-server.ktml", "text/plain; charset=utf-8"
        sav = zonai_save_codec.ktml_to_progress_sav(path.read_text(encoding="utf-8", errors="strict"))
        return sav, f"{self.room_name or 'zonai-realm'}-server.sav", "application/octet-stream"

    def replace_main_save(self, raw: bytes, filename: str):
        """Safely import a realm-wide KTML or progress.sav while truly paused."""
        self._assert_save_upload_safe()
        target = self.workspace / "User" / "SaveServer" / "save.ktml"
        backup = None
        tmp = None
        replaced = False
        try:
            self._log(f"[save-upload] Validating main {Path(filename).suffix.upper().lstrip('.')} save…")
            text, validation = self._prepare_server_compatible_main_ktml(raw, filename)
            self._log(f"[save-upload] Main save compatibility verified: {validation['server_fields']} fields; SAV {validation['verified_sav_bytes']:,} bytes.")

            backup_dir = self.workspace / "Backups" / "MainServerSave"
            backup_dir.mkdir(parents=True, exist_ok=True)
            if target.is_file():
                backup = backup_dir / f"backup_before_upload_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.ktml"
                shutil.copy2(target, backup)
                self._log(f"[save-upload] Backed up existing main save: {backup.name}")

            tmp = self.workspace / "Temp" / "server-save.incoming.ktml"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            parse_ktml(tmp.read_text(encoding="utf-8", errors="strict"))
            os.replace(tmp, target)
            replaced = True

            stored = target.read_text(encoding="utf-8", errors="strict")
            verify = zonai_save_codec.ktml_to_progress_sav(stored)
            zonai_save_codec.progress_sav_to_ktml(verify)
            if len(verify) != zonai_save_codec.FILE_SIZE:
                raise ValueError("Post-write main-save verification failed.")
            self._log(f"Main server save replaced from {filename}." + (f" Backup: {backup.name}" if backup else ""))
            return {
                "backup": str(backup) if backup else None,
                "validation": validation,
                "steps": ["Validated server format.", "Backed up existing main save." if backup else "No previous main save existed.", "Atomically replaced save.ktml.", "Verified replacement with server-derived exporter/loader."],
                "message": "Main save uploaded successfully. Realm remains paused.",
            }
        except Exception as exc:
            rollback = False
            if replaced and backup and backup.is_file():
                try:
                    shutil.copy2(backup, target)
                    rollback = True
                    self._log("[save-upload] Rollback restored original main save.")
                except Exception as rollback_exc:
                    self._log(f"[save upload error] Main save rollback failed: {rollback_exc}")
            self._log(f"[save upload error] Main save upload failed ({Path(filename).suffix.lower() or 'unknown'}): {exc}. Original {'restored' if rollback else 'preserved before replacement' if not replaced else 'may require manual restore'}.")
            raise
        finally:
            if tmp and tmp.exists():
                try: tmp.unlink()
                except OSError: pass

    def merge_romfs_file(self, zip_path: str | Path, filename: str = "romfs.zip") -> int:
        """Safely merge a Romfs zip without deleting unrelated existing files.

        Accepts a path so very large texture/mod packs do not need to be held in
        Python memory. The HTTP layer streams uploads to a temporary file first.
        """
        if self.is_running():
            raise RuntimeError("Pause the realm before merging Romfs mods.")
        if not self.workspace:
            raise RuntimeError("Open a realm first.")
        if not filename.lower().endswith(".zip"):
            raise ValueError("Romfs mods must be provided as a .zip archive.")
        dest_root = (self.workspace / "Romfs").resolve(); dest_root.mkdir(parents=True, exist_ok=True)
        written = 0
        with zipfile.ZipFile(str(zip_path)) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            safe_names = [m.filename.replace('\\','/').lstrip('/') for m in members if '..' not in Path(m.filename.replace('\\','/')).parts]
            nonempty = [n for n in safe_names if n]
            strip_romfs = bool(nonempty) and all(n.lower().startswith('romfs/') for n in nonempty)
            for member in members:
                name = member.filename.replace('\\','/')
                if strip_romfs and name.lower().startswith('romfs/'): name = name[6:]
                name = name.strip('/')
                if not name: continue
                target = (dest_root / name).resolve()
                try: target.relative_to(dest_root)
                except Exception: continue  # zip-slip guard
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(target.name + ".zonai-incoming")
                with zf.open(member) as src, open(tmp, 'wb') as dst: shutil.copyfileobj(src, dst)
                os.replace(tmp, target); written += 1
        if written > 0:
            try:
                (self.workspace / ".sacred_romfs_modified").write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
            except Exception:
                pass
        self._log(f"Merged {written} Romfs mod file(s) from {filename}; unrelated existing files were preserved.")
        return written

    def reset_romfs_to_original(self) -> dict[str, Any]:
        """Restore the realm Romfs tree from the pristine bundled server runtime.

        Based on Wesley Da Man's September 15 Zonai Hosting reset-Romfs update,
        adapted for Sacred Zonai Realms' persistent Windows workspace layout.
        The realm must not be actively running.
        """
        if self.is_running():
            raise RuntimeError("Pause or stop the realm before resetting Romfs mods.")
        if not self.workspace:
            raise RuntimeError("Open a realm first.")
        pristine = resource_path("server_bundle/runtime/Romfs")
        if not pristine.is_dir():
            raise FileNotFoundError("The bundled original Romfs folder is missing.")
        target = self.workspace / "Romfs"
        # Replace the entire customized tree, matching Wesley's new Reset Romfs behavior.
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(pristine, target)
        marker = self.workspace / ".sacred_romfs_modified"
        try:
            marker.unlink(missing_ok=True)
        except TypeError:
            if marker.exists(): marker.unlink()
        self._log("Reset Romfs to the original bundled server files (Wesley Da Man Zonai Hosting update).")
        return {"message": "Romfs reset to the original bundled server files.", "romfs_modified": False}

    def merge_romfs_zip(self, raw: bytes, filename: str = "romfs.zip") -> int:
        import tempfile
        fd, temp_name = tempfile.mkstemp(prefix="zonai-romfs-", suffix=".zip")
        os.close(fd)
        try:
            Path(temp_name).write_bytes(raw)
            return self.merge_romfs_file(temp_name, filename)
        finally:
            try: os.remove(temp_name)
            except OSError: pass

    def kick_player(self, uid: str):
        if not uid:
            raise ValueError("Player UID is required.")
        self.send_command(f"kick {uid}")
        self._log(f"Kick requested for UID {uid}.")

    def ban_player(self, uid: str, include_ip: bool = True):
        if not uid:
            raise ValueError("Player UID is required.")
        self.send_command(("banip " if include_ip else "ban ") + uid)
        self._log(f"{'IP ban' if include_ip else 'UID ban'} requested for UID {uid}.")

    # ------------------------------ diagnostics / firewall / folders
    def diagnostics(self, selected_ip: str | None = None) -> dict[str, Any]:
        docker_ok, docker_detail = self.docker_preflight()
        java = shutil.which("java")
        port_free = self._port_free(self.port) if self.port and not self.is_running() else None
        ip = selected_ip or (self.detect_ipv4_addresses()[0]["ip"] if self.detect_ipv4_addresses() else self._lan_ip())
        return {
            "docker_ok": docker_ok, "docker_detail": docker_detail,
            "java_found": bool(java), "java_path": java,
            "selected_ip": ip, "port": self.port, "port_free_when_stopped": port_free,
            "workspace_ok": bool(self.workspace and self.workspace.is_dir()),
            "runtime_ok": self._runtime_ok(),
            "radmin_detected": any(r["kind"].startswith("Radmin") for r in self.detect_ipv4_addresses()),
        }

    def _runtime_ok(self) -> bool:
        if not self.workspace: return False
        try: self._repair_runtime(); return True
        except Exception: return False

    def allow_firewall_port(self):
        if os.name != "nt": raise RuntimeError("Windows Firewall helper is available on Windows only.")
        if not self.port: raise RuntimeError("Open a realm first so there is a port to allow.")
        name = f"Sacred Realms - Zonai Realm TCP {self.port}"
        script = f"$p={int(self.port)}; $n='{name}'; if (-not (Get-NetFirewallRule -DisplayName $n -ErrorAction SilentlyContinue)) {{ New-NetFirewallRule -DisplayName $n -Direction Inbound -Action Allow -Protocol TCP -LocalPort $p | Out-Null }}"
        # ShellExecute runas is the least surprising elevation path from a desktop app.
        cmd = ["powershell", "-NoProfile", "-Command", f"Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -Command \"{script}\"'"]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0: raise RuntimeError(r.stderr.strip() or "Firewall elevation request failed.")
        return name

    def open_workspace(self):
        target = self.workspace if self.workspace and self.workspace.exists() else self.data_root
        target.mkdir(parents=True, exist_ok=True)
        if os.name == "nt": os.startfile(target)
        elif sys.platform == "darwin": subprocess.Popen(["open", str(target)])
        else: subprocess.Popen(["xdg-open", str(target)])

    # ------------------------------ maintenance scheduler
    def _maintenance_loop(self):
        while not self._stop_event.wait(2.0):
            try:
                # Do not interpret the deliberate stop window inside
                # pause/restart/close as a crash. Lifecycle operations own
                # this lock; crash recovery only runs when it can acquire it.
                if not self.lifecycle_lock.acquire(blocking=False):
                    continue
                try:
                    with self.lock:
                        running = self.is_running(); deadline = self.deadline
                        should_be_live = bool(self.workspace and not self.realm_paused and self.status == "online")
                    if should_be_live and not running:
                        remaining = max(0, int((deadline - datetime.now()).total_seconds())) if deadline else 0
                        self.paused_remaining_seconds = remaining if self.duration_mode != "unlimited" else None
                        if self.duration_mode == "unlimited" and self.realm_started_at:
                            self.uptime_accumulated_seconds += max(0, int((datetime.now(timezone.utc) - self.realm_started_at).total_seconds()))
                            self.realm_started_at = None
                        self.deadline = None
                        self.realm_paused = True
                        self.status = "error"
                        self.connection_state = "Server stopped unexpectedly • realm preserved and ready for recovery"
                        for row in self.players.values():
                            row["online"] = False; row["game_ready"] = False
                        self._save_player_meta(); self._save_session_metadata()
                        self._log("[recovery] Server process/container stopped unexpectedly. Realm files and remaining time were preserved.")
                        continue
                    if running and deadline and datetime.now() >= deadline:
                        self._log("Realm duration reached zero; closing automatically.")
                        self.close(); continue
                    if running and time.monotonic() - self.last_roster_poll >= 30.0:
                        self.last_roster_poll = time.monotonic()
                        threading.Thread(target=self._poll_roster, daemon=True).start()
                finally:
                    self.lifecycle_lock.release()
            except Exception as exc:
                self._log(f"[maintenance warning] {exc}")

    def shutdown(self):
        self._stop_event.set()
        # v7.30: an Unlimited Docker realm is allowed to outlive the GUI.  Its
        # real start timestamp is persisted so reopening the app recovers the
        # same uptime instead of starting at 00:00:00. Timed/native realms keep
        # the conservative v7.26 behavior and are paused on application exit.
        try:
            if self.is_running() and self.duration_mode == "unlimited" and self.engine == "docker":
                self._save_session_metadata()
                self._log("Unlimited realm left running in Docker while the Sacred Zonai Realms GUI closes.")
                return
            if self.is_running():
                self.pause()
        except Exception as exc:
            self._log(f"[shutdown error] {exc}")


HOSTING = ZonaiRealmBackend()
