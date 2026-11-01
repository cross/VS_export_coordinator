#!/usr/bin/env python3
"""Interactively coordinate, monitor, and verify Vintage Story WorldEdit exports.

This program never edits terrain. It writes `/we select` and `/we export`
commands to a file, pastes them to the confirmed tmux pane, then watches the
chosen WorldEdit export directory for the resulting JSON file.
In automatic mode, Ctrl-C pauses with retry/quit choices; retry restarts the
unfinished selection while preserving validated exports. A second Ctrl-C
exits immediately using the last completed checkpoint.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from typing import Any

import v12_capture_plan as capture_plan


WORLD_CENTER = 512_000
ROUTE_XZ = [(-34125, 59710), (-33952, 60548), (-33743, 60934), (-33012, 62110)]
CHANNEL_MAX_WIDTH = 250
CAPTURE_MARGIN = 100
NORTH_EXTRA = 50
DEFAULT_Y_BOUNDS = {"ymin": 80, "ymax": 160}
DEFAULT_Z_SPAN = 50
MAX_SELECTION_BLOCKS = 9_000_000
DEFAULT_PLAYER = "Xplosionist"
STABILITY_SECONDS = 2.0
POLL_SECONDS = 0.5
AUTO_TELEPORT_DELAY = 1.5
STATUS_INTERVAL = 60.0


@dataclass(frozen=True)
class Cuboid:
    index: int
    xmin: int
    xmax: int
    ymin: int
    ymax: int
    zmin: int
    zmax: int
    name: str
    plan_mode: bool = False

    @property
    def size(self) -> tuple[int, int, int]:
        return (self.xmax - self.xmin + 1,
                self.ymax - self.ymin + 1,
                self.zmax - self.zmin + 1)


def region_label(c: Cuboid) -> str:
    sx, sy, sz = c.size
    return (f"section {c.index}, X {c.xmin}..{c.xmax}, Y {c.ymin}..{c.ymax}, "
            f"Z {c.zmin}..{c.zmax} ({sx}×{sy}×{sz})")


def reported_export_size(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return f"{data.get('SizeX')}×{data.get('SizeY')}×{data.get('SizeZ')}"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "unknown dimensions"


def route_center_x(z: float, path: list[tuple[int, int]]) -> float:
    if z <= path[0][1]:
        return float(path[0][0])
    if z >= path[-1][1]:
        return float(path[-1][0])
    for (x0, z0), (x1, z1) in zip(path, path[1:]):
        if z0 <= z <= z1:
            t = (z - z0) / (z1 - z0)
            return x0 + (x1 - x0) * t
    raise ValueError(f"Could not interpolate route at Z={z}")


def route_corridor_slices(ymin: int, ymax: int, z_span: int,
                          prefix: str) -> tuple[list[Cuboid], dict[str, int]]:
    """Create consecutive Z slices whose X bounds follow the buffered route."""
    # Add the requested 50-block northern extension, then buffer the channel
    # half-width plus the 100-block capture margin in X and Z.
    path = [(-34125, 59710 - NORTH_EXTRA), *ROUTE_XZ]
    radius = CHANNEL_MAX_WIDTH // 2 + CAPTURE_MARGIN
    z_low = path[0][1] - radius
    z_high = path[-1][1] + radius
    result: list[Cuboid] = []
    serial = 1
    z0 = z_low
    while z0 <= z_high:
        z1 = min(z_high, z0 + z_span - 1)
        centers = [route_center_x(float(z0), path), route_center_x(float(z1), path)]
        centers.extend(float(x) for x, z in path if z0 <= z <= z1)
        x0 = math.floor(min(centers) - radius)
        x1 = math.ceil(max(centers) + radius)
        name = f"{prefix}_{serial:04d}_x{x0}_{x1}_z{z0}_{z1}"
        result.append(Cuboid(serial, x0, x1, ymin, ymax, z0, z1, name))
        serial += 1
        z0 = z1 + 1
    bounds = {"xmin": min(c.xmin for c in result), "xmax": max(c.xmax for c in result),
              "ymin": ymin, "ymax": ymax, "zmin": min(c.zmin for c in result),
              "zmax": max(c.zmax for c in result)}
    return result, bounds


def expected_export(c: Cuboid) -> dict[str, Any]:
    return {
        "SizeX": c.size[0], "SizeY_max": c.size[1], "SizeZ": c.size[2],
        "OriginalPos": {"X": c.xmin + WORLD_CENTER,
                        "Z": c.zmin + WORLD_CENTER,
                        "InternalY": c.ymin},
    }


def split_largest_dimension(c: Cuboid, prefix: str) -> tuple[Cuboid, Cuboid]:
    """Bisect the longest side; ties prefer X, then Z, then Y."""
    lengths = c.size
    axis = max(range(3), key=lambda i: (lengths[i], -({0: 0, 2: 1, 1: 2}[i])))
    starts = [c.xmin, c.ymin, c.zmin]
    ends = [c.xmax, c.ymax, c.zmax]
    mid = (starts[axis] + ends[axis]) // 2
    children = []
    for part, (lo, hi) in enumerate(((starts[axis], mid), (mid + 1, ends[axis])), 1):
        child_starts, child_ends = starts.copy(), ends.copy()
        child_starts[axis], child_ends[axis] = lo, hi
        name = (f"{prefix}_seg{part}_x{child_starts[0]}_{child_ends[0]}"
                f"_z{child_starts[2]}_{child_ends[2]}")
        children.append(Cuboid(c.index, child_starts[0], child_ends[0],
                               child_starts[1], child_ends[1], child_starts[2],
                               child_ends[2], name, c.plan_mode))
    return children[0], children[1]


def archive_invalid(path: Path) -> None:
    if not path.exists():
        return
    bad = path.with_name(path.stem + ".previous-invalid.json")
    n = 1
    while bad.exists():
        bad = path.with_name(path.stem + f".previous-invalid-{n}.json")
        n += 1
    path.rename(bad)


def merge_exports(parent: Cuboid, pieces: list[tuple[Cuboid, Path]], destination: Path) -> None:
    """Combine WorldEdit sparse block records into one parent-coordinate JSON."""
    if parent.size[0] > 1023 or parent.size[2] > 1023:
        raise ValueError("Cannot merge an export wider/deeper than the 1023-block packed-index safety limit")
    parent_origin = expected_export(parent)["OriginalPos"]
    merged: dict[str, Any] | None = None
    max_y = 0
    for piece, path in pieces:
        data = json.loads(path.read_text(encoding="utf-8"))
        if merged is None:
            merged = dict(data)
            merged["Indices"] = []
            merged["BlockIds"] = []
            merged["DecorIndices"] = []
            merged["DecorIds"] = []
            merged["BlockEntities"] = {}
            merged["Entities"] = []
            merged["BlockCodes"] = {}
            merged["ItemCodes"] = {}
        assert merged is not None
        for palette_key in ("BlockCodes", "ItemCodes"):
            for key, value in data.get(palette_key, {}).items():
                if key in merged[palette_key] and merged[palette_key][key] != value:
                    raise ValueError(f"Conflicting {palette_key} entry {key}")
                merged[palette_key][key] = value

        source_origin = data["OriginalPos"]
        dx = source_origin["X"] - parent_origin["X"]
        dz = source_origin["Z"] - parent_origin["Z"]
        dy = source_origin["InternalY"] - parent_origin["InternalY"]
        max_y = max(max_y, dy + data["SizeY"])

        def reindex(index: int) -> int:
            # WorldEdit packs local coordinates as X + (Z << 10) + (Y << 20).
            x = index & 1023
            z = (index >> 10) & 1023
            y = index >> 20
            x += dx
            z += dz
            y += dy
            if not (0 <= x < parent.size[0] and 0 <= z < parent.size[2] and 0 <= y < parent.size[1]):
                raise ValueError(f"Piece index {index} maps outside parent export bounds")
            return x | (z << 10) | (y << 20)

        merged["Indices"].extend(reindex(i) for i in data.get("Indices", []))
        merged["BlockIds"].extend(data.get("BlockIds", []))
        merged["DecorIndices"].extend(reindex(i) for i in data.get("DecorIndices", []))
        merged["DecorIds"].extend(data.get("DecorIds", []))
        for key, value in data.get("BlockEntities", {}).items():
            new_key = str(reindex(int(key)))
            if new_key in merged["BlockEntities"]:
                raise ValueError(f"Overlapping block entity at merged index {new_key}")
            merged["BlockEntities"][new_key] = value
        merged["Entities"].extend(data.get("Entities", []))

    if merged is None:
        raise ValueError("No complete segment exports are available to merge")
    merged["SizeX"] = parent.size[0]
    merged["SizeY"] = max(1, min(parent.size[1], max_y))
    merged["SizeZ"] = parent.size[2]
    merged["OriginalPos"] = parent_origin
    if parent.plan_mode:
        # Preserve leaf evidence inside generated merges, including when a new
        # extension plan reuses the file without the old progress record.
        merged["CapturePlanPieces"] = [{"file": path.name, **cuboid_record(piece)}
                                       for piece, path in pieces]
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, separators=(",", ":")), encoding="utf-8")
    tmp.replace(destination)


def validate_export(path: Path, c: Cuboid) -> list[str]:
    problems: list[str] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"Could not read valid JSON: {exc}"]
    if not isinstance(data, dict):
        return ["Export root is not an object"]

    expected = expected_export(c)
    for key in ("SizeX", "SizeZ"):
        actual = data.get(key)
        if actual != expected[key]:
            problems.append(f"{key} is {actual!r}; expected {expected[key]}")
    size_y = data.get("SizeY")
    if not isinstance(size_y, int) or not 1 <= size_y <= expected["SizeY_max"]:
        problems.append(f"SizeY is {size_y!r}; expected 1..{expected['SizeY_max']} (empty upper space may be omitted)")
    origin = data.get("OriginalPos")
    if not isinstance(origin, dict):
        problems.append("OriginalPos is missing or is not an object")
    else:
        for key, value in expected["OriginalPos"].items():
            if origin.get(key) != value:
                problems.append(f"OriginalPos.{key} is {origin.get(key)!r}; expected {value}")

    indices, block_ids = data.get("Indices"), data.get("BlockIds")
    if not isinstance(indices, list) or not isinstance(block_ids, list):
        problems.append("Indices or BlockIds is missing or is not an array")
    elif len(indices) != len(block_ids):
        problems.append(f"Indices has {len(indices)} entries but BlockIds has {len(block_ids)}")
    elif not indices:
        problems.append("Export contains no block entries; completeness cannot be established")
    for key in ("DecorIndices", "DecorIds"):
        if key in data and not isinstance(data[key], list):
            problems.append(f"{key} is not an array")
    if c.plan_mode:
        problems.extend(capture_plan.inspect_export(path, cuboid_record(c))["problems"])
    return problems


def returned_export_bounds(path: Path, c: Cuboid) -> dict[str, int] | None:
    """Return the nonempty X/Z envelope reported by an export, if trustworthy."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    origin = data.get("OriginalPos")
    indices, block_ids = data.get("Indices"), data.get("BlockIds")
    size_x, size_y, size_z = data.get("SizeX"), data.get("SizeY"), data.get("SizeZ")
    if not isinstance(origin, dict) or not isinstance(indices, list) or not isinstance(block_ids, list):
        return None
    if not indices or len(indices) != len(block_ids):
        return None
    if any(origin.get(key) != value for key, value in expected_export(c)["OriginalPos"].items()):
        return None
    if not all(isinstance(n, int) and 1 <= n <= maximum for n, maximum in
               ((size_x, c.size[0]), (size_y, c.size[1]), (size_z, c.size[2]))):
        return None
    if any(not isinstance(index, int) or index < 0 or
           (index & 1023) >= size_x or ((index >> 10) & 1023) >= size_z or
           (index >> 20) >= size_y for index in indices):
        return None
    return {"xmin": c.xmin, "xmax": c.xmin + size_x - 1,
            "ymin": c.ymin, "ymax": c.ymin + size_y - 1,
            "zmin": c.zmin, "zmax": c.zmin + size_z - 1}


