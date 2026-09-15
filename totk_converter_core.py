import re
import json
import struct
from pathlib import Path
from collections import Counter

HASH_TABLE_MARKER = 0xA3DB7114

def murmur3_32(key, seed=0):
    if isinstance(key, str):
        key = key.encode("utf-8")
    c1, c2 = 0xCC9E2D51, 0x1B873593
    h1 = seed & 0xFFFFFFFF
    rounded = len(key) & ~3

    for i in range(0, rounded, 4):
        k1 = (
            key[i]
            | (key[i + 1] << 8)
            | (key[i + 2] << 16)
            | (key[i + 3] << 24)
        )
        k1 = (k1 * c1) & 0xFFFFFFFF
        k1 = ((k1 << 15) | (k1 >> 17)) & 0xFFFFFFFF
        k1 = (k1 * c2) & 0xFFFFFFFF

        h1 ^= k1
        h1 = ((h1 << 13) | (h1 >> 19)) & 0xFFFFFFFF
        h1 = (h1 * 5 + 0xE6546B64) & 0xFFFFFFFF

    k1 = 0
    tail = key[rounded:]
    if len(tail) == 3:
        k1 ^= tail[2] << 16
    if len(tail) >= 2:
        k1 ^= tail[1] << 8
    if len(tail) >= 1:
        k1 ^= tail[0]
        k1 = (k1 * c1) & 0xFFFFFFFF
        k1 = ((k1 << 15) | (k1 >> 17)) & 0xFFFFFFFF
        k1 = (k1 * c2) & 0xFFFFFFFF
        h1 ^= k1

    h1 ^= len(key)
    h1 ^= h1 >> 16
    h1 = (h1 * 0x85EBCA6B) & 0xFFFFFFFF
    h1 ^= h1 >> 13
    h1 = (h1 * 0xC2B2AE35) & 0xFFFFFFFF
    h1 ^= h1 >> 16
    return h1 & 0xFFFFFFFF

def key_hash(name):
    m = re.fullmatch(r"unk(\d+)", name)
    return (int(m.group(1)) & 0xFFFFFFFF) if m else murmur3_32(name)

TOKEN_RE = re.compile(
    r'\s*("(?:\\.|[^"\\])*"|-?\d+\.\d+(?:[eE][+-]?\d+)?|-?\d+(?:[eE][+-]?\d+)?|true|false|\{|\}|:)'
)

def parse_ktml(text):
    tokens = [m.group(1) for m in TOKEN_RE.finditer(text)]
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

def p32(x):
    return struct.pack("<I", int(x) & 0xFFFFFFFF)

def p64(x):
    return struct.pack("<Q", int(x) & 0xFFFFFFFFFFFFFFFF)

def write_cstr(buf, off, value, size):
    b = str(value).encode("utf-8")[: max(0, size - 1)]
    buf[off : off + size] = b + b"\0" * (size - len(b))

def write_wstr16(buf, off, value, size=0x20):
    b = str(value).encode("utf-16le")[: size - 2]
    if len(b) & 1:
        b = b[:-1]
    buf[off : off + size] = b + b"\0" * (size - len(b))

def inspect_template(template_bytes):
    marker = struct.pack("<I", HASH_TABLE_MARKER)
    marker_pos = template_bytes.find(marker, 0x28)

    if marker_pos < 0 or marker_pos % 8:
        raise ValueError(
            "This file does not look like a supported TOTK progress.sav "
            "(hash-table marker not found)."
        )

    hmap = {}
    for off in range(0x28, marker_pos + 8, 8):
        h, v = struct.unpack_from("<II", template_bytes, off)
        hmap[h] = (off, v)

    return marker_pos, hmap

