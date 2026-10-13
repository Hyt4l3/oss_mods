#!/usr/bin/env bash
# Quick wrapper to run the Python generator with any passed arguments.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/generate_readme.py" "$@"
