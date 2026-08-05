"""json_flat_store 压平 / 还原 roundtrip。"""
import json

from src.data.json_flat_store import flatten, unflatten


def _rt(obj):
    rows = flatten(obj)
    back = unflatten(rows)
    assert json.loads(json.dumps(obj)) == json.loads(json.dumps(back))


def test_roundtrip_scalar():
    _rt({"a": 1, "b": 2.5, "c": None, "d": True, "e": False, "f": "x"})


def test_roundtrip_nested():
    _rt({"ranking": [{"code": "600000", "gain": 1.2}, {"code": "000001"}]})


def test_empty_containers():
    _rt({"a": {}, "b": []})


def test_root_list():
    _rt([1, 2, {"k": 3}])


def test_review_like_keys():
    _rt({"sector_groups": {"未知": [{"code": "1", "name": "n"}]}})
