#!/bin/sh
# Install a checkout as an isolated user tool, then create initial configuration.
set -eu
task_repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if ! command -v uv >/dev/null 2>&1; then
    echo 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/' >&2
    exit 1
fi
task_constraints=$(mktemp)
trap 'rm -f "$task_constraints"' EXIT HUP INT TERM
uv export --project "$task_repo_dir" --frozen --no-dev --no-emit-project \
    --format requirements-txt --output-file "$task_constraints" >/dev/null
uv tool install --force --python 3.12 --constraints "$task_constraints" "$task_repo_dir"
task_bin_dir=$(uv tool dir --bin)
"$task_bin_dir/acpgw" setup "$@"
echo "Installed: $task_bin_dir/acpgw"
echo 'Ensure the tool bin directory is on PATH (uv tool update-shell).'