def convert_ktml_to_progress(ktml_path, template_path, output_path):
    ktml_path = Path(ktml_path)
    template_path = Path(template_path)
    output_path = Path(output_path)

    root = parse_ktml(ktml_path.read_text(encoding="utf-8", errors="strict"))
    buf = bytearray(template_path.read_bytes())
    marker_pos, hmap = inspect_template(bytes(buf))

    written = Counter()
    missing = Counter()
    skipped = Counter()

    def pair(name):
        return hmap.get(key_hash(name))

    # Inline scalar values stored directly in the hash table.
    for typ in ("Bool", "Int", "Float", "Enum", "UInt"):
        for name, val in root.get(typ, {}).items():
            q = pair(name)
            if not q:
                missing[typ] += 1
                continue

            off = q[0] + 4

            if typ == "Bool":
                buf[off : off + 4] = p32(1 if val else 0)
            elif typ == "Int":
                buf[off : off + 4] = struct.pack("<i", int(val))
            elif typ in ("Enum", "UInt"):
                buf[off : off + 4] = p32(val)
            else:
                buf[off : off + 4] = struct.pack("<f", float(val))

            written[typ] += 1

    # Pointer-backed scalar values.
    for typ in ("Vector3", "String32", "String64", "UInt64"):
        for name, val in root.get(typ, {}).items():
            q = pair(name)
            if not q:
                missing[typ] += 1
                continue

            off = q[1]

            if typ == "Vector3":
                buf[off : off + 12] = struct.pack(
                    "<fff",
                    float(val["x"]),
                    float(val["y"]),
                    float(val["z"]),
                )
            elif typ == "String32":
                write_cstr(buf, off, val, 0x20)
            elif typ == "String64":
                write_cstr(buf, off, val, 0x40)
            else:
                buf[off : off + 8] = p64(val)

            written[typ] += 1

    array_sizes = {
        "IntArray": 4,
        "UIntArray": 4,
        "FloatArray": 4,
        "EnumArray": 4,
        "Vector2Array": 8,
        "Vector3Array": 12,
        "String64Array": 0x40,
        "UInt64Array": 8,
        "WString16Array": 0x20,
    }

    for typ, element_size in array_sizes.items():
        for name, arr in root.get(typ, {}).items():
            q = pair(name)
            if not q:
                missing[typ] += 1
                continue

            off = q[1]
            if off + 4 > len(buf):
                skipped[typ] += 1
                continue

            count = struct.unpack_from("<I", buf, off)[0]
            if count != len(arr):
                skipped[typ] += 1
                continue

            off += 4

            for val in arr:
                if off + element_size > len(buf):
                    skipped[typ] += 1
                    break

                if typ == "IntArray":
                    buf[off : off + 4] = struct.pack("<i", int(val))
                elif typ in ("UIntArray", "EnumArray"):
                    buf[off : off + 4] = p32(val)
                elif typ == "FloatArray":
                    buf[off : off + 4] = struct.pack("<f", float(val))
                elif typ == "Vector2Array":
                    buf[off : off + 8] = struct.pack(
                        "<ff", float(val["x"]), float(val["y"])
                    )
                elif typ == "Vector3Array":
                    buf[off : off + 12] = struct.pack(
                        "<fff",
                        float(val["x"]),
                        float(val["y"]),
                        float(val["z"]),
                    )
                elif typ == "String64Array":
                    write_cstr(buf, off, val, 0x40)
                elif typ == "UInt64Array":
                    buf[off : off + 8] = p64(val)
                elif typ == "WString16Array":
                    write_wstr16(buf, off, val)

                off += element_size
            else:
                written[typ] += 1

    # Bool arrays are bit-packed.
    for name, arr in root.get("BoolArray", {}).items():
        q = pair(name)
        if not q:
            missing["BoolArray"] += 1
            continue

        off = q[1]
        if off + 4 > len(buf):
            skipped["BoolArray"] += 1
            continue

        count = struct.unpack_from("<I", buf, off)[0]
        if count != len(arr):
            skipped["BoolArray"] += 1
            continue

        data_off = off + 4
        byte_count = (count + 7) // 8
        if data_off + byte_count > len(buf):
            skipped["BoolArray"] += 1
            continue

        for i, val in enumerate(arr):
            p = data_off + i // 8
            mask = 1 << (i % 8)
            if val:
                buf[p] |= mask
            else:
                buf[p] &= (~mask) & 0xFF

        written["BoolArray"] += 1

    # Special 64-bit key list. Keep a zero terminator.
    for name, arr in root.get("Bool64bitKey", {}).items():
        q = pair(name)
        if not q:
            missing["Bool64bitKey"] += 1
            continue

        off = q[1]
        vals = sorted(int(x) & 0xFFFFFFFFFFFFFFFF for x in arr)

        needed = 8 * (len(vals) + 1)
        if off + needed > len(buf):
            skipped["Bool64bitKey"] += 1
            continue

        for val in vals:
            buf[off : off + 8] = p64(val)
            off += 8

        buf[off : off + 8] = b"\0" * 8
        written["Bool64bitKey"] += 1

    # Binary arrays are deliberately left untouched unless empty and size-compatible.
    # This avoids corrupting unknown binary payloads.
    for name, arr in root.get("BinaryArray", {}).items():
        q = pair(name)
        if not q:
            missing["BinaryArray"] += 1
            continue

        off = q[1]
        if off + 4 > len(buf):
            skipped["BinaryArray"] += 1
            continue

        count = struct.unpack_from("<I", buf, off)[0]
        if count == 0 and len(arr) == 0:
            written["BinaryArray"] += 1
        else:
            skipped["BinaryArray"] += 1

    output_path.write_bytes(buf)

    return {
        "output_size": len(buf),
        "marker_offset": marker_pos,
        "written": dict(written),
        "missing": dict(missing),
        "skipped": dict(skipped),
        "total_written": sum(written.values()),
        "total_missing": sum(missing.values()),
        "total_skipped": sum(skipped.values()),
    }

