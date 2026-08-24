#!/usr/bin/env bash
set -euo pipefail

test_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
plugin_dir="$(cd -- "$test_dir/.." && pwd)"
peqctl="$plugin_dir/backend/peqctl.py"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
state_dir="$config_dir/omarchy/parametric-eq"
fragment="$config_dir/wireplumber/wireplumber.conf.d/60-omarchy-parametric-eq.conf"
backup_dir="$(mktemp -d)"
had_state=false
had_fragment=false

restore() {
  if [[ "$had_state" == true ]]; then
    rm -rf -- "$state_dir"
    cp -a -- "$backup_dir/state" "$state_dir"
  else
    rm -rf -- "$state_dir"
  fi
  if [[ "$had_fragment" == true ]]; then
    mkdir -p -- "$(dirname -- "$fragment")"
    cp -a -- "$backup_dir/fragment" "$fragment"
  else
    rm -f -- "$fragment"
  fi
  systemctl --user restart wireplumber.service >/dev/null
  rm -rf -- "$backup_dir"
}
trap restore EXIT

if [[ -d "$state_dir" ]]; then cp -a -- "$state_dir" "$backup_dir/state"; had_state=true; fi
if [[ -f "$fragment" ]]; then cp -a -- "$fragment" "$backup_dir/fragment"; had_fragment=true; fi

"$peqctl" ensure >/dev/null
"$peqctl" enable >/dev/null
before_pid="$(systemctl --user show wireplumber.service -p MainPID --value)"
"$peqctl" set-band 4 true bell 777 5.5 1.75 >/dev/null
after_pid="$(systemctl --user show wireplumber.service -p MainPID --value)"
[[ "$before_pid" == "$after_pid" ]] || { printf 'Numeric edit unexpectedly restarted WirePlumber.\n' >&2; exit 1; }

status_json="$("$peqctl" status)"
jq -e '.runtime.node_present and .enabled and (.bands[3].frequency == 777) and (.bands[3].q == 1.75)' <<<"$status_json" >/dev/null

node_id="$(jq -r '.runtime.node_id' <<<"$status_json")"
pw-cli enum-params "$node_id" Props | rg -U -q 'String "band_4:Freq"\n\s+Float 777\.000000'

before_pid="$(systemctl --user show wireplumber.service -p MainPID --value)"
"$peqctl" set-preamp -6 >/dev/null
after_pid="$(systemctl --user show wireplumber.service -p MainPID --value)"
[[ "$before_pid" == "$after_pid" ]] || { printf 'Preamp edit unexpectedly restarted WirePlumber.\n' >&2; exit 1; }
pw-cli enum-params "$node_id" Props | rg -U -q 'String "preamp:Mult"\n\s+Float 0\.501187'

"$peqctl" disable >/dev/null
pw-metadata -n filters | rg -q "subject: $node_id.*filter.smart.disabled|filter.smart.disabled.*true" || {
  printf 'Smart-filter bypass metadata was not published.\n' >&2
  exit 1
}

before_pid="$(systemctl --user show wireplumber.service -p MainPID --value)"
"$peqctl" set-band 4 true notch 777 0 1.75 >/dev/null
after_pid="$(systemctl --user show wireplumber.service -p MainPID --value)"
[[ "$before_pid" != "$after_pid" ]] || { printf 'Topology edit did not rebuild WirePlumber.\n' >&2; exit 1; }

"$peqctl" diagnostics | jq -e '.ok and .fragment_current and .node_present' >/dev/null
"$peqctl" cleanup >/dev/null
[[ ! -e "$fragment" ]] || { printf 'Cleanup left the managed fragment behind.\n' >&2; exit 1; }
"$peqctl" status | jq -e '(.enabled | not) and (.runtime.node_present | not)' >/dev/null
printf 'Live PipeWire integration checks passed; the previous graph will now be restored.\n'
