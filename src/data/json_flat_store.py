"""JSON 兼容对象 ↔ 扁平路径行（SQLite 存 typ + 标量列，不存 JSON 文本）。"""
from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import quote, unquote

Typ = Literal["n", "b", "i", "r", "s"]

_ROOT = "@"
_RE_LIST = re.compile(r"^~(\d+)~$")


def _qkey(k: str) -> str:
    return quote(str(k), safe="")


def _uqkey(s: str) -> str:
    return unquote(s)


def _leaf(typ: Typ, val: Any) -> Any:
    if typ == "n":
        return None
    if typ == "b":
        return bool(val)
    if typ == "i":
        return int(val)  # type: ignore[arg-type]
    if typ == "r":
        return float(val)  # type: ignore[arg-type]
    if typ == "s":
        return str(val)
    return val


def flatten(obj: Any) -> list[tuple[str, Typ, Any]]:
    """路径 / + quote(key) 或 /~n~ 表示 list 下标。"""
    out: list[tuple[str, Typ, Any]] = []

    def walk(prefix: str, v: Any) -> None:
        if v is None:
            out.append((prefix, "n", None))
            return
        if isinstance(v, bool):
            out.append((prefix, "b", bool(v)))
            return
        if isinstance(v, int) and not isinstance(v, bool):
            out.append((prefix, "i", int(v)))
            return
        if isinstance(v, float):
            out.append((prefix, "r", float(v)))
            return
        if isinstance(v, str):
            out.append((prefix, "s", v))
            return
        if isinstance(v, dict):
            if not v:
                out.append((prefix + "/" + _qkey("__empty_dict__"), "b", True))
                return
            for k, vv in v.items():
                walk(prefix + "/" + _qkey(str(k)), vv)
            return
        if isinstance(v, list):
            if not v:
                out.append((prefix + "/" + _qkey("__empty_list__"), "b", True))
                return
            for i, vv in enumerate(v):
                walk(prefix + f"/~{i}~", vv)
            return
        out.append((prefix, "s", str(v)))

    walk("/" + _qkey(_ROOT), obj)
    return out


def _assign(root: dict, parts: list[str], typ: Typ, val: Any) -> None:
    cur: Any = root
    for i, seg in enumerate(parts[:-1]):
        nxt_seg = parts[i + 1]
        is_next_list = bool(_RE_LIST.match(nxt_seg))
        if _RE_LIST.match(seg):
            if not isinstance(cur, list):
                raise TypeError(f"unflatten: expected list at {seg!r}, got {type(cur).__name__}")
            idx = int(_RE_LIST.match(seg).group(1))
            while len(cur) <= idx:
                cur.append(None)
            slot = cur[idx]
            if slot is None:
                cur[idx] = [] if is_next_list else {}
            else:
                if is_next_list and not isinstance(slot, list):
                    cur[idx] = []
                elif not is_next_list and not isinstance(slot, dict):
                    cur[idx] = {}
            cur = cur[idx]
        else:
            if not isinstance(cur, dict):
                raise TypeError(f"unflatten: expected dict at {seg!r}, got {type(cur).__name__}")
            k = _uqkey(seg)
            if k not in cur or cur[k] is None:
                cur[k] = [] if is_next_list else {}
            else:
                slot = cur[k]
                if is_next_list and not isinstance(slot, list):
                    cur[k] = []
                elif not is_next_list and not isinstance(slot, dict):
                    cur[k] = {}
            cur = cur[k]

    last = parts[-1]
    if _RE_LIST.match(last):
        if not isinstance(cur, list):
            raise TypeError(f"unflatten: expected list for leaf {last!r}, got {type(cur).__name__}")
        idx = int(_RE_LIST.match(last).group(1))
        while len(cur) <= idx:
            cur.append(None)
        cur[idx] = _leaf(typ, val)
    else:
        if not isinstance(cur, dict):
            raise TypeError(f"unflatten: expected dict for leaf {last!r}, got {type(cur).__name__}")
        cur[_uqkey(last)] = _leaf(typ, val)


def unflatten(rows: list[tuple[str, Typ, Any]]) -> Any:
    root: dict[str, Any] = {}
    for path, typ, val in sorted(rows, key=lambda r: (len(r[0]), r[0])):
        parts = [p for p in path.split("/") if p]
        if not parts:
            continue
        _assign(root, parts, typ, val)

    def clean(x: Any) -> Any:
        if isinstance(x, dict):
            if x.get("__empty_dict__") is True:
                return {}
            if x.get("__empty_list__") is True:
                return []
            return {
                k: clean(v)
                for k, v in x.items()
                if k not in ("__empty_dict__", "__empty_list__")
            }
        if isinstance(x, list):
            return [clean(v) for v in x]
        return x

    return clean(root).get(_ROOT)
