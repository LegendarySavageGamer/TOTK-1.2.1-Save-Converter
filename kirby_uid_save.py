"""Kirbymimi UID child-save -> editor-ready progress.sav support for Sacred Zonai Realms v8.20.

A Kirbymimi player file is a child/overlay KTML.  It is not a complete
progress.sav by itself.  The real server exports a client save by merging
User/SaveServer/save.ktml (base) with users/<UID>.ktml (overlay).
"""
from __future__ import annotations
import base64, re, struct
from pathlib import Path
from typing import Any
import wesley_zonai_save_converter as codec

_UID_RE = re.compile(r"^[A-Za-z0-9+/]{22}==$")
# Hashes Marc Robledo's bundled TOTK editor explicitly requires in _getOffsets().
_MARC_REQUIRED_HASHES = (
    0xfbe01da1,0xa77921d7,0xf9212c74,0x15ec5858,0xe573f564,0xafd01d68,
    0xc884818d,0x1d6189da,0xd7a3f6ba,0xc61785c2,0x05271e7d,0x14d7f4c4,
    0xf24fc2e7,0xd2025694,0xd27f8651,0xa56722b6,0xc5bf2815,0xef74dca7,
)
_META_SAVE_TYPE_HASH = 0xa3db7114

def uid_from_filename(filename: str) -> str | None:
    stem = Path(filename or "").stem
    if not _UID_RE.fullmatch(stem):
        return None
    try:
        raw = base64.b64decode(stem, validate=True)
    except Exception:
        return None
    return stem if len(raw) == 16 else None

def _field_count(maps: dict[str, Any]) -> int:
    return sum(len(v) for v in maps.values() if isinstance(v, dict))

def _marc_precheck(data: bytes) -> dict[str, Any]:
    result = {
        "file_size_ok": len(data) == codec.FILE_SIZE,
        "magic_ok": False, "header_ok": False, "metadata_start_ok": False,
        "metadata_marker_found": False, "required_editor_hashes_found": False,
        "missing_editor_hashes": [],
    }
    if len(data) < 40:
        return result
    magic, header, meta = struct.unpack_from("<III", data, 0)
    result["magic_ok"] = magic == 0x01020304
    result["header_ok"] = header == 0x0047E0F4
    result["metadata_start_ok"] = meta == 0x0003C088
    hashes = set()
    for pos in range(0x28, min(len(data)-4, 0x100000), 8):
        h = struct.unpack_from("<I", data, pos)[0]
        hashes.add(h)
        if h == _META_SAVE_TYPE_HASH:
            result["metadata_marker_found"] = True
            break
    missing = [f"0x{x:08x}" for x in _MARC_REQUIRED_HASHES if x not in hashes]
    result["missing_editor_hashes"] = missing
    result["required_editor_hashes_found"] = not missing
    result["structural_precheck_passed"] = all((
        result["file_size_ok"], result["magic_ok"], result["header_ok"],
        result["metadata_start_ok"], result["metadata_marker_found"],
        result["required_editor_hashes_found"],
    ))
    return result

def analyze(uid_text: str, filename: str, base_text: str | None = None) -> dict[str, Any]:
    uid = uid_from_filename(filename)
    child_maps = codec.parse_ktml(uid_text)
    report: dict[str, Any] = {
        "recognized": True,
        "uid": uid,
        "uid_from_filename": bool(uid),
        "filename": Path(filename or "player.ktml").name,
        "player_fields": _field_count(child_maps),
        "base_supplied": bool(base_text),
        "storage_model": "Kirbymimi child/overlay KTML (User/SaveServer/users/<UID>.ktml)",
        "original_modified": False,
        "save_editor_compatibility": "NOT TESTED",
    }
    if base_text:
        base_maps = codec.parse_ktml(base_text)
        report["base_fields"] = _field_count(base_maps)
        data = codec.ktml_to_progress_sav(base_text, uid_text)
        # Reparse with the same server-derived binary loader as a corruption/type check.
        codec.progress_sav_to_ktml(data)
        report.update(_marc_precheck(data))
        report["output_bytes"] = len(data)
        report["roundtrip_parse_passed"] = True
    return report

def convert(uid_text: str, filename: str, base_text: str) -> tuple[bytes, dict[str, Any]]:
    if not base_text:
        raise ValueError("Kirby's UID player save needs the realm's User/SaveServer/save.ktml base save to build a complete progress.sav.")
    report = analyze(uid_text, filename, base_text)
    data = codec.ktml_to_progress_sav(base_text, uid_text)
    if len(data) != codec.FILE_SIZE:
        raise ValueError("Generated progress.sav does not have the expected Tears of the Kingdom 1.2.x size.")
    codec.progress_sav_to_ktml(data)
    report["progress_sav_generated"] = True
    report["output_validation_passed"] = bool(report.get("structural_precheck_passed"))
    return data, report


