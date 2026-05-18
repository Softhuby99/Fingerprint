#!/bin/bash
# Symlinks + Log-Datei einrichten
set -e
FP_ROOT="$(cd "$(dirname "$0")" && pwd)"

sudo ln -sf "$FP_ROOT/bin/fingerprint-verify" /usr/local/bin/fingerprint-verify
sudo ln -sf "$FP_ROOT/tools/cwsq"     /usr/local/bin/cwsq
sudo ln -sf "$FP_ROOT/tools/mindtct"  /usr/local/bin/mindtct
sudo ln -sf "$FP_ROOT/tools/bozorth3" /usr/local/bin/bozorth3

sudo touch "$FP_ROOT/logs/fingerprint-auth.log"
sudo chown sku:sku "$FP_ROOT/logs/fingerprint-auth.log"
sudo chmod 644 "$FP_ROOT/logs/fingerprint-auth.log"

echo "? Installation abgeschlossen"
echo ""
echo "PAM-Test: sudo env PAM_USER=sku /usr/local/bin/fingerprint-verify"
