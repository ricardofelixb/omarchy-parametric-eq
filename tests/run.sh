#!/usr/bin/env bash
set -euo pipefail

test_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
plugin_dir="$(cd -- "$test_dir/.." && pwd)"

export PYTHONDONTWRITEBYTECODE=1
python -m unittest discover -s "$test_dir" -p 'test_*.py' -v
python -c 'import pathlib, sys; path = pathlib.Path(sys.argv[1]); compile(path.read_text(encoding="utf-8"), str(path), "exec")' "$plugin_dir/backend/peqctl.py"
bash -n "$plugin_dir/scripts/install.sh" "$plugin_dir/scripts/uninstall.sh" "$test_dir/integration.sh"
omarchy plugin validate "$plugin_dir"
qmllint -I /usr/share/omarchy/shell \
  "$plugin_dir/BarWidget.qml" \
  "$plugin_dir/EqSlider.qml" \
  "$plugin_dir/ResponseGraph.qml" \
  "$plugin_dir/Panel.qml"
QT_QPA_PLATFORM=offscreen /usr/lib/qt6/bin/qmltestrunner \
  -input "$test_dir/tst_eqslider.qml" \
  -import "$test_dir/imports" \
  -import "$plugin_dir" \
  -o -,txt
printf 'All isolated checks passed.\n'