def cuboid_record(c: Cuboid) -> dict[str, int]:
    return {"xmin": c.xmin, "xmax": c.xmax, "ymin": c.ymin,
            "ymax": c.ymax, "zmin": c.zmin, "zmax": c.zmax}


def cuboid_from_record(parent: Cuboid, bounds: dict[str, int], name: str) -> Cuboid:
    return Cuboid(parent.index, bounds["xmin"], bounds["xmax"], bounds["ymin"],
                  bounds["ymax"], bounds["zmin"], bounds["zmax"], name, parent.plan_mode)


def uncovered_remainder(c: Cuboid, returned: dict[str, int] | None) -> list[dict[str, int]]:
    """Describe the unreturned suffixes, assuming WorldEdit anchors at OriginalPos."""
    if returned is None:
        return [cuboid_record(c)]
    gaps: list[dict[str, int]] = []
    if returned["xmax"] < c.xmax:
        gaps.append({"xmin": returned["xmax"] + 1, "xmax": c.xmax,
                     "ymin": c.ymin, "ymax": c.ymax, "zmin": c.zmin, "zmax": c.zmax})
    if returned["zmax"] < c.zmax:
        gaps.append({"xmin": c.xmin, "xmax": returned["xmax"],
                     "ymin": c.ymin, "ymax": c.ymax, "zmin": returned["zmax"] + 1, "zmax": c.zmax})
    return gaps


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    manifest_path = path.with_name(path.stem.replace("-progress", "-coverage") + ".json")
    manifest = {
        "signature": state.get("signature"), "updated_utc": state["updated_utc"],
        "coverage_basis": "Returned X/Z bounds use OriginalPos plus SizeX/SizeZ; unreturned suffixes are reported as gaps. This is WorldEdit's reported extent, not proof of why it was shortened.",
        "sections": state.get("sections", {}),
    }
    if state.get("signature", {}).get("capture_plan_hash"):
        manifest["coverage_basis"] = "Execution checkpoint only. Use the saved v12 plan's independent 3D audit for verified coverage and exact height gaps."
    manifest_tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    manifest_tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_tmp.replace(manifest_path)