def project_edited_sav_to_uid(sav_bytes: bytes, uid_text: str, filename: str, base_text: str) -> tuple[str, dict[str, Any]]:
    """Project an edited *full* progress.sav back into the original Kirby UID child schema.

    The binary save contains the merged Realm + player view.  It must never be
    written directly to users/<UID>.ktml.  The untouched UID file is the schema
    authority: only fields already owned by that child (same KTML section/type
    and same key) are copied back.  Overlay-only fields that the binary exporter
    cannot represent are preserved from the original child.  Bool64bitKey is
    also preserved because progress.sav contains the UNION of Realm + child keys
    and cannot prove ownership of an individual key.
    """
    if not base_text:
        raise ValueError("The matching Realm save.ktml is required when projecting an edited progress.sav back to a Kirby UID save.")
    uid = uid_from_filename(filename)
    child = codec.parse_ktml(uid_text)
    base = codec.parse_ktml(base_text)
    analysis = codec.analyze_progress_sav(sav_bytes)
    full = analysis.pop("maps")

    projected: dict[str, Any] = {}
    preserved = updated = missing = 0
    changed_examples = []
    type_mismatches = []
    for type_name in codec.ALL_TYPES:
        schema = child.get(type_name, {})
        if not isinstance(schema, dict) or not schema:
            continue
        src = full.get(type_name, {})
        if not isinstance(src, dict):
            src = {}
        out = {}
        for key, old_value in schema.items():
            # Binary Bool64bitKey is base+child union; ownership is unrecoverable.
            if type_name == "Bool64bitKey" or key not in src:
                out[key] = old_value
                preserved += 1
                if key not in src:
                    missing += 1
                continue
            new_value = src[key]
            # Section membership is the type schema. Never infer Bool/Int from value truthiness.
            if type_name == "Bool" and not isinstance(new_value, bool):
                type_mismatches.append(f"{type_name}.{key}: expected bool, got {type(new_value).__name__}")
                out[key] = old_value; preserved += 1; continue
            if type_name in {"Int", "Enum", "UInt", "Int64", "UInt64"} and isinstance(new_value, bool):
                type_mismatches.append(f"{type_name}.{key}: expected numeric, got bool")
                out[key] = old_value; preserved += 1; continue
            out[key] = new_value
            updated += 1
            if new_value != old_value and len(changed_examples) < 25:
                changed_examples.append({"field": f"{type_name}.{key}", "before": repr(old_value)[:160], "after": repr(new_value)[:160]})
        projected[type_name] = out

    if type_mismatches:
        raise ValueError("Edited SAV type validation failed: " + "; ".join(type_mismatches[:10]))

    out_text = codec.maps_to_ktml_text(projected)
    reparsed = codec.parse_ktml(out_text)
    # Hard invariant: the output child has exactly the original section/key schema.
    before_schema = {(t, k) for t,v in child.items() if isinstance(v,dict) for k in v}
    after_schema = {(t, k) for t,v in reparsed.items() if isinstance(v,dict) for k in v}
    if before_schema != after_schema:
        raise ValueError("UID projection validation failed: output field/type schema differs from the original UID child save.")

    # Validate the projected child by rebuilding the same server merge and decoding it.
    merged = codec.ktml_to_progress_sav(base_text, out_text)
    codec.progress_sav_to_ktml(merged)
    report = {
        "uid": uid,
        "filename": Path(filename or "player.ktml").name,
        "input_sav_bytes": len(sav_bytes),
        "original_uid_fields": len(before_schema),
        "output_uid_fields": len(after_schema),
        "fields_updated_from_edited_sav": updated,
        "fields_preserved_from_original_uid": preserved,
        "fields_not_representable_in_sav": missing,
        "type_mismatches": 0,
        "bool64_policy": "preserved from original UID; merged SAV contains Realm+player union",
        "schema_authority": "original UID KTML section + field name",
        "changed_examples": changed_examples,
        "merged_validation_bytes": len(merged),
        "roundtrip_binary_parse_passed": True,
        "multiplayer_synchronization": "NOT TESTED",
    }
    return out_text, report


