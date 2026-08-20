#!/usr/bin/env bash
set -euo pipefail

plugin_id="io.github.prudhviy99.parametric-eq"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source_dir="$(cd -- "$script_dir/.." && pwd)"
target_dir="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/$plugin_id"
stage_dir="$(mktemp -d)"
trap 'rm -rf -- "$stage_dir"' EXIT

force=false
case "${1:-}" in
  "") ;;
  --force) force=true ;;
  *) printf 'Usage: %s [--force]\n' "$0" >&2; exit 2 ;;
esac

omarchy plugin validate "$source_dir"
mkdir -p -- "$(dirname -- "$target_dir")"
cp -a -- "$source_dir/." "$stage_dir/plugin/"
rm -rf -- "$stage_dir/plugin/.git" "$stage_dir/plugin/__pycache__" "$stage_dir/plugin/backend/__pycache__"

if [[ -e "$target_dir" || -L "$target_dir" ]]; then
  if [[ "$force" != true ]]; then
    printf 'Refusing to replace existing plugin: %s\nRe-run with --force to move it to the desktop trash first.\n' "$target_dir" >&2
    exit 1
  fi
  command -v gio >/dev/null || { printf 'gio is required for recoverable replacement.\n' >&2; exit 1; }
  gio trash -- "$target_dir"
fi
mv -- "$stage_dir/plugin" "$target_dir"
chmod +x -- "$target_dir/backend/peqctl.py" "$target_dir/scripts/"*.sh "$target_dir/tests/"*.sh
"$target_dir/backend/peqctl.py" ensure >/dev/null
omarchy-shell shell rescanPlugins >/dev/null
printf 'Installed %s at %s\n' "$plugin_id" "$target_dir"
