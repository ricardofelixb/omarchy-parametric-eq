#!/usr/bin/env bash
set -euo pipefail

plugin_id="io.github.prudhviy99.parametric-eq"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
plugin_dir="$(cd -- "$script_dir/.." && pwd)"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
state_dir="$config_dir/omarchy/parametric-eq"
expected_plugin_dir="$HOME/.config/omarchy/plugins/$plugin_id"

purge=false
case "${1:-}" in
  "") ;;
  --purge) purge=true ;;
  *) printf 'Usage: %s [--purge]\n' "$0" >&2; exit 2 ;;
esac

[[ "$plugin_dir" == "$expected_plugin_dir" ]] || {
  printf 'Refusing to uninstall from a development checkout: %s\nRun the installed copy at %s/scripts/uninstall.sh instead.\n' \
    "$plugin_dir" "$expected_plugin_dir" >&2
  exit 1
}

command -v gio >/dev/null || { printf 'gio is required for recoverable removal.\n' >&2; exit 1; }

omarchy plugin disable "$plugin_id" >/dev/null 2>&1 || true
"$plugin_dir/backend/peqctl.py" cleanup >/dev/null

if [[ "$purge" == true && -e "$state_dir" ]]; then
  gio trash -- "$state_dir"
fi

if [[ -e "$plugin_dir" || -L "$plugin_dir" ]]; then
  gio trash -- "$plugin_dir"
fi
omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
printf 'Removed %s (state %s).\n' "$plugin_id" "$([[ "$purge" == true ]] && printf purged || printf retained)"