def load_state(path: Path, signature: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = {}
        if old.get("signature") == signature:
            return old
        if old:
            print("Saved progress belongs to a different capture plan; starting a separate record.")
    return {"signature": signature, "sections": {}, "updated_utc": None}


def stable_file(path: Path, quit_check) -> bool:
    previous: tuple[int, int] | None = None
    stable_since: float | None = None
    while True:
        if quit_check():
            return False
        try:
            stat = path.stat()
            current = (stat.st_size, stat.st_mtime_ns)
        except FileNotFoundError:
            previous, stable_since = None, None
            time.sleep(POLL_SECONDS)
            continue
        if current[0] == 0:
            previous, stable_since = current, None
        elif current == previous:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= STABILITY_SECONDS:
                return True
        else:
            previous, stable_since = current, None
        time.sleep(POLL_SECONDS)


class RunStatus:
    def __init__(self, cuboids: list[Cuboid], state: dict[str, Any]):
        self.cuboids = cuboids
        self.state = state
        self.started = time.monotonic()
        self.last_report = self.started

    def maybe_report(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_report < STATUS_INTERVAL:
            return
        complete = sum(
            self.state.get("sections", {}).get(c.name, {}).get("status") in ("complete", "accepted")
            for c in self.cuboids
        )
        total = len(self.cuboids)
        percent = 100.0 * complete / total if total else 100.0
        print(f"Status: {complete} complete of {total} ({percent:.1f}%).")
        self.last_report = now


class UserQuit(Exception):
    """An explicit quit, distinct from Ctrl-C requesting a pause."""


def abort_immediately(signum=None, frame=None) -> None:
    """Bypass saving, cleanup, and stream flushing on a second Ctrl-C."""
    os._exit(130)


def try_save_state(state_path: Path, state: dict[str, Any]) -> bool:
    try:
        save_state(state_path, state)
        return True
    except (OSError, ValueError, TypeError) as exc:
        print(f"Could not finish saving progress: {exc}. Existing checkpoints are retained.",
              file=sys.stderr)
        return False


def pause_after_interrupt(state: dict[str, Any], state_path: Path) -> None:
    """Save once and prompt; another Ctrl-C aborts even during that save."""
    previous_handler = signal.signal(signal.SIGINT, abort_immediately)
    try:
        print("\nPaused. Saving progress ... (Ctrl-C again aborts)", flush=True)
        try_save_state(state_path, state)
        while True:
            choice = input("[r]etry unfinished selection, [q]uit: ").strip().lower()
            if choice == "r":
                print("Resuming.")
                signal.signal(signal.SIGINT, previous_handler)
                return
            if choice == "q":
                # Keep the emergency handler installed during the final save.
                raise UserQuit
            print("Choose r or q; Ctrl-C again aborts.")
    except KeyboardInterrupt:
        abort_immediately()


def run_export_attempt(c: Cuboid, player: str, target_pane: str, command_dir: Path,
                       export_dir: Path, state: dict[str, Any], state_path: Path,
                       confirm: bool = True, verbose: bool = True) -> tuple[str, list[str]]:
    """Send one section export and return complete/incomplete/skipped plus problems."""
    output_path = export_dir / f"{c.name}.json"
    archive_invalid(output_path)
    command_path = command_dir / f"{c.name}.txt"
    command_text = (
        f"/executeas {player} /we select {c.xmin} {c.ymin} {c.zmin} {c.xmax} {c.ymax} {c.zmax}\n"
        f"/executeas {player} /we export {c.name}\n"
    )
    command_path.write_text(command_text, encoding="utf-8")
    if confirm or verbose:
        print(f"Command file ready for {region_label(c)}.")
    if confirm:
        choice = input("Press Return to load and paste these commands; n to skip; q to quit: ").strip().lower()
        if choice.startswith("q"):
            raise KeyboardInterrupt
        if choice in ("n", "no"):
            return "skipped", []
        if choice not in ("", "y", "yes"):
            print("No action taken; press Return to send, n to skip, or q to quit.")
            return "not-sent", []
    subprocess.run([Path("~/bin/tmux").expanduser(), "load-buffer", str(command_path), ";",
                    "paste-buffer", "-d", "-t", target_pane], check=True)
    if confirm or verbose:
        print("Commands pasted. Waiting for the export file to appear.")
    state["sections"][c.name] = {"status": "waiting", "file": str(output_path)}
    save_state(state_path, state)
    if confirm or verbose:
        print(f"Waiting for a stable export for {region_label(c)}.")
    if not stable_file(output_path, lambda: False):
        raise KeyboardInterrupt
    problems = validate_export(output_path, c)
    if problems:
        state["sections"][c.name] = {"status": "incomplete", "file": str(output_path), "problems": problems}
        save_state(state_path, state)
        if not confirm and not verbose:
            print("incomplete")
        return "incomplete", problems
    state["sections"][c.name] = {"status": "complete", "file": str(output_path),
                                 "validated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if c.plan_mode:
        evidence = capture_plan.inspect_export(output_path, cuboid_record(c))
        state["sections"][c.name].update(returned_bounds=evidence["covered"],
                                         ceiling_blocks=evidence["ceiling_blocks"])
        if evidence["ceiling_blocks"]:
            print(f"\nHeight warning: {evidence['ceiling_blocks']} entries reach requested Y ceiling {c.ymax} in {c.name}.")
        if evidence["covered"] and evidence["covered"]["ymax"] < c.ymax:
            print(f"\nHeight coverage unresolved: {c.name} reports Y through {evidence['covered']['ymax']}; requested through {c.ymax}.")
    save_state(state_path, state)
    if not confirm and not verbose:
        print("complete")
    return "complete", []


def teleport_player_to_cuboid(c: Cuboid, player: str, target_pane: str,
                              command_dir: Path, verbose: bool = True) -> Path:
    """Request creative mode and teleport using the same X/Z coordinates as `/we select`."""
    x = (c.xmin + c.xmax) // 2
    y = max(c.ymin, c.ymax - 2)
    z = (c.zmin + c.zmax) // 2
    command_path = command_dir / f"teleport_section{c.index:04d}_x{c.xmin}_{c.xmax}_z{c.zmin}_{c.zmax}.txt"
    command_path.write_text(f"/gm {player} creative\n/tp {player} {x} {y} {z}\n", encoding="utf-8")
    if verbose:
        print(f"Teleport command file ready: {command_path}")
    subprocess.run([Path("~/bin/tmux").expanduser(), "load-buffer", str(command_path), ";",
                    "paste-buffer", "-d", "-t", target_pane], check=True)
    if verbose:
        print(f"Teleport sent ({x}, {y}, {z}).")
    return command_path


def recover_automatically(c: Cuboid, depth: int, player: str, target_pane: str,
                          command_dir: Path, export_dir: Path, state: dict[str, Any],
                          state_path: Path, status_reporter: RunStatus,
                          verbose: bool = False
                          ) -> tuple[list[tuple[Cuboid, Path]], list[dict[str, int]]]:
    """Pause on Ctrl-C at the current region, retaining completed siblings."""
    restart_pending = False
    while True:
        try:
            return _recover_automatically(c, depth, player, target_pane, command_dir,
                                          export_dir, state, state_path, status_reporter,
                                          verbose, restart_pending)
        except KeyboardInterrupt:
            pause_after_interrupt(state, state_path)
            # A file received while paused is not a validated save. Send this
            # selection again unless its validation was already checkpointed.
            restart_pending = state["sections"].get(c.name, {}).get("status") != "complete"


def _recover_automatically(c: Cuboid, depth: int, player: str, target_pane: str,
                           command_dir: Path, export_dir: Path, state: dict[str, Any],
                           state_path: Path, status_reporter: RunStatus,
                           verbose: bool, restart_pending: bool
                           ) -> tuple[list[tuple[Cuboid, Path]], list[dict[str, int]]]:
    """Attempt, teleport and retry, then split at most twice before asking."""
    output_path = export_dir / f"{c.name}.json"
    saved = state.get("sections", {}).get(c.name, {})
    if not restart_pending and output_path.exists() and not validate_export(output_path, c):
        state["sections"][c.name] = {**saved, "status": "complete", "file": str(output_path),
                                     "validated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        save_state(state_path, state)
        if verbose:
            print(f"Already complete: {region_label(c)}")
        status_reporter.maybe_report()
        return [(c, output_path)], []

    def process_children(children: list[dict[str, Any]],
                         retained: list[dict[str, Any]]
                         ) -> tuple[list[tuple[Cuboid, Path]], list[dict[str, int]]]:
        leaves = [(cuboid_from_record(c, item, Path(item["file"]).stem), Path(item["file"]))
                  for item in retained]
        gaps: list[dict[str, int]] = []

        def checkpoint(pending: list[dict[str, Any]]) -> None:
            remaining = gaps + [cuboid_record(cuboid_from_record(c, item, item["name"]))
                                for item in pending]
            state["sections"][c.name] = {
                "status": "partial" if remaining else "complete", "file": str(output_path),
                "pieces": [str(path) for _, path in leaves], "gaps": remaining,
                "piece_regions": [{"file": str(path), **cuboid_record(piece)} for piece, path in leaves],
                "segmented": True, "children": children, "retained_regions": retained,
            }
            save_state(state_path, state)

        # Persist the work queue before starting a child, and after each result.
        checkpoint(children)
        for index, item in enumerate(children):
            child = cuboid_from_record(c, item, item["name"])
            child_pieces, child_gaps = recover_automatically(
                child, item["depth"], player, target_pane, command_dir, export_dir,
                state, state_path, status_reporter, verbose)
            leaves.extend(child_pieces)
            gaps.extend(child_gaps)
            checkpoint(children[index + 1:])
        return leaves, gaps

    if saved.get("children"):
        return process_children(saved["children"], saved.get("retained_regions", []))

    if saved.get("gaps") and (saved.get("segmented") or saved.get("status") == "partial"):
        retained: list[dict[str, Any]] = []
        for item in saved.get("piece_regions", []):
            piece = cuboid_from_record(c, item, Path(item["file"]).stem)
            piece_path = Path(item["file"])
            if piece_path.is_file() and not validate_export(piece_path, piece):
                retained.append(item)
        children = []
        for index, bounds in enumerate(saved["gaps"], 1):
            gap_name = (f"{c.name}_resume{index}_x{bounds['xmin']}_{bounds['xmax']}"
                        f"_z{bounds['zmin']}_{bounds['zmax']}")
            children.append({"name": gap_name, "depth": min(depth + 1, 2), **bounds})
        return process_children(children, retained)

    def attempt() -> tuple[str, list[str]]:
        if verbose:
            print(f"Selecting {region_label(c)} for export ...")
        else:
            print(f"selecting {c.size[0]}x{c.size[1]}x{c.size[2]} for export ... ", end="", flush=True)
        result = run_export_attempt(c, player, target_pane, command_dir, export_dir,
                                    state, state_path, confirm=False, verbose=verbose)
        if verbose:
            print(f"Export {result[0]} for {region_label(c)}.")
            for problem in result[1]:
                print(f"  - {problem}")
        status_reporter.maybe_report()
        return result

    result, problems = attempt()
    if result == "complete":
        return [(c, output_path)], []

    print("teleporting ... ", end="", flush=True)
    teleport_player_to_cuboid(c, player, target_pane, command_dir, verbose=verbose)
    print(f"waiting {AUTO_TELEPORT_DELAY}s ... ", end="", flush=True)
    time.sleep(AUTO_TELEPORT_DELAY)
    print("ready")
    status_reporter.maybe_report()
    result, problems = attempt()
    if result == "complete":
        return [(c, output_path)], []

    if depth < 2 and max(c.size) > 1:
        print(f"splitting {c.size[0]}x{c.size[1]}x{c.size[2]} ...")
        children = [{"name": child.name, "depth": depth + 1, **cuboid_record(child)}
                    for child in split_largest_dimension(c, f"{c.name}_d{depth+1}")]
        pieces, gaps = process_children(children, [])
        if not gaps:
            print(f"section {c.index} complete")
        return pieces, gaps

    if verbose:
        print(f"Incomplete: {region_label(c)}")
        for problem in problems:
            print(f"  - {problem}")
    while True:
        choice = input("Quarter incomplete: [r]etry, [s]kip, [q]uit: ").strip().lower()
        if choice == "r":
            result, problems = attempt()
            if result == "complete":
                return [(c, output_path)], []
            print("still incomplete")
            continue
        if choice == "s":
            gap = [cuboid_record(c)]
            state["sections"][c.name] = {"status": "partial", "gaps": gap, "pieces": []}
            save_state(state_path, state)
            return [], gap
        if choice == "q":
            raise UserQuit
        print("Choose r, s, or q.")


def recover_by_splitting(c: Cuboid, depth: int, player: str, target_pane: str,
                         command_dir: Path, export_dir: Path, state: dict[str, Any],
                         state_path: Path, split_immediately: bool = False
                         ) -> tuple[list[tuple[Cuboid, Path]], list[dict[str, int]]]:
    """Retry or split a failed region; retain partial exports and report explicit gaps."""
    output_path = export_dir / f"{c.name}.json"
    if output_path.exists() and not validate_export(output_path, c):
        print(f"Using the already validated export for {region_label(c)}; moving on.")
        state["sections"][c.name] = {"status": "complete", "file": str(output_path),
                                     "validated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        save_state(state_path, state)
        return [(c, output_path)], []

    def split_children() -> tuple[list[tuple[Cuboid, Path]], list[dict[str, int]]]:
        leaves: list[tuple[Cuboid, Path]] = []
        gaps: list[dict[str, int]] = []
        for child in split_largest_dimension(c, f"{c.name}_d{depth+1}"):
            child_leaves, child_gaps = recover_by_splitting(
                child, depth + 1, player, target_pane, command_dir, export_dir, state, state_path)
            leaves.extend(child_leaves)
            gaps.extend(child_gaps)
        state["sections"][c.name] = {
            "status": "partial" if gaps else "complete", "file": str(output_path),
            "pieces": [str(path) for _, path in leaves], "gaps": gaps,
            "piece_regions": [{"file": str(path), **cuboid_record(piece)} for piece, path in leaves],
            "segmented": True,
        }
        save_state(state_path, state)
        if not gaps:
            if depth == 0:
                print(f"All split portions of section {c.index} are complete; finalizing the section.")
            else:
                print(f"Both halves are complete for {region_label(c)}; moving up one split level.")
        return leaves, gaps

    saved = state["sections"].get(c.name, {})
    if not split_immediately and saved.get("segmented"):
        if saved.get("gaps"):
            return split_children()
        pieces = []
        for item in saved.get("piece_regions", []):
            piece = cuboid_from_record(c, item, Path(item["file"]).stem)
            piece_path = Path(item["file"])
            if piece_path.is_file() and not validate_export(piece_path, piece):
                pieces.append((piece, piece_path))
        if pieces:
            if depth == 0:
                print(f"Saved split pieces already cover section {c.index}; finalizing the section.")
            else:
                print(f"Saved halves already cover {region_label(c)}; moving up one split level.")
            return pieces, []
    if not split_immediately and saved.get("status") == "partial" and saved.get("gaps"):
        leaves: list[tuple[Cuboid, Path]] = []
        returned = saved.get("returned_bounds")
        saved_file = Path(saved["file"]) if saved.get("file") else None
        if returned is not None and saved_file is not None and saved_file.exists():
            leaves.append((c, saved_file))
        remaining: list[dict[str, int]] = []
        prefix = state.get("signature", {}).get("prefix", "capture")
        for bounds in saved["gaps"]:
            gap_name = (f"{prefix}_{c.index:04d}_gap_x{bounds['xmin']}_{bounds['xmax']}"
                        f"_y{bounds['ymin']}_{bounds['ymax']}_z{bounds['zmin']}_{bounds['zmax']}")
            gap_cuboid = cuboid_from_record(c, bounds, gap_name)
            gap_pieces, gap_remainder = recover_by_splitting(
                gap_cuboid, depth, player, target_pane, command_dir, export_dir, state, state_path)
            leaves.extend(gap_pieces)
            remaining.extend(gap_remainder)
        state["sections"][c.name] = {
            "status": "partial" if remaining else "complete", "file": str(saved_file) if saved_file else None,
            "returned_bounds": returned, "pieces": [str(path) for _, path in leaves],
            "gaps": remaining,
        }
        save_state(state_path, state)
        return leaves, remaining

    # The user selected segmentation for this already failed cuboid. Do not
    # submit the same full-size cuboid again before splitting it.
    if split_immediately:
        if depth >= 3 or max(c.size) <= 1:
            print("Cannot split this section further; the three-level split limit is reached.")
            gap = [cuboid_record(c)]
            state["sections"][c.name] = {"status": "partial", "gaps": gap, "pieces": []}
            save_state(state_path, state)
            return [], gap
        return split_children()

    retry_confirm = True
    while True:
        status, problems = run_export_attempt(c, player, target_pane, command_dir,
                                              export_dir, state, state_path, retry_confirm)
        if status == "complete":
            outcome = "Retry succeeded" if not retry_confirm else "Export passed validation"
            print(f"{outcome} for {region_label(c)}; WorldEdit returned "
                  f"{reported_export_size(output_path)}. Continuing to the next portion.")
            return [(c, output_path)], []
        if status == "skipped":
            gap = [cuboid_record(c)]
            state["sections"][c.name] = {"status": "partial", "gaps": gap, "pieces": []}
            save_state(state_path, state)
            return [], gap
        if status == "not-sent":
            continue
        print(f"Export is incomplete for {region_label(c)}:")
        for problem in problems:
            print(f"  - {problem}")
        can_split = depth < 3 and max(c.size) > 1
        can_teleport = depth >= 2
        if can_split and can_teleport:
            prompt = "[r]etry now, [s]plit largest dimension, [k]keep returned part and skip remainder, [t]eleport and retry, [q]uit: "
        elif can_split:
            prompt = "[r]etry now, [s]plit largest dimension, [k]keep returned part and skip remainder, [q]uit: "
        elif can_teleport:
            prompt = "[r]etry now, [k]keep returned part and skip remainder, [t]eleport and retry, [q]uit (split limit reached): "
        else:
            prompt = "[r]etry now, [k]keep returned part and skip remainder, [q]uit (cannot split further): "
        while True:
            choice = input(prompt).strip().lower()
            if choice == "r":
                retry_confirm = False
                break
            if choice == "s" and can_split:
                return split_children()
            if choice == "k":
                returned = returned_export_bounds(output_path, c) if output_path.exists() else None
                gaps = uncovered_remainder(c, returned)
                pieces = [(c, output_path)] if returned is not None else []
                state["sections"][c.name] = {
                    "status": "partial", "file": str(output_path) if output_path.exists() else None,
                    "returned_bounds": returned, "pieces": [str(output_path)] if pieces else [],
                    "gaps": gaps,
                }
                save_state(state_path, state)
                return pieces, gaps
            if choice == "t" and can_teleport:
                teleport_player_to_cuboid(c, player, target_pane, command_dir)
                while True:
                    ready = input("After the player arrives and the client has synchronized, press Return to issue the capture again; q to quit: ").strip().lower()
                    if ready == "q":
                        raise KeyboardInterrupt
                    if ready == "":
                        retry_confirm = False
                        break
                    print("Press Return to retry now, or q to quit.")
                break
            if choice == "q":
                raise KeyboardInterrupt
            print("No action taken; choose one of the listed options.")


def finalize_split_result(c: Cuboid, pieces: list[tuple[Cuboid, Path]], gaps: list[dict[str, int]],
                          export_dir: Path, state: dict[str, Any], state_path: Path) -> None:
    output_path = export_dir / f"{c.name}.json"
    state["sections"][c.name] = {
        **state["sections"].get(c.name, {}),
        "status": "partial" if gaps else "complete", "file": str(output_path),
        "pieces": [str(path) for _, path in pieces], "gaps": gaps, "segmented": True,
        "piece_regions": [{"file": str(path), **cuboid_record(piece)} for piece, path in pieces],
    }
    save_state(state_path, state)
    if gaps:
        print(f"Split capture kept {len(pieces)} export file(s) and recorded {len(gaps)} uncovered region(s).")
        print("No combined export was written because the requested area still has gaps.")
    elif pieces:
        try:
            archive_invalid(output_path)
            merge_exports(c, pieces, output_path)
            merge_problems = validate_export(output_path, c)
            if merge_problems:
                raise ValueError("Combined export did not validate: " + "; ".join(merge_problems))
            state["sections"][c.name] = {
                "status": "complete", "file": str(output_path), "merged": True,
                "pieces": [str(path) for _, path in pieces], "gaps": [],
                "segmented": True,
                "piece_regions": [{"file": str(path), **cuboid_record(piece)} for piece, path in pieces],
                "validated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")
            }
            save_state(state_path, state)
            print(f"Section {c.index} complete: all {len(pieces)} split exports cover it, "
                  "and the combined export passed validation.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            state["sections"][c.name] = {
                **state["sections"].get(c.name, {}),
                "status": "partial", "file": str(output_path),
                "pieces": [str(path) for _, path in pieces], "gaps": [],
                "problems": [f"Could not combine split exports: {exc}"]
            }
            save_state(state_path, state)
            print(f"Section {c.index} is fully covered by {len(pieces)} split exports, "
                  f"but the combined file could not be created: {exc}")
    else:
        print("No usable export pieces were returned; the full section is recorded as an uncovered region.")


def xz_union_area(rectangles: list[dict[str, int]]) -> int:
    """Count the X/Z footprint without double-counting overlapping rectangles."""
    if not rectangles:
        return 0
    x_edges = sorted({edge for rect in rectangles for edge in (rect["xmin"], rect["xmax"] + 1)})
    area = 0
    for left, right in zip(x_edges, x_edges[1:]):
        intervals = sorted((rect["zmin"], rect["zmax"] + 1) for rect in rectangles
                           if rect["xmin"] <= left and rect["xmax"] + 1 >= right)
        if not intervals:
            continue
        z_total = 0
        start, end = intervals[0]
        for next_start, next_end in intervals[1:]:
            if next_start <= end:
                end = max(end, next_end)
            else:
                z_total += end - start
                start, end = next_start, next_end
        z_total += end - start
        area += (right - left) * z_total
    return area


def xz_complement(c: Cuboid, rectangles: list[dict[str, int]]) -> list[dict[str, int]]:
    """Return uncovered X/Z rectangles inside a planned cuboid."""
    clipped = []
    for rect in rectangles:
        xmin, xmax = max(c.xmin, rect["xmin"]), min(c.xmax, rect["xmax"])
        zmin, zmax = max(c.zmin, rect["zmin"]), min(c.zmax, rect["zmax"])
        if xmin <= xmax and zmin <= zmax:
            clipped.append({"xmin": xmin, "xmax": xmax, "zmin": zmin, "zmax": zmax})
    x_edges = sorted({c.xmin, c.xmax + 1,
                      *(edge for rect in clipped for edge in (rect["xmin"], rect["xmax"] + 1))})
    gaps: list[dict[str, int]] = []
    for left, right in zip(x_edges, x_edges[1:]):
        intervals = sorted((rect["zmin"], rect["zmax"] + 1) for rect in clipped
                           if rect["xmin"] <= left and rect["xmax"] + 1 >= right)
        merged: list[list[int]] = []
        for start, end in intervals:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        cursor = c.zmin
        for start, end in merged:
            if cursor < start:
                gaps.append({"xmin": left, "xmax": right - 1, "ymin": c.ymin, "ymax": c.ymax,
                             "zmin": cursor, "zmax": start - 1})
            cursor = max(cursor, end)
        if cursor <= c.zmax:
            gaps.append({"xmin": left, "xmax": right - 1, "ymin": c.ymin, "ymax": c.ymax,
                         "zmin": cursor, "zmax": c.zmax})
    return gaps


def parse_piece_cuboid(path: Path, parent: Cuboid, item: dict[str, Any] | None = None) -> Cuboid | None:
    if item and all(key in item for key in ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax")):
        bounds = {key: int(item[key]) for key in ("xmin", "xmax", "ymin", "ymax", "zmin", "zmax")}
    else:
        x_match = list(re.finditer(r"_x(-?\d+)_(-?\d+)", path.stem))
        z_match = list(re.finditer(r"_z(-?\d+)_(-?\d+)", path.stem))
        if not x_match or not z_match:
            return None
        bounds = {"xmin": int(x_match[-1].group(1)), "xmax": int(x_match[-1].group(2)),
                  "ymin": parent.ymin, "ymax": parent.ymax,
                  "zmin": int(z_match[-1].group(1)), "zmax": int(z_match[-1].group(2))}
    if bounds["xmin"] > bounds["xmax"] or bounds["zmin"] > bounds["zmax"]:
        return None
    return cuboid_from_record(parent, bounds, path.stem)


def report_coverage(prefix: str, verbose: bool) -> int:
    state_path = Path(__file__).with_name(f"{prefix}-progress.json")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"Could not read progress record {state_path}: {exc}", file=sys.stderr)
        return 2
    signature = state.get("signature", {})
    bounds = signature.get("bounds", {})
    try:
        cuboids, _ = route_corridor_slices(bounds["ymin"], bounds["ymax"],
                                           int(signature["z_span"]), prefix)
    except (KeyError, TypeError, ValueError) as exc:
        print(f"Progress record does not contain a usable capture plan: {exc}", file=sys.stderr)
        return 2

    sections = state.get("sections", {})
    total_area = sum(c.size[0] * c.size[2] for c in cuboids)
    covered_rectangles: list[dict[str, int]] = []
    gaps_to_report: list[tuple[int, dict[str, int]]] = []
    for c in cuboids:
        record = sections.get(c.name, {})
        record_status = record.get("status")
        section_coverage: list[dict[str, int]] = []
        full_file = Path(record["file"]) if record.get("file") else None
        if (record_status == "complete" and not record.get("gaps") and
                full_file is not None and full_file.is_file()):
            section_coverage = [cuboid_record(c)]
        else:
            piece_items = record.get("piece_regions", [])
            piece_paths = record.get("pieces", [])
            if piece_items:
                for item in piece_items:
                    path = Path(item.get("file", ""))
                    piece = parse_piece_cuboid(path, c, item)
                    if piece and path.is_file():
                        box = returned_export_bounds(path, piece)
                        if box:
                            section_coverage.append(box)
            elif piece_paths:
                for filename in piece_paths:
                    path = Path(filename)
                    piece = parse_piece_cuboid(path, c)
                    if piece and path.is_file():
                        box = returned_export_bounds(path, piece)
                        if box:
                            section_coverage.append(box)

            if not section_coverage:
                filename = record.get("file") or str(Path(signature.get("export_dir", "")) / f"{c.name}.json")
                path = Path(filename)
                if path.is_file():
                    box = returned_export_bounds(path, c)
                    if box:
                        section_coverage.append(box)
                    elif not validate_export(path, c):
                        section_coverage.append(cuboid_record(c))

        covered_rectangles.extend(section_coverage)
        gaps = record.get("gaps")
        planned_area = c.size[0] * c.size[2]
        actual_area = xz_union_area(section_coverage)
        if gaps is None or (not gaps and actual_area < planned_area):
            if gaps is not None:
                gaps = xz_complement(c, section_coverage)
            else:
                root_file = Path(record.get("file", "")) if record.get("file") else None
                returned = returned_export_bounds(root_file, c) if root_file and root_file.is_file() else None
                gaps = uncovered_remainder(c, returned)
        for gap in gaps:
            gaps_to_report.append((c.index, gap))

    covered_area = xz_union_area(covered_rectangles)
    percent = 100.0 * covered_area / total_area if total_area else 0.0
    print(f"Coverage for {prefix}: {covered_area:,} of {total_area:,} planned X/Z blocks ({percent:.2f}%).")
    print(f"Recorded gaps: {len(gaps_to_report)} region(s); progress: {state_path}")
    if verbose:
        if not gaps_to_report:
            print("No gaps are recorded.")
        else:
            print("Gap bounds (inclusive):")
            for section_index, gap in sorted(gaps_to_report,
                                             key=lambda entry: (entry[0], entry[1]["zmin"], entry[1]["xmin"])):
                print(f"  Section {section_index}: X {gap['xmin']}..{gap['xmax']}, "
                      f"Y {gap['ymin']}..{gap['ymax']}, Z {gap['zmin']}..{gap['zmax']}")
    if covered_area or gaps_to_report:
        print("Coverage measures exported X/Z envelopes; the manifest does not establish why a shortened export omitted an edge.")
    return 0


def prompt_value(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def read_y_bounds() -> dict[str, int]:
    print("Vertical bounds are inclusive. X/Z captures follow the buffered tentative channel route.")
    bounds = {key: int(prompt_value(key, str(value))) for key, value in DEFAULT_Y_BOUNDS.items()}
    if bounds["ymin"] > bounds["ymax"]:
        raise ValueError("ymin must be <= ymax")
    return bounds


def write_plan_audit(plan: dict, plan_path: Path, state: dict, export_dir: Path) -> int:
    report = capture_plan.audit_coverage(plan, state, export_dir)
    path = plan_path.with_name(plan["prefix"] + "-audit.json")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2) + "\n")
    tmp.replace(path)
    capture_plan.render_preview(plan, plan_path.with_name(plan["prefix"] + "-coverage.svg"), report["gaps"])
    print(f"Verified reported 3D coverage: {report['coverage_percent']:.6f}% "
          f"({report['verified_envelope_blocks']:,}/{report['requested_blocks']:,} positions).")
    print(f"Exact uncovered regions: {len(report['gaps'])}. Audit: {path}")
    for warning in report["warnings"]:
        print(f"Height/coverage warning: {warning}")
    print(report["evidence_limits"])
    return 0 if report["complete"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-pane",
                        help="tmux target pane that receives the confirmed WorldEdit command paste")
    parser.add_argument("--z-span", type=int, default=DEFAULT_Z_SPAN,
                        help=f"Z length of each route-following slice (default: {DEFAULT_Z_SPAN}; max 1023)")
    parser.add_argument("--prefix", help="export prefix; also selects the saved progress record")
    parser.add_argument("--plan", type=Path, help="use a saved v12 capture plan; legacy route mode is unchanged")
    parser.add_argument("--preview", action="store_true", help="render a saved --plan and exit without sending commands")
    parser.add_argument("--export-dir", type=Path, help="WorldEdit JSON directory (otherwise prompted or read from plan progress for a report)")
    parser.add_argument("--report-coverage", action="store_true",
                        help="report saved X/Z coverage and exit without prompting or sending commands")
    parser.add_argument("--verbose", action="store_true",
                        help="print detailed bounds, validation problems, and command-file activity")
    parser.add_argument("--step-by-step", action="store_true",
                        help="use the approval-driven workflow instead of automatic retries and splitting")
    args = parser.parse_args()
    plan = None
    if args.plan:
        try:
            args.plan = args.plan.expanduser().resolve()
            plan = capture_plan.load_plan(args.plan)
            if args.prefix is not None or args.z_span != DEFAULT_Z_SPAN:
                parser.error("--prefix and --z-span belong to legacy mode; v12 settings are saved in --plan")
            capture_plan.render_preview(plan, args.plan.with_suffix(".svg"))
            if args.preview:
                capture_plan.print_summary(plan, args.plan)
                return 0
            if args.report_coverage:
                progress = args.plan.with_name(plan["prefix"] + "-progress.json")
                state = capture_plan.read_json(progress) if progress.exists() else {}
                if state and state.get("signature", {}).get("capture_plan_hash") != plan["plan_hash"]:
                    raise ValueError("Progress belongs to another capture plan")
                export_path = args.export_dir or state.get("signature", {}).get("export_dir")
                if not export_path:
                    parser.error("--export-dir is required until this plan has a saved run")
                return write_plan_audit(plan, args.plan, state, Path(export_path).expanduser())
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(str(exc))
    elif args.preview:
        parser.error("--preview requires --plan")
    if args.report_coverage:
        report_prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", args.prefix or "channel_capture")
        return report_coverage(report_prefix, args.verbose)
    if not args.target_pane:
        parser.error("--target-pane is required unless --report-coverage is used")
    target_pane = args.target_pane.strip()
    if not target_pane:
        parser.error("--target-pane must not be empty")
    if not 1 <= args.z_span <= 1023:
        parser.error("--z-span must be between 1 and 1023")

    print("Vintage Story WorldEdit export coordinator (terrain is never modified).")
    print("Mode: " + ("step-by-step" if args.step_by_step else "automatic"))
    y_bounds = plan["requested_y"] if plan else read_y_bounds()
    z_span = args.z_span
    player = prompt_value("Player name for /executeas", DEFAULT_PLAYER)
    export_dir = args.export_dir or Path(prompt_value(
        "WorldEdit export directory",
        str(Path.home() / "Library" / "Application Support" / "VintagestoryData" / "WorldEdit"),
    ))
    export_dir = export_dir.expanduser().resolve()
    if not export_dir.is_dir():
        print(f"Directory does not exist: {export_dir}\nCreate it or enter the correct server WorldEdit directory, then rerun.")
        return 2
    if plan:
        prefix = plan["prefix"]
    else:
        prefix_input = args.prefix if args.prefix is not None else prompt_value("Export name prefix", "channel_capture")
        prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", prefix_input)

    if plan:
        cuboids = [Cuboid(item["index"], **capture_plan.box(item), name=item["name"], plan_mode=True)
                   for item in plan["cuboids"]]
        bounds = {key: (min if key.endswith("min") else max)(b[key] for b in plan["requests"])
                  for key in capture_plan.BOUND_KEYS}
        print(f"Saved v12 plan: {args.plan}\nRequested footprint and cuboid preview: {args.plan.with_suffix('.svg')}")
        print(f"Geometry: {plan['geometry_version']} / {plan['geometry_hash']}\nRequested initial Y: {y_bounds['ymin']}..{y_bounds['ymax']}")
        print(f"Initial horizontal caps: X {plan['partition'].get('x_span', capture_plan.MAX_SIDE)}, Z {plan['partition']['z_span']} blocks.")
    else:
        cuboids, bounds = route_corridor_slices(y_bounds["ymin"], y_bounds["ymax"], z_span, prefix)
    oversized = [c for c in cuboids if max(c.size) > 1023 or
                 c.size[0] * c.size[1] * c.size[2] > MAX_SELECTION_BLOCKS]
    if oversized:
        worst = max(oversized, key=lambda c: c.size[0] * c.size[1] * c.size[2])
        print(
            f"Requested slice size exceeds safety limits (largest is {worst.size[0]}×{worst.size[1]}×{worst.size[2]}). "
            f"Reduce --z-span; each dimension must be <=1023 and volume <={MAX_SELECTION_BLOCKS:,}."
        )
        return 2
    run_dir = args.plan.parent if plan else Path(__file__).parent
    command_dir = run_dir / f"{prefix}-commands"
    command_dir.mkdir(parents=True, exist_ok=True)
    print(f"Command files are stored in: {command_dir}")
    signature = {"bounds": bounds, "z_span": z_span,
                 "channel_max_width": CHANNEL_MAX_WIDTH, "capture_margin": CAPTURE_MARGIN,
                 "north_extra": NORTH_EXTRA, "route_xz": ROUTE_XZ,
                 "player": player, "target_pane": target_pane,
                 "export_dir": str(export_dir.resolve()), "prefix": prefix}
    if plan:
        signature = {"capture_plan_hash": plan["plan_hash"], "geometry_hash": plan["geometry_hash"],
                     "bounds": bounds, "export_dir": str(export_dir), "prefix": prefix}
    state_path = run_dir / f"{prefix}-progress.json"
    if plan and state_path.exists() and capture_plan.read_json(state_path).get("signature") != signature:
        parser.error("Existing v12 progress uses another plan/export directory; refusing to overwrite it")
    state = load_state(state_path, signature)
    status_reporter = RunStatus(cuboids, state)
    complete_now = accepted_now = incomplete_now = absent_now = 0
    for c in cuboids:
        output_path = export_dir / f"{c.name}.json"
        previous = state["sections"].get(c.name, {})
        if output_path.exists():
            problems = validate_export(output_path, c)
            if not problems:
                if previous.get("status") == "accepted":
                    accepted_now += 1
                else:
                    complete_now += 1
                    state["sections"][c.name] = {**(previous if plan else {}), "status": "complete", "file": str(output_path),
                                                 "validated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            else:
                incomplete_now += 1
                if not (previous.get("segmented") or
                        (previous.get("status") == "partial" and previous.get("gaps"))):
                    state["sections"][c.name] = {"status": "incomplete", "file": str(output_path), "problems": problems}
        elif (previous.get("status") in ("complete", "accepted") and
              not previous.get("segmented")):
            state["sections"].pop(c.name, None)
            absent_now += 1
        else:
            absent_now += 1
    save_state(state_path, state)
    max_volume = max(c.size[0] * c.size[1] * c.size[2] for c in cuboids)
    max_x_span = max(c.size[0] for c in cuboids)
    coverage_path = state_path.with_name(state_path.stem.replace("-progress", "-coverage") + ".json")
    if plan:
        print(f"Capture plan: {len(cuboids)} nonoverlapping v12 cuboids; largest selection {max_volume:,} blocks.")
        print(f"Progress: {state_path}\nVerified {complete_now}; incomplete {incomplete_now}; pending {absent_now}.")
    elif args.verbose or args.step_by_step:
        print(f"\nCapture plan: {len(cuboids)} route-following Z slices; X span varies up to {max_x_span}, Y is {bounds['ymax']-bounds['ymin']+1}, Z is at most {z_span} blocks.")
        print(f"Largest planned selection: {max_volume:,} blocks.")
        print(f"Buffered route corridor extents: X {bounds['xmin']}..{bounds['xmax']}, Z {bounds['zmin']}..{bounds['zmax']}.")
        print(f"Current files: {complete_now} verified, {accepted_now} previously accepted, {incomplete_now} incomplete, {absent_now} not present.")
        print(f"Progress record: {state_path}\nCoverage and gap manifest: {coverage_path}\nExports watched in: {export_dir}\n")
    else:
        print(f"Ready: {len(cuboids)} sections; {complete_now} verified, {incomplete_now} incomplete, {absent_now} pending.")
    if not args.step_by_step:
        print("Ctrl-C pauses; r retries, q quits, a second Ctrl-C aborts.")

    original_sigint_handler = signal.getsignal(signal.SIGINT)
    try:
        for c in cuboids:
            record = state["sections"].get(c.name, {})
            output_path = export_dir / f"{c.name}.json"
            if (record.get("status") in ("complete", "accepted") and output_path.exists()
                    and not record.get("segmented")):
                print(f"Section {c.index}/{len(cuboids)} already complete or accepted; moving on.")
                continue
            if output_path.exists() and not validate_export(output_path, c):
                state["sections"][c.name] = {**(record if plan else {}), "status": "complete", "file": str(output_path),
                                             "validated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                save_state(state_path, state)
                continue

            if args.verbose or args.step_by_step:
                print(f"\nSection {c.index}/{len(cuboids)}: {region_label(c)}")
            else:
                print(f"\nSection {c.index}/{len(cuboids)}")
            if not args.step_by_step:
                while True:
                    try:
                        pieces, gaps = recover_automatically(
                            c, 0, player, target_pane, command_dir, export_dir, state, state_path,
                            status_reporter, args.verbose)
                        record = state["sections"].get(c.name, {})
                        if record.get("segmented") or gaps:
                            finalize_split_result(c, pieces, gaps, export_dir, state, state_path)
                        break
                    except KeyboardInterrupt:
                        pause_after_interrupt(state, state_path)
                status_reporter.maybe_report()
                continue
            if args.step_by_step and record.get("segmented") and record.get("gaps"):
                print("Resuming the recorded split regions; the larger parent selection will not be retried.")
                pieces, gaps = recover_by_splitting(
                    c, 0, player, target_pane, command_dir, export_dir, state, state_path,
                    split_immediately=True)
                finalize_split_result(c, pieces, gaps, export_dir, state, state_path)
                continue
            retry_confirm = True
            while True:
                status, problems = run_export_attempt(c, player, target_pane, command_dir,
                                                      export_dir, state, state_path, retry_confirm)
                if status == "not-sent":
                    continue
                if status == "skipped":
                    gap = [cuboid_record(c)]
                    state["sections"][c.name] = {"status": "partial", "pieces": [], "gaps": gap,
                                                 "skip_reason": "user skipped before export"}
                    save_state(state_path, state)
                    break
                if status == "complete":
                    print(f"Section {c.index} complete: the full export passed validation. Moving to the next section.")
                    break
                print(f"Export is incomplete for {region_label(c)}:")
                for problem in problems:
                    print(f"  - {problem}")
                while True:
                    choice = input("[r]etry now, [b]reak/segment, [a]ccept, [s]kip, [q]uit: ").strip().lower()
                    if choice in ("r", "b", "a", "s", "q"):
                        break
                    print("Enter r, b, a, s, or q. No action was taken.")
                if choice == "r":
                    retry_confirm = False
                    continue
                if choice == "a":
                    returned = returned_export_bounds(output_path, c) if output_path.exists() else None
                    state["sections"][c.name] = {"status": "accepted", "file": str(output_path),
                                                 "problems": problems,
                                                 "returned_bounds": returned,
                                                 "gaps": uncovered_remainder(c, returned),
                                                 "accepted_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                    save_state(state_path, state)
                    break
                if choice == "s":
                    state["sections"][c.name] = {
                        "status": "partial", "file": str(output_path), "pieces": [],
                        "gaps": [cuboid_record(c)], "skip_reason": "user skipped section",
                    }
                    save_state(state_path, state)
                    break
                if choice == "q":
                    raise KeyboardInterrupt

                pieces, gaps = recover_by_splitting(
                    c, 0, player, target_pane, command_dir, export_dir, state, state_path,
                    split_immediately=True)
                finalize_split_result(c, pieces, gaps, export_dir, state, state_path)
                break
        has_gaps = any(record.get("gaps") or record.get("status") not in ("complete", "accepted")
                       for record in state["sections"].values())
        if plan:
            result = write_plan_audit(plan, args.plan, state, export_dir)
            state["finished"] = result == 0
            save_state(state_path, state)
            return result
        state["finished"] = not has_gaps
        save_state(state_path, state)
        if has_gaps:
            print(f"All sections were processed. Uncovered regions are recorded in {coverage_path}.")
        else:
            print("All sections are complete or accepted.")
        return 0
    except (KeyboardInterrupt, EOFError, UserQuit):
        signal.signal(signal.SIGINT, abort_immediately)
        if try_save_state(state_path, state):
            print(f"\nStopped. Progress saved to {state_path}; rerun to resume.")
        else:
            print("\nStopped. Rerun to resume from the last completed save.")
        return 130
    except (OSError, ValueError) as exc:
        signal.signal(signal.SIGINT, abort_immediately)
        saved = try_save_state(state_path, state)
        print(f"Error: {exc}", file=sys.stderr)
        if saved:
            print(f"Progress saved to {state_path}.", file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGINT, original_sigint_handler)


if __name__ == "__main__":
    raise SystemExit(main())
