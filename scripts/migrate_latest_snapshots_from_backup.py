#!/usr/bin/env python3
"""兼容入口：与 `migrate_from_backup_on_upgrade.py` 相同。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    path = ROOT / "scripts" / "migrate_from_backup_on_upgrade.py"
    spec = importlib.util.spec_from_file_location("_migrate_from_backup", path)
    if spec is None or spec.loader is None:
        print("无法加载 migrate_from_backup_on_upgrade.py")
        return 1
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return int(mod.main())


if __name__ == "__main__":
    raise SystemExit(main())
