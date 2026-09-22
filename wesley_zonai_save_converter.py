"""
KTML -> progress.sav conversion, ported directly from the real TOTK
multiplayer server's own save-format code -- see (in SERVER DECOMP)
com/kirbymimi/totkServer/save/SaveServerBinaryExporter.java,
SaveServerKTMLLoader.java, SaveServer.java, and TOTKSaveData.java. This
replaces an earlier version of this module based on reverse-engineering
by a third party; that approach only handled about half of the real
field types and worked from a guessed hash function instead of the
server's own authoritative name -> hash table, so it's been dropped in
favor of this direct port.

Key things this ports faithfully from the real exporter:

* progress.sav is built from scratch for the given save data -- there is
  no "template" file involved, unlike the previous approach. The header,
  field table, and data region are all sized and laid out dynamically
  from whatever fields are present, then the whole thing is zero-padded
  out to the fixed 2,307,656-byte size every real save file has.
* Every progress.sav is inherently a *merge* of two save sources: a
  base/authoritative one (the server-wide save.ktml) and an optional
  overlay (a specific player's users/<uid>.ktml) whose matching fields
  override the base's. A field that exists only in the overlay, not in
  the base, is silently dropped -- matching the real exporter exactly.
  The 64-bit "key" set (Bool64bitKey) is always the union of both,
  regardless of the override rule above -- also matches the real code.
* Field name -> hash lookups come from the server's own
  Resources/SaveServer/name2hash.ktml (loaded once, lazily, straight off
  disk -- it's ~1.6MB/27,000 entries, too large to be worth vendoring a
  copy of), with the same "unkNNNN" fallback the real server uses for a
  hash with no known name.
"""
import os
import re
import struct

ALL_TYPES = [
    "Bool", "BoolArray", "Int", "IntArray", "Float", "FloatArray",
    "Enum", "EnumArray", "Vector2", "Vector2Array", "Vector3",
    "Vector3Array", "String16", "String16Array", "String32",
    "String32Array", "String64", "String64Array", "Binary",
    "BinaryArray", "UInt", "UIntArray", "Int64", "Int64Array",
    "UInt64", "UInt64Array", "WString16", "WString16Array",
    "WString32", "WString32Array", "WString64", "WString64Array",
    "Bool64bitKey",
]

MAGIC1 = 16909060
MAGIC2 = 4710644
FILE_SIZE = 2307656  # server-export layout size; newer vanilla hardware layouts can be larger
BOOL64_KEY_NAME = "unk2749067540"  # the one key TOTKSaveData ever uses for this type

_NAME2HASH_LINE_RE = re.compile(r'^"((?:\\.|[^"\\])*)"\s*:\s*(-?\d+)\s*$')
_UNK_NAME_RE = re.compile(r'^unk(-?\d+)$')

# name2hash.ktml lives with the rest of the server's Resources, a sibling
# of this Website/ directory -- read directly from there rather than
# keeping a separate copy that could drift out of sync with the image.
def _candidate_save_server_dirs():
    """Locations that may contain the server-authoritative name2hash.ktml.

    TOTK_NAME2HASH_PATH may point directly at the file.  Packaged Windows
    builds may bundle it under resources/SaveServer, while the original
    repository layout keeps it beside Website/ under Docker Server/.
    """
    explicit = os.environ.get("TOTK_NAME2HASH_PATH")
    if explicit:
        yield os.path.dirname(os.path.abspath(explicit))

    here = os.path.dirname(os.path.abspath(__file__))
    yield os.path.join(here, "server_bundle", "runtime", "Resources", "SaveServer")
    yield os.path.join(here, "resources", "SaveServer")
    yield os.path.join(here, "..", "Docker Server", "Resources", "SaveServer")


def _name2hash_path():
    explicit = os.environ.get("TOTK_NAME2HASH_PATH")
    if explicit and os.path.isfile(explicit):
        return explicit
    for folder in _candidate_save_server_dirs():
        candidate = os.path.join(folder, "name2hash.ktml")
        if os.path.isfile(candidate):
            return candidate
    # Return the packaged-style path so the resulting error is actionable.
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_bundle", "runtime", "Resources", "SaveServer", "name2hash.ktml")

_name_to_hash_cache = None


class ConversionError(Exception):
    """A KTML save couldn't be converted to progress.sav."""


