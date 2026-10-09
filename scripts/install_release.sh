#!/bin/sh
# Install a release wheel bundle, or an explicitly selected registry/Git source.
set -eu
task_bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! command -v uv >/dev/null 2>&1; then
    echo 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/' >&2
    exit 1
fi
task_source=
task_bin_dir=$(uv tool dir --bin)
if [ "${1:-}" = '--from' ]; then
    [ "$#" -ge 2 ] || { echo 'Usage: install.sh [--from SOURCE] [setup options]' >&2; exit 2; }
    task_source=$2
    shift 2
fi
if [ -z "$task_source" ]; then
    for task_candidate in "$task_bundle_dir"/acp_gateway-*.whl; do
        [ -f "$task_candidate" ] || continue
        [ -z "$task_source" ] || { echo 'Multiple release wheels found; pass --from explicitly.' >&2; exit 2; }
        task_source=$task_candidate
    done
    [ -n "$task_source" ] || { echo 'No release wheel found; pass --from acp-gateway or a Git URL.' >&2; exit 2; }
fi
case "$task_source" in
    -*) echo 'Invalid installation source.' >&2; exit 2 ;;
esac
if [ -x "$task_bin_dir/acpgw" ]; then
    if [ -f "$task_bundle_dir/requirements.lock.txt" ]; then
        "$task_bin_dir/acpgw" update --from "$task_source" --constraints "$task_bundle_dir/requirements.lock.txt"
    else
        "$task_bin_dir/acpgw" update --from "$task_source"
    fi
elif [ -f "$task_bundle_dir/requirements.lock.txt" ]; then
    uv tool install --force --python 3.12 --constraints "$task_bundle_dir/requirements.lock.txt" "$task_source"
else
    uv tool install --force --python 3.12 "$task_source"
fi
"$task_bin_dir/acpgw" setup "$@"
echo "Installed: $task_bin_dir/acpgw"
echo 'Ensure the tool bin directory is on PATH (uv tool update-shell).'