def _read_cstr(buf, off, size):
    if off < 0 or off + size > len(buf):
        raise ValueError("String points outside the progress.sav file.")
    raw = bytes(buf[off:off + size]).split(b"\0", 1)[0]
    return raw.decode("utf-8", errors="replace")

def _read_wstr16(buf, off, size=0x20):
    if off < 0 or off + size > len(buf):
        raise ValueError("Wide string points outside the progress.sav file.")
    raw = bytes(buf[off:off + size])
    end = None
    for i in range(0, len(raw) - 1, 2):
        if raw[i:i+2] == b"\0\0":
            end = i
            break
    if end is not None:
        raw = raw[:end]
    return raw.decode("utf-16le", errors="replace")

def _ktml_quote(value):
    return json.dumps(str(value), ensure_ascii=False)


# ---------------------------------------------------------------------------
# KTML TYPE NORMALIZATION / VALIDATION
#
# Zonai/TOTK server KTML loaders distinguish integer values (Java Long) from
# decimal values (Java Double).  A value such as 123.0 in an integer section
# can therefore crash the server with:
#
#   java.lang.Double cannot be cast to class java.lang.Long
#
# Before writing a generated KTML, normalize each known section to the scalar
# type expected by that section.  This keeps real float/vector values as
# decimal values while guaranteeing integer sections are emitted without ".0".
# ---------------------------------------------------------------------------

_INTEGER_SCALAR_SECTIONS = {"Int", "UInt", "Enum", "Int64", "UInt64"}
_INTEGER_ARRAY_SECTIONS = {"IntArray", "UIntArray", "EnumArray", "Int64Array", "UInt64Array", "Bool64bitKey"}
_FLOAT_SCALAR_SECTIONS = {"Float"}
_FLOAT_ARRAY_SECTIONS = {"FloatArray"}
_STRING_SCALAR_SECTIONS = {"String32", "String64"}
_STRING_ARRAY_SECTIONS = {"String64Array", "WString16Array"}


def _require_integral(value, section, name, index=None):
    """
    Convert a numeric value to int, but reject a real fractional value.

    123, 123.0 and "123" are safe integer representations.
    123.5 is NOT silently rounded because that could corrupt a save.
    """
    label = f"{section}.{name}"
    if index is not None:
        label += f"[{index}]"

    if isinstance(value, bool):
        # bool is technically an int subclass in Python, but integer KTML
        # sections should be explicit numeric values.
        return int(value)

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(
                f"KTML type error: {label} contains fractional value {value!r}, "
                f"but {section} requires an integer."
            )
        return int(value)

    if isinstance(value, str):
        s = value.strip()
        try:
            if re.fullmatch(r"[+-]?\d+", s):
                return int(s)
            f = float(s)
            if f.is_integer():
                return int(f)
        except Exception:
            pass

    raise ValueError(
        f"KTML type error: {label} contains {value!r}, "
        f"but {section} requires an integer."
    )


