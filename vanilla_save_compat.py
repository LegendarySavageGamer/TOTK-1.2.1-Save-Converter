from __future__ import annotations
from pathlib import Path
from datetime import datetime
import json, os, re
import wesley_zonai_save_converter as codec


def _backup_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "SacredZonaiRealms" / "Backups" / "VanillaImports"


def backup_original(raw: bytes, filename: str) -> Path:
    root = _backup_root(); root.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(filename or "progress.sav").name)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = root / f"{stamp}_{safe}"
    path.write_bytes(bytes(raw))
    return path


def analyze(raw: bytes, filename: str = "progress.sav", server_schema_text: str | None = None) -> dict:
    schema = codec.parse_ktml(server_schema_text) if server_schema_text else None
    report = codec.analyze_progress_sav(raw, schema)
    report.pop("maps", None)
    report["input"] = Path(filename).name
    report["original_modified"] = False
    return report


def convert(raw: bytes, filename: str = "progress.sav") -> tuple[str, dict]:
    backup = backup_original(raw, filename)
    text, report = codec.progress_sav_to_ktml_with_report(raw)
    report["input"] = Path(filename).name
    report["backup_created"] = True
    report["backup_path"] = str(backup)
    report["original_modified"] = False
    return text, report
