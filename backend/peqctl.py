#!/usr/bin/env python3
"""PipeWire controller for the Omarchy Parametric EQ plugin.

The program owns a small JSON state file and renders a WirePlumber smart-filter
fragment. Numeric edits are applied to the live filter-chain; topology changes
(filter type / per-band bypass) rebuild the graph safely.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator


PLUGIN_ID = "io.github.prudhviy99.parametric-eq"
SMART_NAME = "filter.sink.omarchy-parametric-eq"
NODE_NAME = "input.omarchy.parametric-eq"
# Bump whenever fragment structure or persistent properties change so an update
# rebuilds the live graph even when its band topology is unchanged.
FRAGMENT_VERSION = 4
FRAGMENT_MARKER = f"# Managed by {PLUGIN_ID}; manual changes are overwritten."
# Local patch: pin the EQ to the UR28M's first output pair (its ports are aux0..aux9).
TARGET_SINK = "alsa_output.usb-Yamaha_Corporation_Steinberg_UR28M-01.multichannel-output"
TARGET_POSITION = "[ AUX0, AUX1 ]"
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
# Every band is a bq_raw biquad fed coefficients computed here, so changing a
# band's type or disabling it is a live update instead of a graph rebuild.
COEFFICIENT_NAMES = ("b0", "b1", "b2", "a0", "a1", "a2")
COEFFICIENT_LIMIT = 9.9  # bq_raw clamps each coefficient port to -10..10
CONFIG_RATES = (44100, 48000, 88200, 96000, 176400, 192000)
DEFAULT_FREQUENCIES = [31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
MAX_STATE_BYTES = 256 * 1024
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 8 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024


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


def _ur28m_room() -> dict[str, Any]:
    state = default_state()
    state["bands"][0].update(type="low-shelf", frequency=126.0, q=0.7071, gain=0.0)
    state["bands"][1].update(type="bell", frequency=126.0, q=1.5, gain=-10.0)
    return state


BUILTIN_PRESETS = {
    "Flat": default_state(),
    "UR28M Room": _ur28m_room(),
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
WP_FRAGMENT = Path(
    os.environ.get(
        "OMARCHY_PEQ_WP_FRAGMENT",
        config_home() / "wireplumber" / "wireplumber.conf.d" / "60-omarchy-parametric-eq.conf",
    )
)
SHELL_CONFIG = Path(
    os.environ.get("OMARCHY_PEQ_SHELL_CONFIG", Path.home() / ".config" / "omarchy" / "shell.json")
)


class PeqError(RuntimeError):
    pass


class _OutputLimitExceeded(RuntimeError):
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


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _open_state_directory() -> int:
    try:
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_fd = os.open(STATE_DIR, flags)
    except OSError as exc:
        raise PeqError(f"could not securely open state directory {STATE_DIR}: {exc}") from exc

    try:
        metadata = os.fstat(directory_fd)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise PeqError(f"state directory must be a user-owned real directory: {STATE_DIR}")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            os.fchmod(directory_fd, 0o700)
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


@contextmanager
def locked_state_directory() -> Iterator[int]:
    """Lock the private state directory itself; no substitutable lock path."""
    directory_fd = _open_state_directory()
    try:
        fcntl.flock(directory_fd, fcntl.LOCK_EX)
        yield directory_fd
    finally:
        try:
            fcntl.flock(directory_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(directory_fd)


def _read_bounded_text_fd(
    file_fd: int,
    *,
    path: Path,
    max_bytes: int,
    require_owner: bool,
    require_single_link: bool,
    make_private: bool = False,
) -> str:
    metadata = os.fstat(file_fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise PeqError(f"refusing to read non-regular file: {path}")
    if require_owner and metadata.st_uid != os.geteuid():
        raise PeqError(f"refusing to read file not owned by the current user: {path}")
    if require_single_link and metadata.st_nlink != 1:
        raise PeqError(f"refusing to read multiply-linked file: {path}")
    if metadata.st_size > max_bytes:
        raise PeqError(f"file exceeds the {max_bytes}-byte limit: {path}")
    if make_private and stat.S_IMODE(metadata.st_mode) != 0o600:
        os.fchmod(file_fd, 0o600)

    chunks = bytearray()
    while len(chunks) <= max_bytes:
        chunk = os.read(file_fd, min(READ_CHUNK_BYTES, max_bytes + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
    if len(chunks) > max_bytes:
        raise PeqError(f"file exceeds the {max_bytes}-byte limit: {path}")
    try:
        return chunks.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PeqError(f"file is not valid UTF-8: {path}") from exc


def read_bounded_text(
    path: Path,
    max_bytes: int,
    *,
    require_owner: bool = True,
    require_single_link: bool = True,
) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        file_fd = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise PeqError(f"could not securely open {path}: {exc}") from exc
    try:
        return _read_bounded_text_fd(
            file_fd,
            path=path,
            max_bytes=max_bytes,
            require_owner=require_owner,
            require_single_link=require_single_link,
        )
    finally:
        os.close(file_fd)


def _read_state_text(directory_fd: int) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        file_fd = os.open(STATE_FILE.name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise PeqError(f"could not securely open state file {STATE_FILE}: {exc}") from exc
    try:
        return _read_bounded_text_fd(
            file_fd,
            path=STATE_FILE,
            max_bytes=MAX_STATE_BYTES,
            require_owner=True,
            require_single_link=True,
            make_private=True,
        )
    finally:
        os.close(file_fd)


def _atomic_state_json(directory_fd: int, value: Any) -> None:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > MAX_STATE_BYTES:
        raise PeqError(f"state exceeds the {MAX_STATE_BYTES}-byte limit")

    temporary = f".{STATE_FILE.name}.{secrets.token_hex(12)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    file_fd = -1
    replaced = False
    try:
        file_fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        view = memoryview(payload)
        while view:
            written = os.write(file_fd, view)
            view = view[written:]
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = -1
        os.replace(temporary, STATE_FILE.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        replaced = True
        os.fsync(directory_fd)
    except OSError as exc:
        raise PeqError(f"could not securely write state file {STATE_FILE}: {exc}") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if not replaced:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def load_state(directory_fd: int | None = None) -> dict[str, Any]:
    owns_directory_fd = directory_fd is None
    if directory_fd is None:
        directory_fd = _open_state_directory()
    try:
        try:
            content = _read_state_text(directory_fd)
        except FileNotFoundError:
            state = default_state()
            _atomic_state_json(directory_fd, state)
            return state
        try:
            return sanitize_state(json.loads(content))
        except (json.JSONDecodeError, PeqError) as exc:
            state = default_state()
            _atomic_state_json(directory_fd, state)
            print(f"warning: replaced invalid state ({exc})", file=sys.stderr)
            return state
    except PeqError as exc:
        state = default_state()
        _atomic_state_json(directory_fd, state)
        print(f"warning: replaced unsafe state ({exc})", file=sys.stderr)
        return state
    finally:
        if owns_directory_fd:
            os.close(directory_fd)


def save_state(state: dict[str, Any], directory_fd: int | None = None) -> None:
    owns_directory_fd = directory_fd is None
    if directory_fd is None:
        directory_fd = _open_state_directory()
    try:
        _atomic_state_json(directory_fd, sanitize_state(state))
    finally:
        if owns_directory_fd:
            os.close(directory_fd)


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _bounded_process_output(
    process: subprocess.Popen[bytes], timeout: float, max_output_bytes: int
) -> tuple[bytes, bytes]:
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout
    assert process.stdout is not None and process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, stdout)
    selector.register(process.stderr, selectors.EVENT_READ, stderr)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(process.args, timeout)
            for key, _ in events:
                total = len(stdout) + len(stderr)
                allowance = max_output_bytes + 1 - total
                chunk = os.read(key.fd, min(READ_CHUNK_BYTES, max(1, allowance)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                key.data.extend(chunk)
                if len(stdout) + len(stderr) > max_output_bytes:
                    raise _OutputLimitExceeded
        remaining = max(0.0, deadline - time.monotonic())
        process.wait(timeout=remaining)
        return bytes(stdout), bytes(stderr)
    finally:
        selector.close()


def run(
    command: list[str],
    *,
    check: bool = True,
    timeout: float = 15,
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
) -> subprocess.CompletedProcess[str]:
    if max_output_bytes < 1:
        raise ValueError("max_output_bytes must be positive")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
    except FileNotFoundError as exc:
        raise PeqError(f"required command not found: {command[0]}") from exc
    except OSError as exc:
        raise PeqError(f"could not start {' '.join(command)}: {exc}") from exc

    try:
        stdout_bytes, stderr_bytes = _bounded_process_output(process, timeout, max_output_bytes)
    except _OutputLimitExceeded as exc:
        _terminate_process(process)
        raise PeqError(f"{' '.join(command)} output exceeded {max_output_bytes} bytes") from exc
    except subprocess.TimeoutExpired as exc:
        _terminate_process(process)
        raise PeqError(f"{' '.join(command)} timed out") from exc
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    completed = subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error").strip()
        raise PeqError(f"{' '.join(command)} failed: {detail}")
    return completed


def topology_signature(state: dict[str, Any]) -> str:
    topology = ["bq_raw"] * len(state["bands"])
    return hashlib.sha256(json.dumps(topology).encode()).hexdigest()[:16]


def fragment_identity(state: dict[str, Any]) -> str:
    return f"{FRAGMENT_VERSION}:{topology_signature(state)}"


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
        tables = []
        for rate in CONFIG_RATES:
            values = " ".join(f"{key} = {fmt(value)}" for key, value in zip(COEFFICIENT_NAMES, band_coefficients(item, rate)))
            tables.append(f"                {{ rate = {rate} {values} }}")
        nodes.extend(
            [
                "          {",
                "            type = builtin",
                f"            name = {name}",
                "            label = bq_raw",
                "            config = {",
                "              coefficients = [",
                *tables,
                "              ]",
                "            }",
                "          }",
            ]
        )
        links.append(f'          {{ output = "{previous}:Out" input = "{name}:In" }}')
        previous = name
    disabled = "false" if state["enabled"] else "true"
    return f'''{FRAGMENT_MARKER}
# fragment-version: {FRAGMENT_VERSION}
# topology-signature: {signature}
wireplumber.profiles = {{
  main = {{
    {SMART_NAME} = required
  }}
}}

wireplumber.components = [
  {{
    name = libpipewire-module-filter-chain, type = pw-module-client
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
        filter.smart.target = {{ node.name = "{TARGET_SINK}" }}
        node.always-process = true
        node.dont-reconnect = false
      }}
      playback.props = {{
        node.name = "output.omarchy.parametric-eq"
        audio.position = {TARGET_POSITION}
        stream.dont-remix = true
        node.dont-fallback = true
        node.linger = true
        node.passive = true
        media.role = "DSP"
      }}
    }}
    provides = {SMART_NAME}
    requires = [ support.client-context ]
  }}
]
'''


def managed_fragment_text() -> str | None:
    try:
        content = read_bounded_text(WP_FRAGMENT, MAX_CONFIG_BYTES)
    except FileNotFoundError:
        return None
    if content.splitlines()[:1] != [FRAGMENT_MARKER]:
        raise PeqError(f"refusing to overwrite unrecognized WirePlumber fragment: {WP_FRAGMENT}")
    return content


def current_fragment_identity() -> str | None:
    try:
        content = managed_fragment_text()
    except PeqError:
        return None
    if content is None:
        return None
    version = re.search(r"^# fragment-version: (\d+)$", content, re.M)
    signature = re.search(r"^# topology-signature: (\S+)$", content, re.M)
    if not version or not signature:
        return None
    return f"{version.group(1)}:{signature.group(1)}"


def write_fragment(state: dict[str, Any]) -> str | None:
    previous = managed_fragment_text()
    content = fragment_text(state)
    if content != previous:
        atomic_text(WP_FRAGMENT, content)
    return previous


def restore_fragment(previous: str | None) -> None:
    current = managed_fragment_text()
    if previous is None:
        if current is not None:
            WP_FRAGMENT.unlink()
    elif current != previous:
        atomic_text(WP_FRAGMENT, previous)


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


def wait_for_node_absent(timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if find_node() is None:
            return True
        time.sleep(0.15)
    return False


def graph_rate() -> int:
    result = run(["pw-metadata", "-n", "settings", "0", "clock.rate"], check=False)
    match = re.search(r"value:'(\d+)'", result.stdout or "")
    return int(match.group(1)) if result.returncode == 0 and match else 48000


def live_controls(state: dict[str, Any], rate: int | None = None) -> dict[str, float]:
    rate = rate or graph_rate()
    controls = {"preamp:Mult": 10 ** (effective_preamp(state) / 20)}
    for index, item in enumerate(state["bands"], 1):
        for key, value in zip(COEFFICIENT_NAMES, band_coefficients(item, rate)):
            controls[f"band_{index}:{key}"] = value
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
    installed = current_fragment_identity()
    previous_fragment = write_fragment(state)
    fragment_changed = previous_fragment != fragment_text(state)
    needs_restart = installed != fragment_identity(state) or old_signature not in (None, desired)
    restart_attempted = False

    try:
        if needs_restart or not find_node():
            restart_attempted = True
            restart_wireplumber()
            if not wait_for_node():
                raise PeqError("parametric EQ node was not created by WirePlumber")
            mode = "rebuilt"
        else:
            mode = "live"

        if not set_live_controls(state):
            restart_attempted = True
            restart_wireplumber()
            mode = "rebuilt"
            if not wait_for_node():
                raise PeqError("parametric EQ node was not created by WirePlumber")
            if not set_live_controls(state):
                raise PeqError("parametric EQ node did not expose writable controls")

        if not set_bypass(state["enabled"]):
            restart_attempted = True
            restart_wireplumber()
            mode = "rebuilt"
            if not wait_for_node():
                raise PeqError("parametric EQ node was not created by WirePlumber")
            if not set_live_controls(state):
                raise PeqError("parametric EQ node did not expose writable controls")
            if not set_bypass(state["enabled"]):
                raise PeqError("parametric EQ node did not accept the master bypass state")
        return mode
    except PeqError as exc:
        rollback_errors = []
        if fragment_changed:
            try:
                restore_fragment(previous_fragment)
            except (OSError, PeqError) as rollback_exc:
                rollback_errors.append(f"fragment rollback failed: {rollback_exc}")
        if restart_attempted and not rollback_errors:
            try:
                restart_wireplumber()
            except PeqError as rollback_exc:
                rollback_errors.append(f"WirePlumber rollback failed: {rollback_exc}")
        detail = f"; {'; '.join(rollback_errors)}" if rollback_errors else ""
        raise PeqError(f"{exc}{detail}") from exc


def shell_entry_id(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("id", ""))
    return str(entry or "")


def plugin_enabled_in_shell_config() -> bool:
    try:
        config = json.loads(read_bounded_text(SHELL_CONFIG, MAX_CONFIG_BYTES))
    except FileNotFoundError:
        return False
    except (PeqError, json.JSONDecodeError) as exc:
        raise PeqError(f"could not determine plugin state from {SHELL_CONFIG}: {exc}") from exc
    if not isinstance(config, dict):
        raise PeqError(f"could not determine plugin state: {SHELL_CONFIG} is not a JSON object")

    bar_config = config.get("bar", {})
    if isinstance(bar_config, dict):
        if shell_entry_id(bar_config.get("id")) == PLUGIN_ID:
            return True
        layout = bar_config.get("layout", {})
        if isinstance(layout, dict):
            for entries in layout.values():
                if isinstance(entries, list) and any(shell_entry_id(entry) == PLUGIN_ID for entry in entries):
                    return True
    plugins = config.get("plugins", [])
    return isinstance(plugins, list) and any(shell_entry_id(entry) == PLUGIN_ID for entry in plugins)


def wait_for_plugin_disabled(timeout: float = 2.0) -> bool:
    """Allow Omarchy's atomic shell.json write to catch up with QML teardown."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if not plugin_enabled_in_shell_config():
                return True
        except PeqError:
            # Atomic replacement is normally immediate, but a transient read
            # failure must never be interpreted as authorization to clean up.
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def cleanup_audio_graph(state: dict[str, Any], state_directory_fd: int | None = None) -> str:
    fragment = managed_fragment_text()
    state["enabled"] = False
    save_state(state, state_directory_fd)

    node = None
    try:
        node = find_node()
    except PeqError:
        pass
    if node is not None:
        try:
            set_bypass(False)
        except PeqError:
            pass

    if fragment is not None:
        try:
            WP_FRAGMENT.unlink()
        except OSError as exc:
            raise PeqError(f"could not remove WirePlumber fragment {WP_FRAGMENT}: {exc}") from exc

    if fragment is None and node is None:
        return "already-clean"

    restart_wireplumber()
    if not wait_for_node_absent():
        raise PeqError("parametric EQ node remained after cleanup")
    return "removed"


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
        root = 2 * math.sqrt(a) * alpha
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