def _load_name_to_hash():
    global _name_to_hash_cache
    if _name_to_hash_cache is not None:
        return _name_to_hash_cache

    path = _name2hash_path()
    table = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                m = _NAME2HASH_LINE_RE.match(line)
                if not m:
                    raise ConversionError(
                        f"name2hash.ktml line {line_no} doesn't look like "
                        f'"Name": hash -- got {line!r}'
                    )
                name = m.group(1).encode("utf-8").decode("unicode_escape")
                table[name] = int(m.group(2)) & 0xFFFFFFFF
    except OSError as e:
        raise ConversionError(
            f"Could not read the server's name2hash.ktml at {path}: {e}"
        ) from e

    _name_to_hash_cache = table
    return table


def _hash_for(name, name_to_hash):
    h = name_to_hash.get(name)
    if h is not None:
        return h
    m = _UNK_NAME_RE.match(name)
    if m:
        return int(m.group(1)) & 0xFFFFFFFF
    raise ConversionError(f"Unknown save field (not in name2hash.ktml): {name!r}")


# ---------------------------------------------------------------------------
# KTML parsing -- generic enough to parse save.ktml/<uid>.ktml's nested
# "TypeName": { "fieldName": value, ... } structure, including the
# vector/array sub-structuring SaveServerKTMLExporter produces.
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r'\s*("(?:\\.|[^"\\])*"|-?\d+\.\d+(?:[eE][+-]?\d+)?|-?\d+(?:[eE][+-]?\d+)?|true|false|\{|\}|:)'
)


def parse_ktml(text):
    import json

    tokens = [m.group(1) for m in _TOKEN_RE.finditer(text)]
    pos = 0

    def value():
        nonlocal pos
        if pos >= len(tokens):
            raise ValueError("Unexpected end of KTML file.")
        tok = tokens[pos]

        if tok == "{":
            pos += 1
            if pos < len(tokens) and tokens[pos] == "}":
                pos += 1
                return []
            is_map = (
                pos < len(tokens)
                and tokens[pos].startswith('"')
                and pos + 1 < len(tokens)
                and tokens[pos + 1] == ":"
            )
            if is_map:
                out = {}
                while pos < len(tokens) and tokens[pos] != "}":
                    key = json.loads(tokens[pos])
                    pos += 1
                    if pos >= len(tokens) or tokens[pos] != ":":
                        raise ValueError("Malformed KTML mapping.")
                    pos += 1
                    out[key] = value()
                if pos >= len(tokens):
                    raise ValueError("Unclosed KTML mapping.")
                pos += 1
                return out
            arr = []
            while pos < len(tokens) and tokens[pos] != "}":
                arr.append(value())
            if pos >= len(tokens):
                raise ValueError("Unclosed KTML array.")
            pos += 1
            return arr

        if tok.startswith('"'):
            pos += 1
            return json.loads(tok)
        if tok == "true":
            pos += 1
            return True
        if tok == "false":
            pos += 1
            return False
        pos += 1
        return float(tok) if any(c in tok for c in ".eE") else int(tok)

    root = {}
    while pos < len(tokens):
        key = json.loads(tokens[pos])
        pos += 1
        if pos >= len(tokens) or tokens[pos] != ":":
            raise ValueError("Malformed KTML top-level entry.")
        pos += 1
        root[key] = value()
    return root


# ---------------------------------------------------------------------------
# Field encoding -- mirrors SaveServerBinaryExporter's per-type processors.
# Returns (inline_bytes_or_None, blob_bytes_or_None): exactly one is set.
# inline_bytes (always 4 bytes) goes straight into the field table; blob
# bytes get written out in the data region, with their offset recorded in
# the table instead.
# ---------------------------------------------------------------------------

def _pack_cstr(value, slot_size):
    b = str(value).encode("utf-8")
    if len(b) > slot_size - 1:
        raise ConversionError(f"String {value!r} too long for its {slot_size}-byte slot.")
    return b + b"\x00" * (slot_size - len(b))


def _pack_wstr(value, slot_size):
    b = str(value).encode("utf-16-le")
    if len(b) > slot_size - 2:
        raise ConversionError(f"Wide string {value!r} too long for its {slot_size}-byte slot.")
    return b + b"\x00" * (slot_size - len(b))


