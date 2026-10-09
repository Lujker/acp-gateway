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
task_bin_dir=$(uv tool dir --bin)
if [ -x "$task_bin_dir/acpgw" ]; then
    task_config_dir=
    task_expect_config=0
    for task_argument in "$@"; do
        if [ "$task_expect_config" = 1 ]; then
            task_config_dir=$task_argument
            task_expect_config=0
        else
            case "$task_argument" in
                --config-dir) task_expect_config=1 ;;
                --config-dir=*) task_config_dir=${task_argument#--config-dir=} ;;
            esac
        fi
    done
    if [ -n "$task_config_dir" ]; then
        "$task_bin_dir/acpgw" --config "$task_config_dir/config.yaml" --env-file "$task_config_dir/.env" update --from "$task_repo_dir" --constraints "$task_constraints"
    else
        "$task_bin_dir/acpgw" update --from "$task_repo_dir" --constraints "$task_constraints"
    fi
else
    uv tool install --python 3.12 --constraints "$task_constraints" "$task_repo_dir"
fi
"$task_bin_dir/acpgw" setup "$@"
echo "Installed: $task_bin_dir/acpgw"
echo 'Ensure the tool bin directory is on PATH (uv tool update-shell).'
