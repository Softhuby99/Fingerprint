#!/usr/bin/env python3
"""Erste pytest-Core-Tests für fp_manager_gui_v5_4_2.py.

Ausführen:
    python3 -m pytest test_biopin_core_v5_4_2.py

Direkt ausführbar ist die Datei auch, wenn pytest installiert ist:
    ./test_biopin_core_v5_4_2.py
"""

import importlib.util
from pathlib import Path
import tempfile
import sys

try:
    import pytest
except Exception:
    pytest = None

MODULE_PATH = Path(__file__).with_name("fp_manager_gui_v5_4_2.py")


def load_module():
    spec = importlib.util.spec_from_file_location("fp_manager_gui_v5_4_1", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_validate_username():
    m = load_module()
    assert m.validate_username("sku")
    assert m.validate_username("user.name_123")
    assert not m.validate_username("")
    assert not m.validate_username("../root")
    assert not m.validate_username("bad/name")


def test_coerce_cfg_ranges():
    m = load_module()
    cfg = m.coerce_cfg({"threshold": "9999", "scan_count": "abc", "language": "xx"})
    assert cfg["threshold"] == m.DEFAULT_CFG["threshold"]
    assert cfg["scan_count"] == m.DEFAULT_CFG["scan_count"]
    assert cfg["language"] == "de"


def test_safe_child_blocks_traversal():
    m = load_module()
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        ok = m.safe_child(base, "user", "finger")
        assert ok.is_relative_to(base.resolve())
        try:
            m.safe_child(base, "..", "outside")
            assert False, "Traversal should fail"
        except ValueError:
            pass


def test_image_mean_std_bytes():
    m = load_module()
    mean, std = m.image_mean_std(bytes([0, 10, 20, 30]))
    assert mean == 15
    assert std > 0


def test_minutiae_stats(tmp_path):
    m = load_module()
    xyt = tmp_path / "tpl.xyt"
    xyt.write_text("10 20 30\n15 25 35\n# comment\n\n")
    stats = m.minutiae_stats(xyt)
    assert stats["count"] == 2
    assert stats["x_min"] == 10
    assert stats["x_max"] == 15


if __name__ == "__main__":
    if pytest is None:
        print("pytest ist nicht installiert. Bitte ausführen: python3 -m pip install pytest")
        sys.exit(1)
    raise SystemExit(pytest.main([__file__]))
