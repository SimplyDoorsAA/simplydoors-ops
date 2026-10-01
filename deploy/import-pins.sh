#!/usr/bin/env bash
# One-time import of the old portal PINs (Google Sheet "PINs" tab, downloaded as CSV).
# Put the file in your home folder on the OptiPlex as pins.csv, then run this.
# The PINs are stored scrambled and the CSV is deleted afterwards.
set -euo pipefail
FILE="${1:-$HOME/pins.csv}"
[ -f "$FILE" ] || { echo "Can't find $FILE"; exit 1; }
DATA="$HOME/ai-server/opsapp-data"
sudo mkdir -p "$DATA/import"
sudo mv "$FILE" "$DATA/import/pins.csv"
sudo chown 1000:1000 "$DATA/import/pins.csv"
docker exec opsapp python -m app.cli import-pins /data/import/pins.csv
sudo rm -f "$DATA/import/pins.csv"
echo "Done. Check Admin > Staff & PINs for anyone still without a PIN."
