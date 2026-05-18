#!/usr/bin/env python3
"""Fingerabdruck registrieren für Linux Login"""

import ctypes
import os
import sys
sys.path.insert(0, "/home/sku/fingerprint/lib")
import json
import time
import getpass

LIB = ctypes.CDLL("/home/sku/fingerprint/lib/libScanAPI.so")

class FTRSCAN_IMAGE_SIZE(ctypes.Structure):
    _fields_ = [("nWidth", ctypes.c_int), ("nHeight", ctypes.c_int), ("nImageSize", ctypes.c_int)]

def open_scanner():
    LIB.ftrScanOpenDevice.restype = ctypes.c_void_p
    handle = LIB.ftrScanOpenDevice()
    if not handle:
        print("? Scanner nicht gefunden!")
        sys.exit(1)
    return handle

def get_image_size(handle):
    img = FTRSCAN_IMAGE_SIZE()
    LIB.ftrScanGetImageSize(ctypes.c_void_p(handle), ctypes.byref(img))
    return img

def get_frame(handle, img_size):
    """Einzelnen Frame holen"""
    buf = ctypes.create_string_buffer(img_size.nImageSize)
    ret = LIB.ftrScanGetFrame(ctypes.c_void_p(handle), buf, None)
    if ret:
        return buf.raw[:img_size.nImageSize]
    return None

def finger_present(data):
    """Prüfen ob ein Finger auf dem Scanner liegt"""
    avg = sum(data) / len(data)
    return avg > 25

def wait_finger_off(handle, img_size):
    """Warten bis Finger vom Scanner genommen wird"""
    print("   ??  Finger jetzt WEGNEHMEN...")
    while True:
        data = get_frame(handle, img_size)
        if data is None or not finger_present(data):
            print("   ? Finger weg erkannt")
            return
        time.sleep(0.3)

def wait_finger_on(handle, img_size):
    """Warten bis Finger aufgelegt wird, mit Countdown"""
    print("   ?? Finger jetzt AUFLEGEN...")
    
    timeout = 30  # 30 Sekunden
    start = time.time()
    
    while time.time() - start < timeout:
        remaining = int(timeout - (time.time() - start))
        data = get_frame(handle, img_size)
        
        if data and finger_present(data):
            # Kurz warten damit Finger richtig aufliegt
            time.sleep(0.5)
            # Nochmal scannen für gutes Bild
            data2 = get_frame(handle, img_size)
            if data2 and finger_present(data2):
                avg = sum(data2) / len(data2)
                print(f"   ? Finger erkannt! (Qualität: {avg:.0f})")
                return data2
        
        # Fortschritt anzeigen
        if remaining % 5 == 0:
            sys.stdout.write(f"\r   ? Warte auf Finger... ({remaining}s) ")
            sys.stdout.flush()
        
        time.sleep(0.3)
    
    print("\n   ? Timeout!")
    return None

def compute_template(raw_data):
    """Template aus Fingerabdruck-Rohdaten erstellen"""
    width, height = 320, 480
    zone_w, zone_h = 32, 48
    features = []
    
    for zy in range(0, height, zone_h):
        for zx in range(0, width, zone_w):
            zone_pixels = []
            for y in range(zy, min(zy + zone_h, height)):
                for x in range(zx, min(zx + zone_w, width)):
                    zone_pixels.append(raw_data[y * width + x])
            
            if zone_pixels:
                avg = sum(zone_pixels) / len(zone_pixels)
                variance = sum((p - avg) ** 2 for p in zone_pixels) / len(zone_pixels)
                features.append((int(avg), int(variance ** 0.5)))
    
    return features

def match_templates(t1, t2):
    """Zwei Templates vergleichen"""
    if len(t1) != len(t2):
        return 0.0
    matches = 0
    for (avg1, std1), (avg2, std2) in zip(t1, t2):
        if abs(avg1 - avg2) < 30 and abs(std1 - std2) < 20:
            matches += 1
    return matches / len(t1)

def save_template(username, templates):
    """Templates für User speichern"""
    path = f"/etc/fingerprints/{username}.dat"
    data = {
        "username": username,
        "enrolled": time.strftime("%Y-%m-%d %H:%M:%S"),
        "templates": templates
    }
    with open(path, "w") as f:
        json.dump(data, f)
    os.chmod(path, 0o600)
    print(f"\n?? Template gespeichert: {path}")

def enroll(username):
    NUM_SCANS = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    print(f"\n?? Fingerabdruck-Registrierung für: {username}")
    print("=" * 50)
    print(f"   Es werden {NUM_SCANS} Scans durchgeführt.")
    print(f"   Bitte den GLEICHEN Finger verwenden!")
    print("=" * 50)
    
    handle = open_scanner()
    img_size = get_image_size(handle)
    
    templates = []
    
    for i in range(NUM_SCANS):
        print(f"\n{'-' * 40}")
        print(f"   ?? Scan {i+1} von {NUM_SCANS}")
        print(f"{'-' * 40}")
        
        # Bei erstem Scan: direkt Finger auflegen
        # Bei weiteren: erst warten bis Finger weg, dann neu auflegen
        if i > 0:
            wait_finger_off(handle, img_size)
            time.sleep(1.0)  # Kurze Pause
            print()
        
        data = wait_finger_on(handle, img_size)
        
        if data is None:
            print("? Enrollment abgebrochen!")
            LIB.ftrScanCloseDevice(ctypes.c_void_p(handle))
            return False
        
        template = compute_template(data)
        templates.append(template)
        
        print(f"   ? Scan {i+1}/{NUM_SCANS} erfolgreich!")
    
    # Konsistenz prüfen
    print(f"\n{'-' * 40}")
    print(f"   ?? Prüfe Scan-Konsistenz...")
    print(f"{'-' * 40}")
    
    scores = []
    for i in range(len(templates)):
        for j in range(i+1, len(templates)):
            score = match_templates(templates[i], templates[j])
            scores.append(score)
    
    avg_score = sum(scores) / len(scores) if scores else 0
    min_score = min(scores) if scores else 0
    
    print(f"   Durchschnitt: {avg_score:.1%}")
    print(f"   Minimum:      {min_score:.1%}")
    
    if avg_score < 0.5:
        print("\n? Scans zu unterschiedlich! Bitte nochmal versuchen.")
        print("   Tipp: Finger immer gleich und mittig auflegen.")
        LIB.ftrScanCloseDevice(ctypes.c_void_p(handle))
        return False
    
    save_template(username, templates)
    
    LIB.ftrScanCloseDevice(ctypes.c_void_p(handle))
    print(f"\n? Enrollment erfolgreich für {username}! ??")
    return True

if __name__ == "__main__":
    username = sys.argv[1] if len(sys.argv) > 1 else getpass.getuser()
    enroll(username)
