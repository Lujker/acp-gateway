#!/bin/sh
set -eu
task_repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec uv run --no-project --python 3.12 --managed-python "$task_repo_dir/scripts/checkout.py" run "$@"