def _pack_bytes(value):
    # Binary/BinaryArray fields round-trip through KTML as a plain array
    # of individual byte values (Java's exporter treats byte[] as just
    # another reflective array type), not as a compact string -- so
    # `value` here is a list of small ints, not a bytes object.
    return bytes(int(v) & 0xFF for v in value)


def _coerce_s32(value, *, field_type="Int"):
    """Encode the low 32 bits using Java-compatible signed int semantics.

    KTML/JSON tooling can expose a 32-bit bit-pattern as 0..4294967295 even
    when the binary field is a signed Java int. struct.pack("<i") rejects
    values above 2147483647, so convert the same 32-bit pattern to its signed
    two's-complement representation before packing.
    """
    n = int(value)
    if n < -0x80000000 or n > 0xFFFFFFFF:
        raise ConversionError(
            f"{field_type} value {n} is outside the supported 32-bit range "
            f"(-2147483648 through 4294967295)."
        )
    n &= 0xFFFFFFFF
    return n - 0x100000000 if n >= 0x80000000 else n


def _encode_field(type_name, value):
    if type_name == "Bool":
        return struct.pack("<i", 1 if value else 0), None
    if type_name == "Int":
        return struct.pack("<i", _coerce_s32(value, field_type="Int")), None
    if type_name == "Float":
        return struct.pack("<f", float(value)), None
    if type_name == "Enum":
        return struct.pack("<i", _coerce_s32(value, field_type="Enum")), None
    if type_name == "UInt":
        return struct.pack("<I", int(value) & 0xFFFFFFFF), None

    if type_name == "BoolArray":
        n = len(value)
        packed = bytearray((n + 7) // 8)
        for i, v in enumerate(value):
            if v:
                packed[i // 8] |= 1 << (i % 8)
        blob = struct.pack("<i", n) + bytes(packed)
        blob += b"\x00" * ((-len(blob)) % 4)  # align up to 4 bytes, like the real exporter
        return None, blob
    if type_name == "IntArray":
        blob = struct.pack("<i", len(value)) + b"".join(struct.pack("<i", _coerce_s32(v, field_type="IntArray")) for v in value)
        return None, blob
    if type_name == "FloatArray":
        blob = struct.pack("<i", len(value)) + b"".join(struct.pack("<f", float(v)) for v in value)
        return None, blob
    if type_name == "EnumArray":
        blob = struct.pack("<i", len(value)) + b"".join(struct.pack("<i", _coerce_s32(v, field_type="EnumArray")) for v in value)
        return None, blob
    if type_name == "Vector2":
        return None, struct.pack("<ff", float(value["x"]), float(value["y"]))
    if type_name == "Vector2Array":
        blob = struct.pack("<i", len(value)) + b"".join(
            struct.pack("<ff", float(v["x"]), float(v["y"])) for v in value
        )
        return None, blob
    if type_name == "Vector3":
        return None, struct.pack("<fff", float(value["x"]), float(value["y"]), float(value["z"]))
    if type_name == "Vector3Array":
        blob = struct.pack("<i", len(value)) + b"".join(
            struct.pack("<fff", float(v["x"]), float(v["y"]), float(v["z"])) for v in value
        )
        return None, blob
    if type_name == "String16":
        return None, _pack_cstr(value, 16)
    if type_name == "String16Array":
        blob = struct.pack("<i", len(value)) + b"".join(_pack_cstr(v, 16) for v in value)
        return None, blob
    if type_name == "String32":
        return None, _pack_cstr(value, 32)
    if type_name == "String32Array":
        blob = struct.pack("<i", len(value)) + b"".join(_pack_cstr(v, 32) for v in value)
        return None, blob
    if type_name == "String64":
        return None, _pack_cstr(value, 64)
    if type_name == "String64Array":
        blob = struct.pack("<i", len(value)) + b"".join(_pack_cstr(v, 64) for v in value)
        return None, blob
    if type_name == "Binary":
        data = _pack_bytes(value)
        return None, struct.pack("<i", len(data)) + data
    if type_name == "BinaryArray":
        parts = [struct.pack("<i", len(value))]
        for v in value:
            data = _pack_bytes(v)
            parts.append(struct.pack("<i", len(data)))
            parts.append(data)
        return None, b"".join(parts)
    if type_name == "UIntArray":
        blob = struct.pack("<i", len(value)) + b"".join(
            struct.pack("<I", int(v) & 0xFFFFFFFF) for v in value
        )
        return None, blob
    if type_name == "Int64":
        return None, struct.pack("<q", int(value))
    if type_name == "Int64Array":
        blob = struct.pack("<i", len(value)) + b"".join(struct.pack("<q", int(v)) for v in value)
        return None, blob
    if type_name == "UInt64":
        return None, struct.pack("<Q", int(value) & 0xFFFFFFFFFFFFFFFF)
    if type_name == "UInt64Array":
        blob = struct.pack("<i", len(value)) + b"".join(
            struct.pack("<Q", int(v) & 0xFFFFFFFFFFFFFFFF) for v in value
        )
        return None, blob
    if type_name == "WString16":
        return None, _pack_wstr(value, 32)
    if type_name == "WString16Array":
        blob = struct.pack("<i", len(value)) + b"".join(_pack_wstr(v, 32) for v in value)
        return None, blob
    if type_name == "WString32":
        return None, _pack_wstr(value, 64)
    if type_name == "WString32Array":
        blob = struct.pack("<i", len(value)) + b"".join(_pack_wstr(v, 64) for v in value)
        return None, blob
    if type_name == "WString64":
        return None, _pack_wstr(value, 128)
    if type_name == "WString64Array":
        blob = struct.pack("<i", len(value)) + b"".join(_pack_wstr(v, 128) for v in value)
        return None, blob

    raise ConversionError(f"Unhandled save field type: {type_name}")


class _GrowableBuffer:
    """Minimal stand-in for the real exporter's seekable, auto-growing
    output stream -- supports writing at an arbitrary absolute position,
    zero-filling any newly-touched space automatically."""

    def __init__(self):
        self._data = bytearray()

    def write_at(self, pos, data):
        end = pos + len(data)
        if end > len(self._data):
            self._data.extend(b"\x00" * (end - len(self._data)))
        self._data[pos:end] = data

    def bytes(self):
        return bytes(self._data)


def export_progress_sav(base_maps, overlay_maps, name_to_hash):
    """Build progress.sav bytes for base_maps (a dict of type_name ->
    {field_name: value}, e.g. from parse_ktml on save.ktml) with
    overlay_maps's matching fields overriding base_maps's (e.g. from a
    specific player's users/<uid>.ktml) -- mirrors
    SaveServerBinaryExporter.export(save, saveChild) exactly, including
    that a field present only in overlay_maps, not base_maps, is never
    exported, and that the 64-bit key set is always the union of both
    regardless of that override rule.

    Pass overlay_maps={} for a standalone export of base_maps alone (no
    player-specific overlay)."""
    overlay_maps = overlay_maps or {}

    total_entries = sum(len(base_maps.get(t, {})) for t in ALL_TYPES)
    data_offset = 32 + 8 * len(ALL_TYPES) + 8 * total_entries

    buf = _GrowableBuffer()
    buf.write_at(0, struct.pack("<iii", MAGIC1, MAGIC2, data_offset))
    # Bytes 12..31 (20 bytes) are left zero, matching the real exporter's
    # unexplained fixed skip there.

    pos = 32
    for idx, type_name in enumerate(ALL_TYPES):
        buf.write_at(pos, struct.pack("<ii", 0, idx))  # type-boundary marker
        pos += 8

        base_map = base_maps.get(type_name, {})
        if isinstance(base_map, list):
            base_map = {}  # an empty "TypeName": { } block parses as [] -- treat as no fields
        overlay_map = overlay_maps.get(type_name, {})
        if isinstance(overlay_map, list):
            overlay_map = {}

        for key, base_value in base_map.items():
            h = _hash_for(key, name_to_hash)

            if type_name == "Bool64bitKey":
                base_set = base_map.get(BOOL64_KEY_NAME, []) or []
                overlay_set = overlay_map.get(BOOL64_KEY_NAME, []) or []
                merged = sorted(
                    {int(v) & 0xFFFFFFFFFFFFFFFF for v in base_set}
                    | {int(v) & 0xFFFFFFFFFFFFFFFF for v in overlay_set}
                )
                blob = b"".join(struct.pack("<Q", v) for v in merged) + b"\x00" * 8
                inline = None
            else:
                value = overlay_map[key] if key in overlay_map else base_value
                inline, blob = _encode_field(type_name, value)

            if blob is not None:
                buf.write_at(data_offset, blob)
                table_value = data_offset
                data_offset += len(blob)
            else:
                buf.write_at(pos + 4, inline)
                table_value = None

            if table_value is not None:
                buf.write_at(pos, struct.pack("<I", h & 0xFFFFFFFF))
                buf.write_at(pos + 4, struct.pack("<I", table_value & 0xFFFFFFFF))
            else:
                buf.write_at(pos, struct.pack("<I", h & 0xFFFFFFFF))
            pos += 8

    if data_offset > FILE_SIZE:
        raise ConversionError(
            f"This save is too large to fit in a standard progress.sav "
            f"({data_offset} bytes needed, {FILE_SIZE} available)."
        )

    buf.write_at(FILE_SIZE - 1, b"\x00")  # zero-pad out to the fixed real file size
    return buf.bytes()[:FILE_SIZE]


def ktml_to_progress_sav(base_ktml_text, overlay_ktml_text=None):
    """Convert save.ktml text (base_ktml_text) -- optionally merged with
    a specific player's users/<uid>.ktml text (overlay_ktml_text) the
    same way the real server merges them when generating that player's
    actual save -- into progress.sav bytes."""
    name_to_hash = _load_name_to_hash()

    try:
        base_maps = parse_ktml(base_ktml_text)
    except Exception as e:
        raise ConversionError(f"Could not parse the server save's KTML: {e}") from e

    overlay_maps = {}
    if overlay_ktml_text:
        try:
            overlay_maps = parse_ktml(overlay_ktml_text)
        except Exception as e:
            raise ConversionError(f"Could not parse this player's KTML: {e}") from e

    return export_progress_sav(base_maps, overlay_maps, name_to_hash)


# ---------------------------------------------------------------------------
# progress.sav -> KTML -- the reverse direction, mirroring
# SaveServerBinaryLoader + SaveServerKTMLExporter. No template file is
# needed here either: every field's name comes from the same
# name2hash.ktml table (used in reverse this time), and its type/layout is
# read directly off the save's own type-boundary markers.
# ---------------------------------------------------------------------------

def _name_for(hash_unsigned, hash_to_name):
    name = hash_to_name.get(hash_unsigned)
    if name is not None:
        return name
    return f"unk{hash_unsigned}"


def _read_cstr(data, off, slot_size):
    raw = data[off:off + slot_size]
    end = raw.find(b"\x00")
    if end == -1:
        end = slot_size
    return raw[:end].decode("utf-8", errors="replace")


def _read_wstr(data, off, slot_size):
    raw = data[off:off + slot_size]
    n = len(raw) // 2
    length = n
    for i in range(n):
        if raw[i * 2] == 0 and raw[i * 2 + 1] == 0:
            length = i
            break
    return raw[:length * 2].decode("utf-16-le", errors="replace")


def _decode_field(type_name, data, raw_value):
    if type_name == "Bool":
        return bool(raw_value)
    if type_name in ("Int", "Enum"):
        return struct.unpack("<i", struct.pack("<I", raw_value & 0xFFFFFFFF))[0]
    if type_name == "Float":
        return struct.unpack("<f", struct.pack("<I", raw_value & 0xFFFFFFFF))[0]
    if type_name == "UInt":
        return raw_value & 0xFFFFFFFF

    off = raw_value & 0xFFFFFFFF  # every other type stores a data-region pointer

    if type_name == "BoolArray":
        n = struct.unpack_from("<i", data, off)[0]
        base = off + 4
        return [bool((data[base + i // 8] >> (i % 8)) & 1) for i in range(n)]
    if type_name == "IntArray":
        n = struct.unpack_from("<i", data, off)[0]
        return list(struct.unpack_from(f"<{n}i", data, off + 4))
    if type_name == "FloatArray":
        n = struct.unpack_from("<i", data, off)[0]
        return list(struct.unpack_from(f"<{n}f", data, off + 4))
    if type_name == "EnumArray":
        n = struct.unpack_from("<i", data, off)[0]
        return list(struct.unpack_from(f"<{n}i", data, off + 4))
    if type_name == "Vector2":
        x, y = struct.unpack_from("<ff", data, off)
        return {"x": x, "y": y}
    if type_name == "Vector2Array":
        n = struct.unpack_from("<i", data, off)[0]
        out, p = [], off + 4
        for _ in range(n):
            x, y = struct.unpack_from("<ff", data, p)
            out.append({"x": x, "y": y})
            p += 8
        return out
    if type_name == "Vector3":
        x, y, z = struct.unpack_from("<fff", data, off)
        return {"x": x, "y": y, "z": z}
    if type_name == "Vector3Array":
        n = struct.unpack_from("<i", data, off)[0]
        out, p = [], off + 4
        for _ in range(n):
            x, y, z = struct.unpack_from("<fff", data, p)
            out.append({"x": x, "y": y, "z": z})
            p += 12
        return out
    if type_name == "String16":
        return _read_cstr(data, off, 16)
    if type_name == "String16Array":
        n = struct.unpack_from("<i", data, off)[0]
        p = off + 4
        return [_read_cstr(data, p + i * 16, 16) for i in range(n)]
    if type_name == "String32":
        return _read_cstr(data, off, 32)
    if type_name == "String32Array":
        n = struct.unpack_from("<i", data, off)[0]
        p = off + 4
        return [_read_cstr(data, p + i * 32, 32) for i in range(n)]
    if type_name == "String64":
        return _read_cstr(data, off, 64)
    if type_name == "String64Array":
        n = struct.unpack_from("<i", data, off)[0]
        p = off + 4
        return [_read_cstr(data, p + i * 64, 64) for i in range(n)]
    if type_name == "Binary":
        n = struct.unpack_from("<i", data, off)[0]
        return list(data[off + 4:off + 4 + n])
    if type_name == "BinaryArray":
        n = struct.unpack_from("<i", data, off)[0]
        out, p = [], off + 4
        for _ in range(n):
            ln = struct.unpack_from("<i", data, p)[0]
            p += 4
            out.append(list(data[p:p + ln]))
            p += ln
        return out
    if type_name == "UIntArray":
        n = struct.unpack_from("<i", data, off)[0]
        return list(struct.unpack_from(f"<{n}I", data, off + 4))
    if type_name == "Int64":
        return struct.unpack_from("<q", data, off)[0]
    if type_name == "Int64Array":
        n = struct.unpack_from("<i", data, off)[0]
        return list(struct.unpack_from(f"<{n}q", data, off + 4))
    if type_name == "UInt64":
        return struct.unpack_from("<Q", data, off)[0]
    if type_name == "UInt64Array":
        n = struct.unpack_from("<i", data, off)[0]
        return list(struct.unpack_from(f"<{n}Q", data, off + 4))
    if type_name == "WString16":
        return _read_wstr(data, off, 32)
    if type_name == "WString16Array":
        n = struct.unpack_from("<i", data, off)[0]
        p = off + 4
        return [_read_wstr(data, p + i * 32, 32) for i in range(n)]
    if type_name == "WString32":
        return _read_wstr(data, off, 64)
    if type_name == "WString32Array":
        n = struct.unpack_from("<i", data, off)[0]
        p = off + 4
        return [_read_wstr(data, p + i * 64, 64) for i in range(n)]
    if type_name == "WString64":
        return _read_wstr(data, off, 128)
    if type_name == "WString64Array":
        n = struct.unpack_from("<i", data, off)[0]
        p = off + 4
        return [_read_wstr(data, p + i * 128, 128) for i in range(n)]
    if type_name == "Bool64bitKey":
        out, p = [], off
        while True:
            v = struct.unpack_from("<Q", data, p)[0]
            if v == 0:
                break
            out.append(v)
            p += 8
        return out

    raise ConversionError(f"Unhandled save field type while reading: {type_name}")


def load_progress_sav(data, hash_to_name):
    """Parse progress.sav bytes into a dict of type_name ->
    {field_name: value}, mirroring SaveServerBinaryLoader.load()
    exactly."""
    if len(data) < 32:
        raise ConversionError("File is too small to be a valid progress.sav.")
    magic1, magic2, data_block_start = struct.unpack_from("<iii", data, 0)
    if magic1 != MAGIC1:
        raise ConversionError("This doesn't look like a valid TOTK progress.sav (primary header mismatch).")
    # v7.50: do not require the old server schema marker/size. Real Switch
    # hardware samples can carry a newer schema marker and a larger file while
    # retaining the same SaveServerBinaryLoader table/pointer structure.
    if data_block_start < 32 or data_block_start > len(data) or (data_block_start - 32) % 8:
        raise ConversionError(
            "This progress.sav has an invalid data-table boundary; it cannot be safely decoded."
        )

    value_count = (data_block_start - 32) // 8
    maps = {t: {} for t in ALL_TYPES}
    pos = 32
    current_type = None

    for _ in range(value_count):
        hash_val, raw_value = struct.unpack_from("<ii", data, pos)
        pos += 8
        if hash_val == 0:
            if raw_value < 0 or raw_value >= len(ALL_TYPES):
                raise ConversionError("Save file data is not correct (bad type index).")
            current_type = ALL_TYPES[raw_value]
            continue
        if current_type is None:
            raise ConversionError("Save file data is not correct (value before a type marker).")
        name = _name_for(hash_val & 0xFFFFFFFF, hash_to_name)
        maps[current_type][name] = _decode_field(current_type, data, raw_value)

    return maps


def _ktml_value_text(value, indent):
    pad = "\t" * indent
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        import json
        return json.dumps(value)
    if isinstance(value, dict):
        lines = ["{"]
        for k in ("x", "y", "z"):
            if k in value:
                lines.append(f'{pad}\t"{k}": {_ktml_value_text(value[k], indent + 1)}')
        lines.append(f"{pad}}}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "{ }"
        lines = ["{"]
        for item in value:
            lines.append(f"{pad}\t{_ktml_value_text(item, indent + 1)}")
        lines.append(f"{pad}}}")
        return "\n".join(lines)
    raise ConversionError(f"Don't know how to serialize this value to KTML: {value!r}")


def _normalize_server_maps(maps):
    """Return KTML maps using the numeric representations Kirbymimi's Java loader expects."""
    out = {}
    int_scalars = {"Int", "Enum", "UInt"}
    int_arrays = {"IntArray", "EnumArray", "UIntArray"}
    long_scalars = {"Int64", "UInt64"}
    long_arrays = {"Int64Array", "UInt64Array", "Bool64bitKey"}

    def as_int(value, label):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            if value.is_integer():
                return int(value)
            raise ConversionError(f"{label} requires an integer, got fractional value {value!r}")
        if isinstance(value, str):
            text = value.strip()
            try:
                n = float(text)
                if n.is_integer():
                    return int(n)
            except Exception:
                pass
        raise ConversionError(f"{label} requires an integer, got {value!r}")

    def as_java_long(value, label):
        n = as_int(value, label)
        if n < -0x8000000000000000 or n > 0xFFFFFFFFFFFFFFFF:
            raise ConversionError(
                f"{label} is outside the supported 64-bit range "
                f"(-9223372036854775808 through 18446744073709551615)."
            )
        # Preserve the low 64 bits but WRITE a signed Java-Long-range decimal.
        # Kirbymimi's KTML parser otherwise promotes > Long.MAX_VALUE to Double;
        # Bool64bitKey then does a direct (Long) cast and crashes.
        n &= 0xFFFFFFFFFFFFFFFF
        return n - 0x10000000000000000 if n >= 0x8000000000000000 else n

    for type_name, field_map in (maps or {}).items():
        if not isinstance(field_map, dict):
            out[type_name] = field_map
            continue
        dst = {}
        for key, value in field_map.items():
            label = f"{type_name}.{key}"
            if type_name in long_scalars:
                value = as_java_long(value, label)
            elif type_name in int_scalars:
                value = as_int(value, label)
            elif type_name in long_arrays:
                if not isinstance(value, list):
                    raise ConversionError(f"{label} must be an array")
                value = [as_java_long(v, f"{label}[{i}]") for i, v in enumerate(value)]
            elif type_name in int_arrays:
                if not isinstance(value, list):
                    raise ConversionError(f"{label} must be an array")
                value = [as_int(v, f"{label}[{i}]") for i, v in enumerate(value)]
            dst[key] = value
        out[type_name] = dst
    return out


def maps_to_ktml_text(maps):
    maps = _normalize_server_maps(maps)
    lines = []
    for type_name in ALL_TYPES:
        field_map = maps.get(type_name) or {}
        if not field_map:
            continue
        lines.append(f'"{type_name}": {{')
        for key, value in field_map.items():
            lines.append(f'\t"{key}": {_ktml_value_text(value, 1)}')
        lines.append("}")
    return "\n".join(lines) + "\n"


def analyze_progress_sav(sav_bytes, server_schema_maps=None):
    """Analyze a progress.sav without modifying it.

    v7.50 recognizes both the historical server/export layout and structurally
    valid newer Nintendo Switch layouts.  Classification is based on the real
    binary header/table and decoded field set, never the filename alone.
    """
    data = bytes(sav_bytes)
    if len(data) < 32:
        raise ConversionError("File is too small to be a valid progress.sav.")
    magic1, schema_marker, data_block_start = struct.unpack_from("<iii", data, 0)
    if magic1 != MAGIC1:
        raise ConversionError("This doesn't look like a valid TOTK progress.sav (primary header mismatch).")
    if data_block_start < 32 or data_block_start > len(data) or (data_block_start - 32) % 8:
        raise ConversionError("Invalid progress.sav table boundary.")
    name_to_hash = _load_name_to_hash()
    maps = load_progress_sav(data, {v: k for k, v in name_to_hash.items()})
    total_fields = sum(len(v) for v in maps.values() if isinstance(v, dict))
    unknown = []
    for type_name, field_map in maps.items():
        if isinstance(field_map, dict):
            unknown.extend((type_name, k) for k in field_map if k.startswith("unk") and k not in name_to_hash)
    standard = schema_marker == MAGIC2 and len(data) == FILE_SIZE
    report = {
        "input_bytes": len(data),
        "primary_magic": f"0x{magic1 & 0xffffffff:08X}",
        "schema_marker": f"0x{schema_marker & 0xffffffff:08X}",
        "data_block_start": data_block_start,
        "table_entries": (data_block_start - 32) // 8,
        "decoded_fields": total_fields,
        "save_type": "Compatible / Server Save Layout" if standard else "Vanilla Nintendo Switch / Newer Save Layout",
        "save_version": "Server-compatible legacy schema" if standard else "Newer save schema (exact game version not encoded here)",
        "conversion_required": not standard,
        "unknown_hash_fields": len(unknown),
        "unknown_hash_field_names": [f"{t}.{k}" for t, k in unknown[:50]],
        "maps": maps,
    }
    if server_schema_maps is not None:
        supported = normalized = defaulted = 0
        unsupported = []
        for type_name in ALL_TYPES:
            src = maps.get(type_name, {}) if isinstance(maps.get(type_name, {}), dict) else {}
            schema = server_schema_maps.get(type_name, {}) if isinstance(server_schema_maps.get(type_name, {}), dict) else {}
            for key in src:
                if key in schema: supported += 1
                else: unsupported.append(f"{type_name}.{key}")
            for key in schema:
                if key not in src: defaulted += 1
        report.update({
            "server_schema_supported_fields": supported,
            "server_schema_defaulted_fields": defaulted,
            "server_schema_unsupported_fields": len(unsupported),
            "server_schema_unsupported_names": unsupported[:100],
        })
    return report


def progress_sav_to_ktml_with_report(sav_bytes):
    report = analyze_progress_sav(sav_bytes)
    maps = report.pop("maps")
    text = maps_to_ktml_text(maps)
    # Parse our own output again. This catches serializer/type regressions before
    # a file is reported as converted.
    parsed = parse_ktml(text)
    report.update({
        "vanilla_save_parsed": True,
        "type_normalization": True,
        "ktml_generated": True,
        "ktml_validation": True,
        "generated_fields": sum(len(v) for v in parsed.values() if isinstance(v, dict)),
        "server_compatibility_validation": "KTML structure/type validation passed; live server workflow requires runtime test",
    })
    return text, report


def progress_sav_to_ktml(sav_bytes):
    """Convert progress.sav bytes to KTML text. No template file is
    needed: field names come from the same authoritative name2hash.ktml
    table (used in reverse), and every field's type/structure is read
    directly from the save's own type-boundary markers -- mirroring
    SaveServerBinaryLoader + SaveServerKTMLExporter exactly."""
    text, _report = progress_sav_to_ktml_with_report(sav_bytes)
    return text