def normalize_ktml_types(root):
    """
    Normalize known KTML sections in-place and return the number of values
    normalized.  BinaryArray is intentionally preserved because its payload
    format is not decoded by this converter.
    """
    normalized = 0

    # Bool
    section = root.get("Bool")
    if isinstance(section, dict):
        for name in list(section.keys()):
            section[name] = bool(section[name])
            normalized += 1

    # Integer scalars
    for section_name in _INTEGER_SCALAR_SECTIONS:
        section = root.get(section_name)
        if not isinstance(section, dict):
            continue
        for name in list(section.keys()):
            section[name] = _require_integral(section[name], section_name, name)
            normalized += 1

    # Float scalars
    for section_name in _FLOAT_SCALAR_SECTIONS:
        section = root.get(section_name)
        if not isinstance(section, dict):
            continue
        for name in list(section.keys()):
            section[name] = float(section[name])
            normalized += 1

    # Strings
    for section_name in _STRING_SCALAR_SECTIONS:
        section = root.get(section_name)
        if not isinstance(section, dict):
            continue
        for name in list(section.keys()):
            section[name] = str(section[name])
            normalized += 1

    # Integer arrays
    for section_name in _INTEGER_ARRAY_SECTIONS:
        section = root.get(section_name)
        if not isinstance(section, dict):
            continue
        for name, arr in list(section.items()):
            if not isinstance(arr, list):
                raise ValueError(f"KTML type error: {section_name}.{name} must be an array.")
            section[name] = [
                _require_integral(v, section_name, name, i)
                for i, v in enumerate(arr)
            ]
            normalized += len(arr)

    # Float arrays
    for section_name in _FLOAT_ARRAY_SECTIONS:
        section = root.get(section_name)
        if not isinstance(section, dict):
            continue
        for name, arr in list(section.items()):
            if not isinstance(arr, list):
                raise ValueError(f"KTML type error: {section_name}.{name} must be an array.")
            section[name] = [float(v) for v in arr]
            normalized += len(arr)

    # Bool arrays
    section = root.get("BoolArray")
    if isinstance(section, dict):
        for name, arr in list(section.items()):
            if not isinstance(arr, list):
                raise ValueError(f"KTML type error: BoolArray.{name} must be an array.")
            section[name] = [bool(v) for v in arr]
            normalized += len(arr)

    # String arrays
    for section_name in _STRING_ARRAY_SECTIONS:
        section = root.get(section_name)
        if not isinstance(section, dict):
            continue
        for name, arr in list(section.items()):
            if not isinstance(arr, list):
                raise ValueError(f"KTML type error: {section_name}.{name} must be an array.")
            section[name] = [str(v) for v in arr]
            normalized += len(arr)

    # Vector3 scalar maps
    section = root.get("Vector3")
    if isinstance(section, dict):
        for name, vec in list(section.items()):
            if not isinstance(vec, dict) or not all(k in vec for k in ("x", "y", "z")):
                raise ValueError(f"KTML type error: Vector3.{name} is malformed.")
            section[name] = {
                "x": float(vec["x"]),
                "y": float(vec["y"]),
                "z": float(vec["z"]),
            }
            normalized += 3

    # Vector arrays
    for section_name, keys in (
        ("Vector2Array", ("x", "y")),
        ("Vector3Array", ("x", "y", "z")),
    ):
        section = root.get(section_name)
        if not isinstance(section, dict):
            continue
        for name, arr in list(section.items()):
            if not isinstance(arr, list):
                raise ValueError(f"KTML type error: {section_name}.{name} must be an array.")
            fixed = []
            for i, vec in enumerate(arr):
                if not isinstance(vec, dict) or not all(k in vec for k in keys):
                    raise ValueError(
                        f"KTML type error: {section_name}.{name}[{i}] is malformed."
                    )
                fixed.append({k: float(vec[k]) for k in keys})
                normalized += len(keys)
            section[name] = fixed

    # Special 64-bit key arrays are integers.
    section = root.get("Bool64bitKey")
    if isinstance(section, dict):
        for name, arr in list(section.items()):
            if not isinstance(arr, list):
                raise ValueError(f"KTML type error: Bool64bitKey.{name} must be an array.")
            section[name] = [
                _require_integral(v, "Bool64bitKey", name, i)
                for i, v in enumerate(arr)
            ]
            normalized += len(arr)

    return normalized


