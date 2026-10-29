#!/bin/bash
cd /apps/www/eric || exit 1
exec python3 scripts/sync_archipelago_images.py