def synchronize_edited_sav_pair(sav_bytes: bytes, uid_text: str, filename: str, base_text: str) -> tuple[str, str, dict[str, Any]]:
    """Route edited merged progress.sav changes back to their original Kirby owners.

    Ownership is learned from the untouched input pair:
      * a (section,key) present in the UID child is player-owned and updates UID;
      * otherwise, a (section,key) present in save.ktml is Realm-owned and updates save.ktml;
      * fields absent from both original schemas are unsupported and are never invented.

    This deliberately operates at KTML field granularity. Array fields remain arrays, so
    their element types and ordering are preserved exactly by the codec. Unknown fields
    are copied from the original owner unchanged.
    """
    if not base_text:
        raise ValueError("The matching Realm save.ktml is required for synchronized conversion.")
    if len(sav_bytes) != codec.FILE_SIZE:
        raise ValueError(f"Edited progress.sav must be exactly {codec.FILE_SIZE:,} bytes.")
    uid = uid_from_filename(filename)
    child = codec.parse_ktml(uid_text)
    base = codec.parse_ktml(base_text)
    edited_analysis = codec.analyze_progress_sav(sav_bytes)
    edited = edited_analysis.pop("maps")

    # Recreate the untouched merged binary through the same server-derived path and
    # parse it. This is the baseline the user actually edited, not a guessed default.
    original_sav = codec.ktml_to_progress_sav(base_text, uid_text)
    original_analysis = codec.analyze_progress_sav(original_sav)
    original = original_analysis.pop("maps")

    out_child = {t: dict(v) for t,v in child.items() if isinstance(v, dict)}
    out_base = {t: dict(v) for t,v in base.items() if isinstance(v, dict)}

    player_changes=[]; realm_changes=[]; unsupported=[]; type_errors=[]
    all_types = codec.ALL_TYPES
    for t in all_types:
        old_sec = original.get(t,{}) if isinstance(original.get(t,{}),dict) else {}
        new_sec = edited.get(t,{}) if isinstance(edited.get(t,{}),dict) else {}
        child_sec = child.get(t,{}) if isinstance(child.get(t,{}),dict) else {}
        base_sec = base.get(t,{}) if isinstance(base.get(t,{}),dict) else {}
        for key in sorted(set(old_sec)|set(new_sec)):
            if key not in old_sec or key not in new_sec:
                if old_sec.get(key) != new_sec.get(key):
                    unsupported.append({"type":t,"field":key,"reason":"field added/removed by edited SAV"})
                continue
            before, after = old_sec[key], new_sec[key]
            if before == after:
                continue
            # Section membership is the type. Never infer type from truthiness/value.
            if t == "Bool" and not isinstance(after,bool):
                type_errors.append(f"{t}.{key}: expected bool, got {type(after).__name__}"); continue
            if t in {"Int","Enum","UInt","Int64","UInt64"} and isinstance(after,bool):
                type_errors.append(f"{t}.{key}: numeric field became bool"); continue
            rec={"type":t,"field":key,"before":before,"after":after}
            if key in child_sec:
                out_child.setdefault(t,{})[key]=after
                player_changes.append(rec)
            elif key in base_sec:
                out_base.setdefault(t,{})[key]=after
                realm_changes.append(rec)
            else:
                rec["reason"]="not owned by original UID or Realm schema"
                unsupported.append(rec)

    if type_errors:
        raise ValueError("Type validation failed: " + "; ".join(type_errors[:10]))

    child_text = codec.maps_to_ktml_text(out_child)
    base_out_text = codec.maps_to_ktml_text(out_base)
    child_check=codec.parse_ktml(child_text); base_check=codec.parse_ktml(base_out_text)

    def schema(m): return {(t,k) for t,v in m.items() if isinstance(v,dict) for k in v}
    if schema(child_check) != schema(child):
        raise ValueError("UID output schema changed; synchronized update aborted.")
    if schema(base_check) != schema(base):
        raise ValueError("Realm output schema changed; synchronized update aborted.")

    # Pair validation: rebuild the merged save from BOTH proposed outputs.
    merged = codec.ktml_to_progress_sav(base_out_text, child_text)
    codec.progress_sav_to_ktml(merged)
    merged_maps = codec.analyze_progress_sav(merged).pop("maps")

    # Every routed field must reproduce the edited SAV value after the server merge.
    validation_errors=[]
    for rec in player_changes + realm_changes:
        got=merged_maps.get(rec["type"],{}).get(rec["field"], object())
        if got != rec["after"]:
            validation_errors.append(f"{rec['type']}.{rec['field']}")
    if validation_errors:
        raise ValueError("Pair validation failed for routed fields: " + ", ".join(validation_errors[:20]))

    # Explicit Light-of-Blessing diagnostics, dynamically resolved by Name[] index.
    def blessing_state(m):
        names=m.get("String64Array",{}).get("Pouch.KeyItem.Content.Name",[])
        stocks=m.get("IntArray",{}).get("Pouch.KeyItem.Content.StockNum",[])
        idx=[i for i,x in enumerate(names) if x=="Obj_DungeonClearSeal"]
        i=idx[0] if len(idx)==1 else None
        return {"index":i,"quantity":stocks[i] if i is not None and i < len(stocks) else None}
    before_bless=blessing_state(base)
    after_bless=blessing_state(base_check)

    report={
        "uid":uid,
        "ownership_rule":"original KTML section+field schema: UID wins ownership when present; otherwise Realm save.ktml",
        "player_changes":len(player_changes), "realm_changes":len(realm_changes),
        "unsupported_changes":len(unsupported), "type_errors":0,
        "player_change_details":player_changes[:500], "realm_change_details":realm_changes[:500],
        "unsupported_details":unsupported[:200],
        "uid_fields":len(schema(child)), "realm_fields":len(schema(base)),
        "uid_schema_preserved":True, "realm_schema_preserved":True,
        "pair_validation_passed":True, "merged_validation_bytes":len(merged),
        "light_of_blessing_before":before_bless, "light_of_blessing_after":after_bless,
        "light_of_blessing_index_policy":"resolved by Obj_DungeonClearSeal in Name[]; never hard-coded",
        "original_files_modified":False,
        "live_multiplayer_tested":False,
    }
    return child_text, base_out_text, report
