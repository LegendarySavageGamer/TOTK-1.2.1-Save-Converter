"""Read-only Light of Blessing / Shrine synchronization diagnostics for SZR v8.20.

This module NEVER writes the supplied KTML. It resolves the key-item slot by
Obj_DungeonClearSeal in the parallel Name array rather than hard-coding an index.
"""
from __future__ import annotations
from typing import Any
from totk_converter_core import parse_ktml

ITEM = "Obj_DungeonClearSeal"
MAX_LIGHTS_OF_BLESSING = 152
NAME_FIELD = "Pouch.KeyItem.Content.Name"
STOCK_FIELD = "Pouch.KeyItem.Content.StockNum"
RELATED_EXACT = (
    f"IsGet.{ITEM}", f"IsGetAnyway.{ITEM}",
    "HavePlayedEvent.DmF_SY_SmallDungeonGoal",
)
RELATED_PREFIXES = ("DungeonState.", "KeyCrystalDungeonState.")

def _section(doc: dict[str, Any], name: str) -> dict[str, Any]:
    value = doc.get(name, {})
    return value if isinstance(value, dict) else {}

def _array_values(value: Any) -> list[Any]:
    # KTML parser represents arrays as Python lists in current converter.
    return list(value) if isinstance(value, (list, tuple)) else []

def _find_field(doc: dict[str, Any], field: str):
    hits=[]
    for section, fields in doc.items():
        if isinstance(fields, dict) and field in fields:
            hits.append((section, fields[field]))
    return hits

def analyze_realm(text: str) -> dict[str, Any]:
    doc=parse_ktml(text)
    names=[]; stocks=[]; name_section=None; stock_section=None
    for section, fields in doc.items():
        if not isinstance(fields, dict): continue
        if NAME_FIELD in fields:
            names=_array_values(fields[NAME_FIELD]); name_section=section
        if STOCK_FIELD in fields:
            stocks=_array_values(fields[STOCK_FIELD]); stock_section=section
    indexes=[i for i,v in enumerate(names) if v == ITEM]
    resolved=indexes[0] if len(indexes)==1 else None
    qty=stocks[resolved] if resolved is not None and resolved < len(stocks) else None
    related={}
    for field in RELATED_EXACT:
        hits=_find_field(doc, field)
        related[field]=[{"section":s,"value":v} for s,v in hits]
    dungeon=[]
    for section, fields in doc.items():
        if not isinstance(fields, dict): continue
        for key,value in fields.items():
            if any(key.startswith(p) for p in RELATED_PREFIXES):
                dungeon.append({"section":section,"field":key,"value":value})
    return {
        "item": ITEM,
        "name_field": NAME_FIELD, "stock_field": STOCK_FIELD,
        "name_section": name_section, "stock_section": stock_section,
        "matching_indexes": indexes, "resolved_index": resolved,
        "quantity": qty,
        "recommended_maximum": MAX_LIGHTS_OF_BLESSING,
        "over_recommended_maximum": isinstance(qty, int) and qty > MAX_LIGHTS_OF_BLESSING,
        "player_warning": (
            "This save has more than 152 Lights of Blessing. Tears of the Kingdom has 152 Shrines. "
            "Using a higher amount can cause shared multiplayer Shrine progress to stop updating correctly. "
            "Please lower it to 152 or less before using this save in a Realm."
            if isinstance(qty, int) and qty > MAX_LIGHTS_OF_BLESSING else None
        ),
        "name_count": len(names), "stock_count": len(stocks),
        "parallel_arrays_aligned": bool(names) and len(names)==len(stocks),
        "related_exact": related,
        "dungeon_state_count": len(dungeon),
        "dungeon_state_sample": dungeon[:40],
        "sync_status": "UNKNOWN",
        "sync_reason": "A persisted Realm snapshot alone cannot prove live multiplayer synchronization.",
        "safe_to_repair": False,
        "repair_reason": "The exact Shrine completion state transition and live server/client authority conflict are not yet fully proven.",
        "original_modified": False,
    }

def compare_realms(before_text: str, after_text: str) -> dict[str, Any]:
    before=parse_ktml(before_text); after=parse_ktml(after_text)
    changes=[]
    sections=sorted(set(before)|set(after))
    for section in sections:
        a=before.get(section,{}); b=after.get(section,{})
        if not isinstance(a,dict) or not isinstance(b,dict): continue
        for key in sorted(set(a)|set(b)):
            av=a.get(key, "<MISSING>"); bv=b.get(key, "<MISSING>")
            if av != bv:
                changes.append({"section":section,"field":key,"before":av,"after":bv})
    focus=[]
    terms=("DungeonClearSeal","SmallDungeon","DungeonState.","KeyCrystalDungeonState.","Shrine")
    for c in changes:
        if c["field"] in (NAME_FIELD,STOCK_FIELD) or any(t in c["field"] for t in terms):
            focus.append(c)
    return {"total_changes":len(changes),"focus_changes":focus,"all_changes":changes[:2000],"truncated":len(changes)>2000,"original_modified":False}
