#!/usr/bin/env bash
set -euo pipefail

python -m ostag.main --platform feishu "$@"
