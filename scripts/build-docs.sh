#!/usr/bin/env bash
# Render docs/*.md to docs/pdf/*.pdf. Needs: .venv/bin/pip install markdown weasyprint
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python scripts/build_docs.py
