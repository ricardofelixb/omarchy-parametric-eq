#!/usr/bin/env python3
"""PipeWire controller for the Omarchy Parametric EQ plugin.

The program owns a small JSON state file and renders a WirePlumber smart-filter
fragment. Numeric edits are applied to the live filter-chain; topology changes
(filter type / per-band bypass) rebuild the graph safely.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any


PLUGIN_ID = "io.github.prudhviy99.parametric-eq"
SMART_NAME = "filter.sink.omarchy-parametric-eq"
NODE_NAME = "input.omarchy.parametric-eq"
FRAGMENT_VERSION = 1
TYPE_LABELS = {
    "bell": "bq_peaking",
    "low-shelf": "bq_lowshelf",
    "high-shelf": "bq_highshelf",
    "low-pass": "bq_lowpass",
    "high-pass": "bq_highpass",
    "notch": "bq_notch",
    "band-pass": "bq_bandpass",
}
GAIN_TYPES = {"bell", "low-shelf", "high-shelf"}
DEFAULT_FREQUENCIES = [31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]


def band(index: int, frequency: float, kind: str = "bell") -> dict[str, Any]:
    return {
        "id": index + 1,
        "enabled": True,
        "type": kind,
        "frequency": float(frequency),
        "gain": 0.0,
        "q": 0.707,
    }


def default_state() -> dict[str, Any]:
    bands = [band(i, value) for i, value in enumerate(DEFAULT_FREQUENCIES)]
    bands[0]["type"] = "low-shelf"
    bands[-1]["type"] = "high-shelf"
    return {
        "version": 1,
        "enabled": False,
        "preamp_db": 0.0,
        "auto_preamp": False,
        "preset": "Flat",
        "bands": bands,
        "snapshots": {"A": None, "B": None},
        "history": [],
    }


def _preset(gains: list[float], preamp: float = 0.0) -> dict[str, Any]:
    state = default_state()
    state["preamp_db"] = preamp
    for item, gain in zip(state["bands"], gains):
        item["gain"] = gain
    return state


BUILTIN_PRESETS = {
    "Flat": default_state(),
    "Bass Boost": _preset([5, 4, 3, 2, 1, 0, -0.5, -1, -1, -1], -5),
    "Bass Cut": _preset([-5, -4, -3, -2, -1, 0, 0, 0, 0, 0]),
    "Treble Boost": _preset([-1, -1, -0.5, 0, 0, 1, 2, 3, 4, 5], -5),
    "Treble Cut": _preset([0, 0, 0, 0, 0, -1, -2, -3, -4, -5]),
    "Vocal Clarity": _preset([-2, -1, 0, -1, 0.5, 2.5, 3, 1, -1, -2], -3),
    "Loudness": _preset([4, 3, 2, 0.5, -1, -1, 0, 1.5, 3, 4], -4),
}


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))


STATE_DIR = Path(os.environ.get("OMARCHY_PEQ_STATE_DIR", config_home() / "omarchy" / "parametric-eq"))
STATE_FILE = STATE_DIR / "state.json"
PRESET_DIR = STATE_DIR / "presets"
LOCK_FILE = STATE_DIR / ".lock"
WP_FRAGMENT = Path(
    os.environ.get(
        "OMARCHY_PEQ_WP_FRAGMENT",
        config_home() / "wireplumber" / "wireplumber.conf.d" / "60-omarchy-parametric-eq.conf",
    )
)


class PeqError(RuntimeError):
    pass


def clamp(value: Any, low: float, high: float, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PeqError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise PeqError(f"{name} must be finite")
    return max(low, min(high, number))


def as_bool(value: Any, name: str = "value") -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise PeqError(f"{name} must be true or false")


def sanitize_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PeqError("state must be a JSON object")
    result = default_state()
    result["enabled"] = as_bool(raw.get("enabled", result["enabled"]), "enabled")
    result["preamp_db"] = clamp(raw.get("preamp_db", 0), -30, 12, "preamp")
    result["auto_preamp"] = as_bool(raw.get("auto_preamp", False), "auto_preamp")
    result["preset"] = str(raw.get("preset", "Custom"))[:80]

    source_bands = raw.get("bands", result["bands"])
    if not isinstance(source_bands, list) or not 1 <= len(source_bands) <= 16:
        raise PeqError("bands must contain between 1 and 16 entries")
    clean_bands = []
    for index, source in enumerate(source_bands):
        if not isinstance(source, dict):
            raise PeqError(f"band {index + 1} must be an object")
        kind = str(source.get("type", "bell"))
        if kind not in TYPE_LABELS:
            raise PeqError(f"unsupported filter type: {kind}")
        clean_bands.append(
            {
                "id": index + 1,
                "enabled": as_bool(source.get("enabled", True), f"band {index + 1} enabled"),
                "type": kind,
                "frequency": clamp(source.get("frequency", 1000), 10, 24000, "frequency"),
                "gain": clamp(source.get("gain", 0), -24, 20, "gain"),
                "q": clamp(source.get("q", 0.707), 0.1, 10, "Q"),
            }
        )
    result["bands"] = clean_bands

    snapshots = raw.get("snapshots", {})
    result["snapshots"] = {"A": None, "B": None}
    if isinstance(snapshots, dict):
        for slot in ("A", "B"):
            snap = snapshots.get(slot)
            if isinstance(snap, dict):
                result["snapshots"][slot] = snapshot_payload(snap)
    history = raw.get("history", [])
    if isinstance(history, list):
        result["history"] = [snapshot_payload(item) for item in history[-10:] if isinstance(item, dict)]
    return result


def snapshot_payload(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "preamp_db": clamp(state.get("preamp_db", 0), -30, 12, "preamp"),
        "auto_preamp": as_bool(state.get("auto_preamp", False), "auto_preamp"),
        "bands": sanitize_state({"bands": state.get("bands", default_state()["bands"])})["bands"],
    }


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_state() -> dict[str, Any]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        state = default_state()
        atomic_json(STATE_FILE, state)
        return state
    try:
        return sanitize_state(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, PeqError) as exc:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        quarantine = STATE_FILE.with_name(f"state.invalid-{stamp}.json")
        try:
            STATE_FILE.replace(quarantine)
        except OSError:
            pass
        state = default_state()
        atomic_json(STATE_FILE, state)
        print(f"warning: replaced invalid state ({exc}); old file: {quarantine}", file=sys.stderr)
        return state


def save_state(state: dict[str, Any]) -> None:
    atomic_json(STATE_FILE, sanitize_state(state))


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=check, text=True, capture_output=True, timeout=15)
    except FileNotFoundError as exc:
        raise PeqError(f"required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown error").strip()
        raise PeqError(f"{' '.join(command)} failed: {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PeqError(f"{' '.join(command)} timed out") from exc


def topology_signature(state: dict[str, Any]) -> str:
    topology = [TYPE_LABELS[item["type"]] if item["enabled"] else "bq_peaking" for item in state["bands"]]
    return hashlib.sha256(json.dumps(topology).encode()).hexdigest()[:16]


def fmt(value: float) -> str:
    return f"{float(value):.8g}"


def effective_preamp(state: dict[str, Any]) -> float:
    if not state["auto_preamp"]:
        return float(state["preamp_db"])
    return min(0.0, -max(0.0, estimated_peak_db(state, include_preamp=False)))


def fragment_text(state: dict[str, Any]) -> str:
    signature = topology_signature(state)
    nodes = [
        "          {",
        "            type = builtin",
        "            name = preamp",
        "            label = linear",
        f'            control = {{ "Mult" = {fmt(10 ** (effective_preamp(state) / 20))} "Add" = 0.0 }}',
        "          }",
    ]
    links = []
    previous = "preamp"
    for index, item in enumerate(state["bands"], 1):
        name = f"band_{index}"
        label = TYPE_LABELS[item["type"]] if item["enabled"] else "bq_peaking"
        gain = item["gain"] if item["enabled"] and item["type"] in GAIN_TYPES else 0.0
        nodes.extend(
            [
                "          {",
                "            type = builtin",
                f"            name = {name}",
                f"            label = {label}",
                f'            control = {{ "Freq" = {fmt(item["frequency"])} "Q" = {fmt(item["q"])} "Gain" = {fmt(gain)} }}',
                "          }",
            ]
        )
        links.append(f'          {{ output = "{previous}:Out" input = "{name}:In" }}')
        previous = name
    disabled = "false" if state["enabled"] else "true"
    return f'''# Managed by {PLUGIN_ID}; manual changes are overwritten.
# fragment-version: {FRAGMENT_VERSION}
# topology-signature: {signature}
wireplumber.profiles = {{
  main = {{
    {SMART_NAME} = required
  }}
}}

wireplumber.components = [
  {{
    name = libpipewire-module-filter-chain, type = pw-module
    arguments = {{
      node.name = "{SMART_NAME}"
      node.description = "Omarchy Parametric EQ"
      media.name = "Omarchy Parametric EQ"
      filter.graph = {{
        nodes = [
{chr(10).join(nodes)}
        ]
        links = [
{chr(10).join(links)}
        ]
      }}
      audio.channels = 2
      audio.position = [ FL, FR ]
      capture.props = {{
        node.name = "{NODE_NAME}"
        node.description = "Omarchy Parametric EQ"
        media.class = Audio/Sink
        filter.smart = true
        filter.smart.name = "{SMART_NAME}"
        filter.smart.disabled = {disabled}
        node.always-process = true
        node.dont-reconnect = false
      }}
      playback.props = {{
        node.name = "output.omarchy.parametric-eq"
        node.passive = true
        media.role = "DSP"
      }}
    }}
    provides = {SMART_NAME}
  }}
]
'''


def current_fragment_signature() -> str | None:
    try:
        match = re.search(r"^# topology-signature: (\S+)$", WP_FRAGMENT.read_text(encoding="utf-8"), re.M)
        return match.group(1) if match else None
    except OSError:
        return None


def write_fragment(state: dict[str, Any]) -> None:
    WP_FRAGMENT.parent.mkdir(parents=True, exist_ok=True)
    content = fragment_text(state)
    fd, temporary = tempfile.mkstemp(prefix=f".{WP_FRAGMENT.name}.", dir=WP_FRAGMENT.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, WP_FRAGMENT)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def restart_wireplumber() -> None:
    result = run(["systemctl", "--user", "restart", "wireplumber.service"], check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PeqError(f"WirePlumber restart failed: {detail}")
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        status = run(["systemctl", "--user", "is-active", "wireplumber.service"], check=False)
        if status.stdout.strip() == "active":
            time.sleep(0.35)
            return
        time.sleep(0.1)
    raise PeqError("WirePlumber did not become active")


def pipewire_objects() -> list[dict[str, Any]]:
    result = run(["pw-dump"], check=False)
    if result.returncode != 0:
        return []
    try:
        value = json.loads(result.stdout)
        return value if isinstance(value, list) else []
    except json.JSONDecodeError:
        return []


def find_node() -> dict[str, Any] | None:
    for obj in pipewire_objects():
        info = obj.get("info", {})
        props = info.get("props", {}) if isinstance(info, dict) else {}
        if props.get("node.name") == NODE_NAME:
            return obj
    return None


def wait_for_node(timeout: float = 8.0) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        node = find_node()
        if node:
            return node
        time.sleep(0.15)
    return None


def live_controls(state: dict[str, Any]) -> dict[str, float]:
    controls = {"preamp:Mult": 10 ** (effective_preamp(state) / 20)}
    for index, item in enumerate(state["bands"], 1):
        controls[f"band_{index}:Freq"] = item["frequency"]
        controls[f"band_{index}:Q"] = item["q"]
        controls[f"band_{index}:Gain"] = item["gain"] if item["enabled"] and item["type"] in GAIN_TYPES else 0.0
    return controls


def set_live_controls(state: dict[str, Any]) -> bool:
    node = find_node()
    if not node:
        return False
    values = []
    for key, value in live_controls(state).items():
        values.extend([json.dumps(key), fmt(value)])
    payload = "{ params = [ " + " ".join(values) + " ] }"
    result = run(["pw-cli", "set-param", str(node["id"]), "Props", payload], check=False)
    return result.returncode == 0


def set_bypass(enabled: bool) -> bool:
    node = find_node()
    if not node:
        return False
    value = "false" if enabled else "true"
    result = run(
        ["pw-metadata", "-n", "filters", str(node["id"]), "filter.smart.disabled", value, "Spa:String:JSON"],
        check=False,
    )
    return result.returncode == 0


def apply_state(state: dict[str, Any], old_signature: str | None = None) -> str:
    desired = topology_signature(state)
    installed = current_fragment_signature()
    needs_restart = installed != desired or old_signature not in (None, desired)
    write_fragment(state)
    if needs_restart or not find_node():
        restart_wireplumber()
        if not wait_for_node():
            raise PeqError("parametric EQ node was not created by WirePlumber")
        mode = "rebuilt"
    else:
        mode = "live"
    if not set_live_controls(state):
        restart_wireplumber()
        mode = "rebuilt"
        if not wait_for_node():
            raise PeqError("parametric EQ node was not created by WirePlumber")
        if not set_live_controls(state):
            raise PeqError("parametric EQ node did not expose writable controls")
    set_bypass(state["enabled"])
    return mode


def biquad_coefficients(item: dict[str, Any], sample_rate: float = 48000.0) -> tuple[float, ...]:
    frequency = min(float(item["frequency"]), sample_rate * 0.49)
    q = float(item["q"])
    gain = float(item["gain"])
    omega = 2 * math.pi * frequency / sample_rate
    sine, cosine = math.sin(omega), math.cos(omega)
    alpha = sine / (2 * q)
    a = 10 ** (gain / 40)
    kind = item["type"]
    if kind == "bell":
        b0, b1, b2 = 1 + alpha * a, -2 * cosine, 1 - alpha * a
        a0, a1, a2 = 1 + alpha / a, -2 * cosine, 1 - alpha / a
    elif kind in {"low-shelf", "high-shelf"}:
        shelf_alpha = sine / 2 * math.sqrt(max(0, (a + 1 / a) * (1 / q - 1) + 2))
        root = 2 * math.sqrt(a) * shelf_alpha
        if kind == "low-shelf":
            b0 = a * ((a + 1) - (a - 1) * cosine + root)
            b1 = 2 * a * ((a - 1) - (a + 1) * cosine)
            b2 = a * ((a + 1) - (a - 1) * cosine - root)
            a0 = (a + 1) + (a - 1) * cosine + root
            a1 = -2 * ((a - 1) + (a + 1) * cosine)
            a2 = (a + 1) + (a - 1) * cosine - root
        else:
            b0 = a * ((a + 1) + (a - 1) * cosine + root)
            b1 = -2 * a * ((a - 1) + (a + 1) * cosine)
            b2 = a * ((a + 1) + (a - 1) * cosine - root)
            a0 = (a + 1) - (a - 1) * cosine + root
            a1 = 2 * ((a - 1) - (a + 1) * cosine)
            a2 = (a + 1) - (a - 1) * cosine - root
    elif kind == "low-pass":
        b0, b1, b2 = (1 - cosine) / 2, 1 - cosine, (1 - cosine) / 2
        a0, a1, a2 = 1 + alpha, -2 * cosine, 1 - alpha
    elif kind == "high-pass":
        b0, b1, b2 = (1 + cosine) / 2, -(1 + cosine), (1 + cosine) / 2
        a0, a1, a2 = 1 + alpha, -2 * cosine, 1 - alpha
    elif kind == "notch":
        b0, b1, b2 = 1, -2 * cosine, 1
        a0, a1, a2 = 1 + alpha, -2 * cosine, 1 - alpha
    else:  # band-pass, constant 0 dB peak gain
        b0, b1, b2 = alpha, 0, -alpha
        a0, a1, a2 = 1 + alpha, -2 * cosine, 1 - alpha
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0


def response_db(state: dict[str, Any], frequency: float, include_preamp: bool = True) -> float:
    total = effective_preamp(state) if include_preamp else 0.0
    omega = 2 * math.pi * frequency / 48000.0
    z1 = complex(math.cos(-omega), math.sin(-omega))
    z2 = z1 * z1
    for item in state["bands"]:
        if not item["enabled"]:
            continue
        b0, b1, b2, a1, a2 = biquad_coefficients(item)
        numerator = b0 + b1 * z1 + b2 * z2
        denominator = 1 + a1 * z1 + a2 * z2
        magnitude = abs(numerator / denominator) if denominator else 0
        total += 20 * math.log10(max(magnitude, 1e-12))
    return total


def response_curve(state: dict[str, Any], points: int = 161, include_preamp: bool = True) -> list[list[float]]:
    return [
        [
            round(20 * (1000 ** (i / (points - 1))), 3),
            round(response_db(state, 20 * (1000 ** (i / (points - 1))), include_preamp), 3),
        ]
        for i in range(points)
    ]


def estimated_peak_db(state: dict[str, Any], include_preamp: bool = True) -> float:
    return max(value for _, value in response_curve(state, 321, include_preamp))


def user_presets() -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    if not PRESET_DIR.exists():
        return values
    for path in PRESET_DIR.glob("*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            values[str(raw["name"])[:80]] = sanitize_state(raw["state"])
        except (OSError, KeyError, json.JSONDecodeError, PeqError):
            continue
    return values


def preset_names() -> list[str]:
    return list(BUILTIN_PRESETS) + sorted(user_presets())


def safe_preset_filename(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")
    if not stem:
        raise PeqError("preset name cannot be empty")
    return stem[:64] + ".json"


AUTOEQ_LINE = re.compile(
    r"^Filter\s+\d+:\s*(ON|OFF)\s+(PK|LSC?|HSC?|LP|HP|NO|BP)\s+Fc\s+([\d.]+)\s*Hz(?:\s+Gain\s+([-+\d.]+)\s*dB)?\s+Q\s+([\d.]+)",
    re.I,
)
AUTOEQ_TYPES = {"PK": "bell", "LS": "low-shelf", "LSC": "low-shelf", "HS": "high-shelf", "HSC": "high-shelf", "LP": "low-pass", "HP": "high-pass", "NO": "notch", "BP": "band-pass"}


def parse_autoeq(text: str) -> dict[str, Any]:
    result = default_state()
    result["bands"] = []
    preamp_match = re.search(r"^Preamp:\s*([-+\d.]+)\s*dB", text, re.I | re.M)
    if preamp_match:
        result["preamp_db"] = clamp(preamp_match.group(1), -30, 12, "preamp")
    for line in text.splitlines():
        match = AUTOEQ_LINE.match(line.strip())
        if not match:
            continue
        enabled, code, frequency, gain, q = match.groups()
        result["bands"].append(
            {
                "id": len(result["bands"]) + 1,
                "enabled": enabled.upper() == "ON",
                "type": AUTOEQ_TYPES[code.upper()],
                "frequency": clamp(frequency, 10, 24000, "frequency"),
                "gain": clamp(gain or 0, -24, 20, "gain"),
                "q": clamp(q, 0.1, 10, "Q"),
            }
        )
    if not result["bands"]:
        raise PeqError("no supported AutoEQ filters found")
    result["preset"] = "Imported AutoEQ"
    return sanitize_state(result)


def status_payload(state: dict[str, Any]) -> dict[str, Any]:
    node = find_node()
    peak = estimated_peak_db(state)
    return {
        **state,
        "effective_preamp_db": round(effective_preamp(state), 3),
        "estimated_peak_db": round(peak, 3),
        "headroom_warning": peak > 0.1,
        "available_presets": preset_names(),
        "filter_types": list(TYPE_LABELS),
        "runtime": {
            "node_present": node is not None,
            "node_id": node.get("id") if node else None,
            "fragment": str(WP_FRAGMENT),
            "topology_signature": topology_signature(state),
        },
        "response": response_curve(state),
    }


def remember(state: dict[str, Any]) -> None:
    history = state.setdefault("history", [])
    history.append(snapshot_payload(state))
    del history[:-10]


def restore_payload(state: dict[str, Any], payload: dict[str, Any]) -> None:
    state["preamp_db"] = payload["preamp_db"]
    state["auto_preamp"] = payload["auto_preamp"]
    state["bands"] = copy.deepcopy(payload["bands"])
    state["preset"] = "Custom"


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Control the Omarchy Parametric EQ")
    sub = cli.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("diagnostics")
    sub.add_parser("ensure")
    sub.add_parser("enable")
    sub.add_parser("disable")
    sub.add_parser("toggle")
    sub.add_parser("reset")
    sub.add_parser("undo")
    preamp = sub.add_parser("set-preamp")
    preamp.add_argument("db", type=float)
    auto = sub.add_parser("set-auto-preamp")
    auto.add_argument("enabled")
    set_band = sub.add_parser("set-band")
    set_band.add_argument("index", type=int)
    set_band.add_argument("enabled")
    set_band.add_argument("type", choices=list(TYPE_LABELS))
    set_band.add_argument("frequency", type=float)
    set_band.add_argument("gain", type=float)
    set_band.add_argument("q", type=float)
    preset = sub.add_parser("preset")
    preset_sub = preset.add_subparsers(dest="preset_command", required=True)
    preset_sub.add_parser("list")
    preset_apply = preset_sub.add_parser("apply")
    preset_apply.add_argument("name")
    preset_save = preset_sub.add_parser("save")
    preset_save.add_argument("name")
    preset_delete = preset_sub.add_parser("delete")
    preset_delete.add_argument("name")
    autoeq = sub.add_parser("import-autoeq")
    autoeq.add_argument("path")
    autoeq.add_argument("--name", default="Imported AutoEQ")
    snapshot = sub.add_parser("snapshot")
    snapshot_sub = snapshot.add_subparsers(dest="snapshot_command", required=True)
    for operation in ("save", "apply"):
        item = snapshot_sub.add_parser(operation)
        item.add_argument("slot", choices=["A", "B"])
    return cli


def mutate(args: argparse.Namespace, state: dict[str, Any]) -> bool:
    command = args.command
    if command in {"enable", "disable", "toggle"}:
        state["enabled"] = command == "enable" or (command == "toggle" and not state["enabled"])
    elif command == "reset":
        enabled, snapshots = state["enabled"], state["snapshots"]
        remember(state)
        history = state["history"]
        state.update(default_state())
        state["enabled"], state["snapshots"], state["history"] = enabled, snapshots, history
    elif command == "undo":
        if not state["history"]:
            raise PeqError("nothing to undo")
        payload = state["history"].pop()
        restore_payload(state, payload)
    elif command == "set-preamp":
        remember(state)
        state["preamp_db"] = clamp(args.db, -30, 12, "preamp")
        state["auto_preamp"] = False
        state["preset"] = "Custom"
    elif command == "set-auto-preamp":
        remember(state)
        state["auto_preamp"] = as_bool(args.enabled)
        state["preset"] = "Custom"
    elif command == "set-band":
        if not 1 <= args.index <= len(state["bands"]):
            raise PeqError(f"band index must be 1..{len(state['bands'])}")
        remember(state)
        state["bands"][args.index - 1] = {
            "id": args.index,
            "enabled": as_bool(args.enabled),
            "type": args.type,
            "frequency": clamp(args.frequency, 10, 24000, "frequency"),
            "gain": clamp(args.gain, -24, 20, "gain"),
            "q": clamp(args.q, 0.1, 10, "Q"),
        }
        state["preset"] = "Custom"
    elif command == "preset":
        if args.preset_command == "list":
            print(json.dumps({"presets": preset_names()}))
            return False
        if args.preset_command == "apply":
            available = {**BUILTIN_PRESETS, **user_presets()}
            if args.name not in available:
                raise PeqError(f"unknown preset: {args.name}")
            remember(state)
            enabled, snapshots, history = state["enabled"], state["snapshots"], state["history"]
            state.update(copy.deepcopy(available[args.name]))
            state["enabled"], state["snapshots"], state["history"] = enabled, snapshots, history
            state["preset"] = args.name
        elif args.preset_command == "save":
            PRESET_DIR.mkdir(parents=True, exist_ok=True)
            atomic_json(PRESET_DIR / safe_preset_filename(args.name), {"name": args.name[:80], "state": snapshot_payload(state)})
            state["preset"] = args.name[:80]
        else:
            path = PRESET_DIR / safe_preset_filename(args.name)
            if not path.exists():
                raise PeqError("only user presets can be deleted")
            path.unlink()
            if state["preset"] == args.name:
                state["preset"] = "Custom"
    elif command == "import-autoeq":
        path = Path(args.path).expanduser().resolve()
        if not path.is_file() or path.stat().st_size > 1_000_000:
            raise PeqError("AutoEQ input must be a regular text file smaller than 1 MB")
        imported = parse_autoeq(path.read_text(encoding="utf-8"))
        remember(state)
        enabled, snapshots, history = state["enabled"], state["snapshots"], state["history"]
        state.update(imported)
        state["enabled"], state["snapshots"], state["history"] = enabled, snapshots, history
        state["preset"] = args.name[:80]
    elif command == "snapshot":
        if args.snapshot_command == "save":
            state["snapshots"][args.slot] = snapshot_payload(state)
        else:
            payload = state["snapshots"].get(args.slot)
            if not payload:
                raise PeqError(f"snapshot {args.slot} is empty")
            remember(state)
            restore_payload(state, payload)
    else:
        return False
    return True


def diagnostics(state: dict[str, Any]) -> dict[str, Any]:
    commands = {name: shutil.which(name) is not None for name in ("pw-cli", "pw-dump", "pw-metadata", "systemctl")}
    active = run(["systemctl", "--user", "is-active", "wireplumber.service"], check=False).stdout.strip()
    return {
        "ok": all(commands.values()) and active == "active" and find_node() is not None,
        "commands": commands,
        "wireplumber": active,
        "fragment_exists": WP_FRAGMENT.exists(),
        "fragment_current": current_fragment_signature() == topology_signature(state),
        "node_present": find_node() is not None,
    }


def main() -> int:
    args = parser().parse_args()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state()
        try:
            if args.command == "status":
                print(json.dumps(status_payload(state), separators=(",", ":")))
                return 0
            if args.command == "diagnostics":
                payload = diagnostics(state)
                print(json.dumps(payload, separators=(",", ":")))
                return 0 if payload["ok"] else 1
            if args.command == "ensure":
                mode = apply_state(state)
            else:
                previous_signature = topology_signature(state)
                if not mutate(args, state):
                    return 0
                state = sanitize_state(state)
                save_state(state)
                mode = apply_state(state, previous_signature)
            payload = status_payload(state)
            payload["apply_mode"] = mode
            print(json.dumps(payload, separators=(",", ":")))
            return 0
        except PeqError as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
