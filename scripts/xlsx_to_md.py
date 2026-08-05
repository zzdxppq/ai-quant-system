#!/usr/bin/env python3
"""将 Excel 转为 Markdown（UTF-8）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


def _fmt_cell(v) -> str:
    if pd.isna(v):
        return ""
    if hasattr(v, "strftime"):
        try:
            return v.strftime("%Y-%m-%d")
        except Exception:
            pass
    s = str(v).strip()
    if s.endswith(".0") and s[:-2].replace("-", "").isdigit():
        try:
            float(s)
            if "." in s:
                return s[:-2]
        except ValueError:
            pass
    return s.replace("|", "\\|").replace("\n", " ")


def df_to_markdown_table(df: pd.DataFrame) -> str:
    if df.empty or len(df.columns) == 0:
        return "_（空表）_\n"
    cols = [str(c) for c in df.columns]
    lines = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for _, row in df.iterrows():
        cells = [_fmt_cell(row[c]) for c in df.columns]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def convert(xlsx: Path, md_out: Path) -> None:
    xl = pd.ExcelFile(xlsx)
    parts = [
        f"# {xlsx.stem}\n",
        f"\n> 由 `{xlsx.name}` 自动转换。\n",
    ]
    for sheet in xl.sheet_names:
        df = pd.read_excel(xlsx, sheet_name=sheet)
        parts.append(f"\n## {sheet}\n\n")
        parts.append(df_to_markdown_table(df))
    md_out.write_text("".join(parts), encoding="utf-8")
    print(f"Wrote {md_out} ({md_out.stat().st_size} bytes)")


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "docs" / "量化交易数据补全.xlsx"
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".md")
    convert(src, dst)
