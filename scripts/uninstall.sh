#!/usr/bin/env bash
set -euo pipefail

plugin_id="io.github.prudhviy99.parametric-eq"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
plugin_dir="$config_dir/omarchy/plugins/$plugin_id"
fragment="$config_dir/wireplumber/wireplumber.conf.d/60-omarchy-parametric-eq.conf"
state_dir="$config_dir/omarchy/parametric-eq"

purge=false
case "${1:-}" in
  "") ;;
  --purge) purge=true ;;
  *) printf 'Usage: %s [--purge]\n' "$0" >&2; exit 2 ;;
esac

command -v gio >/dev/null || { printf 'gio is required for recoverable removal.\n' >&2; exit 1; }

omarchy plugin disable "$plugin_id" >/dev/null 2>&1 || true

removed_fragment=false
if [[ -e "$fragment" ]]; then
  [[ ! -L "$fragment" ]] || { printf 'Refusing to remove symlinked fragment: %s\n' "$fragment" >&2; exit 1; }
  marker=""
  IFS= read -r marker < "$fragment" || true
  [[ "$marker" == "# Managed by $plugin_id; manual changes are overwritten." ]] || {
    printf 'Refusing to remove an unrecognized WirePlumber fragment: %s\n' "$fragment" >&2
    exit 1
  }
  gio trash -- "$fragment"
  removed_fragment=true
fi

if [[ "$purge" == true && -e "$state_dir" ]]; then
  gio trash -- "$state_dir"
fi

if [[ "$removed_fragment" == true ]]; then
  systemctl --user restart wireplumber.service
fi

if [[ -e "$plugin_dir" || -L "$plugin_dir" ]]; then
  gio trash -- "$plugin_dir"
fi
omarchy-shell shell rescanPlugins >/dev/null
printf 'Removed %s (state %s).\n' "$plugin_id" "$([[ "$purge" == true ]] && printf purged || printf retained)"
