#!/usr/bin/env python3
"""Core-Tests für fp_manager_gui_v5_6_6_1.py.

Ausführen:
    sudo apt install python3-pytest
    python3 -m pytest test_biopin_core_v5_6_6.py
"""

import importlib.util
from pathlib import Path
import sys
import tempfile

try:
    import pytest
except Exception:
    pytest = None

MODULE_PATH = Path(__file__).with_name("fp_manager_gui_v5_6_6_1.py")


def load_module():
    spec = importlib.util.spec_from_file_location("fp_manager_gui_v5_6_6", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_validate_username():
    m = load_module()
    assert m.validate_username("sku")
    assert m.validate_username("user.name_123")
    assert m.validate_username("user-name_123")
    assert not m.validate_username("")
    assert not m.validate_username("../root")
    assert not m.validate_username("bad/name")


def test_username_regex_traversal():
    m = load_module()
    assert not m.validate_username("../etc/passwd")
    assert not m.validate_username("/root")
    assert not m.validate_username("user\x00name")


def test_validate_finger():
    m = load_module()
    assert m.validate_finger("right_index")
    assert m.validate_finger("left_thumb")
    assert not m.validate_finger("invalid_finger")
    assert not m.validate_finger("")


def test_coerce_cfg_ranges():
    m = load_module()
    cfg = m.coerce_cfg({"threshold": "9999", "scan_count": "abc", "language": "xx"})
    assert cfg["threshold"] == m.DEFAULT_CFG["threshold"]
    assert cfg["scan_count"] == m.DEFAULT_CFG["scan_count"]
    assert cfg["language"] == "de"


def test_coerce_cfg_bool():
    m = load_module()
    cfg = m.coerce_cfg({"demo_mode": "true", "debug_keep_files": 1})
    assert cfg["demo_mode"] is True
    assert cfg["debug_keep_files"] is True


def test_safe_child_blocks_traversal():
    m = load_module()
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        result = m.safe_child(base, "user", "finger")
        assert result.is_relative_to(base.resolve())
        try:
            m.safe_child(base, "..", "etc", "passwd")
            assert False, "Keine Exception bei Traversal!"
        except ValueError:
            pass


def test_image_mean_std_bytes():
    m = load_module()
    mean, std = m.image_mean_std(bytes([0, 10, 20, 30]))
    assert mean == 15
    assert std > 0


def test_minutiae_stats_empty(tmp_path):
    m = load_module()
    xyt = tmp_path / "empty.xyt"
    xyt.write_text("")
    s = m.minutiae_stats(xyt)
    assert s["count"] == 0


def test_minutiae_stats_normal(tmp_path):
    m = load_module()
    xyt = tmp_path / "test.xyt"
    xyt.write_text("100 200 45\n150 250 90\n200 300 135\n")
    s = m.minutiae_stats(xyt)
    assert s["count"] == 3
    assert s["x_min"] == 100
    assert s["x_max"] == 200


def test_run_bozorth3_missing_binary(tmp_path):
    m = load_module()
    score = m.run_bozorth3("/nonexistent", tmp_path / "a.xyt", tmp_path / "b.xyt")
    assert score == 0


def test_score_basic_stats():
    m = load_module()
    s = m.score_basic_stats([10, 20, 30, 40])
    assert s["count"] == 4
    assert s["min"] == 10
    assert s["max"] == 40
    assert s["median"] == 25
    assert s["avg"] == 25


def test_far_frr_at_threshold():
    m = load_module()
    far, frr = m.far_frr_at_threshold([50, 60, 70], [10, 20, 55], 50)
    assert round(far, 2) == 33.33
    assert round(frr, 2) == 0.00


def test_find_threshold_for_far():
    m = load_module()
    reco = m.find_threshold_for_far([50, 60, 70], [10, 20, 55], 0.0)
    assert reco is not None
    thr, far, frr = reco
    assert far == 0.0
    assert thr > 55


def test_minutiae_quality_and_progress(tmp_path):
    m = load_module()
    xyt = tmp_path / "quality.xyt"
    xyt.write_text("\n".join([
        "10 20 0",
        "120 140 45",
        "230 300 90",
        "280 420 150",
        "300 450 170",
    ]) + "\n")
    q = m.minutiae_quality(xyt, min_minutiae=5)
    assert 0 <= q["score"] <= 100
    assert q["count"] == 5
    assert m.enrollment_progress_percent(1, 5, 100) == 20
    assert m.enrollment_progress_percent(5, 5, 10) == 100


def test_quality_color_rgb():
    m = load_module()
    assert m.quality_color_rgb(None) == (0.62, 0.62, 0.62)
    assert m.quality_color_rgb(85)[2] > 0.8
    assert m.quality_color_rgb(10)[0] > 0.8


def test_roc_points_and_auc():
    m = load_module()
    roc, auc = m.roc_points_and_auc([80, 90, 100], [1, 2, 3])
    assert roc
    assert 0.0 <= auc <= 1.0
    assert auc > 0.9


def test_biometric_curve_data():
    m = load_module()
    data = m.biometric_curve_data([80, 90, 100], [1, 2, 3])
    assert data["thresholds"]
    assert "auc" in data
    assert "eer" in data


def test_filter_xyt_lines():
    m = load_module()
    lines = ["10 20 30 5", "11 21 31 15", "12 22 32", "bad line"]
    out = m.filter_xyt_lines(lines, 10)
    joined = "".join(out)
    assert "10 20 30 5" not in joined
    assert "11 21 31 15" in joined
    assert "12 22 32" in joined


def test_diagnose_scores_and_classification():
    m = load_module()
    d = m.diagnose_scores([80, 90, 100], [1, 2, 3])
    assert d["genuine_count"] == 3
    assert d["impostor_count"] == 3
    assert d["auc"] > 0.9
    assert m.classify_finger_diagnosis(d) in ("ok", "watch", "bad")


def test_rows_for_finger_and_hand():
    m = load_module()
    ds = {"rows": [
        {"type": "genuine", "score": 10, "finger_a": "left_index", "finger_b": "left_index"},
        {"type": "impostor", "score": 80, "finger_a": "right_index", "finger_b": "left_thumb"},
    ]}
    assert len(m.rows_for_finger(ds, "left_index")) == 1
    assert len(m.rows_for_hand(ds, "left")) == 2
    assert len(m.rows_for_hand(ds, "right")) == 1


if __name__ == "__main__":
    if pytest is None:
        print("pytest ist nicht installiert. Bitte: sudo apt install python3-pytest")
        sys.exit(1)
    raise SystemExit(pytest.main([__file__]))
