#!/usr/bin/env python3
"""
Fingerprint Manager — GTK3 GUI v5.6.6

Enthaltene Fixes gegenüber v4.0:
- Atomisches Speichern der Config (.tmp + replace)
- Typ-/Range-Coercion beim Laden der Config
- Scanner wird beim Start initialisiert und beim Beenden geschlossen
- Demo-Modus ist explizit per Config steuerbar
- ScanDialog stoppt seinen Thread sauber bei Cancel/Close
- Temporäre Dateien kollidieren nicht und werden kontrolliert bereinigt
- .xyt-Dateien werden erst nach Speicherung gelöscht
- Template-Dateien: 0600, Datenordner: 0700
- Username-Validierung gegen Pfad-/Löschrisiken
- Verify-Benutzerliste wird nach Änderungen synchronisiert
- FAR/FRR gruppiert genuine Paare nach (User, Finger), nicht nur User
- Hand-Widget nutzt Gdk.EventMask statt Magic Number
- Hand-Finger können per Maus direkt ausgewählt werden
- Einstellbare Pause zwischen Finger auflegen und wegnehmen
- Tool-Version ist im Fenster sichtbar
- Scanner-Anbindung über die funktionierende sensor.py-Schnittstelle
- raw → cwsq → mindtct Pipeline wie in der funktionierenden Version
- Scan-Bildgröße bleibt als Fallback/Anzeige erhalten
- Enrollte Finger können einzeln gelöscht werden
- Fehlerdialoge und Scanfehler werden im Log sichtbar
- Optionaler Rechte-Reparaturdialog bei PermissionError
- Minutien-Infos während des Scans und pro enrolltem Finger sichtbar
- Option A: WSQ-Dateien werden zusätzlich zu XYT dauerhaft gespeichert
- Debug-Modus: Löschvorgänge werden verhindert/übersprungen
- FP-Leser-Auswahl in den Einstellungen
- Sprache Deutsch/English auswählbar
- Minutien-Info rechts im Hochformat
- v5.4.7: Erweiterte i18n-Abdeckung für GUI-, Dialog- und Status-Texte
"""

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, List, Tuple, Optional

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Pango, Gdk


# ─────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────

CFG_FILE = Path.home() / ".config" / "fingerprint" / "config.json"
APP_VERSION = "5.6.6"
APP_NAME = f"Fingerprint Manager v{APP_VERSION}"
LANG_FILE = CFG_FILE.parent / "fp_manager_languages.json"

DEFAULT_CFG: dict[str, Any] = {
    "fp_base":           str(Path.home() / "fingerprint"),
    "template_base_dir": str(Path.home() / "fingerprint" / "templates"),
    "bin_dir":           str(Path.home() / "fingerprint" / "bin"),
    "log_dir":           str(Path.home() / "fingerprint" / "logs"),
    "lib_path":          str(Path.home() / "fingerprint" / "lib" / "libScanAPI.so"),
    "sensor_py":         str(Path.home() / "fingerprint" / "lib" / "sensor.py"),
    "nbis_dir":          "/usr/local/bin",

    "username":          os.environ.get("USER", "user"),
    "threshold":         35,
    "scan_count":        5,
    "min_minutiae":      5,
    "scan_width":        300,
    "scan_height":       400,
    "between_scan_delay": 1.2,
    "demo_mode":         False,
    "debug_keep_files":  False,
    "language":          "de",
    "fp_reader":         "auto",
    "log_level":         "DEBUG",
    "finger_mean_delta": 10.0,
    "finger_present_std": 30.0,
    "finger_off_std":    20.0,
    "verify_top2_min":   20,
    "enroll_pair_min":   18,
    "ignore_outliers_verify": False,
    "show_hand_quality": False,
    "minutiae_filter_enabled": False,
    "minutiae_filter_min_quality": 15,
}

INT_RANGES: dict[str, tuple[int, int]] = {
    "threshold":    (1, 200),
    "scan_count":   (1, 20),
    "min_minutiae": (1, 80),
    "scan_width":    (50, 2000),
    "scan_height":   (50, 2000),
    "verify_top2_min": (0, 200),
    "enroll_pair_min": (0, 200),
    "minutiae_filter_min_quality": (0, 100),
}

FLOAT_RANGES: dict[str, tuple[float, float]] = {
    # Zeit in Sekunden zwischen akzeptiertem Scan und nächstem Auflegen.
    "between_scan_delay": (0.0, 10.0),
    # Finger-Erkennung: bisherige Magic Numbers jetzt konfigurierbar.
    "finger_mean_delta": (0.0, 80.0),
    "finger_present_std": (1.0, 120.0),
    "finger_off_std": (0.0, 80.0),
}

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def validate_username(name: str) -> bool:
    """Nur einfache, sichere Ordnernamen erlauben."""
    return bool(USERNAME_RE.fullmatch(name or ""))


def coerce_cfg(data: dict[str, Any]) -> dict[str, Any]:
    """Defaults ergänzen und Typen/Wertebereiche korrigieren."""
    cfg = dict(DEFAULT_CFG)
    if isinstance(data, dict):
        cfg.update(data)

    # String-Keys normalisieren
    for key in ("fp_base", "template_base_dir", "bin_dir",
                "log_dir", "lib_path", "sensor_py", "nbis_dir", "username", "language", "fp_reader", "log_level"):
        cfg[key] = str(cfg.get(key, DEFAULT_CFG[key])).strip() or DEFAULT_CFG[key]

    # Numerische Werte robust machen
    for key, (lo, hi) in INT_RANGES.items():
        try:
            value = int(cfg.get(key, DEFAULT_CFG[key]))
            if not (lo <= value <= hi):
                raise ValueError
            cfg[key] = value
        except Exception:
            cfg[key] = DEFAULT_CFG[key]

    # Float-Werte robust machen
    for key, (lo, hi) in FLOAT_RANGES.items():
        try:
            value = float(cfg.get(key, DEFAULT_CFG[key]))
            if not (lo <= value <= hi):
                raise ValueError
            cfg[key] = value
        except Exception:
            cfg[key] = DEFAULT_CFG[key]

    # Boolean robust machen
    cfg["demo_mode"] = bool(cfg.get("demo_mode", DEFAULT_CFG["demo_mode"]))
    cfg["debug_keep_files"] = bool(cfg.get("debug_keep_files", DEFAULT_CFG["debug_keep_files"]))
    cfg["ignore_outliers_verify"] = bool(cfg.get("ignore_outliers_verify", DEFAULT_CFG["ignore_outliers_verify"]))
    cfg["show_hand_quality"] = bool(cfg.get("show_hand_quality", DEFAULT_CFG["show_hand_quality"]))
    cfg["minutiae_filter_enabled"] = bool(cfg.get("minutiae_filter_enabled", DEFAULT_CFG["minutiae_filter_enabled"]))

    if cfg.get("language") not in ("de", "en"):
        cfg["language"] = DEFAULT_CFG["language"]
    if not cfg.get("fp_reader"):
        cfg["fp_reader"] = DEFAULT_CFG["fp_reader"]
    if str(cfg.get("log_level", "DEBUG")).upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        cfg["log_level"] = DEFAULT_CFG["log_level"]
    else:
        cfg["log_level"] = str(cfg["log_level"]).upper()

    # Default-Username absichern
    if not validate_username(cfg["username"]):
        cfg["username"] = DEFAULT_CFG["username"]
        if not validate_username(cfg["username"]):
            cfg["username"] = "user"

    return cfg


def load_cfg() -> dict[str, Any]:
    if CFG_FILE.exists():
        try:
            raw = json.loads(CFG_FILE.read_text(encoding="utf-8"))
            return coerce_cfg(raw)
        except Exception:
            pass
    return coerce_cfg({})


