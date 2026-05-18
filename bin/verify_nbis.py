#!/usr/bin/env python3
"""Verify mit NBIS bozorth3"""

import os, sys, subprocess, time, glob
sys.path.insert(0, "/home/sku/fingerprint/lib")
import numpy as np
from sensor import open_scanner, get_size, capture

NBIS_BIN = "/usr/local/bin"
DB_DIR = "/home/sku/fingerprint/templates"
THRESHOLD = 35

def img_to_xyt(img, tmp_prefix, width, height):
    raw_path = f"{tmp_prefix}.raw"
    img.tofile(raw_path)
    subprocess.run([
        f"{NBIS_BIN}/cwsq", "0.75", "wsq", raw_path,
        "-raw_in", f"{width},{height},8,500"
    ], capture_output=True)
    subprocess.run([
        f"{NBIS_BIN}/mindtct", f"{tmp_prefix}.wsq", tmp_prefix
    ], capture_output=True)
    xyt_path = f"{tmp_prefix}.xyt"
    if os.path.exists(xyt_path):
        with open(xyt_path) as f:
            count = len(f.readlines())
        for ext in ['.raw', '.wsq', '.brw', '.dm', '.hcm', '.lcm', '.lfm', '.min', '.qm']:
            p = f"{tmp_prefix}{ext}"
            if os.path.exists(p): os.remove(p)
        return xyt_path, count
    return None, 0

def wait_for_finger(handle, w, h, size):
    print("?? Finger auflegen...")
    baseline = None
    img = capture(handle, w, h, size)
    if img is not None:
        baseline = float(img.mean())
    while True:
        img = capture(handle, w, h, size)
        if img is not None:
            if baseline and abs(float(img.mean()) - baseline) > 10:
                return img
            if img.std() > 30:
                return img
        time.sleep(0.1)

def main():
    if len(sys.argv) < 2:
        print("Usage: sudo python3 verify_nbis.py <username>")
        sys.exit(1)
    
    user = sys.argv[1]
    user_dir = f"{DB_DIR}/{user}"
    templates = sorted(glob.glob(f"{user_dir}/template_*.xyt"))
    
    if not templates:
        print(f"? Keine Templates für '{user}'")
        sys.exit(1)
    
    handle = open_scanner()
    w, h, size = get_size(handle)
    print(f"?? Verify: {user} ({len(templates)} Templates)")
    
    img = wait_for_finger(handle, w, h, size)
    
    probe_xyt, count = img_to_xyt(img, "/home/sku/fingerprint/tmp/verify_probe", w, h)
    if not probe_xyt or count < 5:
        print(f"? Zu wenige Minutien ({count})")
        sys.exit(1)
    
    print(f"   {count} Minutien extrahiert")
    
    best_score = 0
    for tpl in templates:
        result = subprocess.run(
            [f"{NBIS_BIN}/bozorth3", probe_xyt, tpl],
            capture_output=True, text=True
        )
        score = int(result.stdout.strip()) if result.stdout.strip() else 0
        print(f"   vs {os.path.basename(tpl)}: Score {score}")
        best_score = max(best_score, score)
    
    os.remove(probe_xyt)
    
    print(f"\n   Best Score: {best_score} (Threshold: {THRESHOLD})")
    if best_score >= THRESHOLD:
        print("   MATCH")
        sys.exit(0)
    else:
        print("   NO MATCH")
        sys.exit(1)

if __name__ == "__main__":
    main()
