import copy
import importlib.machinery
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


TEST_ROOT = tempfile.TemporaryDirectory()
os.environ["OMARCHY_PEQ_STATE_DIR"] = str(Path(TEST_ROOT.name) / "state")
os.environ["OMARCHY_PEQ_WP_FRAGMENT"] = str(Path(TEST_ROOT.name) / "wireplumber.conf")
os.environ["OMARCHY_PEQ_SHELL_CONFIG"] = str(Path(TEST_ROOT.name) / "shell.json")
MODULE_PATH = Path(__file__).parents[1] / "backend" / "peqctl.py"
loader = importlib.machinery.SourceFileLoader("peqctl", str(MODULE_PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
peqctl = importlib.util.module_from_spec(spec)
loader.exec_module(peqctl)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.state = peqctl.default_state()

    def test_default_is_ten_band_flat_parametric_eq(self):
        self.assertEqual(len(self.state["bands"]), 10)
        self.assertEqual(self.state["bands"][0]["type"], "low-shelf")
        self.assertEqual(self.state["bands"][-1]["type"], "high-shelf")
        self.assertAlmostEqual(peqctl.estimated_peak_db(self.state), 0.0, places=3)

    def test_state_clamps_safe_audio_ranges(self):
        raw = peqctl.default_state()
        raw["preamp_db"] = 99
        raw["bands"][0].update(frequency=1, gain=-99, q=99)
        clean = peqctl.sanitize_state(raw)
        self.assertEqual(clean["preamp_db"], 12)
        self.assertEqual(clean["bands"][0]["frequency"], 10)
        self.assertEqual(clean["bands"][0]["gain"], -24)
        self.assertEqual(clean["bands"][0]["q"], 10)

    def test_invalid_filter_type_is_rejected(self):
        self.state["bands"][0]["type"] = "not-a-filter"
        with self.assertRaises(peqctl.PeqError):
            peqctl.sanitize_state(self.state)

    def test_band_edits_never_change_topology(self):
        # A topology change restarts WirePlumber and drops every stream.
        original = peqctl.topology_signature(self.state)
        edited = copy.deepcopy(self.state)
        edited["bands"][3].update(type="notch", gain=8)
        self.assertEqual(original, peqctl.topology_signature(edited))
        edited["bands"][3]["enabled"] = False
        self.assertEqual(original, peqctl.topology_signature(edited))
        self.assertEqual(peqctl.band_coefficients(edited["bands"][3], 48000), (1.0, 0.0, 0.0, 1.0, 0.0, 0.0))

    def test_fragment_contains_linear_preamp_and_all_bands(self):
        self.state["enabled"] = True
        self.state["preamp_db"] = -6
        fragment = peqctl.fragment_text(self.state)
        self.assertIn("label = linear", fragment)
        self.assertIn('name = band_10', fragment)
        self.assertIn("filter.smart = true", fragment)
        self.assertIn("filter.smart.disabled = false", fragment)
        self.assertIn("node.always-process = true", fragment)
        self.assertIn('"Mult" = 0.50118723', fragment)
        self.assertNotIn("equalizer.plugin", fragment)

    def test_response_model_and_auto_preamp_protect_headroom(self):
        self.state["bands"][5].update(type="bell", frequency=1000, gain=9, q=1.4)
        peak_without_preamp = peqctl.estimated_peak_db(self.state)
        self.assertGreater(peak_without_preamp, 8.8)
        self.state["auto_preamp"] = True
        self.assertLessEqual(peqctl.estimated_peak_db(self.state), 0.01)
        self.assertLess(peqctl.effective_preamp(self.state), -8.8)

    def test_every_filter_shape_has_finite_coefficients(self):
        for kind in peqctl.TYPE_LABELS:
            item = dict(self.state["bands"][4], type=kind, gain=4)
            coefficients = peqctl.biquad_coefficients(item)
            self.assertEqual(len(coefficients), 5)
            self.assertTrue(all(math.isfinite(value) for value in coefficients), kind)

    def test_snapshot_preserves_full_parametric_values(self):
        self.state["bands"][1].update(enabled=False, type="high-pass", frequency=91, gain=2, q=2.5)
        payload = peqctl.snapshot_payload(self.state)
        restored = peqctl.default_state()
        peqctl.restore_payload(restored, payload)
        self.assertEqual(restored["bands"][1]["type"], "high-pass")
        self.assertFalse(restored["bands"][1]["enabled"])
        self.assertEqual(restored["bands"][1]["frequency"], 91)


class ImportTests(unittest.TestCase):
    def test_standard_autoeq_text_import(self):
        text = """Preamp: -7.4 dB
Filter 1: ON PK Fc 105 Hz Gain 6.3 dB Q 1.410
Filter 2: OFF LSC Fc 80 Hz Gain 2.0 dB Q 0.700
Filter 3: ON HP Fc 25 Hz Q 0.900
"""
        state = peqctl.parse_autoeq(text)
        self.assertEqual(state["preamp_db"], -7.4)
        self.assertEqual(len(state["bands"]), 3)
        self.assertEqual(state["bands"][0]["type"], "bell")
        self.assertEqual(state["bands"][1]["type"], "low-shelf")
        self.assertFalse(state["bands"][1]["enabled"])
        self.assertEqual(state["bands"][2]["type"], "high-pass")

    def test_import_without_filters_fails(self):
        with self.assertRaises(peqctl.PeqError):
            peqctl.parse_autoeq("Preamp: -3 dB\nnot a filter")

    def test_preset_filename_cannot_escape_directory(self):
        filename = peqctl.safe_preset_filename("../../ My Headphones")
        self.assertNotIn("/", filename)
        self.assertTrue(filename.endswith(".json"))


class StateFileSecurityTests(unittest.TestCase):
    def setUp(self):
        peqctl.STATE_DIR.mkdir(parents=True, exist_ok=True)
        for path in (peqctl.STATE_FILE, peqctl.STATE_DIR / ".lock"):
            path.unlink(missing_ok=True)

    def test_state_symlink_is_replaced_without_touching_target(self):
        target = Path(TEST_ROOT.name) / "external-state.json"
        target.write_text('{"external": true}\n', encoding="utf-8")
        peqctl.STATE_FILE.symlink_to(target)

        state = peqctl.load_state()

        self.assertEqual(state, peqctl.default_state())
        self.assertEqual(target.read_text(encoding="utf-8"), '{"external": true}\n')
        self.assertFalse(peqctl.STATE_FILE.is_symlink())
        self.assertEqual(peqctl.STATE_FILE.stat().st_mode & 0o777, 0o600)
        self.assertEqual(peqctl.STATE_DIR.stat().st_mode & 0o777, 0o700)

    def test_state_hardlink_is_replaced_without_touching_target(self):
        target = Path(TEST_ROOT.name) / "linked-state.json"
        target.write_text('{"external": true}\n', encoding="utf-8")
        os.link(target, peqctl.STATE_FILE)

        state = peqctl.load_state()

        self.assertEqual(state, peqctl.default_state())
        self.assertEqual(target.read_text(encoding="utf-8"), '{"external": true}\n')
        self.assertEqual(target.stat().st_nlink, 1)

    def test_non_regular_state_does_not_block(self):
        os.mkfifo(peqctl.STATE_FILE)

        state = peqctl.load_state()

        self.assertEqual(state, peqctl.default_state())
        self.assertTrue(peqctl.STATE_FILE.is_file())

    def test_oversized_state_is_replaced_with_bounded_default(self):
        peqctl.STATE_FILE.write_bytes(b"x" * (peqctl.MAX_STATE_BYTES + 1))

        state = peqctl.load_state()

        self.assertEqual(state, peqctl.default_state())
        self.assertLess(peqctl.STATE_FILE.stat().st_size, peqctl.MAX_STATE_BYTES)

    def test_directory_lock_creates_no_predictable_lock_file(self):
        with peqctl.locked_state_directory():
            pass

        self.assertFalse((peqctl.STATE_DIR / ".lock").exists())


class RuntimeCommandTests(unittest.TestCase):
    def test_command_output_is_bounded(self):
        command = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 4096)"]
        with self.assertRaisesRegex(peqctl.PeqError, "output exceeded 128 bytes"):
            peqctl.run(command, max_output_bytes=128)

    def test_command_capture_preserves_separate_streams(self):
        command = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('out'); sys.stderr.write('err')",
        ]
        result = peqctl.run(command)
        self.assertEqual(result.stdout, "out")
        self.assertEqual(result.stderr, "err")

    def test_reviewer_blocked_patterns_do_not_regress(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("capture_output=True", source)
        self.assertNotIn("LOCK_FILE", source)
        self.assertNotIn("STATE_FILE.read_text", source)

    @mock.patch.object(peqctl, "find_node", return_value={"id": 72})
    @mock.patch.object(peqctl, "run")
    def test_live_update_uses_pipewire_props_without_shell(self, run_mock, _node_mock):
        run_mock.return_value = subprocess.CompletedProcess([], 0, "", "")
        state = peqctl.default_state()
        state["preamp_db"] = -3
        self.assertTrue(peqctl.set_live_controls(state))
        command = run_mock.call_args.args[0]
        self.assertEqual(command[:4], ["pw-cli", "set-param", "72", "Props"])
        self.assertIn('"preamp:Mult"', command[4])
        self.assertIn('"band_10:b0"', command[4])
        self.assertNotIn("sh -c", " ".join(command))

    @mock.patch.object(peqctl, "find_node", return_value={"id": 88})
    @mock.patch.object(peqctl, "run")
    def test_bypass_uses_wireplumber_filter_metadata(self, run_mock, _node_mock):
        run_mock.return_value = subprocess.CompletedProcess([], 0, "", "")
        self.assertTrue(peqctl.set_bypass(False))
        self.assertEqual(
            run_mock.call_args.args[0],
            ["pw-metadata", "-n", "filters", "88", "filter.smart.disabled", "true", "Spa:String:JSON"],
        )


class FragmentSafetyTests(unittest.TestCase):
    def setUp(self):
        peqctl.WP_FRAGMENT.parent.mkdir(parents=True, exist_ok=True)
        peqctl.WP_FRAGMENT.unlink(missing_ok=True)

    def test_write_fragment_refuses_to_overwrite_foreign_file(self):
        peqctl.WP_FRAGMENT.write_text("# owned by something else\n", encoding="utf-8")
        with self.assertRaisesRegex(peqctl.PeqError, "unrecognized WirePlumber fragment"):
            peqctl.write_fragment(peqctl.default_state())
        self.assertEqual(peqctl.WP_FRAGMENT.read_text(encoding="utf-8"), "# owned by something else\n")

    def test_fragment_version_participates_in_runtime_identity(self):
        state = peqctl.default_state()
        old_fragment = peqctl.fragment_text(state).replace(
            f"# fragment-version: {peqctl.FRAGMENT_VERSION}", "# fragment-version: 0"
        )
        peqctl.WP_FRAGMENT.write_text(old_fragment, encoding="utf-8")
        self.assertNotEqual(peqctl.current_fragment_identity(), peqctl.fragment_identity(state))

    @mock.patch.object(peqctl, "restart_wireplumber")
    def test_failed_rebuild_restores_previous_fragment(self, restart_mock):
        previous_state = peqctl.default_state()
        previous = peqctl.fragment_text(previous_state).replace(
            f"# fragment-version: {peqctl.FRAGMENT_VERSION}", "# fragment-version: 0"
        )
        peqctl.WP_FRAGMENT.write_text(previous, encoding="utf-8")
        next_state = copy.deepcopy(previous_state)
        restart_mock.side_effect = [peqctl.PeqError("restart failed"), None]

        with self.assertRaisesRegex(peqctl.PeqError, "restart failed"):
            peqctl.apply_state(next_state, peqctl.topology_signature(previous_state))

        self.assertEqual(peqctl.WP_FRAGMENT.read_text(encoding="utf-8"), previous)
        self.assertEqual(restart_mock.call_count, 2)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        peqctl.STATE_DIR.mkdir(parents=True, exist_ok=True)
        peqctl.STATE_FILE.unlink(missing_ok=True)
        peqctl.WP_FRAGMENT.parent.mkdir(parents=True, exist_ok=True)
        peqctl.WP_FRAGMENT.unlink(missing_ok=True)
        peqctl.SHELL_CONFIG.unlink(missing_ok=True)

    def write_shell_config(self, value):
        peqctl.SHELL_CONFIG.write_text(json.dumps(value), encoding="utf-8")

    def test_shell_config_detects_enabled_bar_widget(self):
        self.write_shell_config(
            {"bar": {"layout": {"left": [], "center": [], "right": [{"id": peqctl.PLUGIN_ID}]}}}
        )
        self.assertTrue(peqctl.plugin_enabled_in_shell_config())

    def test_shell_config_detects_removed_bar_widget(self):
        self.write_shell_config({"bar": {"layout": {"left": [], "center": [], "right": []}}})
        self.assertFalse(peqctl.plugin_enabled_in_shell_config())

    @mock.patch.object(peqctl, "plugin_enabled_in_shell_config", side_effect=[True, False])
    def test_unload_waits_for_shell_config_write(self, enabled_mock):
        self.assertTrue(peqctl.wait_for_plugin_disabled(timeout=0.2))
        self.assertEqual(enabled_mock.call_count, 2)

    @mock.patch.object(peqctl, "wait_for_node_absent", return_value=True)
    @mock.patch.object(peqctl, "restart_wireplumber")
    @mock.patch.object(peqctl, "find_node", return_value={"id": 91})
    @mock.patch.object(peqctl, "set_bypass", return_value=True)
    def test_cleanup_bypasses_and_removes_owned_graph(self, bypass_mock, _node_mock, restart_mock, _wait_mock):
        state = peqctl.default_state()
        state["enabled"] = True
        peqctl.save_state(state)
        peqctl.WP_FRAGMENT.write_text(peqctl.fragment_text(state), encoding="utf-8")

        self.assertEqual(peqctl.cleanup_audio_graph(state), "removed")

        bypass_mock.assert_called_once_with(False)
        restart_mock.assert_called_once_with()
        self.assertFalse(peqctl.WP_FRAGMENT.exists())
        self.assertFalse(json.loads(peqctl.STATE_FILE.read_text(encoding="utf-8"))["enabled"])

    @mock.patch.object(peqctl, "wait_for_node", return_value={"id": 72})
    @mock.patch.object(peqctl, "restart_wireplumber")
    @mock.patch.object(peqctl, "find_node", return_value={"id": 72})
    @mock.patch.object(peqctl, "set_live_controls", return_value=True)
    @mock.patch.object(peqctl, "set_bypass", side_effect=[False, True])
    @mock.patch.object(peqctl, "write_fragment")
    @mock.patch.object(peqctl, "current_fragment_identity")
    def test_failed_live_bypass_forces_verified_rebuild(
        self,
        identity_mock,
        write_mock,
        bypass_mock,
        _controls_mock,
        _node_mock,
        restart_mock,
        _wait_mock,
    ):
        state = peqctl.default_state()
        state["enabled"] = True
        identity_mock.return_value = peqctl.fragment_identity(state)
        write_mock.return_value = peqctl.fragment_text(state)

        self.assertEqual(peqctl.apply_state(state), "rebuilt")

        restart_mock.assert_called_once_with()
        self.assertEqual(bypass_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
