"""单测使用临时目录下的 quant 库文件，避免写入仓库 data/ 或依赖本机 data/quant.duckdb。"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_quant_db_path(tmp_path, monkeypatch):
    import src.config as cfg

    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "_pytest_quant.duckdb")
    monkeypatch.setattr(cfg, "SKIP_JSON_DOC_REGISTRY", False)