def save_cfg(cfg: dict[str, Any]) -> None:
    """Config atomar speichern und Dateirechte setzen."""
    cfg = coerce_cfg(cfg)

    CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CFG_FILE.parent.chmod(0o700)

    fd, tmp_name = tempfile.mkstemp(
        prefix="config.",
        suffix=".json.tmp",
        dir=str(CFG_FILE.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

        tmp_path.chmod(0o600)
        tmp_path.replace(CFG_FILE)
        CFG_FILE.chmod(0o600)
    finally:
        # Nach tmp_path.replace(CFG_FILE) existiert tmp_path normalerweise nicht mehr.
        # Dieser Block räumt nur übrig gebliebene Temp-Dateien nach Fehlern auf.
        if tmp_path.exists() and tmp_path != CFG_FILE:
            try:
                tmp_path.unlink()
            except Exception:
                pass



def setup_logging(cfg: dict[str, Any]) -> logging.Logger:
    """Zentrales Logging mit FileHandler und robustem stderr-Fallback."""
    logger = logging.getLogger("fp_manager")
    logger.handlers.clear()
    level = getattr(logging, str(cfg.get("log_level", "DEBUG")).upper(), logging.DEBUG)
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    def _fallback_handler() -> logging.Handler:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        handler.setLevel(level)
        return handler

    try:
        paths = make_paths(cfg)
        ensure_private_dir(paths["log_dir"])
        log_file = paths["log_dir"] / "fp_manager.log"

        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(formatter)
        handler.setLevel(level)
        logger.addHandler(handler)

        try:
            log_file.chmod(0o600)
        except Exception:
            logger.debug("Konnte Logdatei-Rechte nicht setzen: %s", log_file, exc_info=True)

    except Exception:
        logger.addHandler(_fallback_handler())
        logger.warning("File logging unavailable; using stderr fallback.", exc_info=True)

    return logger


def make_paths(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "fp_base":  Path(cfg["fp_base"]),
        "db_dir":   Path(cfg["template_base_dir"]),
        "bin_dir":  Path(cfg["bin_dir"]),
        "log_dir":  Path(cfg["log_dir"]),
        "lib_path": Path(cfg["lib_path"]),
        "sensor_py": Path(cfg["sensor_py"]),
        "nbis_dir": str(cfg["nbis_dir"]),
        "tmp_dir":  Path(cfg["fp_base"]) / "tmp",
    }



def score_basic_stats(scores: list[int]) -> dict[str, float]:
    if not scores:
        return {"count": 0}
    vals = sorted(int(x) for x in scores)
    n = len(vals)
    mid = n // 2
    median = vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0
    return {"count": n, "min": vals[0], "max": vals[-1], "avg": sum(vals) / n, "median": median}


def top_n_average(scores: list[int], n: int = 2) -> float:
    if not scores:
        return 0.0
    vals = sorted((int(x) for x in scores), reverse=True)[:max(1, n)]
    return sum(vals) / len(vals)


def far_frr_at_threshold(genuine_scores: list[int], impostor_scores: list[int], threshold: int) -> tuple[float, float]:
    far = 0.0
    frr = 0.0
    if impostor_scores:
        far = 100.0 * sum(1 for s in impostor_scores if s >= threshold) / len(impostor_scores)
    if genuine_scores:
        frr = 100.0 * sum(1 for s in genuine_scores if s < threshold) / len(genuine_scores)
    return far, frr


def find_eer_threshold(genuine_scores: list[int], impostor_scores: list[int]) -> tuple[float, int]:
    if not genuine_scores or not impostor_scores:
        return 0.0, 0
    max_score = max(genuine_scores + impostor_scores + [200])
    best_diff, best_thr, best_eer = 999.0, 0, 0.0
    for thr in range(0, max_score + 2):
        far, frr = far_frr_at_threshold(genuine_scores, impostor_scores, thr)
        diff = abs(far - frr)
        if diff < best_diff:
            best_diff = diff
            best_thr = thr
            best_eer = (far + frr) / 2.0
    return best_eer, best_thr


def find_threshold_for_far(genuine_scores: list[int], impostor_scores: list[int], target_far: float) -> Optional[tuple[int, float, float]]:
    if not genuine_scores or not impostor_scores:
        return None
    max_score = max(genuine_scores + impostor_scores + [200])
    for thr in range(0, max_score + 2):
        far, frr = far_frr_at_threshold(genuine_scores, impostor_scores, thr)
        if far <= target_far:
            return thr, far, frr
    return None


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except Exception:
        pass


def safe_child(base: Path, *parts: str) -> Path:
    """
    Erzeugt einen Pfad unterhalb von base und verhindert Traversal.

    resolve() löst Symlinks auf. Wenn ein Symlink innerhalb von base nach außen
    zeigt, wird der Zielpfad dadurch erkannt und abgelehnt.
    """
    base_resolved = base.resolve()
    target = base_resolved.joinpath(*parts).resolve()
    try:
        if target == base_resolved or target.is_relative_to(base_resolved):
            return target
    except AttributeError:
        if target == base_resolved or base_resolved in target.parents:
            return target
    raise ValueError("Unsicherer Pfad außerhalb des Basisordners")


# ─────────────────────────────────────────────────────────────────
# Finger
# ─────────────────────────────────────────────────────────────────

FINGERS = [
    ("right_thumb",  "Rechts Daumen"),
    ("right_index",  "Rechts Zeigefinger"),
    ("right_middle", "Rechts Mittelfinger"),
    ("right_ring",   "Rechts Ringfinger"),
    ("right_pinky",  "Rechts Kleiner Finger"),
    ("left_thumb",   "Links Daumen"),
    ("left_index",   "Links Zeigefinger"),
    ("left_middle",  "Links Mittelfinger"),
    ("left_ring",    "Links Ringfinger"),
    ("left_pinky",   "Links Kleiner Finger"),
]

FINGER_KEYS = {key for key, _ in FINGERS}
FINGER_DISPLAY = {key: label for key, label in FINGERS}


def validate_finger(finger: str) -> bool:
    return finger in FINGER_KEYS

# ─────────────────────────────────────────────────────────────────
# Sprache / UI-Texte + FP-Leser-Erkennung
# ─────────────────────────────────────────────────────────────────

DEFAULT_LANGUAGES = {'de': {'settings': '⚙ Einstellungen', 'cancel': 'Abbrechen', 'save': 'Speichern', 'user': 'Benutzer', 'name': 'Name', 'new_user': '➕ Neu', 'delete': '🗑 Löschen', 'finger': 'Finger', 'enrolled_fingers': 'Enrollte Finger', 'key': 'Schlüssel', 'display': 'Anzeige', 'templates': 'Templates', 'enroll_finger': '👆 Finger enrollen', 'delete_finger': '🗑 Finger löschen', 'enrollment_tab': '📋 Enrollment', 'verify_tab': '🔍 Verifikation', 'farfrr_tab': '📊 FAR/FRR', 'log_tab': '📝 Log', 'clear_log': '🗑 Log leeren', 'minute_info_title': 'Minutien-Infos zum ausgewählten Finger', 'minute_per_scan': 'Minutien pro Scan:', 'debug_badge': ' DEBUG: kein Löschen ', 'language': 'Sprache / Language', 'fp_reader': 'FP-Leser / FP reader', 'reader_rescan': 'FP-Leser neu suchen / rescan readers', 'path_base': 'Basis-Verzeichnis', 'path_templates': 'Template-Verzeichnis', 'path_bin': 'Bin-Verzeichnis', 'path_log': 'Log-Verzeichnis', 'path_lib': 'Scanner-Bibliothek (.so)', 'path_sensor': 'Sensor-Modul sensor.py', 'path_nbis': 'NBIS-Verzeichnis', 'default_username': 'Default-Benutzername', 'threshold': 'Bozorth3-Schwellwert', 'scan_count': 'Scan-Anzahl', 'min_minutiae': 'Min. Minutien', 'scan_width': 'Scan-Breite', 'scan_height': 'Scan-Höhe', 'between_scan_delay': 'Pause zwischen Scans (Sek.)', 'finger_mean_delta': 'Finger-Erkennung: Mean-Delta', 'finger_present_std': 'Finger-Erkennung: Std vorhanden', 'finger_off_std': 'Finger weg: Std-Grenze', 'log_level': 'Log-Level', 'demo_mode': 'Demo-Modus ohne echten Scanner', 'debug_keep_files': 'Debug-Modus: keine Dateien löschen', 'debug_keep_files_tip': 'Wenn aktiv, bleiben temporäre RAW/WSQ/XYT-Dateien erhalten. Alte Templates werden beim Enrollment nicht gelöscht und Benutzer-/Finger-Löschen wird blockiert.', 'choose_file': 'Datei wählen', 'choose_dir': 'Verzeichnis wählen', 'settings_saved': 'Einstellungen gespeichert.', 'scanner_initialized': 'Scanner initialisiert.', 'scanner_closed': 'Scanner geschlossen.', 'demo_active': 'Demo-Modus aktiv — Scanner wird nicht verwendet.', 'scanner_missing': 'sensor.py/Scanner nicht verfügbar. Demo-Modus bleibt aus; Scans schlagen fehl.', 'scanner_init_failed': 'Scanner-Initialisierung fehlgeschlagen.', 'right_hand': 'Rechte Hand', 'left_hand': 'Linke Hand', 'legend_enrolled': 'Enrollt', 'legend_selected': 'Ausgewählt', 'legend_mouse': 'Maus', 'scan_dialog_title': 'Finger scannen — {user}/{finger}', 'place_finger': 'Bitte Finger auflegen …', 'scan_of': 'Scan {current} von {total} — Finger auflegen …', 'scan_progress': '{done}/{total}', 'minutiae_per_scan': 'Minutien pro Scan:', 'scan_error_retry': 'Scanner-Fehler — erneut versuchen. ({error})', 'wsq_failed': 'WSQ-Erzeugung fehlgeschlagen: {msg}', 'minutiae_extract_failed': 'Minutien-Extraktion fehlgeschlagen: {msg}', 'scan_rejected': 'Scan {scan}: ABGELEHNT — {summary} | {quality}\n  Mindestwert: {min} Minutien', 'scan_quality_low': 'Scan {scan}: Qualität zu niedrig ({count} Minutien, min. {min}).', 'debug_bad_scan_kept': 'Debug-Modus: schlechte Scan-Dateien bleiben erhalten: {wsq}, {xyt}', 'scan_accepted': 'Scan {scan} akzeptiert ({count} Minutien).', 'scan_accepted_summary': 'Scan {scan}: AKZEPTIERT — {summary} | {quality} | {progress_text}', 'remove_finger_next': 'Finger wegnehmen — nächste Aufnahme in {delay:.1f}s …', 'all_scans_ok': 'Alle Scans erfolgreich.', 'scan_cancelled': 'Scan abgebrochen.', 'scan_incomplete': 'Scan nicht vollständig.', 'new_user_title': 'Neuer Benutzer', 'username_label': 'Benutzername:', 'create': 'Erstellen', 'invalid_username_msg': "Ungültiger Benutzername.\nErlaubt: Buchstaben, Zahlen, '.', '_', '-'", 'user_created': 'Benutzer erstellt: {user}', 'user_create_failed': 'Benutzer konnte nicht erstellt werden:\n{error}', 'delete_user_confirm': "Benutzer '{user}' und alle Fingerabdrücke löschen?", 'user_deleted': 'Benutzer gelöscht: {user}', 'delete_failed': 'Löschen fehlgeschlagen:\n{error}', 'debug_user_delete_blocked': 'Debug-Modus: Benutzer-Löschen wurde blockiert.', 'debug_user_delete_disabled': 'Debug-Modus ist aktiv. Benutzer-Löschen ist deaktiviert.', 'debug_finger_delete_blocked': 'Debug-Modus: Finger-Löschen wurde blockiert.', 'debug_finger_delete_disabled': 'Debug-Modus ist aktiv. Finger-Löschen ist deaktiviert.', 'select_user_and_finger': 'Bitte zuerst Benutzer und enrollten Finger auswählen.', 'finger_delete_confirm': "Finger '{finger}' von Benutzer '{user}' löschen?", 'finger_deleted': 'Finger gelöscht: {user}/{finger}', 'enroll_saved': '✅ {count} Templates gespeichert.', 'enroll_cancelled': 'Enrollment abgebrochen.', 'enroll_ok_log': 'Enrollment OK: {user}/{finger} ({count} Templates)', 'save_failed': 'Speichern fehlgeschlagen:\n{error}', 'no_user_selected': 'Bitte gültigen Benutzer wählen.', 'no_finger_selected': 'Bitte gültigen Finger wählen.', 'no_templates_for_finger': 'Keine Templates für diesen Finger vorhanden.', 'verify_cancelled': 'Scan abgebrochen.', 'verify_match': '✅ MATCH', 'verify_no_match': '❌ KEIN MATCH', 'verify_result': '{result}\nScore: {score}  |  Schwellwert: {threshold}', 'farfrr_calculate': '📊 FAR/FRR berechnen', 'farfrr_stop': '⏹ Stop', 'farfrr_running': 'Berechnung läuft …', 'farfrr_cancelled': 'Abgebrochen.', 'farfrr_too_few': 'Zu wenige Templates für FAR/FRR.', 'repair_permissions': 'Rechte reparieren', 'pkexec_missing': 'pkexec/sudo nicht gefunden. Rechte können nicht automatisch repariert werden.', 'settings_title': 'Einstellungen', 'file_logging_fallback': 'File logging unavailable; using stderr fallback.', 'scanner_loaded': 'Scanner initialisiert. Reader={reader}', 'log_cleared': 'Log geleert.', 'unknown_scanner_error': 'unbekannter Scanner-Fehler', 'minutiae_file': '{file}: {count} Minutien', 'minutiae_xy': '  X: {xmin}–{xmax}   Y: {ymin}–{ymax}', 'minutiae_theta': '  Winkel/Theta: {tmin}–{tmax}', 'minutiae_quality': '  Qualität: Ø {avg}  min/max {qmin}/{qmax}', 'minutiae_sample': '  Erste Werte: {sample}', 'ok': 'OK', 'finger_right_thumb': 'Rechts Daumen', 'finger_right_index': 'Rechts Zeigefinger', 'finger_right_middle': 'Rechts Mittelfinger', 'finger_right_ring': 'Rechts Ringfinger', 'finger_right_pinky': 'Rechts Kleiner Finger', 'finger_left_thumb': 'Links Daumen', 'finger_left_index': 'Links Zeigefinger', 'finger_left_middle': 'Links Mittelfinger', 'finger_left_ring': 'Links Ringfinger', 'finger_left_pinky': 'Links Kleiner Finger', 'verify_button': '🔍 Verifizieren', 'main_started': '{app} gestartet.', 'templates_missing_for_finger': 'Für diesen Finger sind keine Templates vorhanden.', 'delete_enrolled_finger_title': 'Enrollten Finger löschen?\n\nBenutzer: {user}\nFinger: {finger}', 'enrolled_finger_deleted': 'Enrollter Finger gelöscht: {user}/{finger}', 'finger_delete_failed': 'Finger löschen fehlgeschlagen: {error}', 'select_user_finger_minutiae': 'Bitte Benutzer und Finger auswählen.', 'minutiae_unavailable': 'Minutien-Infos nicht verfügbar: {error}', 'minutiae_header_user': 'Benutzer: {user}', 'minutiae_header_finger': 'Finger:   {finger_label} ({finger_key})', 'minutiae_header_templates': 'Templates: {count}', 'minutiae_header_total': 'Minutien gesamt: {total}   Ø pro Template: {avg:.1f}', 'scan_status_accepted': 'AKZEPTIERT', 'scan_status_rejected': 'ABGELEHNT', 'minutiae_word': 'Minutien', 'minutiae_theta_short': 'Theta', 'minutiae_summary': '{count} Minutien', 'farfrr_save': 'Speichern', 'farfrr_defaults': 'Defaults', 'farfrr_explain': 'FAR/FRR berechnet, wie streng der aktuelle Bozorth3-Schwellwert ist. Genuine-Paare sind Scans desselben Users/Fingers; Impostor-Paare sind unterschiedliche User/Finger. FAR zeigt falsche Akzeptanz, FRR falsche Ablehnung.', 'verify_top2_min': 'Verify Top-2-Minimum', 'enroll_pair_min': 'Enrollment Paar-Minimum', 'quality_analysis': 'Matching-Qualität', 'quality_no_scores': 'Keine Scores vorhanden.', 'quality_genuine_stats': 'Genuine Scores: min {min}, Ø {avg:.1f}, Median {median:.1f}, max {max}', 'quality_impostor_stats': 'Impostor Scores: min {min}, Ø {avg:.1f}, Median {median:.1f}, max {max}', 'quality_overlap': 'Overlap: Impostor max {imax} / Genuine min {gmin}', 'quality_reco_eer': 'EER: {eer:.2f}% bei Threshold {thr}', 'quality_reco_far': 'Threshold für FAR ≤ {target}%: {thr}  (FRR {frr:.2f}%)', 'quality_reco_none': 'Keine Empfehlung für FAR ≤ {target}% möglich.', 'quality_outliers': 'Template-Ausreißer', 'quality_outlier_none': 'Keine deutlichen Template-Ausreißer erkannt.', 'quality_outlier_line': '{user}/{finger}/{file}: Ø Genuine-Score {avg:.1f} — prüfen/neu enrollen', 'quality_csv_export': '💾 CSV exportieren', 'quality_csv_saved': 'Score-CSV gespeichert: {path}', 'quality_csv_failed': 'CSV-Export fehlgeschlagen: {error}', 'verify_top2_result': '{result}\nBest Score: {best}  |  Top-2 Ø: {top2:.1f}\nThreshold: {threshold}  |  Top-2-Min: {top2_min}', 'enroll_pair_scores': 'Interne Enrollment-Scores: {scores}', 'enroll_pair_low': 'Warnung: interne Enrollment-Scores niedrig. Minimum {min_score}, Soll {required}. Finger besser neu enrollen.', 'ignore_outliers_verify': 'Ausreißer beim Verify ignorieren', 'ignore_outliers_verify_tip': 'Templates mit schwachem internem Genuine-Score werden beim Verify übersprungen.', 'verify_outliers_ignored': 'Verify: {count} Template-Ausreißer ignoriert.', 'verify_outliers_all_filtered': 'Verify: alle Templates wären Ausreißer; vollständige Gallery wird verwendet.', 'template_quality_header': 'Template-Qualität:', 'template_quality_line': '  {file}: Ø interner Score {avg:.1f} — {status}', 'template_quality_ok': 'OK', 'template_quality_outlier': 'AUSREISSER', 'template_quality_single': '  {file}: nur ein Template vorhanden — keine interne Paarbewertung möglich', 'outlier_verify_note': 'Ausreißer beim Verify ignorieren: {state}', 'enabled': 'aktiv', 'disabled': 'inaktiv', 'scan_quality_good': 'GUT', 'scan_quality_medium': 'MITTEL', 'scan_quality_poor': 'SCHLECHT', 'scan_quality_summary': 'Qualität {score}/100 ({status})', 'scan_quality_detail': 'Qualität {score}/100 ({status}) | Minutien {count_score}/100 | Fläche {area_score}/100 | Winkel {theta_score}/100', 'scan_enroll_progress': 'Enrollment-Fortschritt {progress}%', 'scan_anim_wait': 'Finger auflegen', 'scan_anim_scanning': 'Scanne …', 'scan_anim_quality': 'Qualität', 'scan_anim_progress': 'Fortschritt', 'hand_quality_empty': 'Leer', 'hand_quality_active': 'Aktiv', 'show_hand_quality': 'Qualität im Handbild anzeigen', 'show_hand_quality_tip': 'Zeigt Qualitätszahlen und Mini-Balken im Handbild. Für maximale Performance deaktivieren.', 'hand_quality_disabled': 'Hand-Qualitätsanzeige deaktiviert.', 'show_clicked_quality': 'Einzelfinger-Qualität angezeigt: {finger} = {score}/100', 'show_clicked_quality_missing': 'Für diesen Finger ist keine Qualitätsanzeige verfügbar.', 'stats_tab': '📈 Statistik', 'stats_title': 'Biometrische Statistik / Kurven', 'stats_calculate': '📈 Statistik/Kurven berechnen', 'stats_use_last': 'Letzte FAR/FRR-Daten anzeigen', 'stats_waiting': 'Noch keine Statistik berechnet.', 'stats_running': 'Statistik wird berechnet …', 'stats_ready': 'Statistik bereit.', 'stats_no_data': 'Keine Score-Daten vorhanden. Bitte zuerst berechnen.', 'chart_farfrr': 'FAR / FRR über Threshold', 'chart_hist': 'Score-Verteilung', 'chart_roc': 'ROC-Kurve', 'chart_auc': 'AUC {auc:.4f}', 'chart_eer': 'EER {eer:.2f}% @ T={thr}', 'chart_genuine': 'Genuine', 'chart_impostor': 'Impostor', 'chart_far': 'FAR', 'chart_frr': 'FRR', 'chart_tpr': 'TPR', 'weak_genuine_pairs': 'Schwächste Genuine-Paare', 'strong_impostor_pairs': 'Stärkste Impostor-Paare', 'pair_line': '{score:>4}  {a_user}/{a_finger}/{a_file}  ↔  {b_user}/{b_finger}/{b_file}', 'minutiae_filter_enabled': 'Minutien-Qualitätsfilter aktivieren', 'minutiae_filter_min_quality': 'Min. Minutien-Qualität', 'minutiae_filter_tip': 'Filtert temporär XYT-Minutien nach Qualitätswert. Original-Templates bleiben unverändert.', 'filter_lab': 'Filter-Lab', 'filter_original': 'Original', 'filter_filtered': 'Gefiltert q≥{q}', 'filter_compare': 'Original vs. Filter q≥{q}', 'filter_removed': '{kept}/{total} Minutien behalten', 'filter_no_data': 'Filtervergleich nicht möglich: keine Score-Daten.', 'filter_result_header': 'Filter-Vergleich', 'filter_metric_line': '{name:<12} FAR {far:>6.2f}%  FRR {frr:>6.2f}%  EER {eer:>6.2f}% @ T={thr:<4} AUC {auc:.4f}', 'verify_filter_used': 'Verify nutzt Minutien-Filter q≥{q}.', 'finger_diag_header': 'Finger-Diagnose', 'finger_diag_summary': 'Zusammenfassung nach Finger', 'finger_diag_hand_summary': 'Links/Rechts-Auswertung', 'finger_diag_problem': 'Problemfinger / Empfehlungen', 'finger_diag_line': '{finger:<24} G {genuine:>4}  I {impostor:>5}  EER {eer:>6.2f}%  AUC {auc:.4f}  GenMed {gmed:>6.1f}  GenMin {gmin:>4}  ImpMax {imax:>4}  Status {status}', 'hand_diag_line': '{hand:<10} G {genuine:>4}  I {impostor:>5}  EER {eer:>6.2f}%  AUC {auc:.4f}  GenMed {gmed:>6.1f}  ImpMax {imax:>4}', 'status_ok': 'OK', 'status_watch': 'PRÜFEN', 'status_bad': 'KRITISCH', 'recommend_reenroll': '{finger}: neu enrollen / Auflage variieren — schwache Genuine-Scores.', 'recommend_high_impostor': '{finger}: hohes Impostor-Risiko — Threshold/2-Finger/Filter prüfen.', 'recommend_filter_test': '{finger}: Filter-Autotest sinnvoll.', 'recommend_none': 'Keine auffälligen Problemfinger erkannt.', 'settings_tab_paths': '📁 Verzeichnisse', 'settings_tab_language': '🌐 Sprache', 'settings_tab_reader': '🔌 FP-Leser Auswahl', 'settings_tab_scan': '⚙ Scan Einstellungen', 'user_panel_title': 'Benutzer', 'user_name_column': 'Name', 'delete_user': '🗑 Löschen', 'enroll_finger_button': '👆 Finger enrollen', 'delete_finger_button': '🗑 Finger löschen', 'farfrr_calculate_templates': '📊 FAR/FRR aus vorhandenen Templates berechnen', 'farfrr_full_eer': 'Echtes EER berechnen (alle Thresholds 1–200)', 'reset_scan_defaults': '↩ Scan-Standardwerte', 'reset_scan_defaults_tip': 'Setzt nur die Scan-Einstellungen auf Standardwerte zurück.', 'scan_help_title': 'ℹ Erklärungen zu Scan-Einstellungen', 'scan_help_text': 'Bozorth3-Schwellenwert\n  Höher: sicherer, weniger FAR, aber mehr FRR. Der richtige Finger wird häufiger abgelehnt.\n  Niedriger: bequemer, mehr Treffer, aber höheres Risiko für falsche Akzeptanz.\n  Wirkt direkt auf Verifikation und FAR/FRR.\n\nScan-Anzahl\n  Höher: mehr Templates pro Finger, meist robusteres Enrollment, aber längerer Enrollment-Vorgang.\n  Niedriger: schneller, aber weniger Varianten des Fingers.\n  Empfehlung aktuell: 5 Scans pro Finger.\n\nMin. Minutien\n  Höher: schlechte oder unvollständige Scans werden eher abgelehnt.\n  Niedriger: mehr Scans werden akzeptiert, auch wenn sie schwächer sind.\n  Wirkt auf Enrollment und Verify-Scan-Akzeptanz, nicht direkt auf Bozorth3.\n\nScan-Breite / Scan-Höhe\n  Muss zur Sensorauflösung passen. Falsche Werte können Bildverarbeitung und Minutien-Erkennung verschlechtern.\n  Für FS81/FS80H typischerweise 300 x 400 oder passend zu deiner sensor.py-Ausgabe.\n\nVerify Top-2-Minimum\n  Höher: strenger gegen Verwechslungen zwischen ähnlichen Fingern.\n  Niedriger: toleranter, aber höheres Risiko bei ähnlichen Impostor-Scores.\n\nEnrollment Paar-Minimum\n  Höher: Enrollment wird nur akzeptiert, wenn die Scans gut zueinander passen.\n  Niedriger: Enrollment ist einfacher, aber instabilere Templates bleiben möglich.\n\nMin. Minutien-Qualität\n  Wird nur genutzt, wenn der Minutien-Qualitätsfilter aktiv ist.\n  Höher: schwache Minutien werden entfernt. Kann Rauschen reduzieren, aber auch wichtige echte Punkte entfernen.\n  Niedriger: mehr Minutien bleiben erhalten.\n  Bei deinen Tests war q≥10 besser als q≥15/q≥20.\n\nPause zwischen Scans\n  Höher: mehr Zeit, Finger neu aufzulegen. Gut für bewusst unterschiedliche Auflagen.\n  Niedriger: schnelleres Enrollment, aber ähnliche Scanpositionen wahrscheinlicher.\n\nFinger-Erkennung: Mean-Delta\n  Höher: Finger wird erst bei deutlicher Helligkeitsänderung erkannt.\n  Niedriger: empfindlicher, kann aber Fehltrigger erzeugen.\n\nFinger-Erkennung: Std vorhanden\n  Höher: strenger, weniger Fehltrigger.\n  Niedriger: erkennt Finger schneller, aber auch Rauschen/Schatten eher.\n\nFinger weg: Std-Grenze\n  Höher: Finger-off wird früher erkannt.\n  Niedriger: wartet länger, kann zwischen Scans stabiler sein.\n\nLog-Level\n  DEBUG: viele Details, gut zur Fehlersuche.\n  INFO: ruhiger, gut im normalen Betrieb.\n  WARNING/ERROR: nur wichtige Meldungen.\n\nDemo-Modus\n  Für Demo/Tests ohne echte Scanner-Pipeline.\n  Nicht für echte Sicherheitsbewertung verwenden.\n\nDebug-Dateien behalten\n  Aktiv: temporäre Dateien bleiben erhalten. Gut zur Analyse.\n  Aus: temporäre Dateien werden gelöscht. Besser für normalen Betrieb und Datenschutz.\n\nAusreißer bei Verifikation ignorieren\n  Kann FRR senken, wenn einzelne schlechte Enrollment-Templates stören.\n  Vorsichtig nutzen: nicht als Ersatz für gutes Enrollment.\n\nQualität im Handbild anzeigen\n  An: Qualitätszahl und Mini-Balken pro Finger.\n  Aus: GUI schneller/ruhiger.\n\nMinutien-Qualitätsfilter aktivieren\n  Nutzt temporär gefilterte XYT-Dateien.\n  Original-Templates bleiben unverändert.\n  Bei deinen Daten aktuell empfehlenswert: q≥10 testen.'}, 'en': {'settings': '⚙ Settings', 'cancel': 'Cancel', 'save': 'Save', 'user': 'User', 'name': 'Name', 'new_user': '➕ New', 'delete': '🗑 Delete', 'finger': 'Finger', 'enrolled_fingers': 'Enrolled fingers', 'key': 'Key', 'display': 'Display', 'templates': 'Templates', 'enroll_finger': '👆 Enroll finger', 'delete_finger': '🗑 Delete finger', 'enrollment_tab': '📋 Enrollment', 'verify_tab': '🔍 Verification', 'farfrr_tab': '📊 FAR/FRR', 'log_tab': '📝 Log', 'clear_log': '🗑 Clear log', 'minute_info_title': 'Minutiae information for selected finger', 'minute_per_scan': 'Minutiae per scan:', 'debug_badge': ' DEBUG: no deletion ', 'language': 'Language / Sprache', 'fp_reader': 'FP reader', 'reader_rescan': 'Rescan FP readers', 'path_base': 'Base directory', 'path_templates': 'Template directory', 'path_bin': 'Binary directory', 'path_log': 'Log directory', 'path_lib': 'Scanner library (.so)', 'path_sensor': 'Sensor module sensor.py', 'path_nbis': 'NBIS directory', 'default_username': 'Default username', 'threshold': 'Bozorth3 threshold', 'scan_count': 'Scan count', 'min_minutiae': 'Min. minutiae', 'scan_width': 'Scan width', 'scan_height': 'Scan height', 'between_scan_delay': 'Pause between scans (sec.)', 'finger_mean_delta': 'Finger detection: mean delta', 'finger_present_std': 'Finger detection: present std', 'finger_off_std': 'Finger removed: std threshold', 'log_level': 'Log level', 'demo_mode': 'Demo mode without real scanner', 'debug_keep_files': 'Debug mode: do not delete files', 'debug_keep_files_tip': 'When enabled, temporary RAW/WSQ/XYT files are kept. Old templates are not deleted during enrollment and user/finger deletion is blocked.', 'choose_file': 'Choose file', 'choose_dir': 'Choose directory', 'settings_saved': 'Settings saved.', 'scanner_initialized': 'Scanner initialized.', 'scanner_closed': 'Scanner closed.', 'demo_active': 'Demo mode active — scanner is not used.', 'scanner_missing': 'sensor.py/scanner unavailable. Demo mode remains off; scans will fail.', 'scanner_init_failed': 'Scanner initialization failed.', 'right_hand': 'Right hand', 'left_hand': 'Left hand', 'legend_enrolled': 'Enrolled', 'legend_selected': 'Selected', 'legend_mouse': 'Mouse', 'scan_dialog_title': 'Scan finger — {user}/{finger}', 'place_finger': 'Please place your finger …', 'scan_of': 'Scan {current} of {total} — place finger …', 'scan_progress': '{done}/{total}', 'minutiae_per_scan': 'Minutiae per scan:', 'scan_error_retry': 'Scanner error — try again. ({error})', 'wsq_failed': 'WSQ generation failed: {msg}', 'minutiae_extract_failed': 'Minutiae extraction failed: {msg}', 'scan_rejected': 'Scan {scan}: REJECTED — {summary} | {quality}\n  Minimum: {min} minutiae', 'scan_quality_low': 'Scan {scan}: quality too low ({count} minutiae, min. {min}).', 'debug_bad_scan_kept': 'Debug mode: bad scan files are kept: {wsq}, {xyt}', 'scan_accepted': 'Scan {scan} accepted ({count} minutiae).', 'scan_accepted_summary': 'Scan {scan}: ACCEPTED — {summary} | {quality} | {progress_text}', 'remove_finger_next': 'Remove finger — next capture in {delay:.1f}s …', 'all_scans_ok': 'All scans completed successfully.', 'scan_cancelled': 'Scan cancelled.', 'scan_incomplete': 'Scan incomplete.', 'new_user_title': 'New user', 'username_label': 'Username:', 'create': 'Create', 'invalid_username_msg': "Invalid username.\nAllowed: letters, numbers, '.', '_', '-'", 'user_created': 'User created: {user}', 'user_create_failed': 'User could not be created:\n{error}', 'delete_user_confirm': "Delete user '{user}' and all fingerprints?", 'user_deleted': 'User deleted: {user}', 'delete_failed': 'Delete failed:\n{error}', 'debug_user_delete_blocked': 'Debug mode: user deletion was blocked.', 'debug_user_delete_disabled': 'Debug mode is active. User deletion is disabled.', 'debug_finger_delete_blocked': 'Debug mode: finger deletion was blocked.', 'debug_finger_delete_disabled': 'Debug mode is active. Finger deletion is disabled.', 'select_user_and_finger': 'Please select a user and an enrolled finger first.', 'finger_delete_confirm': "Delete finger '{finger}' for user '{user}'?", 'finger_deleted': 'Finger deleted: {user}/{finger}', 'enroll_saved': '✅ {count} templates saved.', 'enroll_cancelled': 'Enrollment cancelled.', 'enroll_ok_log': 'Enrollment OK: {user}/{finger} ({count} templates)', 'save_failed': 'Save failed:\n{error}', 'no_user_selected': 'Please select a valid user.', 'no_finger_selected': 'Please select a valid finger.', 'no_templates_for_finger': 'No templates available for this finger.', 'verify_cancelled': 'Scan cancelled.', 'verify_match': '✅ MATCH', 'verify_no_match': '❌ NO MATCH', 'verify_result': '{result}\nScore: {score}  |  Threshold: {threshold}', 'farfrr_calculate': '📊 Calculate FAR/FRR', 'farfrr_stop': '⏹ Stop', 'farfrr_running': 'Calculation running …', 'farfrr_cancelled': 'Cancelled.', 'farfrr_too_few': 'Too few templates for FAR/FRR.', 'repair_permissions': 'Repair permissions', 'pkexec_missing': 'pkexec/sudo not found. Permissions cannot be repaired automatically.', 'settings_title': 'Settings', 'file_logging_fallback': 'File logging unavailable; using stderr fallback.', 'scanner_loaded': 'Scanner initialized. Reader={reader}', 'log_cleared': 'Log cleared.', 'unknown_scanner_error': 'unknown scanner error', 'minutiae_file': '{file}: {count} minutiae', 'minutiae_xy': '  X: {xmin}–{xmax}   Y: {ymin}–{ymax}', 'minutiae_theta': '  Angle/Theta: {tmin}–{tmax}', 'minutiae_quality': '  Quality: avg {avg}  min/max {qmin}/{qmax}', 'minutiae_sample': '  First values: {sample}', 'ok': 'OK', 'finger_right_thumb': 'Right thumb', 'finger_right_index': 'Right index finger', 'finger_right_middle': 'Right middle finger', 'finger_right_ring': 'Right ring finger', 'finger_right_pinky': 'Right little finger', 'finger_left_thumb': 'Left thumb', 'finger_left_index': 'Left index finger', 'finger_left_middle': 'Left middle finger', 'finger_left_ring': 'Left ring finger', 'finger_left_pinky': 'Left little finger', 'verify_button': '🔍 Verify', 'main_started': '{app} started.', 'templates_missing_for_finger': 'No templates available for this finger.', 'delete_enrolled_finger_title': 'Delete enrolled finger?\n\nUser: {user}\nFinger: {finger}', 'enrolled_finger_deleted': 'Enrolled finger deleted: {user}/{finger}', 'finger_delete_failed': 'Delete finger failed: {error}', 'select_user_finger_minutiae': 'Please select user and finger.', 'minutiae_unavailable': 'Minutiae information unavailable: {error}', 'minutiae_header_user': 'User: {user}', 'minutiae_header_finger': 'Finger:   {finger_label} ({finger_key})', 'minutiae_header_templates': 'Templates: {count}', 'minutiae_header_total': 'Minutiae total: {total}   avg per template: {avg:.1f}', 'scan_status_accepted': 'ACCEPTED', 'scan_status_rejected': 'REJECTED', 'minutiae_word': 'Minutiae', 'minutiae_theta_short': 'Theta', 'minutiae_summary': '{count} Minutiae', 'farfrr_save': 'Save', 'farfrr_defaults': 'Defaults', 'farfrr_explain': 'FAR/FRR calculates how strict the current Bozorth3 threshold is. Genuine pairs are scans of the same user/finger; impostor pairs are different users/fingers. FAR is false acceptance; FRR is false rejection.', 'verify_top2_min': 'Verify top-2 minimum', 'enroll_pair_min': 'Enrollment pair minimum', 'quality_analysis': 'Matching quality', 'quality_no_scores': 'No scores available.', 'quality_genuine_stats': 'Genuine scores: min {min}, avg {avg:.1f}, median {median:.1f}, max {max}', 'quality_impostor_stats': 'Impostor scores: min {min}, avg {avg:.1f}, median {median:.1f}, max {max}', 'quality_overlap': 'Overlap: impostor max {imax} / genuine min {gmin}', 'quality_reco_eer': 'EER: {eer:.2f}% at threshold {thr}', 'quality_reco_far': 'Threshold for FAR ≤ {target}%: {thr}  (FRR {frr:.2f}%)', 'quality_reco_none': 'No recommendation possible for FAR ≤ {target}%.', 'quality_outliers': 'Template outliers', 'quality_outlier_none': 'No clear template outliers detected.', 'quality_outlier_line': '{user}/{finger}/{file}: avg genuine score {avg:.1f} — check/re-enroll', 'quality_csv_export': '💾 Export CSV', 'quality_csv_saved': 'Score CSV saved: {path}', 'quality_csv_failed': 'CSV export failed: {error}', 'verify_top2_result': '{result}\nBest score: {best}  |  Top-2 avg: {top2:.1f}\nThreshold: {threshold}  |  Top-2 min: {top2_min}', 'enroll_pair_scores': 'Internal enrollment scores: {scores}', 'enroll_pair_low': 'Warning: internal enrollment scores are low. Minimum {min_score}, required {required}. Consider re-enrolling this finger.', 'ignore_outliers_verify': 'Ignore outliers during verify', 'ignore_outliers_verify_tip': 'Templates with weak internal genuine scores are skipped during verify.', 'verify_outliers_ignored': 'Verify: ignored {count} template outlier(s).', 'verify_outliers_all_filtered': 'Verify: all templates would be outliers; using full gallery.', 'template_quality_header': 'Template quality:', 'template_quality_line': '  {file}: avg internal score {avg:.1f} — {status}', 'template_quality_ok': 'OK', 'template_quality_outlier': 'OUTLIER', 'template_quality_single': '  {file}: only one template available — internal pair score not possible', 'outlier_verify_note': 'Ignore outliers during verify: {state}', 'enabled': 'enabled', 'disabled': 'disabled', 'scan_quality_good': 'GOOD', 'scan_quality_medium': 'MEDIUM', 'scan_quality_poor': 'POOR', 'scan_quality_summary': 'Quality {score}/100 ({status})', 'scan_quality_detail': 'Quality {score}/100 ({status}) | Minutiae {count_score}/100 | area {area_score}/100 | angle {theta_score}/100', 'scan_enroll_progress': 'Enrollment progress {progress}%', 'scan_anim_wait': 'Place finger', 'scan_anim_scanning': 'Scanning …', 'scan_anim_quality': 'Quality', 'scan_anim_progress': 'Progress', 'hand_quality_empty': 'Empty', 'hand_quality_active': 'Active', 'show_hand_quality': 'Show quality in hand view', 'show_hand_quality_tip': 'Shows quality numbers and mini bars in the hand view. Disable for maximum performance.', 'hand_quality_disabled': 'Hand quality display disabled.', 'show_clicked_quality': 'Single-finger quality shown: {finger} = {score}/100', 'show_clicked_quality_missing': 'No quality display available for this finger.', 'stats_tab': '📈 Statistics', 'stats_title': 'Biometric statistics / curves', 'stats_calculate': '📈 Calculate statistics/curves', 'stats_use_last': 'Show latest FAR/FRR data', 'stats_waiting': 'No statistics calculated yet.', 'stats_running': 'Calculating statistics …', 'stats_ready': 'Statistics ready.', 'stats_no_data': 'No score data available. Please calculate first.', 'chart_farfrr': 'FAR / FRR over threshold', 'chart_hist': 'Score distribution', 'chart_roc': 'ROC curve', 'chart_auc': 'AUC {auc:.4f}', 'chart_eer': 'EER {eer:.2f}% @ T={thr}', 'chart_genuine': 'Genuine', 'chart_impostor': 'Impostor', 'chart_far': 'FAR', 'chart_frr': 'FRR', 'chart_tpr': 'TPR', 'weak_genuine_pairs': 'Weakest genuine pairs', 'strong_impostor_pairs': 'Strongest impostor pairs', 'pair_line': '{score:>4}  {a_user}/{a_finger}/{a_file}  ↔  {b_user}/{b_finger}/{b_file}', 'minutiae_filter_enabled': 'Enable minutiae quality filter', 'minutiae_filter_min_quality': 'Min. minutiae quality', 'minutiae_filter_tip': 'Temporarily filters XYT minutiae by quality value. Original templates remain unchanged.', 'filter_lab': 'Filter Lab', 'filter_original': 'Original', 'filter_filtered': 'Filtered q≥{q}', 'filter_compare': 'Original vs. filter q≥{q}', 'filter_removed': '{kept}/{total} minutiae kept', 'filter_no_data': 'Filter comparison not possible: no score data.', 'filter_result_header': 'Filter comparison', 'filter_metric_line': '{name:<12} FAR {far:>6.2f}%  FRR {frr:>6.2f}%  EER {eer:>6.2f}% @ T={thr:<4} AUC {auc:.4f}', 'verify_filter_used': 'Verify uses minutiae filter q≥{q}.', 'finger_diag_header': 'Finger diagnosis', 'finger_diag_summary': 'Summary by finger', 'finger_diag_hand_summary': 'Left/right analysis', 'finger_diag_problem': 'Problem fingers / recommendations', 'finger_diag_line': '{finger:<24} G {genuine:>4}  I {impostor:>5}  EER {eer:>6.2f}%  AUC {auc:.4f}  GenMed {gmed:>6.1f}  GenMin {gmin:>4}  ImpMax {imax:>4}  Status {status}', 'hand_diag_line': '{hand:<10} G {genuine:>4}  I {impostor:>5}  EER {eer:>6.2f}%  AUC {auc:.4f}  GenMed {gmed:>6.1f}  ImpMax {imax:>4}', 'status_ok': 'OK', 'status_watch': 'CHECK', 'status_bad': 'CRITICAL', 'recommend_reenroll': '{finger}: re-enroll / vary placement — weak genuine scores.', 'recommend_high_impostor': '{finger}: high impostor risk — check threshold/2-finger/filter.', 'recommend_filter_test': '{finger}: filter autotest recommended.', 'recommend_none': 'No obvious problem fingers detected.', 'settings_tab_paths': '📁 Directories', 'settings_tab_language': '🌐 Language', 'settings_tab_reader': '🔌 FP reader selection', 'settings_tab_scan': '⚙ Scan settings', 'user_panel_title': 'Users', 'user_name_column': 'Name', 'delete_user': '🗑 Delete', 'enroll_finger_button': '👆 Enroll finger', 'delete_finger_button': '🗑 Delete finger', 'farfrr_calculate_templates': '📊 Calculate FAR/FRR from existing templates', 'farfrr_full_eer': 'Calculate real EER (all thresholds 1–200)', 'reset_scan_defaults': '↩ Scan defaults', 'reset_scan_defaults_tip': 'Resets only the scan settings to default values.', 'scan_help_title': 'ℹ Scan settings explanations', 'scan_help_text': 'Bozorth3 threshold\n  Higher: more secure, lower FAR, but higher FRR. The correct finger is rejected more often.\n  Lower: more convenient, more accepts, but higher false-accept risk.\n  Directly affects verification and FAR/FRR.\n\nScan count\n  Higher: more templates per finger, usually more robust enrollment, but enrollment takes longer.\n  Lower: faster, but fewer finger variations.\n  Current recommendation: 5 scans per finger.\n\nMin. minutiae\n  Higher: bad or partial scans are rejected earlier.\n  Lower: more scans are accepted, even weaker ones.\n  Affects enrollment and verify scan acceptance, not Bozorth3 directly.\n\nScan width / scan height\n  Must match the sensor output. Wrong values can reduce image processing and minutiae quality.\n  For FS81/FS80H commonly 300 x 400 or whatever your sensor.py returns.\n\nVerify Top-2 minimum\n  Higher: stricter against confusion between similar fingers.\n  Lower: more tolerant, but higher risk when impostor scores are close.\n\nEnrollment pair minimum\n  Higher: enrollment is accepted only when scans match each other well.\n  Lower: enrollment is easier, but unstable templates may remain.\n\nMin. minutiae quality\n  Used only when minutiae quality filter is enabled.\n  Higher: weak minutiae are removed. This can reduce noise, but can also remove useful real points.\n  Lower: more minutiae remain.\n  In your tests q≥10 was better than q≥15/q≥20.\n\nDelay between scans\n  Higher: more time to reposition the finger. Good for deliberately varied placement.\n  Lower: faster enrollment, but scan positions may be too similar.\n\nFinger detection: mean delta\n  Higher: finger is detected only after a stronger brightness change.\n  Lower: more sensitive, but can cause false triggers.\n\nFinger detection: std present\n  Higher: stricter, fewer false triggers.\n  Lower: detects faster, but may react to noise/shadows.\n\nFinger off: std threshold\n  Higher: finger-off is detected earlier.\n  Lower: waits longer, can be more stable between scans.\n\nLog level\n  DEBUG: many details, useful for troubleshooting.\n  INFO: quieter, good for normal use.\n  WARNING/ERROR: only important messages.\n\nDemo mode\n  For demos/tests without the real scanner pipeline.\n  Do not use for real security evaluation.\n\nKeep debug files\n  Enabled: temporary files are kept. Useful for analysis.\n  Disabled: temporary files are removed. Better for normal operation and privacy.\n\nIgnore verification outliers\n  Can reduce FRR if individual bad enrollment templates disturb the match.\n  Use carefully: not a replacement for good enrollment.\n\nShow quality in hand view\n  On: quality number and mini bar per finger.\n  Off: faster/quieter GUI.\n\nEnable minutiae quality filter\n  Uses temporarily filtered XYT files.\n  Original templates remain unchanged.\n  With your data, currently recommended: test q≥10.'}}


def ensure_language_file() -> None:
    """Legt die externe Sprachdatei an, falls sie noch nicht existiert.

    Die interne DEFAULT_LANGUAGES-Struktur ist der Fallback. Die externe JSON
    kann Keys überschreiben; fehlende Keys werden aus den Defaults ergänzt.
    """
    try:
        LANG_FILE.parent.mkdir(parents=True, exist_ok=True)
        LANG_FILE.parent.chmod(0o700)
        if not LANG_FILE.exists():
            LANG_FILE.write_text(
                json.dumps(DEFAULT_LANGUAGES, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            LANG_FILE.chmod(0o600)
    except Exception:
        pass


def load_language_catalog() -> dict:
    ensure_language_file()
    try:
        import copy
        data = json.loads(LANG_FILE.read_text(encoding="utf-8"))
        merged = copy.deepcopy(DEFAULT_LANGUAGES)
        if isinstance(data, dict):
            for lang, values in data.items():
                if isinstance(values, dict):
                    merged.setdefault(lang, {})
                    merged[lang].update(values)
        return merged
    except Exception:
        return DEFAULT_LANGUAGES


LANG = load_language_catalog()


def tr(cfg: dict, key: str, **kwargs) -> str:
    lang = cfg.get("language", "de")
    value = LANG.get(lang, LANG["de"]).get(key, LANG["de"].get(key, key))
    try:
        return value.format(**kwargs)
    except Exception:
        return value


def finger_label(cfg: dict, finger_key: str) -> str:
    """Lokalisierter Anzeigename für einen Finger."""
    return tr(cfg, "finger_" + finger_key)


def list_fp_readers() -> list[tuple[str, str]]:
    """Findet angeschlossene FP-Reader best-effort über lsusb und /sys."""
    readers: list[tuple[str, str]] = [("auto", "Automatisch / Auto")]

    try:
        r = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            keywords = ("futronic", "finger", "fingerprint", "goodix", "validity", "synaptics")
            for line in r.stdout.splitlines():
                if any(k in line.lower() for k in keywords):
                    m = re.search(r"ID\s+([0-9a-fA-F]{4}:[0-9a-fA-F]{4})", line)
                    rid = m.group(1).lower() if m else line.strip()
                    readers.append((rid, line.strip()))
    except Exception:
        pass

    try:
        sys_usb = Path("/sys/bus/usb/devices")
        if sys_usb.exists():
            for dev in sys_usb.iterdir():
                vid_f = dev / "idVendor"
                pid_f = dev / "idProduct"
                prod_f = dev / "product"
                if not (vid_f.exists() and pid_f.exists()):
                    continue
                vid = vid_f.read_text(errors="ignore").strip().lower()
                pid = pid_f.read_text(errors="ignore").strip().lower()
                product = prod_f.read_text(errors="ignore").strip() if prod_f.exists() else ""
                label_text = product.lower()
                if any(k in label_text for k in ("futronic", "finger", "fingerprint", "goodix", "validity", "synaptics")):
                    readers.append((f"{vid}:{pid}", f"{vid}:{pid} {product}".strip()))
    except Exception:
        pass

    seen = set()
    unique = []
    for rid, label in readers:
        if rid not in seen:
            seen.add(rid)
            unique.append((rid, label))
    return unique


def image_mean_std(img) -> Tuple[float, float]:
    """
    Liefert (mean, std) für Scannerbilder.

    Unterstützt NumPy-Arrays mit .mean()/.std(), aber auch bytes,
    bytearray, memoryview und einfache Listen/Tuples mit 0..255-Werten.
    Damit hängt die Fingererkennung nicht mehr hart an NumPy.
    """
    if img is None:
        return 0.0, 0.0

    # Schneller Pfad für NumPy-Arrays oder ähnliche Objekte.
    try:
        return float(img.mean()), float(img.std())
    except Exception:
        pass

    # Fallback für bytes/list/memoryview.
    try:
        if isinstance(img, (bytes, bytearray)):
            data = img
        else:
            data = bytes(img)
    except Exception:
        try:
            data = bytes(bytearray(img))
        except Exception:
            return 0.0, 0.0

    n = len(data)
    if n == 0:
        return 0.0, 0.0

    mean = sum(data) / n
    var = sum((b - mean) ** 2 for b in data) / n
    return float(mean), float(var ** 0.5)

# ─────────────────────────────────────────────────────────────────
# Scanner
# ─────────────────────────────────────────────────────────────────

class Scanner:
    """
    Scanner-Wrapper über die gleiche sensor.py-Schnittstelle wie in der
    funktionierenden älteren Version:

        from sensor import open_scanner, get_size, capture

    Damit wird NICHT mehr direkt ScanAPI_Init/ScanAPI_Scan aus der .so gesucht.
    Genau das war der Grund für die Meldung
    "ScanAPI_Init/Close/Scan nicht vollständig vorhanden".
    """

    def __init__(
        self,
        sensor_py: str,
        reader_id: str = "auto",
        finger_mean_delta: float = 10.0,
        finger_present_std: float = 30.0,
        finger_off_std: float = 20.0,
    ):
        self.sensor_py = str(sensor_py)
        self.reader_id = str(reader_id or "auto")
        self.finger_mean_delta = float(finger_mean_delta)
        self.finger_present_std = float(finger_present_std)
        self.finger_off_std = float(finger_off_std)
        self.available = False
        self.initialized = False
        self.last_error = ""
        self._sensor = None
        self._handle = None
        self._w = 0
        self._h = 0
        self._size = 0
        self._load()

    def _load(self) -> None:
        sensor_dir_added = False
        sensor_dir = None
        try:
            import importlib.util
            import sys

            sensor_path = Path(self.sensor_py).expanduser()
            if not sensor_path.exists():
                self.last_error = f"sensor.py nicht gefunden: {sensor_path}"
                return

            # sensor.py darf lokale Helper relativ zu sich importieren.
            # Danach wird der temporäre sys.path-Eintrag wieder entfernt.
            sensor_dir = str(sensor_path.parent)
            if sensor_dir not in sys.path:
                sys.path.insert(0, sensor_dir)
                sensor_dir_added = True

            spec = importlib.util.spec_from_file_location("fingerprint_sensor", str(sensor_path))
            if not spec or not spec.loader:
                self.last_error = f"sensor.py kann nicht geladen werden: {sensor_path}"
                return

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            required = ("open_scanner", "get_size", "capture")
            missing = [name for name in required if not hasattr(module, name)]
            if missing:
                self.last_error = "sensor.py unvollständig, fehlt: " + ", ".join(missing)
                return

            self._sensor = module
            self.available = True
            self.last_error = ""
        except Exception as e:
            self.available = False
            self._sensor = None
            self.last_error = f"sensor.py nicht ladbar: {e}"
        finally:
            if sensor_dir_added and sensor_dir:
                try:
                    import sys
                    sys.path.remove(sensor_dir)
                except Exception:
                    pass

    def init(self) -> bool:
        if not self.available or self._sensor is None:
            return False
        try:
            try:
                self._handle = self._sensor.open_scanner(reader_id=self.reader_id)
            except TypeError:
                try:
                    self._handle = self._sensor.open_scanner(self.reader_id)
                except TypeError:
                    self._handle = self._sensor.open_scanner()
            self._w, self._h, self._size = self._sensor.get_size(self._handle)
            self.initialized = True
            self.last_error = ""
            return True
        except Exception as e:
            self._handle = None
            self.initialized = False
            self.last_error = f"Scanner öffnen/get_size fehlgeschlagen: {e}"
            return False

    def close(self) -> None:
        if self._sensor is not None and self._handle is not None:
            try:
                if hasattr(self._sensor, "close_scanner"):
                    self._sensor.close_scanner(self._handle)
            except Exception:
                pass
        self._handle = None
        self.initialized = False

    @property
    def image_size(self) -> tuple[int, int, int]:
        return int(self._w), int(self._h), int(self._size)

    def capture(self):
        if not self.available or self._sensor is None or self._handle is None:
            self.last_error = self.last_error or "Scanner nicht initialisiert"
            return None
        try:
            return self._sensor.capture(self._handle, self._w, self._h, self._size)
        except Exception as e:
            self.last_error = f"capture fehlgeschlagen: {e}"
            return None

    def wait_finger(self, timeout: float = 20.0):
        """Wartet wie die funktionierende Version auf Fingerkontakt."""
        t0 = time.time()
        baseline = None
        while time.time() - t0 < timeout:
            img = self.capture()
            if img is not None:
                mean, std = image_mean_std(img)
                if baseline is None:
                    baseline = mean
                if abs(mean - baseline) > self.finger_mean_delta or std > self.finger_present_std:
                    time.sleep(0.3)
                    img2 = self.capture()
                    return img2 if img2 is not None else img
            time.sleep(0.1)
        self.last_error = "Timeout beim Warten auf Finger"
        return None

    def wait_finger_off(self, timeout: float = 5.0) -> bool:
        """Wartet bis Finger entfernt wurde, ähnlich der alten Version."""
        end = time.time() + max(0.0, timeout)
        while time.time() < end:
            img = self.capture()
            if img is not None:
                _mean, std = image_mean_std(img)
                if std < self.finger_off_std:
                    return True
            time.sleep(0.1)
        return False

# ─────────────────────────────────────────────────────────────────
# NBIS Wrapper
# ─────────────────────────────────────────────────────────────────

def executable(path: Path) -> bool:
    return path.exists() and os.access(path, os.X_OK)


def validate_tools(cfg: dict[str, Any]) -> list[str]:
    paths = make_paths(cfg)
    errors: list[str] = []

    if not paths["sensor_py"].exists() and not cfg.get("demo_mode", False):
        errors.append(f"sensor.py nicht gefunden: {paths['sensor_py']}")

    nbis = Path(paths["nbis_dir"])
    for name in ("cwsq", "mindtct", "bozorth3"):
        exe = nbis / name
        if not executable(exe):
            errors.append(f"NBIS-Tool nicht ausführbar: {exe}")

    return errors


def raw_to_wsq(nbis_dir: str, img, out_base: Path, width: int, height: int, keep_raw: bool = False) -> tuple[bool, Path, str]:
    """Schreibt raw und erzeugt per NBIS cwsq eine WSQ-Datei."""
    raw_path = Path(str(out_base) + ".raw")
    wsq_path = Path(str(out_base) + ".wsq")
    exe = Path(nbis_dir) / "cwsq"
    try:
        try:
            img.tofile(str(raw_path))
        except Exception:
            raw_path.write_bytes(bytes(img))

        r = subprocess.run(
            [str(exe), "0.75", "wsq", str(raw_path), "-raw_in", f"{width},{height},8,500"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "cwsq Fehler").strip()
            return False, wsq_path, msg[:500]
        if not wsq_path.exists():
            return False, wsq_path, f"cwsq hat keine WSQ erzeugt: {wsq_path}"
        try:
            wsq_path.chmod(0o600)
        except Exception:
            pass
        return True, wsq_path, ""
    except Exception as e:
        return False, wsq_path, str(e)
    finally:
        if not keep_raw:
            try:
                raw_path.unlink()
            except Exception:
                pass


def run_mindtct(nbis_dir: str, wsq_path: Path, out_base: Path) -> tuple[bool, str]:
    exe = Path(nbis_dir) / "mindtct"
    try:
        r = subprocess.run(
            [str(exe), str(wsq_path), str(out_base)],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "mindtct Fehler").strip()
            return False, msg[:500]
        return True, ""
    except Exception as e:
        return False, str(e)


def count_minutiae(xyt_path: Path) -> int:
    try:
        lines = xyt_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return sum(1 for line in lines if line.strip() and not line.lstrip().startswith("#"))
    except Exception:
        return 0


def parse_minutiae(xyt_path: Path) -> list[tuple[float, ...]]:
    """Liest numerische Werte aus einer NBIS-.xyt-Datei.

    NBIS-XYT enthält typischerweise mindestens X, Y und Winkel/Theta.
    Manche Varianten enthalten zusätzliche Werte. Diese Funktion bleibt bewusst
    tolerant und nimmt alle numerischen Spalten pro Zeile mit.
    """
    rows: list[tuple[float, ...]] = []
    try:
        for line in xyt_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            nums = []
            for part in line.split():
                try:
                    nums.append(float(part))
                except ValueError:
                    pass
            if nums:
                rows.append(tuple(nums))
    except Exception:
        pass
    return rows


def minutiae_stats(xyt_path: Path) -> dict[str, Any]:
    rows = parse_minutiae(xyt_path)
    stats: dict[str, Any] = {
        "file": xyt_path.name,
        "path": str(xyt_path),
        "count": len(rows),
    }
    if not rows:
        return stats

    def col(idx: int) -> list[float]:
        return [r[idx] for r in rows if len(r) > idx]

    xs = col(0)
    ys = col(1)
    th = col(2)
    q  = col(3)

    if xs:
        stats["x_min"] = int(min(xs)); stats["x_max"] = int(max(xs))
    if ys:
        stats["y_min"] = int(min(ys)); stats["y_max"] = int(max(ys))
    if th:
        stats["theta_min"] = int(min(th)); stats["theta_max"] = int(max(th))
    if q:
        stats["quality_min"] = int(min(q)); stats["quality_max"] = int(max(q))
        stats["quality_avg"] = round(sum(q) / len(q), 1)
    stats["sample"] = [" ".join(str(int(v)) if float(v).is_integer() else f"{v:.1f}" for v in row[:4]) for row in rows[:5]]
    return stats


def format_minutiae_stats(xyt_path: Path, *, include_sample: bool = True) -> str:
    s = minutiae_stats(xyt_path)
    lines = [f"{s['file']}: {s['count']} Minutien"]
    if "x_min" in s and "y_min" in s:
        lines.append(f"  X: {s['x_min']}–{s['x_max']}   Y: {s['y_min']}–{s['y_max']}")
    if "theta_min" in s:
        lines.append(f"  Winkel/Theta: {s['theta_min']}–{s['theta_max']}")
    if "quality_avg" in s:
        lines.append(f"  Qualität: Ø {s['quality_avg']}  min/max {s['quality_min']}/{s['quality_max']}")
    if include_sample and s.get("sample"):
        lines.append("  Erste Werte: " + "; ".join(s["sample"]))
    return "\n".join(lines)


def minutiae_short_summary(xyt_path: Path, cfg: Optional[dict[str, Any]] = None) -> str:
    s = minutiae_stats(xyt_path)
    cfg = cfg or {}
    parts = [tr(cfg, "minutiae_summary", count=s["count"])]
    if "x_min" in s and "y_min" in s:
        parts.append(f"X {s['x_min']}–{s['x_max']}")
        parts.append(f"Y {s['y_min']}–{s['y_max']}")
    if "theta_min" in s:
        parts.append(f"{tr(cfg, 'minutiae_theta_short')} {s['theta_min']}–{s['theta_max']}")
    return " | ".join(parts)


def minutiae_quality(xyt_path: Path, min_minutiae: int = 20) -> dict[str, Any]:
    """Heuristische Scanqualität 0–100 aus der XYT-Datei.

    Das ist kein NFIQ-2-Ersatz, aber praktisch für Live-Feedback:
    - Anzahl der Minutien
    - räumliche Abdeckung in X/Y
    - Winkel-/Theta-Streuung
    - optional vorhandene Qualitäts-Spalte
    """
    s = minutiae_stats(xyt_path)
    count = int(s.get("count", 0))
    min_m = max(1, int(min_minutiae))

    count_score = min(100, int((count / max(min_m, 1)) * 70)) if count < min_m else min(100, 70 + int((count - min_m) * 2))

    area_score = 0
    if "x_min" in s and "x_max" in s and "y_min" in s and "y_max" in s:
        x_span = max(0, int(s["x_max"]) - int(s["x_min"]))
        y_span = max(0, int(s["y_max"]) - int(s["y_min"]))
        # FS81 liegt typischerweise im Bereich ~320x480. Die Werte sind bewusst tolerant.
        x_score = min(100, int((x_span / 220) * 100))
        y_score = min(100, int((y_span / 320) * 100))
        area_score = int((x_score + y_score) / 2)

    theta_score = 0
    if "theta_min" in s and "theta_max" in s:
        theta_span = max(0, int(s["theta_max"]) - int(s["theta_min"]))
        theta_score = min(100, int((theta_span / 160) * 100))

    q_score = None
    if "quality_avg" in s:
        try:
            q_score = max(0, min(100, int(float(s["quality_avg"]))))
        except Exception:
            q_score = None

    parts = [count_score, area_score, theta_score]
    weights = [0.55, 0.25, 0.20]
    if q_score is not None:
        parts.append(q_score)
        weights = [0.45, 0.20, 0.15, 0.20]

    score = int(round(sum(p * w for p, w in zip(parts, weights))))
    status = "good" if score >= 75 else "medium" if score >= 50 else "poor"

    return {
        "score": score,
        "status": status,
        "count": count,
        "count_score": count_score,
        "area_score": area_score,
        "theta_score": theta_score,
        "quality_score": q_score,
    }


def localized_quality_summary(cfg: dict, q: dict[str, Any]) -> str:
    status_key = {
        "good": "scan_quality_good",
        "medium": "scan_quality_medium",
        "poor": "scan_quality_poor",
    }.get(str(q.get("status")), "scan_quality_poor")
    return tr(cfg, "scan_quality_summary", score=int(q.get("score", 0)), status=tr(cfg, status_key))


def localized_quality_detail(cfg: dict, q: dict[str, Any]) -> str:
    status_key = {
        "good": "scan_quality_good",
        "medium": "scan_quality_medium",
        "poor": "scan_quality_poor",
    }.get(str(q.get("status")), "scan_quality_poor")
    return tr(
        cfg,
        "scan_quality_detail",
        score=int(q.get("score", 0)),
        status=tr(cfg, status_key),
        count_score=int(q.get("count_score", 0)),
        area_score=int(q.get("area_score", 0)),
        theta_score=int(q.get("theta_score", 0)),
    )


def enrollment_progress_percent(accepted: int, total: int, last_quality_score: int = 100) -> int:
    """Smartphone-ähnliche Fortschrittsanzeige.

    Der Fortschritt basiert auf akzeptierten Scans und wird leicht von der
    aktuellen Scanqualität beeinflusst. Echte Smartphones messen zusätzlich
    Abdeckungszuwachs/Variation; das ist hier eine transparente Annäherung.
    """
    total = max(1, int(total))
    accepted = max(0, min(int(accepted), total))
    base = (accepted / total) * 100.0
    # Schlechte Qualität soll Fortschritt nicht zu optimistisch wirken lassen.
    quality_factor = max(0.55, min(1.0, float(last_quality_score) / 100.0))
    if accepted >= total:
        return 100
    return max(0, min(99, int(round(base * quality_factor))))




def quality_color_rgb(score: Optional[int], active: bool = False) -> tuple[float, float, float]:
    """UI-Farbe für Finger-/Scanqualität."""
    if active:
        return (0.30, 0.69, 0.31)  # #4CAF50
    if score is None:
        return (0.62, 0.62, 0.62)  # #9E9E9E
    score = int(max(0, min(100, score)))
    if score >= 80:
        return (0.13, 0.59, 0.95)  # #2196F3
    if score >= 60:
        return (0.55, 0.76, 0.29)  # #8BC34A
    if score >= 40:
        return (1.00, 0.76, 0.03)  # #FFC107
    if score >= 20:
        return (1.00, 0.60, 0.00)  # #FF9800
    return (0.96, 0.26, 0.21)      # #F44336


def cairo_rounded_rect(cr, x: float, y: float, w: float, h: float, r: float) -> None:
    r = max(0.0, min(r, w / 2.0, h / 2.0))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def run_bozorth3(nbis_dir: str, probe: Path, gallery: Path) -> int:
    exe = Path(nbis_dir) / "bozorth3"
    try:
        r = subprocess.run(
            [str(exe), str(probe), str(gallery)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode == 0:
            parts = r.stdout.strip().split()
            return int(parts[0]) if parts else 0
    except Exception:
        pass
    return 0


# ─────────────────────────────────────────────────────────────────
# Hand Widget
# ─────────────────────────────────────────────────────────────────

class HandWidget(Gtk.DrawingArea):
    """
    Zwei Hand-Silhouetten. Enrollte Finger = grün, ausgewählt = blau.
    Klick auf Finger ruft callback(finger_key) auf.
    """

    _RIGHT = {
        "right_thumb":  (0.18, 0.76, 0.08, 0.08),
        "right_index":  (0.30, 0.16, 0.07, 0.09),
        "right_middle": (0.44, 0.08, 0.07, 0.09),
        "right_ring":   (0.58, 0.12, 0.07, 0.09),
        "right_pinky":  (0.72, 0.24, 0.06, 0.07),
    }
    _LEFT = {
        "left_thumb":   (0.82, 0.76, 0.08, 0.08),
        "left_index":   (0.70, 0.16, 0.07, 0.09),
        "left_middle":  (0.56, 0.08, 0.07, 0.09),
        "left_ring":    (0.42, 0.12, 0.07, 0.09),
        "left_pinky":   (0.28, 0.24, 0.06, 0.07),
    }

    def __init__(self):
        super().__init__()
        self.enrolled: set[str] = set()
        self.selected: str | None = None
        self.hover: str | None = None
        self.quality: dict[str, int] = {}
        self.active_finger: str | None = None
        self.active_progress: int = 0
        self._callback = None

        self.set_size_request(580, 260)
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK |
            Gdk.EventMask.POINTER_MOTION_MASK |
            Gdk.EventMask.LEAVE_NOTIFY_MASK
        )
        self.connect("draw", self._draw)
        self.connect("button-press-event", self._on_click)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("leave-notify-event", self._on_leave)

    def set_finger_callback(self, cb) -> None:
        self._callback = cb

    def update(self, enrolled: set[str], selected: str | None = None) -> None:
        self.enrolled = set(enrolled)
        self.selected = selected
        self.queue_draw()

    def set_quality_map(self, quality: dict[str, int]) -> None:
        self.quality = {k: int(max(0, min(100, v))) for k, v in dict(quality).items()}
        self.queue_draw()

    def set_active_finger(self, finger: str | None, progress: int = 0) -> None:
        self.active_finger = finger
        self.active_progress = int(max(0, min(100, progress)))
        self.queue_draw()

    def _draw_quality_finger(self, cr, key: str, cx: float, cy: float, rx: float, ry: float) -> None:
        score = self.quality.get(key)
        active = key == self.active_finger
        is_known = key in self.enrolled or score is not None or active or key == self.selected or key == self.hover

        if not is_known:
            return

        if active:
            pulse = 0.10 * (0.5 + 0.5 * math.sin(time.time() * 6.0))
        else:
            pulse = 0.0

        r, g, b = quality_color_rgb(score, active=active)
        alpha = 0.78 if active else 0.68 if key in self.enrolled or score is not None else 0.38

        x = cx - rx * 1.25
        y = cy - ry * 1.55
        ww = rx * 2.50
        hh = ry * 3.10
        radius = min(18.0, rx * 0.85)

        cr.save()
        cairo_rounded_rect(cr, x, y, ww, hh, radius)
        cr.set_source_rgba(r, g, b, min(1.0, alpha + pulse))
        cr.fill_preserve()

        if key == self.selected:
            cr.set_source_rgba(0.05, 0.15, 0.95, 0.95)
            cr.set_line_width(3.0)
        elif key == self.hover:
            cr.set_source_rgba(0.95, 0.45, 0.00, 0.90)
            cr.set_line_width(2.5)
        else:
            cr.set_source_rgba(0.18, 0.18, 0.18, 0.65)
            cr.set_line_width(1.4)
        cr.stroke()

        # Zahl im Finger
        label = str(score) if score is not None else (str(self.active_progress) if active else "")
        if label:
            cr.set_source_rgba(1, 1, 1, 0.96)
            cr.select_font_face("Sans", 0, 1)
            cr.set_font_size(max(10, min(18, rx * 0.85)))
            ext = cr.text_extents(label)
            cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy - ext.height / 2 - ext.y_bearing)
            cr.show_text(label)

        # Mini-Balken
        bar_score = self.active_progress if active and score is None else (score if score is not None else 0)
        bx = x + ww * 0.12
        by = y + hh + 3
        bw = ww * 0.76
        bh = max(3, min(6, ry * 0.16))
        cairo_rounded_rect(cr, bx, by, bw, bh, bh / 2)
        cr.set_source_rgba(0.20, 0.20, 0.20, 0.18)
        cr.fill()
        cairo_rounded_rect(cr, bx, by, bw * max(0.0, min(1.0, bar_score / 100.0)), bh, bh / 2)
        cr.set_source_rgba(r, g, b, 0.92)
        cr.fill()

        cr.restore()

    def _zones(self, w: int, h: int) -> dict[str, tuple[float, float, float, float]]:
        out: dict[str, tuple[float, float, float, float]] = {}
        hw = w / 2
        for k, (cx, cy, rx, ry) in self._RIGHT.items():
            out[k] = (cx * hw, cy * h, rx * hw, ry * h)
        for k, (cx, cy, rx, ry) in self._LEFT.items():
            out[k] = (hw + cx * hw, cy * h, rx * hw, ry * h)
        return out

    def _hit_test(self, widget, x: float, y: float) -> str | None:
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        for key, (cx, cy, rx, ry) in self._zones(w, h).items():
            if rx > 0 and ry > 0:
                if ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0:
                    return key
        return None

    def _on_click(self, widget, event) -> None:
        key = self._hit_test(widget, event.x, event.y)
        if key and self._callback:
            self._callback(key)

    def _on_motion(self, widget, event) -> None:
        key = self._hit_test(widget, event.x, event.y)
        if key != self.hover:
            self.hover = key
            window = widget.get_window()
            if window:
                cursor_name = "pointer" if key else "default"
                window.set_cursor(Gdk.Cursor.new_from_name(widget.get_display(), cursor_name))
            self.queue_draw()

    def _on_leave(self, widget, _event) -> None:
        self.hover = None
        window = widget.get_window()
        if window:
            window.set_cursor(Gdk.Cursor.new_from_name(widget.get_display(), "default"))
        self.queue_draw()

    def _draw(self, widget, cr) -> None:
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        hw = w / 2

        cr.set_source_rgb(0.97, 0.97, 0.97)
        cr.paint()

        # Rechte Hand links
        cr.save()
        cr.rectangle(0, 0, hw, h)
        cr.clip()
        self._draw_hand(cr, hw, h)
        cr.restore()

        # Linke Hand rechts gespiegelt
        cr.save()
        cr.rectangle(hw, 0, hw, h)
        cr.clip()
        cr.translate(w, 0)
        cr.scale(-1, 1)
        self._draw_hand(cr, hw, h)
        cr.restore()

        cr.set_source_rgba(0.70, 0.70, 0.70, 0.40)
        cr.set_line_width(1)
        cr.move_to(hw, 10)
        cr.line_to(hw, h - 10)
        cr.stroke()

        cr.set_source_rgb(0.30, 0.30, 0.30)
        cr.set_font_size(11)
        cr.move_to(hw * 0.38, h - 6)
        cr.show_text(getattr(self, "right_hand_label", "Rechte Hand"))
        cr.move_to(hw * 1.38, h - 6)
        cr.show_text(getattr(self, "left_hand_label", "Linke Hand"))

        # Cartoon-Finger mit Qualitätszahl + Mini-Balken
        for key, (cx, cy, rx, ry) in self._zones(w, h).items():
            self._draw_quality_finger(cr, key, cx, cy, rx, ry)

        self._draw_legend(cr, w, h)

    def _draw_hand(self, cr, W: float, H: float) -> None:
        skin = (0.957, 0.820, 0.722)
        skin_shadow = (0.820, 0.667, 0.565)
        nail_col = (0.980, 0.900, 0.855)
        nail_rim = (0.780, 0.640, 0.580)

        def p(x, y):
            return x * W, y * H

        cr.new_path()
        cr.move_to(*p(0.22, 1.00))
        cr.line_to(*p(0.22, 0.85))

        cr.curve_to(*p(0.17, 0.80), *p(0.09, 0.75), *p(0.10, 0.64))
        cr.curve_to(*p(0.11, 0.56), *p(0.19, 0.57), *p(0.21, 0.64))
        cr.curve_to(*p(0.23, 0.70), *p(0.23, 0.74), *p(0.26, 0.75))

        cr.curve_to(*p(0.28, 0.66), *p(0.27, 0.52), *p(0.28, 0.40))
        cr.curve_to(*p(0.28, 0.28), *p(0.27, 0.10), *p(0.30, 0.06))
        cr.curve_to(*p(0.32, 0.02), *p(0.37, 0.02), *p(0.39, 0.06))
        cr.curve_to(*p(0.41, 0.10), *p(0.40, 0.28), *p(0.39, 0.38))

        cr.curve_to(*p(0.40, 0.28), *p(0.40, 0.05), *p(0.44, 0.01))
        cr.curve_to(*p(0.46, -0.01), *p(0.51, -0.01), *p(0.53, 0.01))
        cr.curve_to(*p(0.55, 0.05), *p(0.54, 0.26), *p(0.53, 0.38))

        cr.curve_to(*p(0.54, 0.28), *p(0.55, 0.07), *p(0.58, 0.04))
        cr.curve_to(*p(0.60, 0.02), *p(0.65, 0.03), *p(0.66, 0.06))
        cr.curve_to(*p(0.68, 0.10), *p(0.67, 0.28), *p(0.66, 0.40))

        cr.curve_to(*p(0.67, 0.32), *p(0.68, 0.17), *p(0.71, 0.15))
        cr.curve_to(*p(0.73, 0.13), *p(0.77, 0.14), *p(0.78, 0.19))
        cr.curve_to(*p(0.79, 0.24), *p(0.78, 0.36), *p(0.77, 0.46))

        cr.curve_to(*p(0.79, 0.58), *p(0.80, 0.70), *p(0.78, 0.82))
        cr.curve_to(*p(0.77, 0.90), *p(0.76, 0.96), *p(0.74, 1.00))
        cr.close_path()

        cr.set_source_rgb(*skin)
        cr.fill_preserve()
        cr.set_source_rgb(*skin_shadow)
        cr.set_line_width(1.8)
        cr.stroke()

        # Fingernägel
        nails = [
            (0.34, 0.07, 0.055, 0.055),
            (0.48, 0.02, 0.055, 0.050),
            (0.62, 0.05, 0.055, 0.050),
            (0.74, 0.18, 0.045, 0.050),
            (0.14, 0.63, 0.050, 0.055),
        ]
        for nx, ny, nrx, nry in nails:
            cr.save()
            cr.translate(nx * W, ny * H)
            cr.scale(nrx * W, nry * H)
            cr.arc(0, 0, 1, 0, 2 * math.pi)
            cr.restore()
            cr.set_source_rgb(*nail_col)
            cr.fill_preserve()
            cr.set_source_rgb(*nail_rim)
            cr.set_line_width(0.8)
            cr.stroke()

        # Knöchellinien
        cr.set_source_rgba(0.72, 0.58, 0.50, 0.55)
        cr.set_line_width(0.9)
        knuckles = [
            [p(0.28, 0.40), p(0.34, 0.38), p(0.40, 0.40)],
            [p(0.28, 0.50), p(0.34, 0.48), p(0.40, 0.50)],
            [p(0.39, 0.38), p(0.46, 0.36), p(0.53, 0.38)],
            [p(0.39, 0.50), p(0.46, 0.48), p(0.53, 0.50)],
            [p(0.53, 0.40), p(0.59, 0.38), p(0.66, 0.40)],
            [p(0.53, 0.52), p(0.59, 0.50), p(0.66, 0.52)],
            [p(0.66, 0.46), p(0.71, 0.44), p(0.77, 0.46)],
        ]
        for pts in knuckles:
            cr.move_to(*pts[0])
            for pt in pts[1:]:
                cr.line_to(*pt)
            cr.stroke()

    def _draw_legend(self, cr, w: int, h: int) -> None:
        items = [
            (0.10, 0.75, 0.10, getattr(self, "legend_enrolled_label", "Enrollt")),
            (0.15, 0.35, 1.00, getattr(self, "legend_selected_label", "Ausgewählt")),
            (1.00, 0.72, 0.18, getattr(self, "legend_mouse_label", "Maus")),
        ]
        cr.set_font_size(10)
        lx = 8
        ly = h - 6
        for r, g, b, txt in items:
            cr.set_source_rgb(r, g, b)
            cr.arc(lx + 5, ly - 4, 5, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgb(0.15, 0.15, 0.15)
            cr.move_to(lx + 13, ly)
            cr.show_text(txt)
            lx += 95


# ─────────────────────────────────────────────────────────────────
# Scan Dialog
# ─────────────────────────────────────────────────────────────────


class ScanProgressWidget(Gtk.DrawingArea):
    """Runde Enrollment-Animation mit symbolischem Fingerabdruck."""

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.scan_no = 1
        self.scan_total = 1
        self.quality: Optional[int] = None
        self.enroll_progress = 0
        self.running = False
        self.phase = 0.0
        self.set_size_request(150, 150)
        self.connect("draw", self._draw)
        self._timer = GLib.timeout_add(45, self._tick)
        self.connect("destroy", self._on_destroy)

    def _on_destroy(self, *_args):
        if getattr(self, "_timer", None):
            try:
                GLib.source_remove(self._timer)
            except Exception:
                pass
            self._timer = None

    def _tick(self):
        if self.running:
            self.phase = (self.phase + 0.025) % 1.0
            self.queue_draw()
        return True

    def set_scan(self, scan_no: int, total: int) -> None:
        self.scan_no = max(1, int(scan_no))
        self.scan_total = max(1, int(total))
        self.quality = None
        self.running = True
        self.phase = 0.0
        self.queue_draw()

    def set_quality(self, quality: int, enroll_progress: int) -> None:
        self.quality = int(max(0, min(100, quality)))
        self.enroll_progress = int(max(0, min(100, enroll_progress)))
        self.running = False
        self.phase = 1.0
        self.queue_draw()

    def set_idle(self) -> None:
        self.running = False
        self.queue_draw()

    def _draw_fingerprint_symbol(self, cr, cx: float, cy: float, scale: float) -> None:
        cr.save()
        cr.set_source_rgba(0.15, 0.15, 0.15, 0.78)
        cr.set_line_width(max(1.2, scale * 0.035))
        for idx, r in enumerate((0.18, 0.30, 0.42)):
            cr.arc(cx, cy + scale * 0.05, scale * r, math.pi * 0.15, math.pi * 1.85)
            cr.stroke()
        cr.arc(cx, cy + scale * 0.10, scale * 0.56, math.pi * 0.28, math.pi * 1.72)
        cr.stroke()
        cr.move_to(cx, cy + scale * 0.08)
        cr.line_to(cx, cy + scale * 0.47)
        cr.stroke()
        cr.restore()

    def _draw(self, widget, cr) -> None:
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        cx = w / 2
        cy = h / 2 - 4
        radius = min(w, h) * 0.34

        cr.set_source_rgba(1, 1, 1, 0)
        cr.paint()

        q = self.quality
        r, g, b = quality_color_rgb(q, active=self.running)

        # Hintergrundkreis
        cr.set_line_width(9)
        cr.set_source_rgba(0.15, 0.15, 0.15, 0.10)
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.stroke()

        # Laufender Ring
        progress = self.phase if self.running else (q or 0) / 100.0
        cr.set_source_rgba(r, g, b, 0.95)
        cr.arc(cx, cy, radius, -math.pi / 2, -math.pi / 2 + 2 * math.pi * progress)
        cr.stroke()

        # Innenfläche
        cr.set_source_rgba(r, g, b, 0.10 if not self.running else 0.18)
        cr.arc(cx, cy, radius - 10, 0, 2 * math.pi)
        cr.fill()

        self._draw_fingerprint_symbol(cr, cx, cy - 4, radius)

        # Scan-Label
        cr.set_source_rgba(0.10, 0.10, 0.10, 0.90)
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(13)
        label = f"{self.scan_no}/{self.scan_total}"
        ext = cr.text_extents(label)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy + radius + 18)
        cr.show_text(label)

        # Qualität oberhalb des Kreises, damit Fingerprint-Symbol lesbar bleibt
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(18)
        val = "…" if q is None else str(q)
        ext = cr.text_extents(val)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy - radius - 12 - ext.y_bearing)
        cr.show_text(val)


class ScanDialog(Gtk.Dialog):
    """Führt N Scans durch; schlechte Scans zählen nicht."""

    def __init__(
        self,
        parent,
        scanner: Scanner,
        nbis_dir: str,
        tmp_dir: Path,
        username: str,
        finger: str,
        count: int,
        min_minutiae: int,
        scan_width: int,
        scan_height: int,
        between_scan_delay: float,
        demo_mode: bool,
        debug_keep_files: bool = False,
    ):
        super().__init__(
            title=tr(parent.cfg if hasattr(parent, "cfg") else {}, "scan_dialog_title", user=username, finger=finger),
            transient_for=parent,
            modal=True,
            destroy_with_parent=True,
        )
        self.scanner = scanner
        self.nbis_dir = nbis_dir
        self.tmp_dir = Path(tmp_dir)
        self.username = username
        self.finger = finger
        self.count = max(1, int(count))
        self.min_minutiae = max(1, int(min_minutiae))
        self.scan_width = max(50, int(scan_width))
        self.scan_height = max(50, int(scan_height))
        self.between_scan_delay = max(0.0, float(between_scan_delay))
        self.demo_mode = bool(demo_mode)
        self.debug_keep_files = bool(debug_keep_files)

        self._results: List[Tuple[Path, Path]] = []
        self._temp_paths: List[Path] = []
        self._running = True
        self._worker: Optional[threading.Thread] = None
        self._finished = threading.Event()
        self._destroyed = False

        self.set_default_size(520, 430)
        self.connect("response", self._on_response)
        self.connect("delete-event", self._on_delete_event)
        self.connect("destroy", self._on_destroy)

        area = self.get_content_area()
        area.set_spacing(8)
        area.set_border_width(12)

        self._lbl_status = Gtk.Label(label=tr(parent.cfg if hasattr(parent, "cfg") else {}, "place_finger"), xalign=0)
        self._lbl_status.set_line_wrap(True)
        area.pack_start(self._lbl_status, False, False, 0)

        self._scan_anim = ScanProgressWidget(parent.cfg if hasattr(parent, "cfg") else {})
        area.pack_start(self._scan_anim, False, False, 0)

        self._progress = Gtk.ProgressBar()
        self._progress.set_show_text(True)
        area.pack_start(self._progress, False, False, 0)

        self._lbl_detail = Gtk.Label(label="", xalign=0)
        self._lbl_detail.set_line_wrap(True)
        area.pack_start(self._lbl_detail, False, False, 0)

        info_lbl = Gtk.Label(label="Minutien pro Scan:", xalign=0)
        info_lbl.set_markup(f"<b>{tr(parent.cfg if hasattr(parent, 'cfg') else {}, 'minutiae_per_scan')}</b>")
        area.pack_start(info_lbl, False, False, 0)

        self._scan_info_buf = Gtk.TextBuffer()
        self._scan_info_view = Gtk.TextView(buffer=self._scan_info_buf)
        self._scan_info_view.set_editable(False)
        self._scan_info_view.set_monospace(True)
        self._scan_info_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scan_info_sw = Gtk.ScrolledWindow()
        scan_info_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scan_info_sw.set_min_content_height(82)
        scan_info_sw.add(self._scan_info_view)
        area.pack_start(scan_info_sw, True, True, 0)

        self.add_button(tr(parent.cfg if hasattr(parent, "cfg") else {}, "cancel"), Gtk.ResponseType.CANCEL)
        self._ok_btn = self.add_button(tr(parent.cfg if hasattr(parent, "cfg") else {}, "ok"), Gtk.ResponseType.OK)
        self._ok_btn.set_sensitive(False)

        self.show_all()

        self._worker = threading.Thread(target=self._scan_thread, daemon=True)
        self._worker.start()

    def get_results(self) -> List[Tuple[Path, Path]]:
        """Gibt pro erfolgreichem Scan ein Paar (xyt_path, wsq_path) zurück."""
        return list(self._results)

    def stop(self) -> None:
        self._running = False

    def cleanup_unkept_temp(self) -> None:
        """Löscht temporäre Dateien, außer erfolgreich zurückgegebene XYT/WSQ-Paare."""
        if self.debug_keep_files:
            parent = self.get_transient_for()
            if parent is not None and hasattr(parent, "_log"):
                try:
                    parent._log("Debug-Modus: Temp-Cleanup übersprungen; temporäre Dateien bleiben erhalten.")
                except Exception:
                    pass
            return

        keep = set()
        for pair in self._results:
            for p in pair:
                try:
                    if p.exists():
                        keep.add(p.resolve())
                except Exception:
                    pass

        for p in list(self._temp_paths):
            try:
                rp = p.resolve()
            except Exception:
                rp = p
            if rp not in keep and p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

    def _on_response(self, _dlg, response_id) -> None:
        if response_id != Gtk.ResponseType.OK:
            self.stop()
            self.cleanup_unkept_temp()

    def _on_delete_event(self, *_args):
        self.stop()
        self.cleanup_unkept_temp()
        return False

    def _on_destroy(self, *_args) -> None:
        self._destroyed = True
        self.stop()

    def _idle(self, fn, *args) -> None:
        if self._destroyed:
            return

        def _do():
            if self._destroyed:
                return False
            try:
                fn(*args)
            except Exception:
                pass
            return False

        GLib.idle_add(_do)

    def _set_status(self, txt: str) -> None:
        self._idle(self._lbl_status.set_text, txt)

    def _set_detail(self, txt: str) -> None:
        self._idle(self._lbl_detail.set_text, txt)
        # Fehler/Hinweise aus dem Scan-Dialog auch im Haupt-Log sichtbar machen.
        lowered = txt.lower()
        if any(word in lowered for word in (
            "fehler", "fehlgeschlagen", "permission", "zugriff",
            "abgebrochen", "zu niedrig", "timeout"
        )):
            parent = self.get_transient_for()
            if parent is not None and hasattr(parent, "_log"):
                try:
                    parent._log(f"Scan: {txt}")
                except Exception:
                    pass

    def _append_scan_info(self, txt: str) -> None:
        def _do():
            if hasattr(self, "_scan_info_buf"):
                end = self._scan_info_buf.get_end_iter()
                self._scan_info_buf.insert(end, txt.rstrip() + "\n")
                self._scan_info_view.scroll_to_iter(self._scan_info_buf.get_end_iter(), 0, False, 0, 0)
        GLib.idle_add(_do)

        parent = self.get_transient_for()
        if parent is not None and hasattr(parent, "_log"):
            try:
                parent._log("Scan-Minutien: " + txt.replace("\n", " | "))
            except Exception:
                pass

    def _set_progress(self, frac: float, text: str = "") -> None:
        frac = min(max(frac, 0.0), 1.0)

        def _do():
            self._progress.set_fraction(frac)
            self._progress.set_text(text)
        self._idle(_do)

    def _anim_scan_start(self, scan_no: int) -> None:
        self._idle(self._scan_anim.set_scan, scan_no, self.count)

    def _anim_scan_quality(self, quality: int, progress: int) -> None:
        self._idle(self._scan_anim.set_quality, quality, progress)

    def _anim_idle(self) -> None:
        self._idle(self._scan_anim.set_idle)

    def _make_demo_xyt(self, xyt_path: Path) -> None:
        # Dummy-Minutiendatei nur für UI-/Workflow-Test.
        rows = [f"{10+i*3} {10+i*5} {i % 180}" for i in range(max(self.min_minutiae, 8))]
        xyt_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        try:
            xyt_path.chmod(0o600)
        except Exception:
            pass

    def _scan_thread(self) -> None:
        try:
            ensure_private_dir(self.tmp_dir)
            i = 0

            while i < self.count and self._running:
                self._set_status(tr(self.get_transient_for().cfg if hasattr(self.get_transient_for(), "cfg") else {}, "scan_of", current=i + 1, total=self.count))
                self._set_progress(i / self.count, f"{enrollment_progress_percent(i, self.count, 100)}%")
                self._anim_scan_start(i + 1)

                stamp = f"{int(time.time() * 1000)}_{os.getpid()}_{threading.get_ident()}_{i}"
                safe_user = self.username if validate_username(self.username) else "user"
                safe_finger = self.finger if validate_finger(self.finger) else "finger"
                base = self.tmp_dir / f"{safe_user}_{safe_finger}_{stamp}"
                wsq = base.with_suffix(".wsq")
                xyt = Path(str(base) + ".xyt")

                self._temp_paths.extend([wsq, xyt])

                if self.demo_mode:
                    wsq.write_bytes(b"DEMO-WSQ\n")
                    try:
                        wsq.chmod(0o600)
                    except Exception:
                        pass
                    self._make_demo_xyt(xyt)
                else:
                    img = self.scanner.wait_finger(timeout=20.0)
                    if img is None:
                        err = self.scanner.last_error or "unbekannter Scanner-Fehler"
                        self._set_detail(tr(self.get_transient_for().cfg if hasattr(self.get_transient_for(), "cfg") else {}, "scan_error_retry", error=err))
                        time.sleep(0.8)
                        continue

                    w, h, _size = self.scanner.image_size
                    if not w or not h:
                        w, h = self.scan_width, self.scan_height

                    ok_w, wsq, msg = raw_to_wsq(self.nbis_dir, img, base, w, h, keep_raw=self.debug_keep_files)
                    if not ok_w:
                        self._set_detail(tr(self.get_transient_for().cfg if hasattr(self.get_transient_for(), "cfg") else {}, "wsq_failed", msg=msg))
                        time.sleep(0.8)
                        continue

                    ok_m, msg = run_mindtct(self.nbis_dir, wsq, base)
                    if not ok_m:
                        self._set_detail(tr(self.get_transient_for().cfg if hasattr(self.get_transient_for(), "cfg") else {}, "minutiae_extract_failed", msg=msg))
                        time.sleep(0.8)
                        continue

                minutiae = count_minutiae(xyt)
                current_scan_no = i + 1
                if minutiae < self.min_minutiae:
                    scan_cfg = self.get_transient_for().cfg if hasattr(self.get_transient_for(), "cfg") else {}
                    q = minutiae_quality(xyt, self.min_minutiae)
                    quality_text = localized_quality_summary(scan_cfg, q)
                    self._append_scan_info(
                        tr(scan_cfg, "scan_rejected",
                           scan=current_scan_no,
                           summary=minutiae_short_summary(xyt, scan_cfg),
                           quality=quality_text,
                           min=self.min_minutiae)
                    )
                    scan_cfg = self.get_transient_for().cfg if hasattr(self.get_transient_for(), "cfg") else {}
                    self._set_detail(localized_quality_detail(scan_cfg, q))
                    self._anim_scan_quality(int(q.get("score", 0)), enrollment_progress_percent(i, self.count, int(q.get("score", 0))))
                    if self.debug_keep_files:
                        self._set_detail(tr(scan_cfg, "debug_bad_scan_kept", wsq=wsq, xyt=xyt))
                    else:
                        for p in (wsq, xyt):
                            try:
                                p.unlink()
                            except Exception:
                                pass
                    time.sleep(0.8)
                    continue

                try:
                    xyt.chmod(0o600)
                except Exception:
                    pass

                self._results.append((xyt, wsq))
                i += 1
                self._set_progress(i / self.count, f"{enrollment_progress_percent(i, self.count, 100)}%")
                scan_cfg = self.get_transient_for().cfg if hasattr(self.get_transient_for(), "cfg") else {}
                q = minutiae_quality(xyt, self.min_minutiae)
                progress = enrollment_progress_percent(i, self.count, int(q.get("score", 0)))
                quality_text = localized_quality_summary(scan_cfg, q)
                progress_text = tr(scan_cfg, "scan_enroll_progress", progress=progress)
                self._append_scan_info(
                    tr(scan_cfg, "scan_accepted_summary",
                       scan=i,
                       summary=minutiae_short_summary(xyt, scan_cfg),
                       quality=quality_text,
                       progress_text=progress_text)
                )
                self._set_detail(localized_quality_detail(scan_cfg, q) + " | " + progress_text)
                self._anim_scan_quality(int(q.get("score", 0)), progress)

                if i < self.count and self.between_scan_delay > 0 and self._running:
                    self._set_status(
                        tr(scan_cfg, "remove_finger_next", delay=self.between_scan_delay)
                    )
                    if not self.demo_mode:
                        self.scanner.wait_finger_off(timeout=self.between_scan_delay)
                    end_time = time.time() + self.between_scan_delay
                    while self._running and time.time() < end_time:
                        time.sleep(0.05)
                else:
                    time.sleep(0.2)

            if self._running and len(self._results) == self.count:
                self._set_status(tr(self.get_transient_for().cfg if hasattr(self.get_transient_for(), "cfg") else {}, "all_scans_ok"))
                self._set_progress(1.0, "100%")
                self._anim_idle()
                self._idle(self._ok_btn.set_sensitive, True)
            elif not self._running:
                self._set_status(tr(self.get_transient_for().cfg if hasattr(self.get_transient_for(), "cfg") else {}, "scan_cancelled"))
            else:
                self._set_status(tr(self.get_transient_for().cfg if hasattr(self.get_transient_for(), "cfg") else {}, "scan_incomplete"))
        finally:
            self._finished.set()


# ─────────────────────────────────────────────────────────────────
# Settings Dialog
# ─────────────────────────────────────────────────────────────────

def roc_points_and_auc(genuine_scores: list[int], impostor_scores: list[int]) -> tuple[list[tuple[float, float]], float]:
    """ROC-Punkte als (FPR, TPR) und trapezoidale AUC."""
    if not genuine_scores or not impostor_scores:
        return [], 0.0
    max_score = max(genuine_scores + impostor_scores + [200])
    pts: list[tuple[float, float]] = []
    for thr in range(max_score + 1, -1, -1):
        far, frr = far_frr_at_threshold(genuine_scores, impostor_scores, thr)
        pts.append((far / 100.0, 1.0 - frr / 100.0))
    pts = sorted(set(pts), key=lambda p: p[0])
    auc = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        auc += (x2 - x1) * (y1 + y2) / 2.0
    return pts, max(0.0, min(1.0, auc))


def biometric_curve_data(genuine_scores: list[int], impostor_scores: list[int], max_threshold: Optional[int] = None) -> dict[str, Any]:
    if not genuine_scores and not impostor_scores:
        return {"thresholds": [], "far": [], "frr": [], "roc": [], "auc": 0.0, "eer": 0.0, "eer_thr": 0}
    max_score = max(genuine_scores + impostor_scores + [200])
    if max_threshold is not None:
        max_score = max(max_score, int(max_threshold))
    thresholds = list(range(0, max_score + 2))
    far_vals: list[float] = []
    frr_vals: list[float] = []
    for thr in thresholds:
        far, frr = far_frr_at_threshold(genuine_scores, impostor_scores, thr)
        far_vals.append(far)
        frr_vals.append(frr)
    eer, eer_thr = find_eer_threshold(genuine_scores, impostor_scores)
    roc, auc = roc_points_and_auc(genuine_scores, impostor_scores)
    return {"thresholds": thresholds, "far": far_vals, "frr": frr_vals, "roc": roc, "auc": auc, "eer": eer, "eer_thr": eer_thr}



def filter_xyt_lines(lines: list[str], min_quality: int) -> list[str]:
    """XYT-Zeilen nach 4. Spalte Qualität filtern.

    Zeilen ohne Qualitäts-Spalte werden behalten, damit ältere XYT-Formate nicht
    versehentlich komplett verschwinden.
    """
    min_quality = int(max(0, min(100, min_quality)))
    out: list[str] = []
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        parts = raw.split()
        if len(parts) >= 4:
            try:
                q = int(float(parts[3]))
                if q < min_quality:
                    continue
            except Exception:
                pass
        out.append(raw + "\n")
    return out


def filter_xyt_file(src: Path, dst: Path, min_quality: int) -> tuple[int, int]:
    lines = src.read_text(errors="ignore").splitlines()
    total = len([ln for ln in lines if ln.strip()])
    filtered = filter_xyt_lines(lines, min_quality)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("".join(filtered), encoding="utf-8")
    try:
        dst.chmod(0o600)
    except Exception:
        pass
    return len(filtered), total



def scores_from_rows(rows: list[dict[str, Any]]) -> tuple[list[int], list[int]]:
    genuine = [int(r.get("score", 0)) for r in rows if r.get("type") == "genuine"]
    impostor = [int(r.get("score", 0)) for r in rows if r.get("type") == "impostor"]
    return genuine, impostor


def finger_key_from_label_side(finger: str) -> str:
    if finger.startswith("left_"):
        return "left"
    if finger.startswith("right_"):
        return "right"
    return "unknown"


def diagnose_scores(genuine: list[int], impostor: list[int]) -> dict[str, Any]:
    gstats = score_basic_stats(genuine)
    istats = score_basic_stats(impostor)
    eer, eer_thr = find_eer_threshold(genuine, impostor)
    _roc, auc = roc_points_and_auc(genuine, impostor)
    return {
        "genuine_count": len(genuine),
        "impostor_count": len(impostor),
        "eer": eer,
        "eer_thr": eer_thr,
        "auc": auc,
        "genuine_min": int(gstats.get("min", 0)) if gstats else 0,
        "genuine_median": float(gstats.get("median", 0.0)) if gstats else 0.0,
        "genuine_avg": float(gstats.get("avg", 0.0)) if gstats else 0.0,
        "genuine_max": int(gstats.get("max", 0)) if gstats else 0,
        "impostor_max": int(istats.get("max", 0)) if istats else 0,
        "impostor_median": float(istats.get("median", 0.0)) if istats else 0.0,
        "impostor_avg": float(istats.get("avg", 0.0)) if istats else 0.0,
    }


def classify_finger_diagnosis(diag: dict[str, Any]) -> str:
    eer = float(diag.get("eer", 0.0))
    gmin = int(diag.get("genuine_min", 0))
    gmed = float(diag.get("genuine_median", 0.0))
    imax = int(diag.get("impostor_max", 0))
    if eer >= 12.0 or gmin < 10 or (imax >= gmed and imax > 60):
        return "bad"
    if eer >= 6.0 or gmin < 25 or imax >= 80:
        return "watch"
    return "ok"


def rows_for_finger(dataset: dict[str, Any], finger: str) -> list[dict[str, Any]]:
    rows = dataset.get("rows", [])
    return [
        r for r in rows
        if r.get("finger_a") == finger or r.get("finger_b") == finger
    ]


def rows_for_hand(dataset: dict[str, Any], hand: str) -> list[dict[str, Any]]:
    rows = dataset.get("rows", [])
    return [
        r for r in rows
        if finger_key_from_label_side(str(r.get("finger_a", ""))) == hand
        or finger_key_from_label_side(str(r.get("finger_b", ""))) == hand
    ]



class SettingsDialog(Gtk.Dialog):
    def __init__(self, parent, cfg: dict[str, Any]):
        self.cfg = coerce_cfg(cfg)
        super().__init__(
            title=tr(self.cfg, "settings_title"),
            transient_for=parent,
            modal=True,
            destroy_with_parent=True,
        )
        self.set_default_size(900, 720)

        area = self.get_content_area()
        area.set_border_width(12)
        area.set_spacing(6)

        self._entries: dict[str, Any] = {}
        self._labels: dict[str, Gtk.Label] = {}
        self._label_keys: dict[str, str] = {}
        self._check_keys: dict[str, str] = {}
        self._tab_labels: dict[str, Gtk.Label] = {}

        self._settings_nb = Gtk.Notebook()
        area.pack_start(self._settings_nb, True, True, 0)

        self._build_paths_tab()
        self._build_language_tab()
        self._build_reader_tab()
        self._build_scan_tab()

        self._status = Gtk.Label(label="", xalign=0)
        self._status.set_line_wrap(True)
        area.pack_start(self._status, False, False, 0)

        self._btn_cancel = self.add_button(tr(self.cfg, "cancel"), Gtk.ResponseType.CANCEL)
        self._btn_save = self.add_button(tr(self.cfg, "save"), Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)

        self.show_all()

    def _make_tab(self, tab_key: str) -> tuple[Gtk.Box, Gtk.Grid]:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=8)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.set_hexpand(True)
        sw.set_vexpand(True)
        outer.pack_start(sw, True, True, 0)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=10)
        sw.add(box)

        grid = Gtk.Grid(column_spacing=10, row_spacing=7)
        grid.set_hexpand(True)
        box.pack_start(grid, False, False, 0)

        lbl = Gtk.Label(label=tr(self.cfg, tab_key))
        self._tab_labels[tab_key] = lbl
        self._settings_nb.append_page(outer, lbl)
        return box, grid


    def _add_label(self, grid: Gtk.Grid, row_no: int, name: str, lang_key: str) -> Gtk.Label:
        lbl = Gtk.Label(label=tr(self.cfg, lang_key) + ":", xalign=1)
        grid.attach(lbl, 0, row_no, 1, 1)
        self._labels[name] = lbl
        self._label_keys[name] = lang_key
        return lbl

    def _build_paths_tab(self) -> None:
        _box, grid = self._make_tab("settings_tab_paths")
        row = 0
        fields = [
            ("fp_base", "path_base"),
            ("template_base_dir", "path_templates"),
            ("bin_dir", "path_bin"),
            ("log_dir", "path_log"),
            ("lib_path", "path_lib"),
            ("sensor_py", "path_sensor"),
            ("nbis_dir", "path_nbis"),
            ("username", "default_username"),
        ]

        for key, lang_key in fields:
            self._add_label(grid, row, key, lang_key)
            ent = Gtk.Entry(text=str(self.cfg.get(key, "")))
            ent.set_hexpand(True)
            grid.attach(ent, 1, row, 1, 1)
            self._entries[key] = ent

            if key != "username":
                btn = Gtk.Button(label="…")
                btn.connect("clicked", self._browse, ent, key in ("lib_path", "sensor_py"))
                grid.attach(btn, 2, row, 1, 1)
            row += 1

    def _build_language_tab(self) -> None:
        _box, grid = self._make_tab("settings_tab_language")
        row = 0
        self._add_label(grid, row, "language", "language")
        lang_combo = Gtk.ComboBoxText()
        lang_combo.append("de", "Deutsch")
        lang_combo.append("en", "English")
        lang_combo.set_active_id(str(self.cfg.get("language", "de")))
        lang_combo.connect("changed", self._on_language_changed)
        grid.attach(lang_combo, 1, row, 1, 1)
        self._entries["language"] = lang_combo

    def _build_reader_tab(self) -> None:
        _box, grid = self._make_tab("settings_tab_reader")
        row = 0
        self._add_label(grid, row, "fp_reader", "fp_reader")
        reader_combo = Gtk.ComboBoxText()
        current_reader = str(self.cfg.get("fp_reader", "auto"))
        found = False
        for rid, label in list_fp_readers():
            reader_combo.append(rid, label)
            if rid == current_reader:
                found = True
        if current_reader and not found:
            reader_combo.append(current_reader, f"{current_reader} (gespeichert / saved)")
        reader_combo.set_active_id(current_reader or "auto")
        grid.attach(reader_combo, 1, row, 1, 1)

        self._btn_refresh_reader = Gtk.Button(label="🔄")
        self._btn_refresh_reader.set_tooltip_text(tr(self.cfg, "reader_rescan"))
        self._btn_refresh_reader.connect("clicked", self._refresh_reader_combo, reader_combo)
        grid.attach(self._btn_refresh_reader, 2, row, 1, 1)
        self._entries["fp_reader"] = reader_combo

    def _build_scan_tab(self) -> None:
        _box, grid = self._make_tab("settings_tab_scan")
        row = 0

        # Numerische Scan-/Matching-Einstellungen
        int_fields = [
            ("threshold", "threshold", 1, 200),
            ("scan_count", "scan_count", 1, 20),
            ("min_minutiae", "min_minutiae", 1, 80),
            ("scan_width", "scan_width", 50, 2000),
            ("scan_height", "scan_height", 50, 2000),
            ("verify_top2_min", "verify_top2_min", 0, 200),
            ("enroll_pair_min", "enroll_pair_min", 0, 200),
            ("minutiae_filter_min_quality", "minutiae_filter_min_quality", 0, 100),
        ]
        for key, lang_key, lo, hi in int_fields:
            self._add_label(grid, row, key, lang_key)
            spin = Gtk.SpinButton.new_with_range(lo, hi, 1)
            spin.set_value(int(self.cfg.get(key, DEFAULT_CFG.get(key, lo))))
            spin.set_hexpand(True)
            grid.attach(spin, 1, row, 1, 1)
            self._entries[key] = spin
            row += 1

        self._add_label(grid, row, "between_scan_delay", "between_scan_delay")
        delay_spin = Gtk.SpinButton.new_with_range(0.0, 10.0, 0.1)
        delay_spin.set_digits(1)
        delay_spin.set_value(float(self.cfg.get("between_scan_delay", 1.2)))
        delay_spin.set_hexpand(True)
        grid.attach(delay_spin, 1, row, 1, 1)
        self._entries["between_scan_delay"] = delay_spin
        row += 1

        float_fields = [
            ("finger_mean_delta", "finger_mean_delta", 0.0, 80.0, 0.5),
            ("finger_present_std", "finger_present_std", 1.0, 120.0, 0.5),
            ("finger_off_std", "finger_off_std", 0.0, 80.0, 0.5),
        ]
        for key, lang_key, lo, hi, step in float_fields:
            self._add_label(grid, row, key, lang_key)
            spin = Gtk.SpinButton.new_with_range(lo, hi, step)
            spin.set_digits(1)
            spin.set_value(float(self.cfg.get(key, DEFAULT_CFG.get(key, lo))))
            spin.set_hexpand(True)
            grid.attach(spin, 1, row, 1, 1)
            self._entries[key] = spin
            row += 1

        self._add_label(grid, row, "log_level", "log_level")
        log_combo = Gtk.ComboBoxText()
        for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            log_combo.append(level, level)
        log_combo.set_active_id(str(self.cfg.get("log_level", "DEBUG")).upper())
        log_combo.set_hexpand(True)
        grid.attach(log_combo, 1, row, 1, 1)
        self._entries["log_level"] = log_combo
        row += 1

        # Schalter
        demo_chk = Gtk.CheckButton(label=tr(self.cfg, "demo_mode"))
        demo_chk.set_active(bool(self.cfg.get("demo_mode", False)))
        grid.attach(demo_chk, 0, row, 2, 1)
        self._entries["demo_mode"] = demo_chk
        self._check_keys["demo_mode"] = "demo_mode"
        row += 1

        debug_chk = Gtk.CheckButton(label=tr(self.cfg, "debug_keep_files"))
        debug_chk.set_tooltip_text(tr(self.cfg, "debug_keep_files_tip"))
        debug_chk.set_active(bool(self.cfg.get("debug_keep_files", False)))
        grid.attach(debug_chk, 0, row, 2, 1)
        self._entries["debug_keep_files"] = debug_chk
        self._check_keys["debug_keep_files"] = "debug_keep_files"
        row += 1

        ignore_outliers_chk = Gtk.CheckButton(label=tr(self.cfg, "ignore_outliers_verify"))
        ignore_outliers_chk.set_tooltip_text(tr(self.cfg, "ignore_outliers_verify_tip"))
        ignore_outliers_chk.set_active(bool(self.cfg.get("ignore_outliers_verify", False)))
        grid.attach(ignore_outliers_chk, 0, row, 2, 1)
        self._entries["ignore_outliers_verify"] = ignore_outliers_chk
        self._check_keys["ignore_outliers_verify"] = "ignore_outliers_verify"
        row += 1

        show_hand_quality_chk = Gtk.CheckButton(label=tr(self.cfg, "show_hand_quality"))
        show_hand_quality_chk.set_tooltip_text(tr(self.cfg, "show_hand_quality_tip"))
        show_hand_quality_chk.set_active(bool(self.cfg.get("show_hand_quality", False)))
        grid.attach(show_hand_quality_chk, 0, row, 2, 1)
        self._entries["show_hand_quality"] = show_hand_quality_chk
        self._check_keys["show_hand_quality"] = "show_hand_quality"
        row += 1

        filter_chk = Gtk.CheckButton(label=tr(self.cfg, "minutiae_filter_enabled"))
        filter_chk.set_tooltip_text(tr(self.cfg, "minutiae_filter_tip"))
        filter_chk.set_active(bool(self.cfg.get("minutiae_filter_enabled", False)))
        grid.attach(filter_chk, 0, row, 2, 1)
        self._entries["minutiae_filter_enabled"] = filter_chk
        self._check_keys["minutiae_filter_enabled"] = "minutiae_filter_enabled"
        row += 1

        self._btn_reset_scan_defaults = Gtk.Button(label=tr(self.cfg, "reset_scan_defaults"))
        self._btn_reset_scan_defaults.set_tooltip_text(tr(self.cfg, "reset_scan_defaults_tip"))
        self._btn_reset_scan_defaults.connect("clicked", self._reset_scan_defaults)
        grid.attach(self._btn_reset_scan_defaults, 0, row, 2, 1)
        row += 1

        self._scan_help_expander = Gtk.Expander(label=tr(self.cfg, "scan_help_title"))
        self._scan_help_expander.set_expanded(False)
        self._scan_help_label = Gtk.Label(label=tr(self.cfg, "scan_help_text"), xalign=0, yalign=0)
        self._scan_help_label.set_line_wrap(True)
        self._scan_help_label.set_selectable(True)
        self._scan_help_label.set_margin_top(8)
        self._scan_help_label.set_margin_bottom(8)
        self._scan_help_label.set_margin_start(8)
        self._scan_help_label.set_margin_end(8)
        self._scan_help_expander.add(self._scan_help_label)
        grid.attach(self._scan_help_expander, 0, row, 2, 1)


    def _on_language_changed(self, combo: Gtk.ComboBoxText) -> None:
        self.cfg["language"] = combo.get_active_id() or "de"
        self._apply_language()

    def _apply_language(self) -> None:
        self.set_title(tr(self.cfg, "settings_title"))

        for key, lbl in self._tab_labels.items():
            lbl.set_text(tr(self.cfg, key))

        for name, lang_key in self._label_keys.items():
            lbl = self._labels.get(name)
            if lbl is not None:
                lbl.set_text(tr(self.cfg, lang_key) + ":")

        for name, lang_key in self._check_keys.items():
            widget = self._entries.get(name)
            if widget is not None:
                widget.set_label(tr(self.cfg, lang_key))

        if "debug_keep_files" in self._entries:
            self._entries["debug_keep_files"].set_tooltip_text(tr(self.cfg, "debug_keep_files_tip"))
        if "ignore_outliers_verify" in self._entries:
            self._entries["ignore_outliers_verify"].set_tooltip_text(tr(self.cfg, "ignore_outliers_verify_tip"))
        if "show_hand_quality" in self._entries:
            self._entries["show_hand_quality"].set_tooltip_text(tr(self.cfg, "show_hand_quality_tip"))
        if "minutiae_filter_enabled" in self._entries:
            self._entries["minutiae_filter_enabled"].set_tooltip_text(tr(self.cfg, "minutiae_filter_tip"))

        if hasattr(self, "_btn_refresh_reader"):
            self._btn_refresh_reader.set_tooltip_text(tr(self.cfg, "reader_rescan"))

        if hasattr(self, "_btn_reset_scan_defaults"):
            self._btn_reset_scan_defaults.set_label(tr(self.cfg, "reset_scan_defaults"))
            self._btn_reset_scan_defaults.set_tooltip_text(tr(self.cfg, "reset_scan_defaults_tip"))

        if hasattr(self, "_scan_help_expander"):
            self._scan_help_expander.set_label(tr(self.cfg, "scan_help_title"))
        if hasattr(self, "_scan_help_label"):
            self._scan_help_label.set_text(tr(self.cfg, "scan_help_text"))

        if hasattr(self, "_btn_cancel"):
            self._btn_cancel.set_label(tr(self.cfg, "cancel"))
        if hasattr(self, "_btn_save"):
            self._btn_save.set_label(tr(self.cfg, "save"))

    def _browse(self, _btn, entry: Gtk.Entry, is_file: bool) -> None:
        action = Gtk.FileChooserAction.OPEN if is_file else Gtk.FileChooserAction.SELECT_FOLDER
        title = tr(self.cfg, "choose_file") if is_file else tr(self.cfg, "choose_dir")
        dlg = Gtk.FileChooserDialog(title=title, transient_for=self, action=action)
        dlg.add_buttons(tr(self.cfg, "cancel"), Gtk.ResponseType.CANCEL, tr(self.cfg, "ok"), Gtk.ResponseType.OK)

        current = entry.get_text().strip()
        if current:
            p = Path(current)
            try:
                if is_file:
                    if p.exists():
                        dlg.set_filename(str(p))
                    elif p.parent.exists():
                        dlg.set_current_folder(str(p.parent))
                else:
                    dlg.set_current_folder(str(p if p.is_dir() else p.parent))
            except Exception:
                pass

        if dlg.run() == Gtk.ResponseType.OK:
            selected = dlg.get_filename()
            if selected:
                entry.set_text(selected)
        dlg.destroy()

    def _refresh_reader_combo(self, _btn, combo: Gtk.ComboBoxText) -> None:
        current = combo.get_active_id() or "auto"
        combo.remove_all()
        found = False
        for rid, label in list_fp_readers():
            combo.append(rid, label)
            if rid == current:
                found = True
        if current and not found:
            combo.append(current, f"{current} (gespeichert / saved)")
        combo.set_active_id(current or "auto")

    def _reset_scan_defaults(self, *_args) -> None:
        """Nur Scan-/Matching-Einstellungen auf DEFAULT_CFG zurücksetzen."""
        for key in [
            "threshold", "scan_count", "min_minutiae", "scan_width", "scan_height",
            "verify_top2_min", "enroll_pair_min", "minutiae_filter_min_quality",
        ]:
            if key in self._entries:
                self._entries[key].set_value(int(DEFAULT_CFG[key]))

        for key in [
            "between_scan_delay", "finger_mean_delta", "finger_present_std", "finger_off_std",
        ]:
            if key in self._entries:
                self._entries[key].set_value(float(DEFAULT_CFG[key]))

        if "log_level" in self._entries:
            self._entries["log_level"].set_active_id(str(DEFAULT_CFG.get("log_level", "DEBUG")).upper())

        for key in [
            "demo_mode", "debug_keep_files", "ignore_outliers_verify",
            "show_hand_quality", "minutiae_filter_enabled",
        ]:
            if key in self._entries:
                self._entries[key].set_active(bool(DEFAULT_CFG.get(key, False)))

        self._status.set_text(tr(self.cfg, "reset_scan_defaults"))

    def get_cfg(self) -> dict[str, Any]:
        c = dict(self.cfg)
        str_keys = [
            "fp_base", "template_base_dir", "bin_dir",
            "log_dir", "lib_path", "sensor_py", "nbis_dir", "username"
        ]
        for key in str_keys:
            c[key] = self._entries[key].get_text().strip()

        for key in INT_RANGES:
            c[key] = int(self._entries[key].get_value())

        for key in FLOAT_RANGES:
            c[key] = float(self._entries[key].get_value())

        c["demo_mode"] = bool(self._entries["demo_mode"].get_active())
        c["debug_keep_files"] = bool(self._entries["debug_keep_files"].get_active())
        c["ignore_outliers_verify"] = bool(self._entries["ignore_outliers_verify"].get_active())
        c["show_hand_quality"] = bool(self._entries["show_hand_quality"].get_active())
        c["minutiae_filter_enabled"] = bool(self._entries["minutiae_filter_enabled"].get_active())
        c["language"] = self._entries["language"].get_active_id() or "de"
        c["fp_reader"] = self._entries["fp_reader"].get_active_id() or "auto"
        c["log_level"] = self._entries["log_level"].get_active_id() or "DEBUG"
        return coerce_cfg(c)


# ─────────────────────────────────────────────────────────────────
# Main Window
# ─────────────────────────────────────────────────────────────────


class ScoreCurvesWidget(Gtk.DrawingArea):
    """Cairo-basierte Kurvenansicht ohne Matplotlib-Abhängigkeit."""

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.dataset: dict[str, Any] | None = None
        self.threshold = int(cfg.get("threshold", 35))
        self.set_size_request(980, 620)
        self.connect("draw", self._draw)

    def set_dataset(self, dataset: dict[str, Any] | None, threshold: int) -> None:
        self.dataset = dataset
        self.threshold = int(threshold)
        self.queue_draw()

    def _text(self, cr, x, y, s, size=10, bold=False):
        cr.select_font_face("Sans", 0, 1 if bold else 0)
        cr.set_font_size(size)
        cr.move_to(x, y)
        cr.show_text(str(s))

    def _panel(self, cr, x, y, w, h, title):
        cr.set_source_rgb(1, 1, 1)
        cairo_rounded_rect(cr, x, y, w, h, 10)
        cr.fill_preserve()
        cr.set_source_rgba(0, 0, 0, 0.18)
        cr.set_line_width(1)
        cr.stroke()
        cr.set_source_rgb(0.05, 0.05, 0.05)
        self._text(cr, x + 12, y + 22, title, 11, True)

    def _plot_frame(self, cr, x, y, w, h):
        cr.set_source_rgba(0, 0, 0, 0.10)
        cr.set_line_width(1)
        for i in range(1, 5):
            yy = y + h * i / 5
            cr.move_to(x, yy)
            cr.line_to(x + w, yy)
            cr.stroke()
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.rectangle(x, y, w, h)
        cr.stroke()

    def _draw_farfrr(self, cr, x, y, w, h, genuine, impostor):
        self._panel(cr, x, y, w, h, tr(self.cfg, "chart_farfrr"))
        px, py, pw, ph = x + 48, y + 42, w - 70, h - 72
        self._plot_frame(cr, px, py, pw, ph)
        data = biometric_curve_data(genuine, impostor, max_threshold=self.threshold)
        thresholds, far, frr = data["thresholds"], data["far"], data["frr"]
        if not thresholds:
            return
        max_thr = max(thresholds) or 1

        def draw_line(vals, rgb):
            cr.set_source_rgb(*rgb)
            cr.set_line_width(2)
            for idx, (thr, val) in enumerate(zip(thresholds, vals)):
                xx = px + pw * (thr / max_thr)
                yy = py + ph * (1.0 - max(0.0, min(100.0, val)) / 100.0)
                if idx == 0:
                    cr.move_to(xx, yy)
                else:
                    cr.line_to(xx, yy)
            cr.stroke()

        draw_line(far, (0.85, 0.10, 0.10))
        draw_line(frr, (0.10, 0.20, 0.85))
        eer, eer_thr = data["eer"], data["eer_thr"]
        ex = px + pw * (eer_thr / max_thr)
        ey = py + ph * (1.0 - max(0.0, min(100.0, eer)) / 100.0)
        cr.set_source_rgb(0, 0, 0)
        cr.arc(ex, ey, 4, 0, 2 * math.pi)
        cr.fill()
        tx = px + pw * (self.threshold / max_thr)
        cr.set_source_rgba(0.1, 0.45, 0.1, 0.85)
        cr.move_to(tx, py)
        cr.line_to(tx, py + ph)
        cr.stroke()
        cr.set_source_rgb(0.85, 0.10, 0.10)
        self._text(cr, px + 8, y + h - 18, tr(self.cfg, "chart_far"), 9, True)
        cr.set_source_rgb(0.10, 0.20, 0.85)
        self._text(cr, px + 70, y + h - 18, tr(self.cfg, "chart_frr"), 9, True)
        cr.set_source_rgb(0, 0, 0)
        self._text(cr, px + 142, y + h - 18, tr(self.cfg, "chart_eer", eer=eer, thr=eer_thr), 9, False)

    def _draw_hist(self, cr, x, y, w, h, genuine, impostor):
        self._panel(cr, x, y, w, h, tr(self.cfg, "chart_hist"))
        px, py, pw, ph = x + 48, y + 42, w - 70, h - 72
        self._plot_frame(cr, px, py, pw, ph)
        scores = genuine + impostor
        if not scores:
            return
        max_score = max(scores + [100])
        bins = 28
        def hist(vals):
            arr = [0] * bins
            for s in vals:
                idx = min(bins - 1, int(max(0, s) / max_score * bins))
                arr[idx] += 1
            return arr
        gh, ih = hist(genuine), hist(impostor)
        m = max(gh + ih + [1])
        bw = pw / bins
        for i, v in enumerate(ih):
            hh = ph * v / m
            cr.set_source_rgba(0.85, 0.10, 0.10, 0.46)
            cr.rectangle(px + i * bw, py + ph - hh, bw * 0.92, hh)
            cr.fill()
        for i, v in enumerate(gh):
            hh = ph * v / m
            cr.set_source_rgba(0.10, 0.65, 0.20, 0.45)
            cr.rectangle(px + i * bw + bw * 0.18, py + ph - hh, bw * 0.72, hh)
            cr.fill()
        tx = px + pw * (self.threshold / max_score)
        cr.set_source_rgba(0, 0, 0, 0.55)
        cr.move_to(tx, py)
        cr.line_to(tx, py + ph)
        cr.stroke()
        cr.set_source_rgb(0.10, 0.65, 0.20)
        self._text(cr, px + 8, y + h - 18, tr(self.cfg, "chart_genuine"), 9, True)
        cr.set_source_rgb(0.85, 0.10, 0.10)
        self._text(cr, px + 96, y + h - 18, tr(self.cfg, "chart_impostor"), 9, True)

    def _draw_roc(self, cr, x, y, w, h, genuine, impostor):
        self._panel(cr, x, y, w, h, tr(self.cfg, "chart_roc"))
        px, py, pw, ph = x + 55, y + 42, w - 82, h - 76
        self._plot_frame(cr, px, py, pw, ph)
        roc, auc_val = roc_points_and_auc(genuine, impostor)
        cr.set_source_rgba(0.1, 0.1, 0.1, 0.25)
        cr.set_line_width(1)
        cr.move_to(px, py + ph)
        cr.line_to(px + pw, py)
        cr.stroke()
        if roc:
            cr.set_source_rgb(0.95, 0.45, 0.05)
            cr.set_line_width(2)
            for idx, (fpr, tpr) in enumerate(roc):
                xx = px + pw * max(0.0, min(1.0, fpr))
                yy = py + ph * (1.0 - max(0.0, min(1.0, tpr)))
                if idx == 0:
                    cr.move_to(xx, yy)
                else:
                    cr.line_to(xx, yy)
            cr.stroke()
        cr.set_source_rgb(0, 0, 0)
        self._text(cr, px + 8, y + h - 18, tr(self.cfg, "chart_auc", auc=auc_val), 9, True)
        self._text(cr, px + pw - 86, y + h - 18, "FAR/FPR →", 9, False)
        self._text(cr, x + 12, py + 14, tr(self.cfg, "chart_tpr"), 9, False)

    def _draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        cr.set_source_rgb(0.96, 0.96, 0.96)
        cr.paint()
        if not self.dataset:
            cr.set_source_rgb(0.2, 0.2, 0.2)
            self._text(cr, 20, 35, tr(self.cfg, "stats_waiting"), 13, True)
            return
        genuine = [int(x) for x in self.dataset.get("genuine", [])]
        impostor = [int(x) for x in self.dataset.get("impostor", [])]
        if not genuine and not impostor:
            cr.set_source_rgb(0.2, 0.2, 0.2)
            self._text(cr, 20, 35, tr(self.cfg, "stats_no_data"), 13, True)
            return
        gap = 14
        top_h = max(210, (h - 3 * gap) * 0.48)
        bottom_h = max(210, h - top_h - 3 * gap)
        left_w = (w - 3 * gap) / 2
        self._draw_farfrr(cr, gap, gap, left_w, top_h, genuine, impostor)
        self._draw_hist(cr, 2 * gap + left_w, gap, left_w, top_h, genuine, impostor)
        self._draw_roc(cr, gap, 2 * gap + top_h, w - 2 * gap, bottom_h, genuine, impostor)


class FPManager(Gtk.Window):
    def __init__(self):
        super().__init__(title=f"🔒 {APP_NAME}")
        self.set_default_size(860, 640)
        self.set_border_width(0)
        self.connect("destroy", self._on_quit)

        self.cfg = load_cfg()
        self.paths = make_paths(self.cfg)
        self.logger = setup_logging(self.cfg)

        self.scanner = Scanner(
            str(self.paths["sensor_py"]),
            str(self.cfg.get("fp_reader", "auto")),
            float(self.cfg.get("finger_mean_delta", 10.0)),
            float(self.cfg.get("finger_present_std", 30.0)),
            float(self.cfg.get("finger_off_std", 20.0)),
        )
        self._farfrr_stop = False

        self._build_ui()
        self.show_all()

        self._ensure_dirs()
        self._init_scanner()

        self._refresh_user_list()
        self._refresh_enroll_hand()
        self._populate_ver_users()
        self._update_debug_badge()
        self._update_language_labels()
        self._log(tr(self.cfg, "main_started", app=APP_NAME))

    def _ensure_dirs(self) -> None:
        for key in ("fp_base", "db_dir", "bin_dir", "log_dir", "tmp_dir"):
            ensure_private_dir(self.paths[key])

    def _init_scanner(self) -> None:
        # Scanner nur initialisieren, wenn kein Demo-Modus aktiv ist.
        if self.cfg.get("demo_mode", False):
            self._log(tr(self.cfg, "demo_active"))
            return

        if not self.scanner.available:
            self._log(tr(self.cfg, "scanner_missing"))
            return

        if self.scanner.init():
            self._log(tr(self.cfg, "scanner_loaded", reader=self.cfg.get("fp_reader", "auto")))
        else:
            self._log(tr(self.cfg, "scanner_init_failed"))

    def _reload_scanner(self) -> None:
        try:
            self.scanner.close()
        except Exception:
            pass
        self.scanner = Scanner(
            str(self.paths["sensor_py"]),
            str(self.cfg.get("fp_reader", "auto")),
            float(self.cfg.get("finger_mean_delta", 10.0)),
            float(self.cfg.get("finger_present_std", 30.0)),
            float(self.cfg.get("finger_off_std", 20.0)),
        )
        self._init_scanner()

    def _update_debug_badge(self) -> None:
        if not hasattr(self, "_debug_label"):
            return
        if self.cfg.get("debug_keep_files", False):
            self._debug_label.set_markup(
                f'<span foreground="orange"><b>{tr(self.cfg, "debug_badge")}</b></span>'
            )
        else:
            self._debug_label.set_text("")

    def _update_finger_combo_labels(self) -> None:
        if hasattr(self, "_finger_combo"):
            active = self._finger_combo.get_active_id()
            self._finger_combo.remove_all()
            for key, _label in FINGERS:
                self._finger_combo.append(key, finger_label(self.cfg, key))
            self._finger_combo.set_active_id(active or FINGERS[0][0])

        if hasattr(self, "_ver_finger_combo"):
            active = self._ver_finger_combo.get_active_id()
            self._ver_finger_combo.remove_all()
            for key, _label in FINGERS:
                self._ver_finger_combo.append(key, finger_label(self.cfg, key))
            self._ver_finger_combo.set_active_id(active or FINGERS[0][0])

    def _update_language_labels(self) -> None:
        if hasattr(self, "_lbl_users"):
            self._lbl_users.set_markup(f"<b>{tr(self.cfg, 'user_panel_title')}</b>")
        if hasattr(self, "_user_col"):
            self._user_col.set_title(tr(self.cfg, "user_name_column"))
        if hasattr(self, "_btn_new_user"):
            self._btn_new_user.set_label(tr(self.cfg, "new_user"))
        if hasattr(self, "_btn_del_user"):
            self._btn_del_user.set_label(tr(self.cfg, "delete_user"))
        if hasattr(self, "_btn_enroll"):
            self._btn_enroll.set_label(tr(self.cfg, "enroll_finger_button"))
        if hasattr(self, "_btn_delete_finger"):
            self._btn_delete_finger.set_label(tr(self.cfg, "delete_finger_button"))
        if hasattr(self, "_btn_farfrr_calc"):
            self._btn_farfrr_calc.set_label(tr(self.cfg, "farfrr_calculate_templates"))
        if hasattr(self, "_chk_farfrr_full_eer"):
            self._chk_farfrr_full_eer.set_label(tr(self.cfg, "farfrr_full_eer"))

        if hasattr(self, "_lbl_users"):
            self._lbl_users.set_text(tr(self.cfg, "user_panel_title"))
        if hasattr(self, "_user_col"):
            self._user_col.set_title(tr(self.cfg, "user_name_column"))
        if hasattr(self, "_btn_new_user"):
            self._btn_new_user.set_label(tr(self.cfg, "new_user"))
        if hasattr(self, "_btn_del_user"):
            self._btn_del_user.set_label(tr(self.cfg, "delete_user"))
        if hasattr(self, "_btn_enroll"):
            self._btn_enroll.set_label(tr(self.cfg, "enroll_finger_button"))
        if hasattr(self, "_btn_delete_finger"):
            self._btn_delete_finger.set_label(tr(self.cfg, "delete_finger_button"))
        if hasattr(self, "_btn_farfrr_calc"):
            self._btn_farfrr_calc.set_label(tr(self.cfg, "farfrr_calculate_templates"))
        if hasattr(self, "_chk_farfrr_full_eer"):
            self._chk_farfrr_full_eer.set_label(tr(self.cfg, "farfrr_full_eer"))

        if hasattr(self, "_btn_settings"):
            self._btn_settings.set_label(tr(self.cfg, "settings"))

        if hasattr(self, "_minute_info_title"):
            self._minute_info_title.set_markup(f"<b>{tr(self.cfg, 'minute_info_title')}</b>")

        if hasattr(self, "_hand"):
            self._hand.right_hand_label = tr(self.cfg, "right_hand")
            self._hand.left_hand_label = tr(self.cfg, "left_hand")
            self._hand.legend_enrolled_label = tr(self.cfg, "legend_enrolled")
            self._hand.legend_selected_label = tr(self.cfg, "legend_selected")
            self._hand.legend_mouse_label = tr(self.cfg, "legend_mouse")
            self._hand.queue_draw()

        self._update_finger_combo_labels()

        if hasattr(self, "_ver_user_label"):
            self._ver_user_label.set_text(tr(self.cfg, "user") + ":")
        if hasattr(self, "_ver_finger_label"):
            self._ver_finger_label.set_text(tr(self.cfg, "finger") + ":")
        if hasattr(self, "_chk_hand_quality"):
            self._chk_hand_quality.set_label(tr(self.cfg, "show_hand_quality"))
            self._chk_hand_quality.set_tooltip_text(tr(self.cfg, "show_hand_quality_tip"))
            self._chk_hand_quality.set_active(bool(self.cfg.get("show_hand_quality", False)))

        if hasattr(self, "_btn_verify"):
            self._btn_verify.set_label(tr(self.cfg, "verify_button"))
        if hasattr(self, "_btn_farfrr_save"):
            self._btn_farfrr_save.set_label(tr(self.cfg, "farfrr_save"))
        if hasattr(self, "_btn_farfrr_defaults"):
            self._btn_farfrr_defaults.set_label(tr(self.cfg, "farfrr_defaults"))
        if hasattr(self, "_btn_quality_csv"):
            self._btn_quality_csv.set_label(tr(self.cfg, "quality_csv_export"))

        if hasattr(self, "_btn_stats_calc"):
            self._btn_stats_calc.set_label(tr(self.cfg, "stats_calculate"))
        if hasattr(self, "_btn_stats_last"):
            self._btn_stats_last.set_label(tr(self.cfg, "stats_use_last"))
        if hasattr(self, "_stats_chart"):
            self._stats_chart.cfg = self.cfg
            self._stats_chart.queue_draw()

        if hasattr(self, "nb"):
            tab_keys = ["enrollment_tab", "verify_tab", "farfrr_tab", "stats_tab", "log_tab"]
            for i, key in enumerate(tab_keys):
                page = self.nb.get_nth_page(i)
                if page is not None:
                    self.nb.set_tab_label_text(page, tr(self.cfg, key))

        self._refresh_enrolled_list()
        self._update_minutiae_info(self._finger_combo.get_active_id() if hasattr(self, "_finger_combo") else None)




    def _on_quit(self, *_args) -> None:
        self._farfrr_stop = True
        try:
            self.scanner.close()
            self._log(tr(self.cfg, "scanner_closed"))
        except Exception:
            pass
        Gtk.main_quit()

    def _build_ui(self) -> None:
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(vbox)

        hbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, border_width=6)
        hbar.get_style_context().add_class("titlebar")

        self._btn_settings = Gtk.Button(label=tr(self.cfg, "settings"))
        self._btn_settings.connect("clicked", self._on_settings)
        hbar.pack_end(self._btn_settings, False, False, 0)

        self._version_label = Gtk.Label(label=APP_NAME, xalign=0)
        self._version_label.set_markup(f"<b>{APP_NAME}</b>")
        hbar.pack_start(self._version_label, False, False, 0)

        self._debug_label = Gtk.Label(label="", xalign=0)
        hbar.pack_start(self._debug_label, False, False, 0)

        self._status_label = Gtk.Label(label="", xalign=0)
        hbar.pack_start(self._status_label, True, True, 0)

        vbox.pack_start(hbar, False, False, 0)

        self.nb = Gtk.Notebook()
        vbox.pack_start(self.nb, True, True, 0)

        self._build_enroll_tab()
        self._build_verify_tab()
        self._build_farfrr_tab()
        self._build_stats_tab()
        self._build_log_tab()

    def _on_settings(self, *_args) -> None:
        dlg = SettingsDialog(self, self.cfg)
        if dlg.run() == Gtk.ResponseType.OK:
            self.cfg = dlg.get_cfg()
            save_cfg(self.cfg)
            self.paths = make_paths(self.cfg)
            self.logger = setup_logging(self.cfg)
            self._ensure_dirs()
            self._reload_scanner()
            self._refresh_user_list()
            self._refresh_enrolled_list()
            self._refresh_enroll_hand()
            self._populate_ver_users()
            self._update_debug_badge()
            self._update_language_labels()
            if hasattr(self, "_farfrr_adj"):
                self._farfrr_adj.set_value(int(self.cfg["threshold"]))
            elif hasattr(self, "_farfrr_spin"):
                self._farfrr_spin.set_value(int(self.cfg["threshold"]))
            self._update_language_labels()
            self._refresh_enroll_hand()
            self._log(tr(self.cfg, "settings_saved"))
        dlg.destroy()

    # ─────────────────────────────────────────────────────────────
    # Tab 1: Enrollment
    # ─────────────────────────────────────────────────────────────

    def _build_enroll_tab(self) -> None:
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, border_width=8)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.set_size_request(190, -1)

        self._lbl_users = Gtk.Label(xalign=0)
        self._lbl_users.set_markup(f"<b>{tr(self.cfg, 'user_panel_title')}</b>")
        left.pack_start(self._lbl_users, False, False, 0)

        self._user_store = Gtk.ListStore(str)
        self._user_tv = Gtk.TreeView(model=self._user_store)
        self._user_col = Gtk.TreeViewColumn(tr(self.cfg, "user_name_column"), Gtk.CellRendererText(), text=0)
        self._user_tv.append_column(self._user_col)
        self._user_tv.get_selection().connect("changed", self._on_user_selected)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.add(self._user_tv)
        left.pack_start(sw, True, True, 0)

        btn_box = Gtk.Box(spacing=4)
        self._btn_new_user = Gtk.Button(label=tr(self.cfg, "new_user"))
        self._btn_del_user = Gtk.Button(label=tr(self.cfg, "delete_user"))
        self._btn_new_user.connect("clicked", self._on_user_add)
        self._btn_del_user.connect("clicked", self._on_user_del)
        btn_box.pack_start(self._btn_new_user, True, True, 0)
        btn_box.pack_start(self._btn_del_user, True, True, 0)
        left.pack_start(btn_box, False, False, 0)

        hbox.pack_start(left, False, False, 0)
        hbox.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        self._hand = HandWidget()
        self._hand.set_finger_callback(self._on_finger_click)
        right.pack_start(self._hand, False, False, 0)

        self._chk_hand_quality = Gtk.CheckButton(label=tr(self.cfg, "show_hand_quality"))
        self._chk_hand_quality.set_tooltip_text(tr(self.cfg, "show_hand_quality_tip"))
        self._chk_hand_quality.set_active(bool(self.cfg.get("show_hand_quality", False)))
        self._chk_hand_quality.connect("toggled", self._on_enroll_quality_toggle)
        right.pack_start(self._chk_hand_quality, False, False, 0)

        fbox = Gtk.Box(spacing=6)
        fbox.pack_start(Gtk.Label(label=tr(self.cfg, "finger") + ":"), False, False, 0)
        self._finger_combo = Gtk.ComboBoxText()
        for key, _label in FINGERS:
            self._finger_combo.append(key, finger_label(self.cfg, key))
        self._finger_combo.set_active(0)
        self._finger_combo.connect("changed", self._on_finger_changed)
        fbox.pack_start(self._finger_combo, True, True, 0)
        right.pack_start(fbox, False, False, 0)

        lbl2 = Gtk.Label(xalign=0)
        lbl2.set_markup(f"<b>{tr(self.cfg, 'enrolled_fingers')}</b>")
        right.pack_start(lbl2, False, False, 0)

        self._enroll_store = Gtk.ListStore(str, str, int)
        self._enroll_tv = Gtk.TreeView(model=self._enroll_store)
        tv = self._enroll_tv
        tv.append_column(Gtk.TreeViewColumn(tr(self.cfg, "key"), Gtk.CellRendererText(), text=0))
        tv.append_column(Gtk.TreeViewColumn(tr(self.cfg, "display"), Gtk.CellRendererText(), text=1))
        tv.append_column(Gtk.TreeViewColumn(tr(self.cfg, "templates"), Gtk.CellRendererText(), text=2))
        tv.get_selection().connect("changed", self._on_enrolled_selection_changed)

        sw2 = Gtk.ScrolledWindow()
        sw2.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw2.set_size_request(-1, 110)
        sw2.add(tv)
        right.pack_start(sw2, False, False, 0)

        action_box = Gtk.Box(spacing=6)

        self._btn_enroll = Gtk.Button(label=tr(self.cfg, "enroll_finger_button"))
        self._btn_enroll.connect("clicked", self._on_enroll_click)
        self._btn_enroll.set_sensitive(False)
        action_box.pack_start(self._btn_enroll, True, True, 0)

        self._btn_delete_finger = Gtk.Button(label=tr(self.cfg, "delete_finger_button"))
        self._btn_delete_finger.connect("clicked", self._on_delete_enrolled_finger)
        self._btn_delete_finger.set_sensitive(False)
        action_box.pack_start(self._btn_delete_finger, True, True, 0)

        right.pack_start(action_box, False, False, 0)

        self._enroll_status = Gtk.Label(label="", xalign=0)
        self._enroll_status.set_line_wrap(True)
        right.pack_start(self._enroll_status, False, False, 0)

        # Mittleres Enrollment-Panel zuerst einhängen.
        hbox.pack_start(right, True, True, 0)

        # Rechtes Hochformat-Panel für Minutien-Infos.
        hbox.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.VERTICAL),
            False, False, 0
        )

        minute_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        minute_panel.set_size_request(340, -1)

        self._minute_info_title = Gtk.Label(xalign=0)
        self._minute_info_title.set_markup(f"<b>{tr(self.cfg, 'minute_info_title')}</b>")
        minute_panel.pack_start(self._minute_info_title, False, False, 0)

        self._minutiae_info_buf = Gtk.TextBuffer()
        self._minutiae_info_view = Gtk.TextView(buffer=self._minutiae_info_buf)
        self._minutiae_info_view.set_editable(False)
        self._minutiae_info_view.set_monospace(True)
        self._minutiae_info_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._minutiae_info_view.set_left_margin(6)
        self._minutiae_info_view.set_right_margin(6)
        self._minutiae_info_view.set_top_margin(6)
        self._minutiae_info_view.set_bottom_margin(6)

        mi_sw = Gtk.ScrolledWindow()
        mi_sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        mi_sw.set_min_content_width(330)
        mi_sw.add(self._minutiae_info_view)
        minute_panel.pack_start(mi_sw, True, True, 0)

        hbox.pack_start(minute_panel, False, False, 0)

        self.nb.append_page(hbox, Gtk.Label(label=tr(self.cfg, "enrollment_tab")))

    def _cur_user(self) -> str | None:
        model, it = self._user_tv.get_selection().get_selected()
        return model[it][0] if it else None

    def _user_dir(self, user: str) -> Path:
        if not validate_username(user):
            raise ValueError("Ungültiger Benutzername")
        return safe_child(self.paths["db_dir"], user)

    def _finger_dir(self, user: str, finger: str) -> Path:
        if not validate_finger(finger):
            raise ValueError("Ungültiger Finger")
        return safe_child(self._user_dir(user), finger)

    def _finger_fast_quality_score(self, user: str, finger: str) -> int | None:
        """Schnelle Einzelbewertung für genau einen Finger ohne Bozorth3."""
        try:
            fdir = self._finger_dir(user, finger)
            templates = sorted(fdir.glob("*.xyt")) if fdir.exists() else []
            if not templates:
                return None
            qvals = [
                int(minutiae_quality(p, int(self.cfg.get("min_minutiae", 20))).get("score", 0))
                for p in templates
            ]
            return int(max(0, min(100, sum(qvals) / len(qvals)))) if qvals else None
        except Exception as e:
            self._log_debug(f"Einzelfinger-Qualität nicht berechenbar für {user}/{finger}: {e}")
            return None

    def _show_single_finger_quality(self, finger_key: str) -> None:
        """Wenn die globale Qualitätsanzeige aus ist: nur angeklickten Finger anzeigen."""
        if self.cfg.get("show_hand_quality", False):
            return
        user = self._cur_user()
        if not user or not validate_finger(finger_key):
            return
        if finger_key not in self._enrolled_set(user):
            self._hand.set_quality_map({})
            return

        score = self._finger_fast_quality_score(user, finger_key)
        if score is None:
            self._hand.set_quality_map({})
            self._log(tr(self.cfg, "show_clicked_quality_missing"))
            return

        self._hand.set_quality_map({finger_key: score})
        self._log_debug(tr(self.cfg, "show_clicked_quality", finger=finger_label(self.cfg, finger_key), score=score))

    def _on_enroll_quality_toggle(self, widget: Gtk.CheckButton) -> None:
        self.cfg["show_hand_quality"] = bool(widget.get_active())
        try:
            save_cfg(self.cfg)
        except Exception as e:
            self._log_error(f"Qualitätsanzeige konnte nicht gespeichert werden: {e}")
        self._refresh_enroll_hand()

    def _on_finger_click(self, finger_key: str) -> None:
        self._finger_combo.set_active_id(finger_key)
        self._select_enrolled_row(finger_key)
        self._update_minutiae_info(finger_key)
        self._show_single_finger_quality(finger_key)

    def _on_finger_changed(self, *_args) -> None:
        self._refresh_enroll_hand()
        self._update_minutiae_info(self._finger_combo.get_active_id())

    def _on_user_selected(self, *_args) -> None:
        has = self._cur_user() is not None
        self._btn_enroll.set_sensitive(has)
        if hasattr(self, "_btn_delete_finger"):
            self._btn_delete_finger.set_sensitive(has)
        self._refresh_enrolled_list()
        self._refresh_enroll_hand()
        self._update_minutiae_info(self._finger_combo.get_active_id())

    def _on_enrolled_selection_changed(self, selection) -> None:
        model, it = selection.get_selected()
        if it:
            finger = model[it][0]
            if validate_finger(finger):
                self._finger_combo.set_active_id(finger)
                self._update_minutiae_info(finger)

    def _select_enrolled_row(self, finger: str) -> None:
        if not hasattr(self, "_enroll_tv") or not finger:
            return
        model = self._enroll_store
        for row in model:
            if row[0] == finger:
                self._enroll_tv.get_selection().select_iter(row.iter)
                return

    def _update_minutiae_info(self, finger: str | None = None) -> None:
        if not hasattr(self, "_minutiae_info_buf"):
            return
        user = self._cur_user()
        finger = finger or self._finger_combo.get_active_id()
        if not user or not finger or not validate_finger(finger):
            self._minutiae_info_buf.set_text(tr(self.cfg, "select_user_finger_minutiae"))
            return

        try:
            fdir = self._finger_dir(user, finger)
        except Exception as e:
            self._minutiae_info_buf.set_text(tr(self.cfg, "minutiae_unavailable", error=e))
            return

        templates = sorted(fdir.glob("*.xyt")) if fdir.exists() else []
        label = finger_label(self.cfg, finger)
        if not templates:
            self._minutiae_info_buf.set_text(
                f"{user} / {label}\n{tr(self.cfg, 'no_templates_for_finger')}"
            )
            return

        counts = [count_minutiae(p) for p in templates]
        total = sum(counts)
        avg = total / len(counts) if counts else 0
        lines = [
            tr(self.cfg, "minutiae_header_user", user=user),
            tr(self.cfg, "minutiae_header_finger", finger_label=label, finger_key=finger),
            tr(self.cfg, "minutiae_header_templates", count=len(templates)),
            tr(self.cfg, "minutiae_header_total", total=total, avg=avg),
            "",
        ]
        for p in templates:
            lines.append(format_minutiae_stats(p, include_sample=True))
            lines.append("")
        self._minutiae_info_buf.set_text("\n".join(lines).rstrip())

    def _refresh_user_list(self) -> None:
        self._user_store.clear()
        db = self.paths["db_dir"]
        if db.exists():
            for d in sorted(db.iterdir()):
                if d.is_dir() and validate_username(d.name):
                    self._user_store.append([d.name])

    def _refresh_enrolled_list(self) -> None:
        self._enroll_store.clear()
        user = self._cur_user()
        if not user:
            return

        try:
            udir = self._user_dir(user)
        except ValueError:
            return

        if not udir.exists():
            return

        for finger, _label in FINGERS:
            fdir = udir / finger
            count = len(list(fdir.glob("*.xyt"))) if fdir.exists() else 0
            if count > 0:
                self._enroll_store.append([finger, finger_label(self.cfg, finger), count])

        self._update_minutiae_info(self._finger_combo.get_active_id())

    def _enrolled_set(self, user: str | None = None) -> set[str]:
        user = user or self._cur_user()
        if not user:
            return set()

        out: set[str] = set()
        try:
            udir = self._user_dir(user)
        except ValueError:
            return out

        for finger, _ in FINGERS:
            fdir = udir / finger
            if fdir.exists() and any(fdir.glob("*.xyt")):
                out.add(finger)
        return out

    def _finger_quality_scores(self, user: str | None = None) -> dict[str, int]:
        """Schnelle Handbild-Qualität ohne Bozorth3-Paarvergleiche.

        Die vorherige Variante konnte beim Start/Einstellungen blockieren, weil
        externe Matcher-Aufrufe im GUI-Refresh liefen. Hier wird nur eine schnelle
        XYT-Heuristik pro Template verwendet.
        """
        if not self.cfg.get("show_hand_quality", False):
            return {}

        user = user or self._cur_user()
        if not user:
            return {}

        out: dict[str, int] = {}
        for finger, _label in FINGERS:
            fdir = self._finger_dir(user, finger)
            templates = sorted(fdir.glob("*.xyt")) if fdir.exists() else []
            if not templates:
                continue

            try:
                qvals = [
                    int(minutiae_quality(p, int(self.cfg.get("min_minutiae", 20))).get("score", 0))
                    for p in templates
                ]
                if qvals:
                    out[finger] = int(max(0, min(100, sum(qvals) / len(qvals))))
            except Exception as e:
                self._log_debug(f"Hand-Qualität nicht berechenbar für {user}/{finger}: {e}")
        return out


    def _refresh_enroll_hand(self) -> None:
        selected = self._finger_combo.get_active_id()
        self._hand.update(self._enrolled_set(), selected)
        if self.cfg.get("show_hand_quality", False):
            self._hand.set_quality_map(self._finger_quality_scores())
        else:
            self._hand.set_quality_map({})

    def _on_user_add(self, *_args) -> None:
        dlg = Gtk.Dialog(title=tr(self.cfg, "new_user_title"), transient_for=self, modal=True)
        dlg.set_default_size(320, 120)
        area = dlg.get_content_area()
        area.set_border_width(10)
        area.set_spacing(6)
        area.pack_start(Gtk.Label(label=tr(self.cfg, "username_label")), False, False, 0)

        ent = Gtk.Entry()
        ent.set_text(str(self.cfg.get("username", "")))
        area.pack_start(ent, False, False, 0)

        dlg.add_button(tr(self.cfg, "cancel"), Gtk.ResponseType.CANCEL)
        dlg.add_button(tr(self.cfg, "create"), Gtk.ResponseType.OK)
        dlg.show_all()

        if dlg.run() == Gtk.ResponseType.OK:
            name = ent.get_text().strip()
            if not validate_username(name):
                self._show_error(tr(self.cfg, "invalid_username_msg"))
            else:
                try:
                    udir = self._user_dir(name)
                    ensure_private_dir(udir)
                    self._refresh_user_list()
                    self._populate_ver_users()
                    self._log(tr(self.cfg, "user_created", user=name))
                except Exception as e:
                    self._show_error(tr(self.cfg, "user_create_failed", error=e))
        dlg.destroy()

    def _on_user_del(self, *_args) -> None:
        user = self._cur_user()
        if not user:
            return
        if self.cfg.get("debug_keep_files", False):
            self._log(tr(self.cfg, "debug_user_delete_blocked"))
            self._show_error(tr(self.cfg, "debug_user_delete_disabled"))
            return

        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=tr(self.cfg, "delete_user_confirm", user=user),
        )
        resp = dlg.run()
        dlg.destroy()

        if resp != Gtk.ResponseType.YES:
            return

        try:
            udir = self._user_dir(user)
            if udir.exists():
                shutil.rmtree(udir)
            self._refresh_user_list()
            self._populate_ver_users()
            self._refresh_enrolled_list()
            self._refresh_enroll_hand()
            self._log(tr(self.cfg, "user_deleted", user=user))
        except PermissionError as e:
            self._log_error(f"Benutzer löschen fehlgeschlagen: {e}")
            if self._ask_repair_permissions(udir, e):
                try:
                    shutil.rmtree(udir)
                    self._refresh_user_list()
                    self._populate_ver_users()
                    self._refresh_enrolled_list()
                    self._refresh_enroll_hand()
                    self._log(tr(self.cfg, "user_deleted", user=user))
                except Exception as e2:
                    self._show_error(f"Löschen auch nach Rechte-Reparatur fehlgeschlagen:\n{e2}")
            else:
                self._show_error(tr(self.cfg, "delete_failed", error=e))
        except Exception as e:
            self._show_error(tr(self.cfg, "delete_failed", error=e))

    def _selected_enrolled_finger(self) -> str | None:
        """Nimmt die Auswahl aus der Tabelle, sonst den aktuell gewählten Finger."""
        if hasattr(self, "_enroll_tv"):
            model, it = self._enroll_tv.get_selection().get_selected()
            if it:
                finger = model[it][0]
                if validate_finger(finger):
                    return finger
        finger = self._finger_combo.get_active_id()
        return finger if finger and validate_finger(finger) else None

    def _on_delete_enrolled_finger(self, *_args) -> None:
        user = self._cur_user()
        finger = self._selected_enrolled_finger()
        if not user or not finger:
            self._show_error(tr(self.cfg, "select_user_and_finger"))
            return
        if self.cfg.get("debug_keep_files", False):
            self._log(tr(self.cfg, "debug_finger_delete_blocked"))
            self._show_error(tr(self.cfg, "debug_finger_delete_disabled"))
            return

        fdir = self._finger_dir(user, finger)
        if not fdir.exists() or not any(fdir.glob("*.xyt")):
            self._show_error(tr(self.cfg, "templates_missing_for_finger"))
            return

        label = finger_label(self.cfg, finger)
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=tr(self.cfg, "delete_enrolled_finger_title", user=user, finger=label),
        )
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.YES:
            return

        try:
            shutil.rmtree(fdir)
            self._refresh_enrolled_list()
            self._refresh_enroll_hand()
            self._populate_ver_users()
            self._update_minutiae_info(finger)
            self._log(tr(self.cfg, "enrolled_finger_deleted", user=user, finger=finger))
        except PermissionError as e:
            self._log_error(tr(self.cfg, "finger_delete_failed", error=e))
            if self._ask_repair_permissions(fdir, e):
                try:
                    shutil.rmtree(fdir)
                    self._refresh_enrolled_list()
                    self._refresh_enroll_hand()
                    self._populate_ver_users()
                    self._update_minutiae_info(finger)
                    self._log(tr(self.cfg, "enrolled_finger_deleted", user=user, finger=finger))
                except Exception as e2:
                    self._show_error(f"Finger konnte auch nach Rechte-Reparatur nicht gelöscht werden:\n{e2}")
            else:
                self._show_error(f"Finger konnte nicht gelöscht werden:\n{e}")
        except Exception as e:
            self._show_error(f"Finger konnte nicht gelöscht werden:\n{e}")

    def _on_enroll_click(self, *_args) -> None:
        user = self._cur_user()
        finger = self._finger_combo.get_active_id()

        if not user or not finger:
            return

        if hasattr(self, "_hand"):
            self._hand.set_active_finger(finger, 0)

        dlg = ScanDialog(
            parent=self,
            scanner=self.scanner,
            nbis_dir=str(self.cfg["nbis_dir"]),
            tmp_dir=self.paths["tmp_dir"],
            username=user,
            finger=finger,
            count=int(self.cfg["scan_count"]),
            min_minutiae=int(self.cfg["min_minutiae"]),
            scan_width=int(self.cfg.get("scan_width", 300)),
            scan_height=int(self.cfg.get("scan_height", 400)),
            between_scan_delay=float(self.cfg.get("between_scan_delay", 1.2)),
            demo_mode=bool(self.cfg["demo_mode"]),
            debug_keep_files=bool(self.cfg.get("debug_keep_files", False)),
        )

        resp = dlg.run()
        results = dlg.get_results()
        dlg.stop()
        dlg.destroy()
        if hasattr(self, "_hand"):
            self._hand.set_active_finger(None, 0)

        if resp == Gtk.ResponseType.OK and results:
            try:
                saved = self._save_templates(user, finger, results)
                self._enroll_status.set_text(tr(self.cfg, "enroll_saved", count=saved))
                self._refresh_enrolled_list()
                self._refresh_enroll_hand()
                self._update_minutiae_info(finger)
                self._log(tr(self.cfg, "enroll_ok_log", user=user, finger=finger, count=saved))
            except PermissionError as e:
                self._log_error(f"Speichern fehlgeschlagen: {e}")
                if self._ask_repair_permissions(self._finger_dir(user, finger), e):
                    try:
                        saved = self._save_templates(user, finger, results)
                        self._enroll_status.set_text(tr(self.cfg, "enroll_saved", count=saved))
                        self._refresh_enrolled_list()
                        self._refresh_enroll_hand()
                        self._update_minutiae_info(finger)
                        self._log(f"Enrollment OK nach Rechte-Reparatur: {user}/{finger} ({saved} Templates)")
                    except Exception as e2:
                        self._show_error(f"Speichern auch nach Rechte-Reparatur fehlgeschlagen:\n{e2}")
                else:
                    self._show_error(tr(self.cfg, "save_failed", error=e))
            except Exception as e:
                self._show_error(tr(self.cfg, "save_failed", error=e))
        else:
            dlg.cleanup_unkept_temp()
            self._enroll_status.set_text(tr(self.cfg, "enroll_cancelled"))
            self._log(tr(self.cfg, "enroll_cancelled"))

    def _save_templates(self, user: str, finger: str, scan_files: List[Tuple[Path, Path]]) -> int:
        """Speichert pro Scan sowohl XYT als auch WSQ dauerhaft.

        Debug-Modus:
        - alte Templates werden NICHT gelöscht
        - Temp-Dateien werden nach dem Kopieren NICHT gelöscht
        - neue Dateien bekommen einen Debug-Zeitstempel, damit nichts überschrieben wird
        """
        fdir = self._finger_dir(user, finger)
        ensure_private_dir(fdir)

        debug_keep = bool(self.cfg.get("debug_keep_files", False))

        if not os.access(fdir, os.W_OK):
            raise PermissionError(
                f"Keine Schreibrechte für Template-Ordner: {fdir}. "
                "Vermutlich wurden alte Templates/Ordner als root angelegt."
            )

        if debug_keep:
            stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}_{id(self)}_{int(time.time() * 1000) % 1000:03d}"
            self._log("Debug-Modus: alte Templates werden nicht gelöscht.")
        else:
            stamp = ""
            # Alte Templates dieses Fingers löschen, damit keine alten Scores mitlaufen.
            for pattern in ("*.xyt", "*.wsq"):
                for old in fdir.glob(pattern):
                    try:
                        old.unlink()
                    except PermissionError:
                        raise PermissionError(
                            f"Keine Rechte zum Löschen der alten Template-Datei: {old}. "
                            "Bitte Besitzrechte reparieren."
                        )

        saved = 0
        for i, pair in enumerate(scan_files):
            try:
                xyt_src, wsq_src = pair
            except Exception:
                self._log_error(f"Ungültiges Scan-Ergebnis übersprungen: {pair!r}")
                continue

            xyt_src = Path(xyt_src)
            wsq_src = Path(wsq_src)

            if not xyt_src.exists():
                self._log_error(f"XYT-Quelle fehlt und wurde übersprungen: {xyt_src}")
                continue
            if not wsq_src.exists():
                self._log_error(f"WSQ-Quelle fehlt und wurde übersprungen: {wsq_src}")
                continue

            if debug_keep:
                stem = f"debug_{stamp}_tpl_{i:02d}"
            else:
                stem = f"tpl_{i:02d}"

            xyt_dst = fdir / f"{stem}.xyt"
            wsq_dst = fdir / f"{stem}.wsq"

            try:
                shutil.copy2(xyt_src, xyt_dst)
                xyt_dst.chmod(0o600)

                shutil.copy2(wsq_src, wsq_dst)
                wsq_dst.chmod(0o600)
            except PermissionError:
                raise PermissionError(
                    f"Keine Schreibrechte für Template-Dateien in: {fdir}. "
                    "Bitte Besitzrechte reparieren."
                )

            saved += 1
            self._log(
                f"Gespeichert: {user}/{finger}/{xyt_dst.name} + {wsq_dst.name}"
            )

            if debug_keep:
                self._log(f"Debug-Modus: Temp-Dateien bleiben erhalten: {xyt_src}, {wsq_src}")
            else:
                for tmp in (xyt_src, wsq_src):
                    try:
                        tmp.unlink()
                    except Exception as e:
                        self._log_error(f"Temporäre Datei konnte nicht gelöscht werden: {tmp} ({e})")

        return saved

    # ─────────────────────────────────────────────────────────────
    # Tab 2: Verify
    # ─────────────────────────────────────────────────────────────

    def _build_verify_tab(self) -> None:
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=10)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        self._ver_user_label = Gtk.Label(label=tr(self.cfg, "user") + ":", xalign=1)
        grid.attach(self._ver_user_label, 0, 0, 1, 1)

        self._ver_user_combo = Gtk.ComboBoxText()
        self._ver_user_combo.set_hexpand(True)
        grid.attach(self._ver_user_combo, 1, 0, 1, 1)

        self._ver_finger_label = Gtk.Label(label=tr(self.cfg, "finger") + ":", xalign=1)
        grid.attach(self._ver_finger_label, 0, 1, 1, 1)
        self._ver_finger_combo = Gtk.ComboBoxText()
        for key, _label in FINGERS:
            self._ver_finger_combo.append(key, finger_label(self.cfg, key))
        self._ver_finger_combo.set_active(0)
        grid.attach(self._ver_finger_combo, 1, 1, 1, 1)

        vbox.pack_start(grid, False, False, 0)

        self._btn_verify = Gtk.Button(label=tr(self.cfg, "verify_button"))
        self._btn_verify.connect("clicked", self._on_verify)
        vbox.pack_start(self._btn_verify, False, False, 0)

        self._ver_result = Gtk.Label(label="", xalign=0)
        self._ver_result.set_line_wrap(True)
        vbox.pack_start(self._ver_result, False, False, 0)

        self.nb.append_page(vbox, Gtk.Label(label=tr(self.cfg, "verify_tab")))

    def _populate_ver_users(self) -> None:
        self._ver_user_combo.remove_all()
        db = self.paths["db_dir"]
        if db.exists():
            for d in sorted(db.iterdir()):
                if d.is_dir() and validate_username(d.name):
                    self._ver_user_combo.append_text(d.name)

        model = self._ver_user_combo.get_model()
        if model is not None and len(model) > 0:
            self._ver_user_combo.set_active(0)

    def _template_base_path(self) -> Path:
        """Robust den Template-Basisordner ermitteln.

        make_paths() kann je nach Version interne Keys wie "templates" oder
        "template_dir" verwenden. Die Config heißt aber template_base_dir.
        """
        for key in ("template_base_dir", "templates", "template_dir", "tpl_dir"):
            val = self.paths.get(key) if isinstance(self.paths, dict) else None
            if val:
                return Path(val)
        return Path(str(self.cfg.get("template_base_dir", DEFAULT_CFG["template_base_dir"]))).expanduser()

    def _all_template_entries(self) -> list[tuple[str, str, Path]]:
        entries: list[tuple[str, str, Path]] = []
        base = self._template_base_path()
        if not base.exists():
            self._log_error(f"Template-Basisordner nicht gefunden: {base}")
            return entries
        for udir in sorted([p for p in base.iterdir() if p.is_dir()]):
            user = udir.name
            if not validate_username(user):
                continue
            for fkey, _label in FINGERS:
                fdir = udir / fkey
                if not fdir.exists():
                    continue
                for xyt in sorted(fdir.glob("*.xyt")):
                    entries.append((user, fkey, xyt))
        return entries

    def _finger_internal_scores(self, user: str, finger: str) -> list[int]:
        fdir = self._finger_dir(user, finger)
        templates = sorted(fdir.glob("*.xyt")) if fdir.exists() else []
        scores: list[int] = []
        for i in range(len(templates)):
            for j in range(i + 1, len(templates)):
                scores.append(run_bozorth3(str(self.cfg["nbis_dir"]), templates[i], templates[j]))
        return scores

    def _filter_cache_dir(self) -> Path:
        base = self.paths.get("tmp_dir") if isinstance(self.paths, dict) else None
        root = Path(base) if base else Path(tempfile.gettempdir()) / "fp_manager_filters"
        d = root / f"filter_q{int(self.cfg.get('minutiae_filter_min_quality', 15))}_{os.getpid()}"
        ensure_private_dir(d)
        return d

    def _filtered_xyt_path(self, xyt: Path, min_quality: int) -> Path:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(xyt).strip("/").replace("/", "__"))
        out = self._filter_cache_dir() / safe_name
        try:
            if (not out.exists()) or out.stat().st_mtime < xyt.stat().st_mtime:
                filter_xyt_file(xyt, out, min_quality)
        except Exception as e:
            self._log_debug(f"XYT-Filter fehlgeschlagen für {xyt}: {e}")
            return xyt
        return out

    def _maybe_filtered_gallery(self, gallery: list[Path]) -> list[Path]:
        if not self.cfg.get("minutiae_filter_enabled", False):
            return gallery
        q = int(self.cfg.get("minutiae_filter_min_quality", 15))
        self._log(tr(self.cfg, "verify_filter_used", q=q))
        return [self._filtered_xyt_path(p, q) for p in gallery]

    def _compute_score_dataset(self, use_filter: bool = False, min_quality: Optional[int] = None) -> dict[str, Any]:
        entries = self._all_template_entries()
        genuine_scores: list[int] = []
        impostor_scores: list[int] = []
        rows: list[dict[str, Any]] = []
        nbis = str(self.cfg["nbis_dir"])
        if min_quality is None:
            min_quality = int(self.cfg.get("minutiae_filter_min_quality", 15))

        if use_filter:
            entries = [(u, f, self._filtered_xyt_path(x, int(min_quality))) for (u, f, x) in entries]

        for i in range(len(entries)):
            if getattr(self, "_farfrr_stop", False):
                break
            u1, f1, x1 = entries[i]
            for j in range(i + 1, len(entries)):
                if getattr(self, "_farfrr_stop", False):
                    break
                u2, f2, x2 = entries[j]
                score = run_bozorth3(nbis, x1, x2)
                pair_type = "genuine" if (u1 == u2 and f1 == f2) else "impostor"
                rows.append({
                    "type": pair_type,
                    "score": score,
                    "user_a": u1,
                    "finger_a": f1,
                    "file_a": x1.name,
                    "user_b": u2,
                    "finger_b": f2,
                    "file_b": x2.name,
                })
                if pair_type == "genuine":
                    genuine_scores.append(score)
                else:
                    impostor_scores.append(score)

        return {"genuine": genuine_scores, "impostor": impostor_scores, "rows": rows}

    def _template_outlier_lines(self, dataset: dict[str, Any]) -> list[str]:
        rows = dataset.get("rows", [])
        scores_by_tpl: dict[tuple[str, str, str], list[int]] = {}
        for row in rows:
            if row.get("type") != "genuine":
                continue
            a = (row["user_a"], row["finger_a"], row["file_a"])
            b = (row["user_b"], row["finger_b"], row["file_b"])
            scores_by_tpl.setdefault(a, []).append(int(row["score"]))
            scores_by_tpl.setdefault(b, []).append(int(row["score"]))

        required = int(self.cfg.get("enroll_pair_min", 0))
        lines: list[str] = []
        for (user, finger, file), scores in sorted(scores_by_tpl.items()):
            avg = sum(scores) / len(scores) if scores else 0.0
            if required and avg < required:
                lines.append(tr(self.cfg, "quality_outlier_line", user=user, finger=finger_label(self.cfg, finger), file=file, avg=avg))
        if not lines:
            lines.append(tr(self.cfg, "quality_outlier_none"))
        return lines

    def _format_matching_quality(self, dataset: dict[str, Any], threshold: int) -> str:
        genuine = dataset.get("genuine", [])
        impostor = dataset.get("impostor", [])
        if not genuine and not impostor:
            return tr(self.cfg, "quality_no_scores")

        lines: list[str] = []
        if genuine:
            gs = score_basic_stats(genuine)
            lines.append(tr(self.cfg, "quality_genuine_stats", min=int(gs["min"]), avg=gs["avg"], median=gs["median"], max=int(gs["max"])))
        if impostor:
            is_ = score_basic_stats(impostor)
            lines.append(tr(self.cfg, "quality_impostor_stats", min=int(is_["min"]), avg=is_["avg"], median=is_["median"], max=int(is_["max"])))

        if genuine and impostor:
            lines.append(tr(self.cfg, "quality_overlap", imax=max(impostor), gmin=min(genuine)))
            eer, eer_thr = find_eer_threshold(genuine, impostor)
            lines.append(tr(self.cfg, "quality_reco_eer", eer=eer, thr=eer_thr))
            for target in (5.0, 1.0, 0.1):
                reco = find_threshold_for_far(genuine, impostor, target)
                if reco:
                    thr, far, frr = reco
                    lines.append(tr(self.cfg, "quality_reco_far", target=target, thr=thr, frr=frr))
                else:
                    lines.append(tr(self.cfg, "quality_reco_none", target=target))

        lines.append("")
        lines.append(tr(self.cfg, "quality_outliers"))
        lines.extend(self._template_outlier_lines(dataset))
        return "\n".join(lines)

    def _export_score_csv(self, dataset: dict[str, Any]) -> Path:
        import csv
        ensure_private_dir(self.paths["log_dir"])
        out = self.paths["log_dir"] / f"farfrr_scores_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        with open(out, "w", newline="", encoding="utf-8") as f:
            fieldnames = ["type", "score", "user_a", "finger_a", "file_a", "user_b", "finger_b", "file_b"]
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for row in dataset.get("rows", []):
                w.writerow({k: row.get(k, "") for k in fieldnames})
        try:
            out.chmod(0o600)
        except Exception:
            pass
        return out

    def _template_quality_map(self, user: str, finger: str) -> dict[Path, Optional[float]]:
        fdir = self._finger_dir(user, finger)
        templates = sorted(fdir.glob("*.xyt")) if fdir.exists() else []
        scores: dict[Path, list[int]] = {p: [] for p in templates}
        for i in range(len(templates)):
            for j in range(i + 1, len(templates)):
                score = run_bozorth3(str(self.cfg["nbis_dir"]), templates[i], templates[j])
                scores[templates[i]].append(score)
                scores[templates[j]].append(score)
        return {tpl: (sum(vals) / len(vals) if vals else None) for tpl, vals in scores.items()}

    def _filtered_gallery_for_verify(self, user: str, finger: str, gallery: list[Path]) -> list[Path]:
        if not self.cfg.get("ignore_outliers_verify", False):
            return gallery
        required = int(self.cfg.get("enroll_pair_min", 0))
        if required <= 0 or len(gallery) <= 1:
            return gallery
        qmap = self._template_quality_map(user, finger)
        filtered = [p for p in gallery if qmap.get(p) is None or qmap.get(p, 0.0) >= required]
        ignored = len(gallery) - len(filtered)
        if ignored > 0 and filtered:
            self._log(tr(self.cfg, "verify_outliers_ignored", count=ignored))
            return filtered
        if ignored > 0 and not filtered:
            self._log_error(tr(self.cfg, "verify_outliers_all_filtered"))
            return gallery
        return gallery

    def _template_quality_lines(self, user: str, finger: str) -> list[str]:
        qmap = self._template_quality_map(user, finger)
        if not qmap:
            return []
        required = int(self.cfg.get("enroll_pair_min", 0))
        lines = ["", tr(self.cfg, "template_quality_header")]
        for tpl, avg in sorted(qmap.items(), key=lambda item: item[0].name):
            if avg is None:
                lines.append(tr(self.cfg, "template_quality_single", file=tpl.name))
                continue
            status = tr(self.cfg, "template_quality_ok") if avg >= required else tr(self.cfg, "template_quality_outlier")
            lines.append(tr(self.cfg, "template_quality_line", file=tpl.name, avg=avg, status=status))
        state = tr(self.cfg, "enabled") if self.cfg.get("ignore_outliers_verify", False) else tr(self.cfg, "disabled")
        lines.append(tr(self.cfg, "outlier_verify_note", state=state))
        return lines

    def _on_verify(self, *_args) -> None:
        user = self._ver_user_combo.get_active_text()
        finger = self._ver_finger_combo.get_active_id()

        if not user or not validate_username(user):
            self._ver_result.set_text(tr(self.cfg, "no_user_selected"))
            return
        if not finger or not validate_finger(finger):
            self._ver_result.set_text(tr(self.cfg, "no_finger_selected"))
            return

        try:
            gallery_dir = self._finger_dir(user, finger)
        except ValueError as e:
            self._ver_result.set_text(str(e))
            return

        gallery = sorted(gallery_dir.glob("*.xyt")) if gallery_dir.exists() else []
        if not gallery:
            self._ver_result.set_text(tr(self.cfg, "no_templates_for_finger"))
            return
        gallery = self._filtered_gallery_for_verify(user, finger, gallery)
        gallery = self._maybe_filtered_gallery(gallery)

        dlg = ScanDialog(
            parent=self,
            scanner=self.scanner,
            nbis_dir=str(self.cfg["nbis_dir"]),
            tmp_dir=self.paths["tmp_dir"],
            username=user,
            finger=finger,
            count=1,
            min_minutiae=int(self.cfg["min_minutiae"]),
            scan_width=int(self.cfg.get("scan_width", 300)),
            scan_height=int(self.cfg.get("scan_height", 400)),
            between_scan_delay=float(self.cfg.get("between_scan_delay", 1.2)),
            demo_mode=bool(self.cfg["demo_mode"]),
            debug_keep_files=bool(self.cfg.get("debug_keep_files", False)),
        )

        resp = dlg.run()
        results = dlg.get_results()
        dlg.stop()
        dlg.destroy()

        if resp != Gtk.ResponseType.OK or not results:
            dlg.cleanup_unkept_temp()
            self._ver_result.set_text(tr(self.cfg, "verify_cancelled"))
            return

        probe, probe_wsq = results[0]
        scores = [run_bozorth3(str(self.cfg["nbis_dir"]), probe, g) for g in gallery]
        best = max(scores) if scores else 0
        top2 = top_n_average(scores, 2)
        thr = int(self.cfg["threshold"])
        top2_min = int(self.cfg.get("verify_top2_min", 0))
        match = best >= thr and top2 >= top2_min

        if self.cfg.get("debug_keep_files", False):
            self._log(f"Debug-Modus: Verify-Temp-Dateien bleiben erhalten: {probe}, {probe_wsq}")
        else:
            for tmp in (probe, probe_wsq):
                try:
                    tmp.unlink()
                except Exception:
                    pass

        sym = tr(self.cfg, "verify_match") if match else tr(self.cfg, "verify_no_match")
        self._ver_result.set_text(
            tr(self.cfg, "verify_top2_result",
               result=sym,
               best=best,
               top2=top2,
               threshold=thr,
               top2_min=top2_min)
        )
        self._log(f"Verify {user}/{finger}: score={best} thr={thr} → {'OK' if match else 'FAIL'}")

    # ─────────────────────────────────────────────────────────────
    # Tab 3: FAR/FRR
    # ─────────────────────────────────────────────────────────────

    def _build_farfrr_tab(self) -> None:
        vbox = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            border_width=16,
        )

        info = Gtk.Label(xalign=0)
        info.set_markup(
            "<b>FAR / FRR Tuning</b>\n\n"
            "<b>Threshold ↑</b> (z.B. 60+) → niedrige <b>FAR</b>, "
            "höhere <b>FRR</b>  <i>(sicher)</i>\n"
            "<b>Threshold ↓</b> (z.B. 20)  → höhere <b>FAR</b>, "
            "niedrige <b>FRR</b>  <i>(bequem)</i>\n\n"
            "• <b>FAR</b> = False Accept Rate  (Fremder wird akzeptiert)\n"
            "• <b>FRR</b> = False Reject Rate  (Du wirst abgelehnt)\n"
            "• NBIS bozorth3-Werte: 20=lax · 35=empfohlen · "
            "60=streng · 100=sehr streng"
        )
        vbox.pack_start(info, False, False, 0)

        vbox.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            False, False, 0,
        )

        # ── Match-Threshold Slider ────────────────────────────────
        thr_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        thr_box.pack_start(
            Gtk.Label(label="Match-Threshold:", xalign=1),
            False, False, 0,
        )
        thr_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._farfrr_adj = Gtk.Adjustment(
            value=int(self.cfg["threshold"]),
            lower=1,
            upper=200,
            step_increment=1,
            page_increment=5,
        )
        self._farfrr_scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=self._farfrr_adj,
        )
        self._farfrr_scale.set_hexpand(True)
        self._farfrr_scale.set_digits(0)
        self._farfrr_scale.set_value_pos(Gtk.PositionType.TOP)
        self._farfrr_scale.connect("value-changed", self._on_thr_changed)

        for val, label in [
            (20, "lax"),
            (35, "empfohlen"),
            (60, "streng"),
            (100, "sehr streng"),
        ]:
            self._farfrr_scale.add_mark(val, Gtk.PositionType.BOTTOM, label)

        thr_col.pack_start(self._farfrr_scale, False, False, 0)
        thr_box.pack_start(thr_col, True, True, 0)
        vbox.pack_start(thr_box, False, False, 0)

        # ── Scans / Enrollment ───────────────────────────────────
        sc_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sc_box.pack_start(
            Gtk.Label(label="Scans / Enrollment:", xalign=1),
            False, False, 0,
        )
        self._scan_entry = Gtk.Entry(
            text=str(self.cfg["scan_count"]),
            width_chars=5,
        )
        sc_box.pack_start(self._scan_entry, False, False, 0)
        btn_sm = Gtk.Button(label="−")
        btn_sp = Gtk.Button(label="+")
        btn_sm.connect("clicked", self._spin_scan, -1)
        btn_sp.connect("clicked", self._spin_scan, +1)
        sc_box.pack_start(btn_sm, False, False, 0)
        sc_box.pack_start(btn_sp, False, False, 0)
        vbox.pack_start(sc_box, False, False, 0)

        # ── Min. Minutien ────────────────────────────────────────
        mm_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mm_box.pack_start(
            Gtk.Label(label="Min. Minutien:", xalign=1),
            False, False, 0,
        )
        self._min_min_entry = Gtk.Entry(
            text=str(self.cfg["min_minutiae"]),
            width_chars=5,
        )
        mm_box.pack_start(self._min_min_entry, False, False, 0)
        btn_mm = Gtk.Button(label="−")
        btn_mp = Gtk.Button(label="+")
        btn_mm.connect("clicked", self._spin_min_min, -1)
        btn_mp.connect("clicked", self._spin_min_min, +1)
        mm_box.pack_start(btn_mm, False, False, 0)
        mm_box.pack_start(btn_mp, False, False, 0)
        vbox.pack_start(mm_box, False, False, 0)

        vbox.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            False, False, 0,
        )

        # ── Speichern / Defaults ─────────────────────────────────
        btn_row = Gtk.Box(spacing=8)
        self._btn_farfrr_save = Gtk.Button(label=tr(self.cfg, "farfrr_save"))
        self._btn_farfrr_defaults = Gtk.Button(label=tr(self.cfg, "farfrr_defaults"))
        self._btn_farfrr_save.connect("clicked", self._on_farfrr_save)
        self._btn_farfrr_defaults.connect("clicked", self._on_farfrr_defaults)
        btn_row.pack_start(self._btn_farfrr_save, False, False, 0)
        btn_row.pack_start(self._btn_farfrr_defaults, False, False, 0)
        vbox.pack_start(btn_row, False, False, 0)

        self._farfrr_status = Gtk.Label(label="", xalign=0)
        vbox.pack_start(self._farfrr_status, False, False, 0)

        vbox.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
            False, False, 0,
        )

        # ── FAR/FRR Berechnen ────────────────────────────────────
        calc_lbl = Gtk.Label(
            xalign=0,
            label=tr(self.cfg, "farfrr_calculate_templates") + ":",
        )
        vbox.pack_start(calc_lbl, False, False, 0)

        eer_row = Gtk.Box(spacing=8)
        self._chk_farfrr_full_eer = Gtk.CheckButton(
            label=tr(self.cfg, "farfrr_full_eer")
        )
        self._eer_chk = self._chk_farfrr_full_eer
        eer_row.pack_start(self._chk_farfrr_full_eer, False, False, 0)
        vbox.pack_start(eer_row, False, False, 0)

        self._btn_farfrr_calc = Gtk.Button(label=tr(self.cfg, "farfrr_calculate_templates"))
        self._btn_farfrr_calc.connect("clicked", self._on_farfrr_calc)
        vbox.pack_start(self._btn_farfrr_calc, False, False, 0)

        self._farfrr_prog = Gtk.ProgressBar()
        self._farfrr_result = Gtk.Label(label="", xalign=0)
        self._farfrr_result.set_selectable(True)
        self._farfrr_result.set_line_wrap(True)
        vbox.pack_start(self._farfrr_prog, False, False, 0)
        vbox.pack_start(self._farfrr_result, False, False, 0)

        self.nb.append_page(vbox, Gtk.Label(label="⚙️ FAR/FRR"))

    def _on_thr_changed(self, scale) -> None:
        self.cfg["threshold"] = int(scale.get_value())

    def _spin_scan(self, _btn, delta: int) -> None:
        try:
            value = int(self._scan_entry.get_text()) + int(delta)
            value = max(INT_RANGES["scan_count"][0], min(INT_RANGES["scan_count"][1], value))
            self._scan_entry.set_text(str(value))
        except ValueError:
            self._scan_entry.set_text(str(DEFAULT_CFG["scan_count"]))

    def _spin_min_min(self, _btn, delta: int) -> None:
        try:
            value = int(self._min_min_entry.get_text()) + int(delta)
            value = max(INT_RANGES["min_minutiae"][0], min(INT_RANGES["min_minutiae"][1], value))
            self._min_min_entry.set_text(str(value))
        except ValueError:
            self._min_min_entry.set_text(str(DEFAULT_CFG["min_minutiae"]))

    def _on_farfrr_save(self, *_args) -> None:
        try:
            threshold = int(self._farfrr_adj.get_value())
            scan_count = int(self._scan_entry.get_text())
            min_minutiae = int(self._min_min_entry.get_text())

            scan_lo, scan_hi = INT_RANGES["scan_count"]
            min_lo, min_hi = INT_RANGES["min_minutiae"]
            if not scan_lo <= scan_count <= scan_hi:
                raise ValueError(f"Scan-Anzahl muss zwischen {scan_lo} und {scan_hi} liegen.")
            if not min_lo <= min_minutiae <= min_hi:
                raise ValueError(f"Min. Minutien muss zwischen {min_lo} und {min_hi} liegen.")

            self.cfg["threshold"] = threshold
            self.cfg["scan_count"] = scan_count
            self.cfg["min_minutiae"] = min_minutiae
            save_cfg(self.cfg)

            self._farfrr_status.set_markup(
                '<span foreground="green">💾 Gespeichert.</span>'
            )
            self._log(
                f"FAR/FRR-Einstellungen gespeichert: "
                f"threshold={self.cfg['threshold']} "
                f"scan_count={self.cfg['scan_count']} "
                f"min_minutiae={self.cfg['min_minutiae']}"
            )
        except ValueError as e:
            self._farfrr_status.set_markup(
                f'<span foreground="red">Ungültige Eingabe: {e}</span>'
            )
            self._log(f"FAR/FRR-Eingabe ungültig: {e}")

    def _on_farfrr_defaults(self, *_args) -> None:
        self._farfrr_adj.set_value(DEFAULT_CFG["threshold"])
        self._scan_entry.set_text(str(DEFAULT_CFG["scan_count"]))
        self._min_min_entry.set_text(str(DEFAULT_CFG["min_minutiae"]))
        self._farfrr_status.set_markup(
            '<span foreground="blue">↺ Defaults geladen — noch nicht gespeichert.</span>'
        )

    def _on_farfrr_calc(self, *_args) -> None:
        threshold = int(self._farfrr_adj.get_value())
        full_eer = bool(self._eer_chk.get_active())
        self._farfrr_result.set_text("Berechne …")
        self._farfrr_prog.set_fraction(0.0)
        threading.Thread(
            target=self._farfrr_thread,
            args=(threshold, full_eer),
            daemon=True,
        ).start()

    def _on_export_scores(self, *_args) -> None:
        dataset = getattr(self, "_last_score_dataset", None)
        if not dataset:
            dataset = self._compute_score_dataset()
            self._last_score_dataset = dataset
        try:
            path = self._export_score_csv(dataset)
            self._log(tr(self.cfg, "quality_csv_saved", path=path))
        except Exception as e:
            self._show_error(tr(self.cfg, "quality_csv_failed", error=e))

    def _farfrr_thread(self, threshold: int, full_eer: bool = True) -> None:
        try:
            self._farfrr_stop = False
            GLib.idle_add(self._farfrr_result.set_text, tr(self.cfg, "farfrr_running"))

            dataset = self._compute_score_dataset()
            self._last_score_dataset = dataset

            if getattr(self, "_farfrr_stop", False):
                GLib.idle_add(self._farfrr_result.set_text, tr(self.cfg, "farfrr_cancelled"))
                return

            genuine_scores = dataset.get("genuine", [])
            impostor_scores = dataset.get("impostor", [])
            if not genuine_scores and not impostor_scores:
                GLib.idle_add(self._farfrr_result.set_text, tr(self.cfg, "farfrr_too_few"))
                return

            far, frr = far_frr_at_threshold(genuine_scores, impostor_scores, threshold)
            eer, eer_thr = find_eer_threshold(genuine_scores, impostor_scores)

            lines = [
                f"Threshold    : {threshold}",
                f"Genuine      : {len(genuine_scores)} Paare",
                f"Impostor     : {len(impostor_scores)} Paare",
                f"FAR          : {far:.2f}%",
                f"FRR          : {frr:.2f}%",
                f"EER          : {eer:.2f}%  (bei Threshold {eer_thr})",
                "",
                tr(self.cfg, "quality_analysis"),
                "────────────────────────────────",
                self._format_matching_quality(dataset, threshold),
            ]
            GLib.idle_add(self._farfrr_result.set_text, "\n".join(lines))
            if hasattr(self, "_stats_chart"):
                GLib.idle_add(self._update_stats_tab, dataset, threshold)
        except Exception as e:
            GLib.idle_add(self._farfrr_result.set_text, f"ERROR: {e}")


    def _build_stats_tab(self) -> None:
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, border_width=8)
        title = Gtk.Label(label=tr(self.cfg, "stats_title"), xalign=0)
        title.set_markup(f"<b>{tr(self.cfg, 'stats_title')}</b>")
        vbox.pack_start(title, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._btn_stats_calc = Gtk.Button(label=tr(self.cfg, "stats_calculate"))
        self._btn_stats_calc.connect("clicked", self._on_stats_calc)
        row.pack_start(self._btn_stats_calc, False, False, 0)

        self._btn_stats_last = Gtk.Button(label=tr(self.cfg, "stats_use_last"))
        self._btn_stats_last.connect("clicked", self._on_stats_use_last)
        row.pack_start(self._btn_stats_last, False, False, 0)

        self._stats_status = Gtk.Label(label=tr(self.cfg, "stats_waiting"), xalign=0)
        row.pack_start(self._stats_status, True, True, 0)
        vbox.pack_start(row, False, False, 0)

        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        vbox.pack_start(paned, True, True, 0)

        self._stats_chart = ScoreCurvesWidget(self.cfg)
        sw_chart = Gtk.ScrolledWindow()
        sw_chart.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw_chart.add(self._stats_chart)
        paned.add1(sw_chart)

        self._stats_buf = Gtk.TextBuffer()
        self._stats_view = Gtk.TextView(buffer=self._stats_buf)
        self._stats_view.set_editable(False)
        self._stats_view.set_monospace(True)
        self._stats_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        sw_text = Gtk.ScrolledWindow()
        sw_text.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw_text.set_size_request(-1, 190)
        sw_text.add(self._stats_view)
        paned.add2(sw_text)

        self.nb.append_page(vbox, Gtk.Label(label=tr(self.cfg, "stats_tab")))

    def _on_stats_use_last(self, *_args) -> None:
        dataset = getattr(self, "_last_score_dataset", None)
        if not dataset:
            self._stats_status.set_text(tr(self.cfg, "stats_no_data"))
            return
        self._update_stats_tab(dataset, int(self.cfg.get("threshold", 35)))

    def _on_stats_calc(self, *_args) -> None:
        self._stats_status.set_text(tr(self.cfg, "stats_running"))
        threading.Thread(target=self._stats_thread, daemon=True).start()

    def _stats_thread(self) -> None:
        try:
            dataset = self._compute_score_dataset()
            self._last_score_dataset = dataset
            threshold = int(self.cfg.get("threshold", 35))
            GLib.idle_add(self._update_stats_tab, dataset, threshold)
        except Exception as e:
            GLib.idle_add(self._stats_status.set_text, f"ERROR: {e}")

    def _format_pair_row(self, row: dict[str, Any]) -> str:
        return tr(
            self.cfg,
            "pair_line",
            score=int(row.get("score", 0)),
            a_user=row.get("user_a", ""),
            a_finger=row.get("finger_a", ""),
            a_file=row.get("file_a", ""),
            b_user=row.get("user_b", ""),
            b_finger=row.get("finger_b", ""),
            b_file=row.get("file_b", ""),
        )

    def _filter_compare_text(self, threshold: int) -> list[str]:
        q = int(self.cfg.get("minutiae_filter_min_quality", 15))
        try:
            original = getattr(self, "_last_score_dataset", None) or self._compute_score_dataset(use_filter=False)
            filtered = self._compute_score_dataset(use_filter=True, min_quality=q)
        except Exception as e:
            return [f"{tr(self.cfg, 'filter_result_header')}: ERROR {e}"]

        def one_line(name: str, ds: dict[str, Any]) -> str:
            g = ds.get("genuine", [])
            i = ds.get("impostor", [])
            if not g and not i:
                return tr(self.cfg, "filter_no_data")
            far, frr = far_frr_at_threshold(g, i, threshold)
            data = biometric_curve_data(g, i, max_threshold=threshold)
            return tr(self.cfg, "filter_metric_line", name=name, far=far, frr=frr, eer=data["eer"], thr=data["eer_thr"], auc=data["auc"])

        return [
            "",
            tr(self.cfg, "filter_result_header"),
            "────────────────────────────────",
            one_line(tr(self.cfg, "filter_original"), original),
            one_line(tr(self.cfg, "filter_filtered", q=q), filtered),
        ]

    def _finger_diagnosis_text(self, dataset: dict[str, Any]) -> list[str]:
        lines: list[str] = [
            "",
            tr(self.cfg, "finger_diag_header"),
            "════════════════════════════════",
            tr(self.cfg, "finger_diag_summary"),
            "────────────────────────────────",
        ]

        problem_lines: list[str] = []
        for fkey, _label in FINGERS:
            rows = rows_for_finger(dataset, fkey)
            genuine, impostor = scores_from_rows(rows)
            if not genuine and not impostor:
                continue
            diag = diagnose_scores(genuine, impostor)
            status_id = classify_finger_diagnosis(diag)
            status = tr(self.cfg, {"ok": "status_ok", "watch": "status_watch", "bad": "status_bad"}[status_id])
            flabel = finger_label(self.cfg, fkey)

            lines.append(tr(
                self.cfg,
                "finger_diag_line",
                finger=flabel,
                genuine=diag["genuine_count"],
                impostor=diag["impostor_count"],
                eer=diag["eer"],
                auc=diag["auc"],
                gmed=diag["genuine_median"],
                gmin=diag["genuine_min"],
                imax=diag["impostor_max"],
                status=status,
            ))

            if status_id == "bad":
                problem_lines.append(tr(self.cfg, "recommend_reenroll", finger=flabel))
            elif status_id == "watch":
                problem_lines.append(tr(self.cfg, "recommend_filter_test", finger=flabel))
            if diag["impostor_max"] >= max(80, int(diag["genuine_median"])):
                problem_lines.append(tr(self.cfg, "recommend_high_impostor", finger=flabel))

        lines.extend(["", tr(self.cfg, "finger_diag_hand_summary"), "────────────────────────────────"])
        for hand, label_de, label_en in (("left", "Links", "Left"), ("right", "Rechts", "Right")):
            rows = rows_for_hand(dataset, hand)
            genuine, impostor = scores_from_rows(rows)
            if not genuine and not impostor:
                continue
            diag = diagnose_scores(genuine, impostor)
            hand_label = label_en if self.cfg.get("language") == "en" else label_de
            lines.append(tr(
                self.cfg,
                "hand_diag_line",
                hand=hand_label,
                genuine=diag["genuine_count"],
                impostor=diag["impostor_count"],
                eer=diag["eer"],
                auc=diag["auc"],
                gmed=diag["genuine_median"],
                imax=diag["impostor_max"],
            ))

        lines.extend(["", tr(self.cfg, "finger_diag_problem"), "────────────────────────────────"])
        if problem_lines:
            dedup: list[str] = []
            for p in problem_lines:
                if p not in dedup:
                    dedup.append(p)
            lines.extend(dedup)
        else:
            lines.append(tr(self.cfg, "recommend_none"))
        return lines

    def _stats_text_for_dataset(self, dataset: dict[str, Any], threshold: int) -> str:
        genuine = dataset.get("genuine", [])
        impostor = dataset.get("impostor", [])
        rows = dataset.get("rows", [])
        if not genuine and not impostor:
            return tr(self.cfg, "stats_no_data")

        data = biometric_curve_data(genuine, impostor, max_threshold=threshold)
        far, frr = far_frr_at_threshold(genuine, impostor, threshold)
        lines = [
            f"Threshold    : {threshold}",
            f"Genuine      : {len(genuine)} Paare",
            f"Impostor     : {len(impostor)} Paare",
            f"FAR          : {far:.2f}%",
            f"FRR          : {frr:.2f}%",
            f"EER          : {data['eer']:.2f}%  (Threshold {data['eer_thr']})",
            f"AUC          : {data['auc']:.4f}",
            "",
            self._format_matching_quality(dataset, threshold),
            "",
        ]

        genuine_rows = sorted([r for r in rows if r.get("type") == "genuine"], key=lambda r: int(r.get("score", 0)))[:10]
        impostor_rows = sorted([r for r in rows if r.get("type") == "impostor"], key=lambda r: int(r.get("score", 0)), reverse=True)[:10]

        lines.extend(self._finger_diagnosis_text(dataset))
        lines.extend(self._filter_compare_text(threshold))

        lines.append("")
        lines.append(tr(self.cfg, "weak_genuine_pairs"))
        lines.append("────────────────────────────────")
        lines.extend([self._format_pair_row(r) for r in genuine_rows] or ["-"])
        lines.append("")
        lines.append(tr(self.cfg, "strong_impostor_pairs"))
        lines.append("────────────────────────────────")
        lines.extend([self._format_pair_row(r) for r in impostor_rows] or ["-"])
        return "\n".join(lines)

    def _update_stats_tab(self, dataset: dict[str, Any], threshold: int) -> bool:
        if hasattr(self, "_stats_chart"):
            self._stats_chart.cfg = self.cfg
            self._stats_chart.set_dataset(dataset, threshold)
        if hasattr(self, "_stats_buf"):
            self._stats_buf.set_text(self._stats_text_for_dataset(dataset, threshold))
        if hasattr(self, "_stats_status"):
            self._stats_status.set_text(tr(self.cfg, "stats_ready"))
        return False

    def _build_log_tab(self) -> None:
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, border_width=8)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self._log_buf = Gtk.TextBuffer()
        self._log_view = Gtk.TextView(buffer=self._log_buf)
        self._log_view.set_editable(False)
        self._log_view.set_monospace(True)
        self._log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)

        sw.add(self._log_view)
        vbox.pack_start(sw, True, True, 0)

        btn_clear = Gtk.Button(label=tr(self.cfg, "clear_log"))
        btn_clear.connect("clicked", lambda *_: (self._log_buf.set_text(""), self._log(tr(self.cfg, "log_cleared"))))
        vbox.pack_start(btn_clear, False, False, 0)

        self.nb.append_page(vbox, Gtk.Label(label=tr(self.cfg, "log_tab")))

    def _log(self, msg: str, level: int = logging.INFO) -> None:
        ts = time.strftime("%H:%M:%S")
        gui_text = f"[{ts}] {msg}\n"

        if hasattr(self, "logger"):
            try:
                self.logger.log(level, msg)
            except Exception:
                pass

        def _do():
            if hasattr(self, "_log_buf"):
                end = self._log_buf.get_end_iter()
                self._log_buf.insert(end, gui_text)
                self._log_view.scroll_to_iter(self._log_buf.get_end_iter(), 0, False, 0, 0)
            if hasattr(self, "_status_label"):
                self._status_label.set_text(msg)

        GLib.idle_add(_do)

    def _log_debug(self, msg: str) -> None:
        self._log("DEBUG: " + msg, logging.DEBUG)

    def _log_error(self, msg: str) -> None:
        self._log("ERROR: " + msg, logging.ERROR)

    def _ask_repair_permissions(self, failed_path: Path, error: Exception) -> bool:
        """
        Bei PermissionError kann diese App die Besitzrechte optional mit pkexec reparieren.
        Das ist nötig, wenn alte Versionen Templates als root angelegt haben.
        """
        msg = (
            "Es gibt ein Rechteproblem beim Zugriff auf Fingerprint-Dateien.\n\n"
            f"Pfad: {failed_path}\n"
            f"Fehler: {error}\n\n"
            "Soll ich versuchen, die Besitzrechte unter deinem Fingerprint-Ordner "
            "per pkexec zu reparieren?"
        )
        self._log_error(msg.replace("\n", " | "))
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=msg,
        )
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.YES:
            return False
        return self._repair_permissions()

    def _repair_permissions(self) -> bool:
        target = self.paths["fp_base"]
        uid = os.getuid()
        gid = os.getgid()
        helper = shutil.which("pkexec") or shutil.which("sudo")
        if not helper:
            self._show_error(tr(self.cfg, "pkexec_missing"))
            return False
        cmd = [helper, "chown", "-R", f"{uid}:{gid}", str(target)]
        self._log(f"Rechte-Reparatur gestartet: {' '.join(cmd)}")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                err = (r.stderr or r.stdout or f"Returncode {r.returncode}").strip()
                self._log_error(f"Rechte-Reparatur fehlgeschlagen: {err}")
                self._show_error(f"Rechte-Reparatur fehlgeschlagen:\n{err}")
                return False
        except Exception as e:
            self._log_error(f"Rechte-Reparatur konnte nicht gestartet werden: {e}")
            self._show_error(f"Rechte-Reparatur konnte nicht gestartet werden:\n{e}")
            return False

        # Danach möglichst restriktive Rechte setzen. Fehler werden nur geloggt.
        try:
            for root, dirs, files in os.walk(target):
                for d in dirs:
                    try:
                        Path(root, d).chmod(0o700)
                    except Exception as e:
                        self._log_error(f"chmod Ordner fehlgeschlagen: {Path(root, d)} ({e})")
                for f in files:
                    p = Path(root, f)
                    try:
                        if p.suffix.lower() in (".xyt", ".wsq", ".log", ".json"):
                            p.chmod(0o600)
                    except Exception as e:
                        self._log_error(f"chmod Datei fehlgeschlagen: {p} ({e})")
        except Exception as e:
            self._log_error(f"Rechte-Nachbearbeitung fehlgeschlagen: {e}")

        self._log("Rechte-Reparatur abgeschlossen.")
        return True

    def _show_error(self, msg: str) -> None:
        try:
            self._log_error(msg.replace("\n", " | "))
        except Exception:
            pass
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=msg,
        )
        dlg.run()
        dlg.destroy()


# ─────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────

def main() -> int:
    win = FPManager()
    win.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