def validate_ktml_types(root):
    """
    Validate the important Java-sensitive numeric sections after serialization
    and parsing.  Returns a list of human-readable problems.
    """
    errors = []

    for section_name in _INTEGER_SCALAR_SECTIONS:
        section = root.get(section_name)
        if not isinstance(section, dict):
            continue
        for name, value in section.items():
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(
                    f"{section_name}.{name}: expected integer, got "
                    f"{type(value).__name__} ({value!r})"
                )

    for section_name in _INTEGER_ARRAY_SECTIONS:
        section = root.get(section_name)
        if not isinstance(section, dict):
            continue
        for name, arr in section.items():
            if not isinstance(arr, list):
                errors.append(f"{section_name}.{name}: expected array")
                continue
            for i, value in enumerate(arr):
                if isinstance(value, bool) or not isinstance(value, int):
                    errors.append(
                        f"{section_name}.{name}[{i}]: expected integer, got "
                        f"{type(value).__name__} ({value!r})"
                    )

    section = root.get("Bool64bitKey")
    if isinstance(section, dict):
        for name, arr in section.items():
            if not isinstance(arr, list):
                errors.append(f"Bool64bitKey.{name}: expected array")
                continue
            for i, value in enumerate(arr):
                if isinstance(value, bool) or not isinstance(value, int):
                    errors.append(
                        f"Bool64bitKey.{name}[{i}]: expected integer, got "
                        f"{type(value).__name__} ({value!r})"
                    )

    return errors



def serialize_ktml(root):
    """
    Serialize the parsed KTML structure back to Zonai-style KTML:
    quoted keys, colon separators, brace-delimited maps/arrays, and no commas.
    """
    lines = []

    def emit_value(value, indent):
        pad = "\t" * indent

        if isinstance(value, dict):
            lines.append("{")
            for key, child in value.items():
                lines.append(f'{pad}\t{_ktml_quote(key)}: ' + _render_prefix(child))
                _emit_after_prefix(child, indent + 1)
            lines.append(f"{pad}}}")
            return

        if isinstance(value, list):
            lines.append("{")
            for child in value:
                child_pad = "\t" * (indent + 1)
                if isinstance(child, (dict, list)):
                    lines.append(child_pad + _render_prefix(child))
                    _emit_after_prefix(child, indent + 1)
                else:
                    lines.append(child_pad + _scalar_text(child))
            lines.append(f"{pad}}}")
            return

        lines.append(_scalar_text(value))

    def _scalar_text(value):
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, float):
            return repr(value)
        return str(value)

    def _render_prefix(value):
        if isinstance(value, (dict, list)):
            return "{"
        return _scalar_text(value)

    def _emit_after_prefix(value, indent):
        # The opening "{" was already placed on the key/value line.
        pad = "\t" * indent

        if isinstance(value, dict):
            for key, child in value.items():
                lines.append(f'{pad}\t{_ktml_quote(key)}: ' + _render_prefix(child))
                _emit_after_prefix(child, indent + 1)
            lines.append(f"{pad}}}")
        elif isinstance(value, list):
            for child in value:
                child_pad = "\t" * (indent + 1)
                if isinstance(child, (dict, list)):
                    lines.append(child_pad + _render_prefix(child))
                    _emit_after_prefix(child, indent + 1)
                else:
                    lines.append(child_pad + _scalar_text(child))
            lines.append(f"{pad}}}")

    for key, value in root.items():
        if isinstance(value, (dict, list)):
            lines.append(f'{_ktml_quote(key)}: {{')
            if isinstance(value, dict):
                for child_key, child in value.items():
                    lines.append(f'\t{_ktml_quote(child_key)}: ' + _render_prefix(child))
                    _emit_after_prefix(child, 1)
            else:
                for child in value:
                    if isinstance(child, (dict, list)):
                        lines.append("\t" + _render_prefix(child))
                        _emit_after_prefix(child, 1)
                    else:
                        lines.append("\t" + _scalar_text(child))
            lines.append("}")
        else:
            lines.append(f'{_ktml_quote(key)}: {_scalar_text(value)}')

    return "\n".join(lines) + "\n"

