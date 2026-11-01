#!/usr/bin/env python3
# vim:et sts=4 sw=4
"""Collect and verify Vintage Story WorldEdit exports from a saved coastline plan.

The saved plan defines the normal selection bounds, heights, and export
names. Empty columns can trigger retained, chunk-sized deeper diagnostic
exports without changing the plan. The default plan is
capture-plans/coastal-connection/plan.json beside this
script; v12_capture_plan.py creates and revises that specification.
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
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

import v12_capture_plan as capture_plan
import capture_rescan


WORLD_CENTER = capture_plan.WORLD_CENTER
DEFAULT_PLAN = Path(__file__).resolve().parent / "capture-plans" / "coastal-connection" / "plan.json"
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
                               child_ends[2], name))
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
    for key in ("DecorIndices", "DecorIds"):
        if key in data and not isinstance(data[key], list):
            problems.append(f"{key} is not an array")
    evidence = capture_plan.inspect_export(path, cuboid_record(c))
    problems.extend(evidence["problems"])
    columns = evidence.get("column_presence", {})
    missing = columns.get("unverified_columns", columns.get("missing_columns", 0))
    if missing:
        problems.append(f"{missing} X/Z columns have no block/decor record at any returned height; column coverage is unverified")
    return problems


def validated_export_record(c: Cuboid, path: Path, previous: dict | None = None) -> dict:
    """Replace failed-split state when a usable direct parent export exists."""
    evidence = capture_plan.inspect_export(path, cuboid_record(c))
    if evidence["problems"] or evidence["covered"] is None:
        raise ValueError("Cannot checkpoint an invalid export: " + "; ".join(evidence["problems"]))
    if evidence.get("column_presence", {}).get("unverified_columns"):
        raise ValueError("Cannot checkpoint an export with unverified empty columns")
    # Keep evidence for a locally merged file: its envelope must not conceal a
    # missing upper layer in an individual piece. Direct WorldEdit files can
    # supersede the recovery tree that the earlier validator created for them.
    retained = previous if previous and previous.get("merged") else {}
    record = {**retained, "status": "complete", "file": str(path),
              "validated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "returned_bounds": evidence["covered"], "ceiling_blocks": evidence["ceiling_blocks"],
              "column_presence": evidence["column_presence"]}
    if evidence.get("pieces"):
        pieces = [{**item, "file": str(path.parent / Path(item["file"]).name)} for item in evidence["pieces"]]
        record.update(merged=True, segmented=True, piece_regions=pieces,
                      pieces=[item["file"] for item in pieces], gaps=[])
    return record


def returned_export_bounds(path: Path, c: Cuboid) -> dict[str, int] | None:
    """Return the independently validated, reported three-dimensional envelope."""
    return capture_plan.inspect_export(path, cuboid_record(c))["covered"]


def cuboid_record(c: Cuboid) -> dict[str, int]:
    return {"xmin": c.xmin, "xmax": c.xmax, "ymin": c.ymin,
            "ymax": c.ymax, "zmin": c.zmin, "zmax": c.zmax}


def cuboid_from_record(parent: Cuboid, bounds: dict[str, int], name: str) -> Cuboid:
    return Cuboid(parent.index, bounds["xmin"], bounds["xmax"], bounds["ymin"],
                  bounds["ymax"], bounds["zmin"], bounds["zmax"], name)


def uncovered_remainder(c: Cuboid, returned: dict[str, int] | None) -> list[dict[str, int]]:
    """Return exact missing X/Y/Z regions, without treating shortened Y as air."""
    return capture_plan.difference([cuboid_record(c)], [returned] if returned else [])


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    manifest_path = path.with_name(path.stem.replace("-progress", "-coverage") + ".json")
    manifest = {
        "signature": state.get("signature"), "updated_utc": state["updated_utc"],
        "coverage_basis": "Execution checkpoint only. Use the saved plan's independent 3D audit for verified coverage and exact height gaps.",
        "sections": state.get("sections", {}),
    }
    manifest_tmp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    manifest_tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_tmp.replace(manifest_path)


def load_state(path: Path, signature: dict[str, Any]) -> dict[str, Any]:
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(old, dict) or old.get("signature") != signature:
            raise ValueError("Existing progress uses another plan/export directory; refusing to overwrite it")
        return old
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


def paste_commands(command_path: Path, target_pane: str, tmux_binary: str = "tmux") -> None:
    """Use the same tmux executable for selection, export, and teleport commands."""
    subprocess.run([tmux_binary, "load-buffer", str(command_path), ";",
                    "paste-buffer", "-d", "-t", target_pane], check=True)


def maybe_rescan_export(c: Cuboid, output_path: Path, player: str, target_pane: str,
                        command_dir: Path, export_dir: Path, state: dict, state_path: Path,
                        tmux_binary: str) -> None:
    """Probe empty columns only during collection, with acknowledged loading."""
    evidence = capture_plan.inspect_export(output_path, cuboid_record(c))
    columns = evidence.get("column_presence", {})
    if not columns.get("unverified_columns", columns.get("missing_columns", 0)):
        return
    settings = state.get("rescan_settings", {})
    logfile = Path(settings.get("server_log") or export_dir.parent / "Logs" / "server-main.log")
    if not logfile.is_file():
        print(f"Cannot verify chunk loading for deeper rescan: server log unavailable at {logfile}. Use --server-log.")
        return
    registry = state.setdefault("column_rescans", {"version": 1, "probes": {}, "sections": {}})
    console = capture_rescan.Console(tmux_binary, target_pane, logfile,
                                    command_dir / "rescans", paste_commands)
    try:
        capture_rescan.rescan_export(
            output_path, cuboid_record(c), prefix=state.get("signature", {}).get("prefix", c.name),
            player=player, console=console, export_dir=export_dir, registry=registry,
            save=lambda: save_state(state_path, state),
            stable=lambda path: stable_file(path, lambda: False), archive=archive_invalid,
            ymin=settings.get("ymin", 0))
    except (OSError, ValueError, TimeoutError, RuntimeError) as exc:
        # Leave the source intact and fall back to bounded recovery. A deeper
        # export lacking records must never silently bless an unloaded chunk.
        print(f"Deeper rescan unresolved: {exc}")
        record = registry.setdefault("sections", {}).setdefault(c.name, {})
        record.update(status="unresolved", problem=str(exc))
        save_state(state_path, state)


def run_export_attempt(c: Cuboid, player: str, target_pane: str, command_dir: Path,
                       export_dir: Path, state: dict[str, Any], state_path: Path,
                       confirm: bool = True, verbose: bool = True,
                       tmux_binary: str = "tmux") -> tuple[str, list[str]]:
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
    paste_commands(command_path, target_pane, tmux_binary)
    if confirm or verbose:
        print("Commands pasted. Waiting for the export file to appear.")
    state["sections"][c.name] = {"status": "waiting", "file": str(output_path)}
    save_state(state_path, state)
    if confirm or verbose:
        print(f"Waiting for a stable export for {region_label(c)}.")
    if not stable_file(output_path, lambda: False):
        raise KeyboardInterrupt
    maybe_rescan_export(c, output_path, player, target_pane, command_dir, export_dir,
                        state, state_path, tmux_binary)
    problems = validate_export(output_path, c)
    if problems:
        state["sections"][c.name] = {"status": "incomplete", "file": str(output_path), "problems": problems}
        save_state(state_path, state)
        if not confirm and not verbose:
            print("incomplete: " + "; ".join(problems))
        return "incomplete", problems
    state["sections"][c.name] = {"status": "complete", "file": str(output_path),
                                 "validated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    evidence = capture_plan.inspect_export(output_path, cuboid_record(c))
    state["sections"][c.name].update(returned_bounds=evidence["covered"],
                                     ceiling_blocks=evidence["ceiling_blocks"],
                                     column_presence=evidence["column_presence"])
    short_height = evidence["covered"] and evidence["covered"]["ymax"] < c.ymax
    if not confirm and not verbose:
        print("export validated" + (" (height trimmed)" if short_height else ""))
    if evidence["ceiling_blocks"]:
        print(f"\nHeight warning: {evidence['ceiling_blocks']} entries reach requested Y ceiling {c.ymax} in {c.name}.")
    if short_height:
        print(f"Stored height ends at Y {evidence['covered']['ymax']}; requested through {c.ymax}. "
              "WorldEdit omits air; the final audit keeps this height difference unverified.")
    save_state(state_path, state)
    return "complete", []


def teleport_player_to_cuboid(c: Cuboid, player: str, target_pane: str,
                              command_dir: Path, verbose: bool = True,
                              tmux_binary: str = "tmux") -> Path:
    """Request creative mode and teleport using the same X/Z coordinates as `/we select`."""
    x = (c.xmin + c.xmax) // 2
    y = max(c.ymin, c.ymax - 2)
    z = (c.zmin + c.zmax) // 2
    command_path = command_dir / f"teleport_section{c.index:04d}_x{c.xmin}_{c.xmax}_z{c.zmin}_{c.zmax}.txt"
    command_path.write_text(f"/gm {player} creative\n/tp {player} {x} {y} {z}\n", encoding="utf-8")
    if verbose:
        print(f"Teleport command file ready: {command_path}")
    paste_commands(command_path, target_pane, tmux_binary)
    if verbose:
        print(f"Teleport sent ({x}, {y}, {z}).")
    return command_path


def recover_automatically(c: Cuboid, depth: int, player: str, target_pane: str,
                          command_dir: Path, export_dir: Path, state: dict[str, Any],
                          state_path: Path, status_reporter: RunStatus,
                          verbose: bool = False, tmux_binary: str = "tmux"
                          ) -> tuple[list[tuple[Cuboid, Path]], list[dict[str, int]]]:
    """Pause on Ctrl-C at the current region, retaining completed siblings."""
    restart_pending = False
    while True:
        try:
            return _recover_automatically(c, depth, player, target_pane, command_dir,
                                          export_dir, state, state_path, status_reporter,
                                          verbose, restart_pending, tmux_binary)
        except KeyboardInterrupt:
            pause_after_interrupt(state, state_path)
            # A file received while paused is not a validated save. Send this
            # selection again unless its validation was already checkpointed.
            restart_pending = state["sections"].get(c.name, {}).get("status") != "complete"


def _recover_automatically(c: Cuboid, depth: int, player: str, target_pane: str,
                           command_dir: Path, export_dir: Path, state: dict[str, Any],
                           state_path: Path, status_reporter: RunStatus,
                           verbose: bool, restart_pending: bool, tmux_binary: str
                           ) -> tuple[list[tuple[Cuboid, Path]], list[dict[str, int]]]:
    """Attempt, teleport and retry, then split at most twice before asking."""
    output_path = export_dir / f"{c.name}.json"
    saved = state.get("sections", {}).get(c.name, {})
    if restart_pending and output_path.exists():
        checkpoint = state.get("column_rescans", {}).get("sections", {}).get(c.name, {})
        if checkpoint.get("sha256"):
            current = capture_plan.inspect_export(output_path, cuboid_record(c))
            if not current["problems"] and current.get("sha256") == checkpoint["sha256"]:
                # A completed rescan checkpoint is usable even when the
                # larger selection was interrupted before reaching complete.
                restart_pending = False
    if not restart_pending and output_path.exists() and not saved.get("segmented"):
        maybe_rescan_export(c, output_path, player, target_pane, command_dir, export_dir,
                            state, state_path, tmux_binary)
    if not restart_pending and output_path.exists() and not validate_export(output_path, c):
        state["sections"][c.name] = validated_export_record(c, output_path, saved)
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
                state, state_path, status_reporter, verbose, tmux_binary)
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
                                    state, state_path, confirm=False, verbose=verbose,
                                    tmux_binary=tmux_binary)
        if verbose:
            label = "passed validation" if result[0] == "complete" else result[0]
            print(f"Export {label} for {region_label(c)}.")
            for problem in result[1]:
                print(f"  - {problem}")
        status_reporter.maybe_report()
        return result

    result, problems = attempt()
    if result == "complete":
        return [(c, output_path)], []

    print("teleporting ... ", end="", flush=True)
    teleport_player_to_cuboid(c, player, target_pane, command_dir, verbose=verbose,
                              tmux_binary=tmux_binary)
    print(f"waiting {AUTO_TELEPORT_DELAY}s ... ", end="", flush=True)
    time.sleep(AUTO_TELEPORT_DELAY)
    print("delay elapsed; retrying export")
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
            print(f"section {c.index} export files validated")
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
                         state_path: Path, split_immediately: bool = False,
                         tmux_binary: str = "tmux"
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
                child, depth + 1, player, target_pane, command_dir, export_dir, state, state_path,
                tmux_binary=tmux_binary)
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
                gap_cuboid, depth, player, target_pane, command_dir, export_dir, state, state_path,
                tmux_binary=tmux_binary)
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
                                              export_dir, state, state_path, retry_confirm,
                                              tmux_binary=tmux_binary)
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
                teleport_player_to_cuboid(c, player, target_pane, command_dir,
                                          tmux_binary=tmux_binary)
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
            print(f"Section {c.index}: all {len(pieces)} split files and the combined export passed validation. "
                  "The final audit determines 3D coverage.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            state["sections"][c.name] = {
                **state["sections"].get(c.name, {}),
                "status": "partial", "file": str(output_path),
                "pieces": [str(path) for _, path in pieces], "gaps": [],
                "problems": [f"Could not combine split exports: {exc}"]
            }
            save_state(state_path, state)
            print(f"Section {c.index} has {len(pieces)} validated split files, "
                  f"but the combined file could not be created: {exc}")
    else:
        print("No usable export pieces were returned; the full section is recorded as an uncovered region.")


def prompt_value(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def write_plan_audit(plan: dict, plan_path: Path, state: dict, export_dir: Path) -> int:
    report = capture_plan.audit_coverage(plan, state, export_dir, progress=capture_plan.audit_progress)
    path = plan_path.with_name(plan["prefix"] + "-audit.json")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2) + "\n")
    tmp.replace(path)
    capture_plan.render_preview(plan, plan_path.with_name(plan["prefix"] + "-coverage.svg"), report["gaps"])
    capture_plan.render_column_preview(plan, report, plan_path.with_name(plan["prefix"] + "-columns.svg"))
    print(f"Verified reported 3D coverage: {report['coverage_percent']:.6f}% "
          f"({report['verified_envelope_blocks']:,}/{report['requested_blocks']:,} positions).")
    print(f"Exact uncovered regions: {len(report['gaps'])}. Audit: {path}")
    columns = report["column_presence"]
    print(f"Column evidence: {columns['observed_columns']:,}/{columns['requested_columns']:,}; "
          f"{columns['missing_columns']:,} unverified; "
          f"{columns['empty_columns_confirmed_by_rescan']:,} empty bands corroborated by deeper rescans.")
    for warning in report["warnings"]:
        print(f"Height/coverage warning: {warning}")
    print(report["evidence_limits"])
    return 0 if report["complete"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN,
                        help="saved coastline capture plan (default: capture-plans/coastal-connection/plan.json beside the script)")
    parser.add_argument("--target-pane", help="tmux pane containing the server console")
    parser.add_argument("--tmux-binary", type=Path,
                        help="specific tmux executable; otherwise use tmux from PATH")
    parser.add_argument("--preview", action="store_true",
                        help="render the saved plan and exit without sending commands")
    parser.add_argument("--export-dir", type=Path,
                        help="directory containing WorldEdit JSON exports (otherwise prompted, or read from progress for a report)")
    parser.add_argument("--report-coverage", action="store_true",
                        help="revalidate exports and report exact X/Y/Z coverage without sending commands")
    parser.add_argument("--verbose", action="store_true", help="print bounds and validation details")
    parser.add_argument("--server-log", type=Path,
                        help="server-main.log for acknowledged chunk loading during automatic deeper rescans (default: ../Logs/server-main.log relative to export directory)")
    parser.add_argument("--rescan-y-min", type=int, default=0,
                        help="lower Y for automatic 32x32 deeper probes of empty columns (default: 0; normal plan bounds stay unchanged)")
    parser.add_argument("--step-by-step", action="store_true",
                        help="confirm each export instead of using automatic recovery")
    args = parser.parse_args()
    if not args.preview and not args.report_coverage and not (args.target_pane or "").strip():
        parser.error("--target-pane is required for collection")
    args.plan = args.plan.expanduser().resolve()
    try:
        plan = capture_plan.load_plan(args.plan)
        capture_plan.render_preview(plan, args.plan.with_suffix(".svg"))
        if args.preview:
            capture_plan.print_summary(plan, args.plan)
            return 0
        if args.report_coverage:
            progress = args.plan.with_name(plan["prefix"] + "-progress.json")
            state = capture_plan.read_json(progress) if progress.exists() else {}
            if state and state.get("signature", {}).get("capture_plan_hash") != plan["plan_hash"]:
                raise ValueError("Progress belongs to a different capture plan")
            export_path = args.export_dir or state.get("signature", {}).get("export_dir")
            if not export_path:
                parser.error("--export-dir is required until this plan has a saved run")
            return write_plan_audit(plan, args.plan, state, Path(export_path).expanduser().resolve())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))

    target_pane = args.target_pane.strip()
    tmux_binary = str(args.tmux_binary.expanduser().resolve()) if args.tmux_binary else "tmux"
    print("Vintage Story coastline export coordinator (terrain is never modified).")
    print("Mode: " + ("step-by-step" if args.step_by_step else "automatic"))
    player = prompt_value("Player name for /executeas", DEFAULT_PLAYER)
    export_dir = args.export_dir or Path(prompt_value(
        "WorldEdit export directory",
        str(Path.home() / "Library" / "Application Support" / "VintagestoryData" / "WorldEdit"),
    ))
    export_dir = export_dir.expanduser().resolve()
    if not export_dir.is_dir():
        print(f"Export directory does not exist: {export_dir}. Enter the actual WorldEdit export directory.")
        return 2

    prefix = plan["prefix"]
    cuboids = [Cuboid(item["index"], **capture_plan.box(item), name=item["name"])
               for item in plan["cuboids"]]
    bounds = {key: (min if key.endswith("min") else max)(b[key] for b in plan["requests"])
              for key in capture_plan.BOUND_KEYS}
    y_bounds = plan["requested_y"]
    print(f"Saved coastline plan: {args.plan}\nFootprint and cuboid preview: {args.plan.with_suffix('.svg')}")
    print(f"Geometry: {plan['geometry_version']} / {plan['geometry_hash']}")
    print(f"Requested initial Y: {y_bounds['ymin']}..{y_bounds['ymax']}")
    print(f"Initial horizontal caps: X {plan['partition'].get('x_span', capture_plan.MAX_SIDE)}, Z {plan['partition']['z_span']} blocks.")
    run_dir = args.plan.parent
    command_dir = run_dir / f"{prefix}-commands"
    signature = {"capture_plan_hash": plan["plan_hash"], "geometry_hash": plan["geometry_hash"],
                 "bounds": bounds, "export_dir": str(export_dir), "prefix": prefix}
    state_path = run_dir / f"{prefix}-progress.json"
    try:
        state = load_state(state_path, signature)
        state["rescan_settings"] = {"ymin": args.rescan_y_min,
            "server_log": str(args.server_log.expanduser().resolve()) if args.server_log else None}
        command_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(f"Command files: {command_dir}")
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
                    state["sections"][c.name] = validated_export_record(c, output_path, previous)
            else:
                incomplete_now += 1
                if not (previous.get("segmented") or
                        (previous.get("status") == "partial" and previous.get("gaps"))):
                    state["sections"][c.name] = {"status": "incomplete", "file": str(output_path), "problems": problems}
        elif (previous.get("status") in ("complete", "accepted") and not previous.get("segmented")):
            state["sections"].pop(c.name, None)
            absent_now += 1
        else:
            absent_now += 1
    save_state(state_path, state)
    max_volume = max(c.size[0] * c.size[1] * c.size[2] for c in cuboids)
    print(f"Capture plan: {len(cuboids)} nonoverlapping cuboids; largest selection {max_volume:,} blocks.")
    print(f"Progress: {state_path}\nVerified {complete_now}; accepted {accepted_now}; incomplete {incomplete_now}; pending {absent_now}.")
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
                state["sections"][c.name] = validated_export_record(c, output_path, record)
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
                            status_reporter, args.verbose, tmux_binary)
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
                    split_immediately=True, tmux_binary=tmux_binary)
                finalize_split_result(c, pieces, gaps, export_dir, state, state_path)
                continue
            retry_confirm = True
            while True:
                status, problems = run_export_attempt(c, player, target_pane, command_dir,
                                                      export_dir, state, state_path, retry_confirm,
                                                      tmux_binary=tmux_binary)
                if status == "not-sent":
                    continue
                if status == "skipped":
                    gap = [cuboid_record(c)]
                    state["sections"][c.name] = {"status": "partial", "pieces": [], "gaps": gap,
                                                 "skip_reason": "user skipped before export"}
                    save_state(state_path, state)
                    break
                if status == "complete":
                    print(f"Section {c.index}: export file passed validation. The final audit checks 3D coverage.")
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
                    split_immediately=True, tmux_binary=tmux_binary)
                finalize_split_result(c, pieces, gaps, export_dir, state, state_path)
                break
        result = write_plan_audit(plan, args.plan, state, export_dir)
        state["finished"] = result == 0
        save_state(state_path, state)
        return result
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
