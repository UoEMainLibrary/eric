#!/bin/bash
cd /apps/www/eric || exit 1
exec python3 scripts/backfill_from_archipelago.py