def band_coefficients(item: dict[str, Any], sample_rate: float) -> tuple[float, ...]:
    """Unnormalized bq_raw coefficients; a disabled band passes audio through."""
    if not item["enabled"]:
        return 1.0, 0.0, 0.0, 1.0, 0.0, 0.0
    b0, b1, b2, a1, a2 = biquad_coefficients(item, sample_rate)
    values = (b0, b1, b2, 1.0, a1, a2)
    # bq_raw divides by a0, so shrinking all six keeps the filter identical.
    scale = max(1.0, max(abs(value) for value in values) / COEFFICIENT_LIMIT)
    return tuple(value / scale for value in values)


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
            raw = json.loads(read_bounded_text(path, MAX_STATE_BYTES))
            values[str(raw["name"])[:80]] = sanitize_state(raw["state"])
        except (FileNotFoundError, KeyError, json.JSONDecodeError, PeqError):
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
    sub.add_parser("cleanup")
    sub.add_parser("cleanup-if-disabled")
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
        path = Path(args.path).expanduser()
        try:
            autoeq_text = read_bounded_text(
                path,
                1_000_000,
                require_owner=False,
                require_single_link=False,
            )
        except FileNotFoundError as exc:
            raise PeqError(f"AutoEQ input does not exist: {path}") from exc
        imported = parse_autoeq(autoeq_text)
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
        "fragment_current": current_fragment_identity() == fragment_identity(state),
        "node_present": find_node() is not None,
    }


def main() -> int:
    args = parser().parse_args()
    if args.command in {"cleanup", "cleanup-if-disabled"} and not STATE_DIR.exists() \
            and not WP_FRAGMENT.exists() and not WP_FRAGMENT.is_symlink():
        print(json.dumps({"cleanup": "already-clean"}, separators=(",", ":")))
        return 0
    with locked_state_directory() as state_directory_fd:
        state = load_state(state_directory_fd)
        try:
            if args.command == "cleanup-if-disabled" and not wait_for_plugin_disabled():
                print(json.dumps({"cleanup": "skipped", "reason": "plugin-enabled"}, separators=(",", ":")))
                return 0
            if args.command in {"cleanup", "cleanup-if-disabled"}:
                mode = cleanup_audio_graph(state, state_directory_fd)
                print(json.dumps({"cleanup": mode}, separators=(",", ":")))
                return 0
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
                mode = apply_state(state, previous_signature)
                save_state(state, state_directory_fd)
            payload = status_payload(state)
            payload["apply_mode"] = mode
            print(json.dumps(payload, separators=(",", ":")))
            return 0
        except PeqError as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