def convert_progress_to_ktml(progress_path, ktml_template_path, output_path):
    """
    Convert a normal TOTK progress.sav to Zonai-compatible KTML.

    A KTML template is required because progress.sav stores 32-bit hashed keys
    rather than the original human-readable variable names. The template supplies
    those names, types, and ordering; values are read from progress.sav.
    """
    progress_path = Path(progress_path)
    ktml_template_path = Path(ktml_template_path)
    output_path = Path(output_path)

    buf = progress_path.read_bytes()
    marker_pos, hmap = inspect_template(buf)
    root = parse_ktml(
        ktml_template_path.read_text(encoding="utf-8", errors="strict")
    )

    read_count = Counter()
    missing = Counter()
    skipped = Counter()

    def pair(name):
        return hmap.get(key_hash(name))

    # Inline scalar values.
    for typ in ("Bool", "Int", "Float", "Enum", "UInt"):
        section = root.get(typ, {})
        if not isinstance(section, dict):
            continue

        for name in list(section.keys()):
            q = pair(name)
            if not q:
                missing[typ] += 1
                continue

            off = q[0] + 4
            if off + 4 > len(buf):
                skipped[typ] += 1
                continue

            if typ == "Bool":
                section[name] = bool(struct.unpack_from("<I", buf, off)[0])
            elif typ == "Int":
                section[name] = struct.unpack_from("<i", buf, off)[0]
            elif typ in ("Enum", "UInt"):
                section[name] = struct.unpack_from("<I", buf, off)[0]
            else:
                section[name] = struct.unpack_from("<f", buf, off)[0]

            read_count[typ] += 1

    # Pointer-backed scalar values.
    for typ in ("Vector3", "String32", "String64", "UInt64"):
        section = root.get(typ, {})
        if not isinstance(section, dict):
            continue

        for name in list(section.keys()):
            q = pair(name)
            if not q:
                missing[typ] += 1
                continue

            off = q[1]
            try:
                if typ == "Vector3":
                    if off + 12 > len(buf):
                        raise ValueError
                    x, y, z = struct.unpack_from("<fff", buf, off)
                    section[name] = {"x": x, "y": y, "z": z}
                elif typ == "String32":
                    section[name] = _read_cstr(buf, off, 0x20)
                elif typ == "String64":
                    section[name] = _read_cstr(buf, off, 0x40)
                else:
                    if off + 8 > len(buf):
                        raise ValueError
                    section[name] = struct.unpack_from("<Q", buf, off)[0]
                read_count[typ] += 1
            except Exception:
                skipped[typ] += 1

    array_sizes = {
        "IntArray": 4,
        "UIntArray": 4,
        "FloatArray": 4,
        "EnumArray": 4,
        "Vector2Array": 8,
        "Vector3Array": 12,
        "String64Array": 0x40,
        "UInt64Array": 8,
        "WString16Array": 0x20,
    }

    for typ, element_size in array_sizes.items():
        section = root.get(typ, {})
        if not isinstance(section, dict):
            continue

        for name in list(section.keys()):
            q = pair(name)
            if not q:
                missing[typ] += 1
                continue

            off = q[1]
            if off + 4 > len(buf):
                skipped[typ] += 1
                continue

            count = struct.unpack_from("<I", buf, off)[0]
            if count > 1_000_000:
                skipped[typ] += 1
                continue

            p = off + 4
            arr = []
            ok = True

            for _ in range(count):
                if p + element_size > len(buf):
                    ok = False
                    break

                if typ == "IntArray":
                    arr.append(struct.unpack_from("<i", buf, p)[0])
                elif typ in ("UIntArray", "EnumArray"):
                    arr.append(struct.unpack_from("<I", buf, p)[0])
                elif typ == "FloatArray":
                    arr.append(struct.unpack_from("<f", buf, p)[0])
                elif typ == "Vector2Array":
                    x, y = struct.unpack_from("<ff", buf, p)
                    arr.append({"x": x, "y": y})
                elif typ == "Vector3Array":
                    x, y, z = struct.unpack_from("<fff", buf, p)
                    arr.append({"x": x, "y": y, "z": z})
                elif typ == "String64Array":
                    arr.append(_read_cstr(buf, p, 0x40))
                elif typ == "UInt64Array":
                    arr.append(struct.unpack_from("<Q", buf, p)[0])
                elif typ == "WString16Array":
                    arr.append(_read_wstr16(buf, p, 0x20))

                p += element_size

            if ok:
                section[name] = arr
                read_count[typ] += 1
            else:
                skipped[typ] += 1

    # Bit-packed bool arrays.
    section = root.get("BoolArray", {})
    if isinstance(section, dict):
        for name in list(section.keys()):
            q = pair(name)
            if not q:
                missing["BoolArray"] += 1
                continue

            off = q[1]
            if off + 4 > len(buf):
                skipped["BoolArray"] += 1
                continue

            count = struct.unpack_from("<I", buf, off)[0]
            if count > 8_000_000:
                skipped["BoolArray"] += 1
                continue

            data_off = off + 4
            byte_count = (count + 7) // 8
            if data_off + byte_count > len(buf):
                skipped["BoolArray"] += 1
                continue

            arr = []
            for i in range(count):
                p = data_off + i // 8
                mask = 1 << (i % 8)
                arr.append(bool(buf[p] & mask))

            section[name] = arr
            read_count["BoolArray"] += 1

    # Special 64-bit key list. It is zero-terminated in progress.sav.
    section = root.get("Bool64bitKey", {})
    if isinstance(section, dict):
        for name in list(section.keys()):
            q = pair(name)
            if not q:
                missing["Bool64bitKey"] += 1
                continue

            p = q[1]
            arr = []
            max_entries = 100_000

            for _ in range(max_entries):
                if p + 8 > len(buf):
                    break
                val = struct.unpack_from("<Q", buf, p)[0]
                p += 8
                if val == 0:
                    break
                arr.append(val)
            else:
                skipped["Bool64bitKey"] += 1
                continue

            section[name] = arr
            read_count["Bool64bitKey"] += 1

    # Unknown BinaryArray payloads are preserved from the KTML template.
    # This is safer than guessing their binary layout.
    binary_section = root.get("BinaryArray", {})
    if isinstance(binary_section, dict):
        for _name in binary_section:
            skipped["BinaryArray"] += 1

    # Normalize Java-sensitive numeric types before serializing.
    normalized_values = normalize_ktml_types(root)

    ktml_text = serialize_ktml(root)

    # Parse the exact text we are about to save and verify that integer
    # sections still parse as integers rather than decimals/Doubles.
    parsed_output = parse_ktml(ktml_text)
    validation_errors = validate_ktml_types(parsed_output)
    if validation_errors:
        preview = "\n".join(validation_errors[:20])
        more = len(validation_errors) - 20
        if more > 0:
            preview += f"\n...and {more} more type errors."
        raise ValueError(
            "Generated KTML failed numeric type validation.\n\n" + preview
        )

    with output_path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(ktml_text)

    return {
        "output_size": output_path.stat().st_size,
        "marker_offset": marker_pos,
        "read": dict(read_count),
        "missing": dict(missing),
        "skipped": dict(skipped),
        "total_read": sum(read_count.values()),
        "total_missing": sum(missing.values()),
        "total_skipped": sum(skipped.values()),
        "normalized_values": normalized_values,
        "validation_errors": 0,
        "note": (
            "KTML numeric types were normalized and validated before saving. "
            "Integer sections are emitted as whole numbers to prevent Java "
            "Double-to-Long ClassCastException crashes. "
            "BinaryArray values are preserved from the KTML template because their "
            "binary layout is not yet decoded."
        ),
    }

