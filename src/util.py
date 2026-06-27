"""Shared helpers: dependency-free config loading, HTTP, and small utilities.

The whole pipeline targets the Python standard library only, so this module
includes a minimal YAML reader sufficient for ``config.yaml`` (nested maps,
inline ``[a, b]`` lists, ``- item`` lists, and scalars).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

UA = "MLB-Modelling/1.0"


# --------------------------------------------------------------------------- #
# Minimal YAML
# --------------------------------------------------------------------------- #
def _coerce(scalar: str) -> Any:
    s = scalar.strip()
    if s == "" or s in ("~", "null", "None"):
        return None
    if (s[0] == s[-1]) and s[0] in ("'", '"') and len(s) >= 2:
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _split_top(s: str) -> list[str]:
    """Split on commas not inside quotes/brackets."""
    out, depth, buf, quote = [], 0, "", None
    for ch in s:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf += ch
        elif ch in "[{":
            depth += 1
            buf += ch
        elif ch in "]}":
            depth -= 1
            buf += ch
        elif ch == "," and depth == 0:
            out.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf)
    return out


def _parse_inline_list(s: str) -> list:
    inner = s.strip()[1:-1].strip()
    if not inner:
        return []
    return [_coerce(p) for p in _split_top(inner)]


def _strip_comment(line: str) -> str:
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#":
            return line[:i]
    return line


def load_yaml(text: str) -> Any:
    rows = []
    for raw in text.splitlines():
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        rows.append((indent, line.strip()))

    def parse_block(idx: int, indent: int):
        if rows[idx][1].startswith("- "):
            items = []
            while idx < len(rows) and rows[idx][0] == indent and rows[idx][1].startswith("- "):
                items.append(_coerce(rows[idx][1][2:].strip()))
                idx += 1
            return items, idx
        mapping: dict[str, Any] = {}
        while idx < len(rows) and rows[idx][0] == indent:
            key_part = rows[idx][1]
            if ":" not in key_part:
                idx += 1
                continue
            key, _, rest = key_part.partition(":")
            key, rest = key.strip(), rest.strip()
            if rest == "":
                if idx + 1 < len(rows) and rows[idx + 1][0] > indent:
                    value, idx = parse_block(idx + 1, rows[idx + 1][0])
                else:
                    value, idx = None, idx + 1
                mapping[key] = value
            elif rest.startswith("["):
                mapping[key] = _parse_inline_list(rest)
                idx += 1
            else:
                mapping[key] = _coerce(rest)
                idx += 1
        return mapping, idx

    if not rows:
        return {}
    value, _ = parse_block(0, rows[0][0])
    return value


def load_config(path: str | None = None) -> dict:
    path = path or os.path.join(ROOT, "config.yaml")
    with open(path, "r", encoding="utf-8") as fh:
        return load_yaml(fh.read())


# --------------------------------------------------------------------------- #
# Filesystem / HTTP
# --------------------------------------------------------------------------- #
def abspath(rel: str) -> str:
    return rel if os.path.isabs(rel) else os.path.join(ROOT, rel)


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def http_get(url: str, retries: int = 3, timeout: int = 120) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({last})")


def http_get_json(url: str, retries: int = 3, timeout: int = 120) -> Any:
    """GET and parse JSON. Returns ``None`` on hard failure (best-effort callers)."""
    try:
        return json.loads(http_get(url, retries=retries, timeout=timeout).decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        log(f"http_get_json failed: {url} ({exc})")
        return None


def write_json(path: str, obj: Any, indent: int | None = None) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=indent, ensure_ascii=False)


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def num(val: Any, default: float = 0.0) -> float:
    """Coerce MLB API string/None numerics (e.g. ``'.580'``, ``'176.1'``) to float."""
    if val is None or val == "" or val == "-.--" or val == "-":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def innings_to_float(ip: Any) -> float:
    """MLB innings-pitched are ``X.Y`` where Y is thirds (e.g. ``176.1`` = 176⅓)."""
    if ip is None or ip == "":
        return 0.0
    s = str(ip)
    if "." in s:
        whole, _, frac = s.partition(".")
        thirds = {"0": 0.0, "1": 1 / 3, "2": 2 / 3}.get(frac[:1], 0.0)
        try:
            return int(whole or 0) + thirds
        except ValueError:
            return 0.0
    return num(s)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)
