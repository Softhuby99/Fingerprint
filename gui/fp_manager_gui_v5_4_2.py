#!/usr/bin/env python3
"""
Fingerprint Manager — GTK3 GUI v5.4.2

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
APP_VERSION = "5.4.2"
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
}

INT_RANGES: dict[str, tuple[int, int]] = {
    "threshold":    (1, 200),
    "scan_count":   (1, 20),
    "min_minutiae": (1, 80),
    "scan_width":    (50, 2000),
    "scan_height":   (50, 2000),
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
        if tmp_path.exists() and tmp_path != CFG_FILE:
            try:
                tmp_path.unlink()
            except Exception:
                pass



def setup_logging(cfg: dict[str, Any]) -> logging.Logger:
    """Zentrales Logging mit FileHandler und Fallback auf stderr."""
    logger = logging.getLogger("fp_manager")
    logger.handlers.clear()
    logger.setLevel(getattr(logging, str(cfg.get("log_level", "DEBUG")).upper(), logging.DEBUG))
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    try:
        paths = make_paths(cfg)
        ensure_private_dir(paths["log_dir"])
        log_file = paths["log_dir"] / "fp_manager.log"
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(formatter)
        handler.setLevel(logger.level)
        logger.addHandler(handler)
        try:
            log_file.chmod(0o600)
        except Exception:
            pass
    except Exception:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        handler.setLevel(logger.level)
        logger.addHandler(handler)
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

DEFAULT_LANGUAGES = {'de': {'settings': '⚙ Einstellungen', 'cancel': 'Abbrechen', 'save': 'Speichern', 'user': 'Benutzer', 'name': 'Name', 'new_user': '➕ Neu', 'delete': '🗑 Löschen', 'finger': 'Finger', 'enrolled_fingers': 'Enrollte Finger', 'key': 'Schlüssel', 'display': 'Anzeige', 'templates': 'Templates', 'enroll_finger': '👆 Finger enrollen', 'delete_finger': '🗑 Finger löschen', 'enrollment_tab': '📋 Enrollment', 'verify_tab': '🔍 Verifikation', 'farfrr_tab': '📊 FAR/FRR', 'log_tab': '📝 Log', 'clear_log': '🗑 Log leeren', 'minute_info_title': 'Minutien-Infos zum ausgewählten Finger', 'minute_per_scan': 'Minutien pro Scan:', 'debug_badge': ' DEBUG: kein Löschen ', 'language': 'Sprache / Language', 'fp_reader': 'FP-Leser / FP reader', 'reader_rescan': 'FP-Leser neu suchen / rescan readers', 'path_base': 'Basis-Verzeichnis', 'path_templates': 'Template-Verzeichnis', 'path_bin': 'Bin-Verzeichnis', 'path_log': 'Log-Verzeichnis', 'path_lib': 'Scanner-Bibliothek (.so)', 'path_sensor': 'Sensor-Modul sensor.py', 'path_nbis': 'NBIS-Verzeichnis', 'default_username': 'Default-Benutzername', 'threshold': 'Bozorth3-Schwellwert', 'scan_count': 'Scan-Anzahl', 'min_minutiae': 'Min. Minutien', 'scan_width': 'Scan-Breite', 'scan_height': 'Scan-Höhe', 'between_scan_delay': 'Pause zwischen Scans (Sek.)', 'finger_mean_delta': 'Finger-Erkennung: Mean-Delta', 'finger_present_std': 'Finger-Erkennung: Std vorhanden', 'finger_off_std': 'Finger weg: Std-Grenze', 'log_level': 'Log-Level', 'demo_mode': 'Demo-Modus ohne echten Scanner', 'debug_keep_files': 'Debug-Modus: keine Dateien löschen', 'debug_keep_files_tip': 'Wenn aktiv, bleiben temporäre RAW/WSQ/XYT-Dateien erhalten. Alte Templates werden beim Enrollment nicht gelöscht und Benutzer-/Finger-Löschen wird blockiert.', 'choose_file': 'Datei wählen', 'choose_dir': 'Verzeichnis wählen', 'settings_saved': 'Einstellungen gespeichert.', 'scanner_initialized': 'Scanner initialisiert.', 'scanner_closed': 'Scanner geschlossen.', 'demo_active': 'Demo-Modus aktiv — Scanner wird nicht verwendet.', 'scanner_missing': 'sensor.py/Scanner nicht verfügbar. Demo-Modus bleibt aus; Scans schlagen fehl.', 'scanner_init_failed': 'Scanner-Initialisierung fehlgeschlagen.'}, 'en': {'settings': '⚙ Settings', 'cancel': 'Cancel', 'save': 'Save', 'user': 'User', 'name': 'Name', 'new_user': '➕ New', 'delete': '🗑 Delete', 'finger': 'Finger', 'enrolled_fingers': 'Enrolled fingers', 'key': 'Key', 'display': 'Display', 'templates': 'Templates', 'enroll_finger': '👆 Enroll finger', 'delete_finger': '🗑 Delete finger', 'enrollment_tab': '📋 Enrollment', 'verify_tab': '🔍 Verification', 'farfrr_tab': '📊 FAR/FRR', 'log_tab': '📝 Log', 'clear_log': '🗑 Clear log', 'minute_info_title': 'Minutiae information for selected finger', 'minute_per_scan': 'Minutiae per scan:', 'debug_badge': ' DEBUG: no deletion ', 'language': 'Language / Sprache', 'fp_reader': 'FP reader', 'reader_rescan': 'Rescan FP readers', 'path_base': 'Base directory', 'path_templates': 'Template directory', 'path_bin': 'Binary directory', 'path_log': 'Log directory', 'path_lib': 'Scanner library (.so)', 'path_sensor': 'Sensor module sensor.py', 'path_nbis': 'NBIS directory', 'default_username': 'Default username', 'threshold': 'Bozorth3 threshold', 'scan_count': 'Scan count', 'min_minutiae': 'Min. minutiae', 'scan_width': 'Scan width', 'scan_height': 'Scan height', 'between_scan_delay': 'Pause between scans (sec.)', 'finger_mean_delta': 'Finger detection: mean delta', 'finger_present_std': 'Finger detection: present std', 'finger_off_std': 'Finger removed: std threshold', 'log_level': 'Log level', 'demo_mode': 'Demo mode without real scanner', 'debug_keep_files': 'Debug mode: do not delete files', 'debug_keep_files_tip': 'When enabled, temporary RAW/WSQ/XYT files are kept. Old templates are not deleted during enrollment and user/finger deletion is blocked.', 'choose_file': 'Choose file', 'choose_dir': 'Choose directory', 'settings_saved': 'Settings saved.', 'scanner_initialized': 'Scanner initialized.', 'scanner_closed': 'Scanner closed.', 'demo_active': 'Demo mode active — scanner is not used.', 'scanner_missing': 'sensor.py/scanner unavailable. Demo mode remains off; scans will fail.', 'scanner_init_failed': 'Scanner initialization failed.'}}


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
        try:
            import importlib.util
            sensor_path = Path(self.sensor_py).expanduser()
            if not sensor_path.exists():
                self.last_error = f"sensor.py nicht gefunden: {sensor_path}"
                return

            # Damit sensor.py ggf. lokale Hilfsdateien/libs relativ zu sich findet.
            import sys
            sensor_dir = str(sensor_path.parent)
            if sensor_dir not in sys.path:
                sys.path.insert(0, sensor_dir)

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


def minutiae_short_summary(xyt_path: Path) -> str:
    s = minutiae_stats(xyt_path)
    parts = [f"{s['count']} Minutien"]
    if "x_min" in s and "y_min" in s:
        parts.append(f"X {s['x_min']}–{s['x_max']}")
        parts.append(f"Y {s['y_min']}–{s['y_max']}")
    if "theta_min" in s:
        parts.append(f"Theta {s['theta_min']}–{s['theta_max']}")
    return " | ".join(parts)


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
        cr.show_text("Rechte Hand")
        cr.move_to(hw * 1.38, h - 6)
        cr.show_text("Linke Hand")

        # Highlights
        for key, (cx, cy, rx, ry) in self._zones(w, h).items():
            if key == self.selected:
                fill = (0.15, 0.35, 1.00, 0.50)
                stroke = (0.00, 0.25, 0.90, 0.90)
            elif key in self.enrolled:
                fill = (0.10, 0.75, 0.10, 0.45)
                stroke = (0.00, 0.55, 0.00, 0.90)
            elif key == self.hover:
                fill = (1.00, 0.72, 0.18, 0.38)
                stroke = (0.90, 0.45, 0.00, 0.90)
            else:
                continue

            cr.save()
            cr.translate(cx, cy)
            cr.scale(rx, ry)
            cr.arc(0, 0, 1, 0, 2 * math.pi)
            cr.restore()
            cr.set_source_rgba(*fill)
            cr.fill_preserve()
            cr.set_source_rgba(*stroke)
            cr.set_line_width(2.0)
            cr.stroke()

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
            (0.10, 0.75, 0.10, "Enrollt"),
            (0.15, 0.35, 1.00, "Ausgewählt"),
            (1.00, 0.72, 0.18, "Maus"),
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
            title=f"Finger scannen — {username}/{finger}",
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

        self.set_default_size(420, 240)
        self.connect("response", self._on_response)
        self.connect("delete-event", self._on_delete_event)
        self.connect("destroy", self._on_destroy)

        area = self.get_content_area()
        area.set_spacing(8)
        area.set_border_width(12)

        self._lbl_status = Gtk.Label(label="Bitte Finger auflegen …", xalign=0)
        self._lbl_status.set_line_wrap(True)
        area.pack_start(self._lbl_status, False, False, 0)

        self._progress = Gtk.ProgressBar()
        self._progress.set_show_text(True)
        area.pack_start(self._progress, False, False, 0)

        self._lbl_detail = Gtk.Label(label="", xalign=0)
        self._lbl_detail.set_line_wrap(True)
        area.pack_start(self._lbl_detail, False, False, 0)

        info_lbl = Gtk.Label(label="Minutien pro Scan:", xalign=0)
        info_lbl.set_markup("<b>Minutien pro Scan:</b>")
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

        self.add_button("Abbrechen", Gtk.ResponseType.CANCEL)
        self._ok_btn = self.add_button("OK", Gtk.ResponseType.OK)
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
                self._set_status(f"Scan {i + 1} von {self.count} — Finger auflegen …")
                self._set_progress(i / self.count, f"{i}/{self.count}")

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
                        self._set_detail(f"Scanner-Fehler — erneut versuchen. ({err})")
                        time.sleep(0.8)
                        continue

                    w, h, _size = self.scanner.image_size
                    if not w or not h:
                        w, h = self.scan_width, self.scan_height

                    ok_w, wsq, msg = raw_to_wsq(self.nbis_dir, img, base, w, h, keep_raw=self.debug_keep_files)
                    if not ok_w:
                        self._set_detail(f"WSQ-Erzeugung fehlgeschlagen: {msg}")
                        time.sleep(0.8)
                        continue

                    ok_m, msg = run_mindtct(self.nbis_dir, wsq, base)
                    if not ok_m:
                        self._set_detail(f"Minutien-Extraktion fehlgeschlagen: {msg}")
                        time.sleep(0.8)
                        continue

                minutiae = count_minutiae(xyt)
                current_scan_no = i + 1
                if minutiae < self.min_minutiae:
                    self._append_scan_info(
                        f"Scan {current_scan_no}: ABGELEHNT — {minutiae_short_summary(xyt)}\n"
                        f"  Mindestwert: {self.min_minutiae} Minutien"
                    )
                    self._set_detail(
                        f"Scan {i + 1}: Qualität zu niedrig "
                        f"({minutiae} Minutien, min. {self.min_minutiae})."
                    )
                    if self.debug_keep_files:
                        self._set_detail(f"Debug-Modus: schlechte Scan-Dateien bleiben erhalten: {wsq}, {xyt}")
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
                self._set_progress(i / self.count, f"{i}/{self.count}")
                self._append_scan_info(
                    f"Scan {i}: AKZEPTIERT — {minutiae_short_summary(xyt)}"
                )
                self._set_detail(f"Scan {i} akzeptiert ({minutiae} Minutien).")

                if i < self.count and self.between_scan_delay > 0 and self._running:
                    self._set_status(
                        f"Finger wegnehmen — nächste Aufnahme in {self.between_scan_delay:.1f}s …"
                    )
                    if not self.demo_mode:
                        self.scanner.wait_finger_off(timeout=self.between_scan_delay)
                    end_time = time.time() + self.between_scan_delay
                    while self._running and time.time() < end_time:
                        time.sleep(0.05)
                else:
                    time.sleep(0.2)

            if self._running and len(self._results) == self.count:
                self._set_status("Alle Scans erfolgreich.")
                self._set_progress(1.0, f"{self.count}/{self.count}")
                self._idle(self._ok_btn.set_sensitive, True)
            elif not self._running:
                self._set_status("Scan abgebrochen.")
            else:
                self._set_status("Scan nicht vollständig.")
        finally:
            self._finished.set()


# ─────────────────────────────────────────────────────────────────
# Settings Dialog
# ─────────────────────────────────────────────────────────────────

class SettingsDialog(Gtk.Dialog):
    def __init__(self, parent, cfg: dict[str, Any]):
        super().__init__(
            title="Einstellungen",
            transient_for=parent,
            modal=True,
            destroy_with_parent=True,
        )
        self.set_default_size(560, 470)
        self.cfg = coerce_cfg(cfg)

        area = self.get_content_area()
        area.set_border_width(12)
        area.set_spacing(6)

        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        area.pack_start(grid, True, True, 0)

        self._entries: dict[str, Any] = {}

        fields = [
            ("fp_base",           tr(self.cfg, "path_base")),
            ("template_base_dir", tr(self.cfg, "path_templates")),
            ("bin_dir",           tr(self.cfg, "path_bin")),
            ("log_dir",           tr(self.cfg, "path_log")),
            ("lib_path",          tr(self.cfg, "path_lib")),
            ("sensor_py",         tr(self.cfg, "path_sensor")),
            ("nbis_dir",          tr(self.cfg, "path_nbis")),
            ("username",          tr(self.cfg, "default_username")),
        ]

        for row, (key, label) in enumerate(fields):
            grid.attach(Gtk.Label(label=label + ":", xalign=1), 0, row, 1, 1)
            ent = Gtk.Entry(text=str(self.cfg.get(key, "")))
            ent.set_hexpand(True)
            grid.attach(ent, 1, row, 1, 1)
            self._entries[key] = ent

            if key != "username":
                btn = Gtk.Button(label="…")
                btn.connect("clicked", self._browse, ent, key in ("lib_path", "sensor_py"))
                grid.attach(btn, 2, row, 1, 1)

        row = len(fields)

        # Sprache / Language
        grid.attach(Gtk.Label(label=tr(self.cfg, "language") + ":", xalign=1), 0, row, 1, 1)
        lang_combo = Gtk.ComboBoxText()
        lang_combo.append("de", "Deutsch")
        lang_combo.append("en", "English")
        lang_combo.set_active_id(str(self.cfg.get("language", "de")))
        grid.attach(lang_combo, 1, row, 1, 1)
        self._entries["language"] = lang_combo
        row += 1

        # FP-Leser / FP reader
        grid.attach(Gtk.Label(label=tr(self.cfg, "fp_reader") + ":", xalign=1), 0, row, 1, 1)
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

        btn_refresh_reader = Gtk.Button(label="🔄")
        btn_refresh_reader.set_tooltip_text("FP-Leser neu suchen / rescan readers")
        btn_refresh_reader.connect("clicked", self._refresh_reader_combo, reader_combo)
        grid.attach(btn_refresh_reader, 2, row, 1, 1)
        self._entries["fp_reader"] = reader_combo
        row += 1

        for key, label, lo, hi in [
            ("threshold",    tr(self.cfg, "threshold"), 1, 200),
            ("scan_count",   tr(self.cfg, "scan_count"),          1, 20),
            ("min_minutiae", tr(self.cfg, "min_minutiae"),         1, 80),
            ("scan_width",    tr(self.cfg, "scan_width"),           50, 2000),
            ("scan_height",   tr(self.cfg, "scan_height"),             50, 2000),
        ]:
            grid.attach(Gtk.Label(label=label + ":", xalign=1), 0, row, 1, 1)
            spin = Gtk.SpinButton.new_with_range(lo, hi, 1)
            spin.set_value(int(self.cfg[key]))
            grid.attach(spin, 1, row, 1, 1)
            self._entries[key] = spin
            row += 1

        grid.attach(Gtk.Label(label="Pause zwischen Scans (Sek.):", xalign=1), 0, row, 1, 1)
        delay_spin = Gtk.SpinButton.new_with_range(0.0, 10.0, 0.1)
        delay_spin.set_digits(1)
        delay_spin.set_value(float(self.cfg.get("between_scan_delay", 1.2)))
        grid.attach(delay_spin, 1, row, 1, 1)
        self._entries["between_scan_delay"] = delay_spin
        row += 1

        for key, label, lo, hi, step in [
            ("finger_mean_delta", tr(self.cfg, "finger_mean_delta"), 0.0, 80.0, 0.5),
            ("finger_present_std", tr(self.cfg, "finger_present_std"), 1.0, 120.0, 0.5),
            ("finger_off_std", tr(self.cfg, "finger_off_std"), 0.0, 80.0, 0.5),
        ]:
            grid.attach(Gtk.Label(label=label + ":", xalign=1), 0, row, 1, 1)
            spin = Gtk.SpinButton.new_with_range(lo, hi, step)
            spin.set_digits(1)
            spin.set_value(float(self.cfg.get(key, DEFAULT_CFG[key])))
            grid.attach(spin, 1, row, 1, 1)
            self._entries[key] = spin
            row += 1

        grid.attach(Gtk.Label(label=tr(self.cfg, "log_level") + ":", xalign=1), 0, row, 1, 1)
        log_combo = Gtk.ComboBoxText()
        for level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            log_combo.append(level, level)
        log_combo.set_active_id(str(self.cfg.get("log_level", "DEBUG")).upper())
        grid.attach(log_combo, 1, row, 1, 1)
        self._entries["log_level"] = log_combo
        row += 1

        demo_chk = Gtk.CheckButton(label=tr(self.cfg, "demo_mode"))
        demo_chk.set_active(bool(self.cfg.get("demo_mode", False)))
        grid.attach(demo_chk, 0, row, 3, 1)
        self._entries["demo_mode"] = demo_chk
        row += 1

        debug_chk = Gtk.CheckButton(label=tr(self.cfg, "debug_keep_files"))
        debug_chk.set_tooltip_text(
            "Wenn aktiv, bleiben temporäre RAW/WSQ/XYT-Dateien erhalten. "
            "Alte Templates werden beim Enrollment nicht gelöscht und "
            "Benutzer-/Finger-Löschen wird blockiert."
        )
        debug_chk.set_active(bool(self.cfg.get("debug_keep_files", False)))
        grid.attach(debug_chk, 0, row, 3, 1)
        self._entries["debug_keep_files"] = debug_chk
        row += 1

        self._status = Gtk.Label(label="", xalign=0)
        self._status.set_line_wrap(True)
        area.pack_start(self._status, False, False, 0)

        self.add_button("Abbrechen", Gtk.ResponseType.CANCEL)
        self.add_button("Speichern", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)

        self.show_all()

    def _browse(self, _btn, entry: Gtk.Entry, is_file: bool) -> None:
        action = Gtk.FileChooserAction.OPEN if is_file else Gtk.FileChooserAction.SELECT_FOLDER
        title = "Datei wählen" if is_file else "Verzeichnis wählen"
        dlg = Gtk.FileChooserDialog(title=title, transient_for=self, action=action)
        dlg.add_buttons("Abbrechen", Gtk.ResponseType.CANCEL, "OK", Gtk.ResponseType.OK)

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
        c["language"] = self._entries["language"].get_active_id() or "de"
        c["fp_reader"] = self._entries["fp_reader"].get_active_id() or "auto"
        c["log_level"] = self._entries["log_level"].get_active_id() or "DEBUG"
        return coerce_cfg(c)


# ─────────────────────────────────────────────────────────────────
# Main Window
# ─────────────────────────────────────────────────────────────────

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

        self._build_ui()
        self.show_all()

        self._ensure_dirs()
        self._init_scanner()

        self._refresh_user_list()
        self._refresh_enroll_hand()
        self._populate_ver_users()
        self._update_debug_badge()
        self._update_language_labels()
        self._log(f"{APP_NAME} gestartet.")

    def _ensure_dirs(self) -> None:
        for key in ("fp_base", "db_dir", "bin_dir", "log_dir", "tmp_dir"):
            ensure_private_dir(self.paths[key])

    def _init_scanner(self) -> None:
        # Scanner nur initialisieren, wenn kein Demo-Modus aktiv ist.
        if self.cfg.get("demo_mode", False):
            self._log("Demo-Modus aktiv — Scanner wird nicht verwendet.")
            return

        if not self.scanner.available:
            self._log("sensor.py/Scanner nicht verfügbar. Demo-Modus bleibt aus; Scans schlagen fehl.")
            return

        if self.scanner.init():
            self._log(f"Scanner initialisiert. Reader={self.cfg.get('fp_reader', 'auto')}")
        else:
            self._log("Scanner-Initialisierung fehlgeschlagen.")

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

    def _update_language_labels(self) -> None:
        if hasattr(self, "_btn_settings"):
            self._btn_settings.set_label(tr(self.cfg, "settings"))
        if hasattr(self, "_minute_info_title"):
            self._minute_info_title.set_markup(f"<b>{tr(self.cfg, 'minute_info_title')}</b>")


    def _on_quit(self, *_args) -> None:
        try:
            self.scanner.close()
            self._log("Scanner geschlossen.")
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
            self._log("Einstellungen gespeichert.")
        dlg.destroy()

    # ─────────────────────────────────────────────────────────────
    # Tab 1: Enrollment
    # ─────────────────────────────────────────────────────────────

    def _build_enroll_tab(self) -> None:
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, border_width=8)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.set_size_request(190, -1)

        lbl = Gtk.Label(xalign=0)
        lbl.set_markup(f"<b>{tr(self.cfg, 'user')}</b>")
        left.pack_start(lbl, False, False, 0)

        self._user_store = Gtk.ListStore(str)
        self._user_tv = Gtk.TreeView(model=self._user_store)
        self._user_tv.append_column(Gtk.TreeViewColumn(tr(self.cfg, "name"), Gtk.CellRendererText(), text=0))
        self._user_tv.get_selection().connect("changed", self._on_user_selected)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.add(self._user_tv)
        left.pack_start(sw, True, True, 0)

        btn_box = Gtk.Box(spacing=4)
        btn_add = Gtk.Button(label=tr(self.cfg, "new_user"))
        btn_del = Gtk.Button(label=tr(self.cfg, "delete"))
        btn_add.connect("clicked", self._on_user_add)
        btn_del.connect("clicked", self._on_user_del)
        btn_box.pack_start(btn_add, True, True, 0)
        btn_box.pack_start(btn_del, True, True, 0)
        left.pack_start(btn_box, False, False, 0)

        hbox.pack_start(left, False, False, 0)
        hbox.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

        self._hand = HandWidget()
        self._hand.set_finger_callback(self._on_finger_click)
        right.pack_start(self._hand, False, False, 0)

        fbox = Gtk.Box(spacing=6)
        fbox.pack_start(Gtk.Label(label=tr(self.cfg, "finger") + ":"), False, False, 0)
        self._finger_combo = Gtk.ComboBoxText()
        for key, label in FINGERS:
            self._finger_combo.append(key, label)
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

        self._btn_enroll = Gtk.Button(label=tr(self.cfg, "enroll_finger"))
        self._btn_enroll.connect("clicked", self._on_enroll_click)
        self._btn_enroll.set_sensitive(False)
        action_box.pack_start(self._btn_enroll, True, True, 0)

        self._btn_delete_finger = Gtk.Button(label=tr(self.cfg, "delete_finger"))
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

    def _on_finger_click(self, finger_key: str) -> None:
        self._finger_combo.set_active_id(finger_key)
        self._select_enrolled_row(finger_key)
        self._update_minutiae_info(finger_key)

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
            self._minutiae_info_buf.set_text("Bitte Benutzer und Finger auswählen.")
            return

        try:
            fdir = self._finger_dir(user, finger)
        except Exception as e:
            self._minutiae_info_buf.set_text(f"Minutien-Infos nicht verfügbar: {e}")
            return

        templates = sorted(fdir.glob("*.xyt")) if fdir.exists() else []
        label = FINGER_DISPLAY.get(finger, finger)
        if not templates:
            self._minutiae_info_buf.set_text(
                f"{user} / {label}\nKeine Templates für diesen Finger vorhanden."
            )
            return

        counts = [count_minutiae(p) for p in templates]
        total = sum(counts)
        avg = total / len(counts) if counts else 0
        lines = [
            f"Benutzer: {user}",
            f"Finger:   {label} ({finger})",
            f"Templates: {len(templates)}",
            f"Minutien gesamt: {total}   Ø pro Template: {avg:.1f}",
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

        for finger, label in FINGERS:
            fdir = udir / finger
            count = len(list(fdir.glob("*.xyt"))) if fdir.exists() else 0
            if count > 0:
                self._enroll_store.append([finger, label, count])

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

    def _refresh_enroll_hand(self) -> None:
        selected = self._finger_combo.get_active_id()
        self._hand.update(self._enrolled_set(), selected)

    def _on_user_add(self, *_args) -> None:
        dlg = Gtk.Dialog(title="Neuer Benutzer", transient_for=self, modal=True)
        dlg.set_default_size(320, 120)
        area = dlg.get_content_area()
        area.set_border_width(10)
        area.set_spacing(6)
        area.pack_start(Gtk.Label(label="Benutzername:"), False, False, 0)

        ent = Gtk.Entry()
        ent.set_text(str(self.cfg.get("username", "")))
        area.pack_start(ent, False, False, 0)

        dlg.add_button("Abbrechen", Gtk.ResponseType.CANCEL)
        dlg.add_button("Erstellen", Gtk.ResponseType.OK)
        dlg.show_all()

        if dlg.run() == Gtk.ResponseType.OK:
            name = ent.get_text().strip()
            if not validate_username(name):
                self._show_error("Ungültiger Benutzername.\nErlaubt: Buchstaben, Zahlen, '.', '_', '-'")
            else:
                try:
                    udir = self._user_dir(name)
                    ensure_private_dir(udir)
                    self._refresh_user_list()
                    self._populate_ver_users()
                    self._log(f"Benutzer erstellt: {name}")
                except Exception as e:
                    self._show_error(f"Benutzer konnte nicht erstellt werden:\n{e}")
        dlg.destroy()

    def _on_user_del(self, *_args) -> None:
        user = self._cur_user()
        if not user:
            return
        if self.cfg.get("debug_keep_files", False):
            self._log("Debug-Modus: Benutzer-Löschen wurde blockiert.")
            self._show_error("Debug-Modus ist aktiv. Benutzer-Löschen ist deaktiviert.")
            return

        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Benutzer '{user}' und alle Fingerabdrücke löschen?",
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
            self._log(f"Benutzer gelöscht: {user}")
        except PermissionError as e:
            self._log_error(f"Benutzer löschen fehlgeschlagen: {e}")
            if self._ask_repair_permissions(udir, e):
                try:
                    shutil.rmtree(udir)
                    self._refresh_user_list()
                    self._populate_ver_users()
                    self._refresh_enrolled_list()
                    self._refresh_enroll_hand()
                    self._log(f"Benutzer gelöscht: {user}")
                except Exception as e2:
                    self._show_error(f"Löschen auch nach Rechte-Reparatur fehlgeschlagen:\n{e2}")
            else:
                self._show_error(f"Löschen fehlgeschlagen:\n{e}")
        except Exception as e:
            self._show_error(f"Löschen fehlgeschlagen:\n{e}")

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
            self._show_error("Bitte zuerst Benutzer und enrollten Finger auswählen.")
            return
        if self.cfg.get("debug_keep_files", False):
            self._log("Debug-Modus: Finger-Löschen wurde blockiert.")
            self._show_error("Debug-Modus ist aktiv. Finger-Löschen ist deaktiviert.")
            return

        fdir = self._finger_dir(user, finger)
        if not fdir.exists() or not any(fdir.glob("*.xyt")):
            self._show_error("Für diesen Finger sind keine Templates vorhanden.")
            return

        label = FINGER_DISPLAY.get(finger, finger)
        dlg = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f"Enrollten Finger löschen?\n\nBenutzer: {user}\nFinger: {label}",
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
            self._log(f"Enrollter Finger gelöscht: {user}/{finger}")
        except PermissionError as e:
            self._log_error(f"Finger löschen fehlgeschlagen: {e}")
            if self._ask_repair_permissions(fdir, e):
                try:
                    shutil.rmtree(fdir)
                    self._refresh_enrolled_list()
                    self._refresh_enroll_hand()
                    self._populate_ver_users()
                    self._update_minutiae_info(finger)
                    self._log(f"Enrollter Finger gelöscht: {user}/{finger}")
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

        if resp == Gtk.ResponseType.OK and results:
            try:
                saved = self._save_templates(user, finger, results)
                self._enroll_status.set_text(f"✅ {saved} Templates gespeichert.")
                self._refresh_enrolled_list()
                self._refresh_enroll_hand()
                self._update_minutiae_info(finger)
                self._log(f"Enrollment OK: {user}/{finger} ({saved} Templates)")
            except PermissionError as e:
                self._log_error(f"Speichern fehlgeschlagen: {e}")
                if self._ask_repair_permissions(self._finger_dir(user, finger), e):
                    try:
                        saved = self._save_templates(user, finger, results)
                        self._enroll_status.set_text(f"✅ {saved} Templates gespeichert.")
                        self._refresh_enrolled_list()
                        self._refresh_enroll_hand()
                        self._update_minutiae_info(finger)
                        self._log(f"Enrollment OK nach Rechte-Reparatur: {user}/{finger} ({saved} Templates)")
                    except Exception as e2:
                        self._show_error(f"Speichern auch nach Rechte-Reparatur fehlgeschlagen:\n{e2}")
                else:
                    self._show_error(f"Speichern fehlgeschlagen:\n{e}")
            except Exception as e:
                self._show_error(f"Speichern fehlgeschlagen:\n{e}")
        else:
            dlg.cleanup_unkept_temp()
            self._enroll_status.set_text("Enrollment abgebrochen.")
            self._log("Enrollment abgebrochen.")

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
            stamp = time.strftime("%Y%m%d_%H%M%S")
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
        grid.attach(Gtk.Label(label="Benutzer:", xalign=1), 0, 0, 1, 1)

        self._ver_user_combo = Gtk.ComboBoxText()
        self._ver_user_combo.set_hexpand(True)
        grid.attach(self._ver_user_combo, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Finger:", xalign=1), 0, 1, 1, 1)
        self._ver_finger_combo = Gtk.ComboBoxText()
        for key, label in FINGERS:
            self._ver_finger_combo.append(key, label)
        self._ver_finger_combo.set_active(0)
        grid.attach(self._ver_finger_combo, 1, 1, 1, 1)

        vbox.pack_start(grid, False, False, 0)

        btn_ver = Gtk.Button(label="🔍 Verifizieren")
        btn_ver.connect("clicked", self._on_verify)
        vbox.pack_start(btn_ver, False, False, 0)

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

    def _on_verify(self, *_args) -> None:
        user = self._ver_user_combo.get_active_text()
        finger = self._ver_finger_combo.get_active_id()

        if not user or not validate_username(user):
            self._ver_result.set_text("Bitte gültigen Benutzer wählen.")
            return
        if not finger or not validate_finger(finger):
            self._ver_result.set_text("Bitte gültigen Finger wählen.")
            return

        try:
            gallery_dir = self._finger_dir(user, finger)
        except ValueError as e:
            self._ver_result.set_text(str(e))
            return

        gallery = sorted(gallery_dir.glob("*.xyt")) if gallery_dir.exists() else []
        if not gallery:
            self._ver_result.set_text("Keine Templates für diesen Finger vorhanden.")
            return

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
            self._ver_result.set_text("Scan abgebrochen.")
            return

        probe, probe_wsq = results[0]
        best = max(run_bozorth3(str(self.cfg["nbis_dir"]), probe, g) for g in gallery)
        thr = int(self.cfg["threshold"])
        match = best >= thr

        if self.cfg.get("debug_keep_files", False):
            self._log(f"Debug-Modus: Verify-Temp-Dateien bleiben erhalten: {probe}, {probe_wsq}")
        else:
            for tmp in (probe, probe_wsq):
                try:
                    tmp.unlink()
                except Exception:
                    pass

        sym = "✅ MATCH" if match else "❌ KEIN MATCH"
        self._ver_result.set_text(f"{sym}\nScore: {best}  |  Schwellwert: {thr}")
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
        btn_save = Gtk.Button(label="💾 Speichern")
        btn_def = Gtk.Button(label="↺ Defaults")
        btn_save.connect("clicked", self._on_farfrr_save)
        btn_def.connect("clicked", self._on_farfrr_defaults)
        btn_row.pack_start(btn_save, False, False, 0)
        btn_row.pack_start(btn_def, False, False, 0)
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
            label="FAR/FRR aus vorhandenen Templates berechnen:",
        )
        vbox.pack_start(calc_lbl, False, False, 0)

        eer_row = Gtk.Box(spacing=8)
        self._eer_chk = Gtk.CheckButton(
            label="Echtes EER berechnen (alle Thresholds 1–200)"
        )
        eer_row.pack_start(self._eer_chk, False, False, 0)
        vbox.pack_start(eer_row, False, False, 0)

        btn_calc = Gtk.Button(label="📊 FAR/FRR berechnen")
        btn_calc.connect("clicked", self._on_farfrr_calc)
        vbox.pack_start(btn_calc, False, False, 0)

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

    def _farfrr_thread(self, threshold: int, full_eer: bool) -> None:
        db = self.paths["db_dir"]
        all_xyt = list(db.rglob("*.xyt")) if db.exists() else []

        # Gruppierung nach (user, finger), passend zur v4.5-Datenstruktur:
        # template_base_dir/user/finger/*.xyt
        groups: dict[tuple[str, str], list[Path]] = {}
        for xyt in all_xyt:
            try:
                parts = xyt.relative_to(db).parts
                if len(parts) >= 3:
                    key = (parts[0], parts[1])
                    groups.setdefault(key, []).append(xyt)
            except Exception:
                continue

        keys = list(groups.keys())

        genuine_pairs: list[tuple[Path, Path]] = []
        for xlist in groups.values():
            for i in range(len(xlist)):
                for j in range(i + 1, len(xlist)):
                    genuine_pairs.append((xlist[i], xlist[j]))

        impostor_pairs: list[tuple[Path, Path]] = []
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                for x1 in groups[keys[i]]:
                    for x2 in groups[keys[j]]:
                        impostor_pairs.append((x1, x2))

        total = len(genuine_pairs) + len(impostor_pairs)
        if total == 0:
            GLib.idle_add(
                self._farfrr_result.set_text,
                "Zu wenige Templates: Für FAR/FRR werden mindestens zwei Templates benötigt.",
            )
            GLib.idle_add(self._farfrr_prog.set_fraction, 0.0)
            return

        nbis = self.cfg["nbis_dir"]
        done = 0
        genuine_scores: list[int] = []
        impostor_scores: list[int] = []

        for a, b in genuine_pairs:
            genuine_scores.append(run_bozorth3(nbis, a, b))
            done += 1
            if done % 5 == 0 or done == total:
                GLib.idle_add(self._farfrr_prog.set_fraction, done / total)

        for a, b in impostor_pairs:
            impostor_scores.append(run_bozorth3(nbis, a, b))
            done += 1
            if done % 5 == 0 or done == total:
                GLib.idle_add(self._farfrr_prog.set_fraction, done / total)

        def far_at(t: int) -> float:
            if not impostor_scores:
                return 0.0
            return sum(1 for score in impostor_scores if score >= t) / len(impostor_scores) * 100

        def frr_at(t: int) -> float:
            if not genuine_scores:
                return 0.0
            return sum(1 for score in genuine_scores if score < t) / len(genuine_scores) * 100

        far_val = far_at(threshold)
        frr_val = frr_at(threshold)

        if full_eer:
            best_diff = float("inf")
            eer_thr = threshold
            eer_val = (far_val + frr_val) / 2
            for t in range(1, 201):
                f_a = far_at(t)
                f_r = frr_at(t)
                diff = abs(f_a - f_r)
                if diff < best_diff:
                    best_diff = diff
                    eer_thr = t
                    eer_val = (f_a + f_r) / 2
            eer_text = f"\nEER          : {eer_val:.2f}%  (bei Threshold {eer_thr})"
        else:
            eer_text = f"\nEER ≈         {(far_val + frr_val) / 2:.2f}%  (Näherung bei diesem Threshold)"

        txt = (
            f"Threshold    : {threshold}\n"
            f"Genuine      : {len(genuine_scores)} Paare\n"
            f"Impostor     : {len(impostor_scores)} Paare\n"
            f"FAR          : {far_val:.2f}%\n"
            f"FRR          : {frr_val:.2f}%"
            f"{eer_text}"
        )
        GLib.idle_add(self._farfrr_result.set_text, txt)
        GLib.idle_add(self._farfrr_prog.set_fraction, 1.0)
        self._log(
            f"FAR/FRR: thr={threshold} FAR={far_val:.2f}% FRR={frr_val:.2f}%"
        )

    # ─────────────────────────────────────────────────────────────
    # Tab 4: Log
    # ─────────────────────────────────────────────────────────────

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
        btn_clear.connect("clicked", lambda *_: self._log_buf.set_text(""))
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
        cmd = ["pkexec", "chown", "-R", f"{uid}:{gid}", str(target)]
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
