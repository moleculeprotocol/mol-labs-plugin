#!/usr/bin/env bash
# Refresh the bundled plugin copies from the canonical sources in ../skills.
# The canonical, edit-here copies live in molecule_core/skills/{aura-orchestrator,
# molecule-x402,privy-agentic-wallets} and molecule_core/skills/molecule-mcp.
# This plugin directory is the packaged/distributable form — run this after
# editing the canonical sources so the plugin doesn't drift.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../skills"

for s in aura-orchestrator molecule-x402 privy-agentic-wallets; do
  rsync -a --delete --exclude '.venv' --exclude '__pycache__' "$SRC/$s/" "$HERE/skills/$s/"
done

for f in server.py pyproject.toml requirements.txt README.md smoke.py; do
  cp "$SRC/molecule-mcp/$f" "$HERE/mcp/$f"
done

echo "Synced plugin from $SRC -> $HERE"
