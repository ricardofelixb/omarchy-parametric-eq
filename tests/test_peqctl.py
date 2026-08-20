import copy
import importlib.machinery
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


TEST_ROOT = tempfile.TemporaryDirectory()
os.environ["OMARCHY_PEQ_STATE_DIR"] = str(Path(TEST_ROOT.name) / "state")
os.environ["OMARCHY_PEQ_WP_FRAGMENT"] = str(Path(TEST_ROOT.name) / "wireplumber.conf")
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

    def test_topology_only_changes_for_structural_edits(self):
        original = peqctl.topology_signature(self.state)
        gain_change = copy.deepcopy(self.state)
        gain_change["bands"][3]["gain"] = 8
        self.assertEqual(original, peqctl.topology_signature(gain_change))
        type_change = copy.deepcopy(self.state)
        type_change["bands"][3]["type"] = "notch"
        self.assertNotEqual(original, peqctl.topology_signature(type_change))
        non_gain_filter = copy.deepcopy(self.state)
        non_gain_filter["bands"][3]["type"] = "notch"
        active_notch = peqctl.topology_signature(non_gain_filter)
        non_gain_filter["bands"][3]["enabled"] = False
        self.assertNotEqual(active_notch, peqctl.topology_signature(non_gain_filter))

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


class RuntimeCommandTests(unittest.TestCase):
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
        self.assertIn('"band_10:Q"', command[4])
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


if __name__ == "__main__":
    unittest.main()
