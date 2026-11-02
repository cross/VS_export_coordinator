#!/usr/bin/env python3
"""Offline v12 capture planning, previews, and raw WorldEdit validation.

All bounds are inclusive, in relative Vintage Story block coordinates. No server
commands are issued by this module. The saved plan, not current source geometry,
defines the requested capture geometry. Run audits are handled by capture_run.
"""
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from fractions import Fraction
import hashlib
from html import escape
import json
import math
from pathlib import Path
from typing import Any

VERSION = "v12-capture/1"
WORLD_CENTER = 512_000
MAX_SIDE = 1023
DEFAULT_X_SPAN = 450
MAX_VOLUME = 9_000_000
BUFFER = 100
AXES = ("x", "y", "z")
BOUND_KEYS = tuple(k for axis in AXES for k in (axis + "min", axis + "max"))
HERE = Path(__file__).resolve().parent
DEFAULT_GEOMETRY = HERE / "capture-plans" / "coastal-connection" / "coastline_route_v12.py"


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def box(value: dict) -> dict[str, int]:
    result = {key: value[key] for key in BOUND_KEYS}
    if any(type(v) is not int for v in result.values()):
        raise ValueError("Cuboid bounds must be integers")
    if any(result[a + "min"] > result[a + "max"] for a in AXES):
        raise ValueError("Cuboid minimum exceeds maximum")
    return result


def volume(b: dict) -> int:
    return math.prod(b[a + "max"] - b[a + "min"] + 1 for a in AXES)


def intersection(a: dict, b: dict) -> dict | None:
    out = {key: (max(a[key], b[key]) if key.endswith("min") else min(a[key], b[key]))
           for key in BOUND_KEYS}
    return out if all(out[k + "min"] <= out[k + "max"] for k in AXES) else None


def subtract(a: dict, b: dict) -> list[dict]:
    """Disjoint, exact A minus B; at most six boxes, including Y gaps."""
    overlap = intersection(a, b)
    if overlap is None:
        return [box(a)]
    core = box(a)
    pieces = []
    for axis in AXES:
        lo, hi = axis + "min", axis + "max"
        if core[lo] < overlap[lo]:
            pieces.append({**core, hi: overlap[lo] - 1})
            core[lo] = overlap[lo]
        if core[hi] > overlap[hi]:
            pieces.append({**core, lo: overlap[hi] + 1})
            core[hi] = overlap[hi]
    return pieces


def difference(requests: list[dict], covers: list[dict]) -> list[dict]:
    # Broad phase by Z prevents scanning unrelated exports for each row run.
    remaining = []
    ordered = sorted(covers, key=lambda b: b["zmin"])
    for request in requests:
        pieces = [box(request)]
        for cover in ordered:
            if cover["zmin"] > request["zmax"]:
                break
            if cover["zmax"] < request["zmin"]:
                continue
            pieces = [part for p in pieces for part in subtract(p, cover)]
            if not pieces:
                break
        remaining.extend(pieces)
    return remaining


def union_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(intervals):
        if lo > hi:
            raise ValueError("Reversed row interval")
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def read_shores(path: Path) -> dict:
    """Read literals without executing the external geometry module."""
    values = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("WEST_SHORE", "EAST_SHORE"):
                    values[target.id] = ast.literal_eval(node.value)
    if set(values) != {"WEST_SHORE", "EAST_SHORE"}:
        raise ValueError("Geometry must define literal WEST_SHORE and EAST_SHORE paths")
    # Normalize tuples to JSON arrays so repeat generation is byte-for-byte
    # comparable to a plan loaded from disk.
    return json.loads(json.dumps(values))


def shore_segments(parts: list) -> list:
    segments = []
    start = None
    for op, points in parts:
        pts = [tuple(Fraction(str(v)) for v in p) for p in points]
        if op == "M" and start is None and len(pts) == 1:
            start = pts[0]
            continue
        if start is None or op not in ("L", "C") or len(pts) != (1 if op == "L" else 3):
            raise ValueError("Expected one M followed by L/C segments")
        controls = [start, *pts]
        if any(a[1] > b[1] for a, b in zip(controls, controls[1:])) or start[1] >= pts[-1][1]:
            raise ValueError("Shoreline must be strictly Z-monotone (controls may share Z)")
        scale = math.lcm(*(v.denominator for p in controls for v in p))
        integers = [tuple(int(v * scale) for v in p) for p in controls]
        segments.append((controls, integers, scale))
        start = pts[-1]
    return segments


def bezier_num(values: list[int], n: int, d: int) -> int:
    u = d - n
    return values[0]*u**3 + 3*values[1]*u*u*n + 3*values[2]*u*n*n + values[3]*n**3


def shore_interval(segments: list, z: int) -> tuple[Fraction, Fraction]:
    """Certified enclosure of X at integer Z, with exact rational arithmetic.

    Bisection brackets the unique Z root. The Bezier derivative bound
    3*max(abs(adjacent X differences)) encloses X throughout that bracket.
    No floating-point sampling or chord approximation can miss a curve bulge.
    """
    for controls, integers, scale in segments:
        if not controls[0][1] <= z <= controls[-1][1]:
            continue
        if z == controls[0][1]:
            return controls[0][0], controls[0][0]
        if z == controls[-1][1]:
            return controls[-1][0], controls[-1][0]
        if len(controls) == 2:
            a, b = controls
            x = a[0] + (b[0] - a[0]) * (z - a[1]) / (b[1] - a[1])
            return x, x
        zs = [p[1] for p in integers]
        xs = [p[0] for p in integers]
        lo, d = 0, 1
        for _ in range(48):
            n, d = lo * 2 + 1, d * 2
            zz = bezier_num(zs, n, d)
            target = z * scale * d**3
            if zz == target:
                x = Fraction(bezier_num(xs, n, d), scale * d**3)
                return x, x
            lo = n if zz < target else n - 1
        x = Fraction(bezier_num(xs, lo, d), scale * d**3)
        error = Fraction(3 * max(abs(a - b) for a, b in zip(xs, xs[1:])), scale * d)
        return x - error, x + error
    raise ValueError(f"Shore does not reach Z={z}")


def footprint(shores: dict) -> tuple[dict, dict]:
    west, east = (shore_segments(shores[k]) for k in ("WEST_SHORE", "EAST_SHORE"))
    raw = {}
    for z in range(59375, 62361):
        left = Fraction(-34500) if z < 59750 else shore_interval(west, z)[0]
        right = Fraction(-32680) if z > 61994 else shore_interval(east, z)[1]
        if left > right:
            raise ValueError(f"Crossed capture shores at Z={z}")
        raw[z] = [(math.floor(left), math.ceil(right))]
    widened = {z: [(lo - BUFFER, hi + BUFFER) for lo, hi in intervals]
               for z, intervals in raw.items()}
    buffered = {}
    for z in range(59375 - BUFFER, 62361 + BUFFER):
        buffered[z] = union_intervals([interval for source_z in range(z-BUFFER, z+BUFFER+1)
                                      for interval in widened.get(source_z, [])])
    return raw, buffered


def row_boxes(rows: dict, ymin: int, ymax: int) -> list[dict]:
    """Exact rectangle partition by extending identical intervals across rows."""
    active: dict[tuple[int, int], dict] = {}
    result = []
    last_z = None
    for z, intervals in sorted(rows.items()):
        if last_z is not None and z != last_z + 1:
            result.extend(active.values())
            active = {}
        current = {}
        for lo, hi in union_intervals(intervals):
            key = lo, hi
            current[key] = active.pop(key, {"xmin": lo, "xmax": hi, "ymin": ymin,
                                           "ymax": ymax, "zmin": z, "zmax": z})
            current[key]["zmax"] = z
        result.extend(active.values())
        active, last_z = current, z
    return result + list(active.values())


def rows_for_boxes(boxes: list[dict]) -> dict:
    rows = defaultdict(list)
    for b in boxes:
        for z in range(b["zmin"], b["zmax"] + 1):
            rows[z].append((b["xmin"], b["xmax"]))
    return {z: union_intervals(intervals) for z, intervals in rows.items()}


def limited_boxes(b: dict, z_span: int, x_span: int = DEFAULT_X_SPAN) -> list[dict]:
    """Pre-split X, Z, and volume, including tall height supplements."""
    if any(type(n) is not int or not 1 <= n <= MAX_SIDE for n in (x_span, z_span)):
        raise ValueError("X and Z spans must be integers between 1 and 1023")
    out = []
    for y in range(b["ymin"], b["ymax"] + 1, MAX_SIDE):
        yhi = min(y + MAX_SIDE - 1, b["ymax"])
        for x in range(b["xmin"], b["xmax"] + 1, x_span):
            xhi = min(x + x_span - 1, b["xmax"])
            depth = min(z_span, MAX_SIDE, MAX_VOLUME // ((xhi-x+1)*(yhi-y+1)))
            for z in range(b["zmin"], b["zmax"] + 1, depth):
                out.append({"xmin": x, "xmax": xhi, "ymin": y, "ymax": yhi,
                            "zmin": z, "zmax": min(z + depth - 1, b["zmax"])})
    return out


def partition(requests: list[dict], z_span: int, mode: str,
              x_span: int = DEFAULT_X_SPAN) -> tuple[list[dict], list[dict]]:
    """Normalize requested union in Y slabs, then make disjoint safe exports.

    Exact mode exports only requested columns. Banded mode explicitly permits
    extra edge columns to reduce export-command overhead; audit still uses the
    original exact request. Every band and every Y slab is disjoint.
    """
    y_edges = sorted({v for b in requests for v in (b["ymin"], b["ymax"] + 1)})
    normalized, tasks = [], []
    for ymin, yend in zip(y_edges, y_edges[1:]):
        rows = rows_for_boxes([b for b in requests if b["ymin"] <= ymin < b["ymax"] + 1])
        exact = row_boxes(rows, ymin, yend - 1)
        normalized.extend(exact)
        if mode == "exact":
            task_boxes = exact
        else:
            bands = defaultdict(list)
            for z, intervals in rows.items():
                bands[(z // z_span) * z_span].extend(intervals)
            task_boxes = []
            for z, intervals in sorted(bands.items()):
                zs = [zz for zz in range(z, z+z_span) if zz in rows]
                for lo, hi in union_intervals(intervals):
                    task_boxes.append({"xmin": lo, "xmax": hi, "ymin": ymin,
                                       "ymax": yend - 1, "zmin": min(zs), "zmax": max(zs)})
        tasks.extend(part for b in task_boxes for part in limited_boxes(b, z_span, x_span))
    return normalized, sorted(tasks, key=lambda b: (b["zmin"], b["xmin"], b["ymin"]))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def supplemental_lakes(path: Path | None, base_rows: dict, ymin: int, ymax: int) -> tuple[list, list]:
    requests, records = [], []
    if path is None:
        return requests, records
    ids = set()
    for body in read_json(path)["bodies"]:
        name = body["id"]
        if not isinstance(name, str) or not name or name in ids:
            raise ValueError("Freshwater bodies need unique nonempty string IDs")
        ids.add(name)
        rows = defaultdict(list)
        for z, lo, hi in body["rows"]:
            if any(type(n) is not int for n in (z, lo, hi)) or lo > hi:
                raise ValueError("Freshwater rows must be [integer Z, X min, X max]")
            rows[z].append((lo, hi))
        selected = any(a <= hi and lo <= b for z, intervals in rows.items()
                       for lo, hi in intervals for a, b in base_rows.get(z, []))
        bounds = row_boxes(rows, body.get("ymin", ymin), body.get("ymax", ymax))
        for b in bounds:
            box(b)
        records.append({"id": name, "selected": selected, "rows": body["rows"],
                        "ymin": body.get("ymin", ymin), "ymax": body.get("ymax", ymax)})
        if selected:
            requests.extend(bounds)
    return requests, records


def seal_plan(payload: dict) -> dict:
    seed = digest(payload)[:12]
    payload["prefix"] = "v12_capture_" + seed
    for index, task in enumerate(payload["cuboids"], 1):
        task["index"] = index
        task["name"] = f"{payload['prefix']}_{index:05d}"
    payload["plan_hash"] = digest(payload)
    return payload


def create_plan(ymin: int, ymax: int, geometry: Path, z_span: int = 50,
                mode: str = "banded", freshwater: Path | None = None,
                x_span: int = DEFAULT_X_SPAN) -> dict:
    box(dict(xmin=0, xmax=0, ymin=ymin, ymax=ymax, zmin=0, zmax=0))
    if (any(type(n) is not int or not 1 <= n <= MAX_SIDE for n in (x_span, z_span))
            or mode not in ("exact", "banded")):
        raise ValueError("Invalid partition mode or X/Z span (expected integers 1..1023)")
    shores = read_shores(geometry)
    raw, rows = footprint(shores)
    base = row_boxes(rows, ymin, ymax)
    extra, lakes = supplemental_lakes(freshwater, rows, ymin, ymax)
    requested, tasks = partition(base + extra, z_span, mode, x_span)
    plan = seal_plan({
        "version": VERSION, "geometry_version": "accepted-coastlines-v12",
        "geometry_hash": digest(shores), "geometry_source_sha256": hashlib.sha256(geometry.read_bytes()).hexdigest(),
        "geometry": shores, "world_center": WORLD_CENTER,
        "coordinate_convention": "relative integer blocks; inclusive bounds; floor west / ceil east",
        "buffer": {"x": BUFFER, "z": BUFFER, "metric": "square / Chebyshev; union over integer rows"},
        "mouths": [{"zmin": 59375, "zmax": 59749, "west_anchor": -34500},
                   {"zmin": 61995, "zmax": 62360, "east_anchor": -32680}],
        "requested_y": {"ymin": ymin, "ymax": ymax},
        "limits": {"max_side": MAX_SIDE, "max_volume": MAX_VOLUME},
        "partition": {"mode": mode, "z_span": z_span, "x_span": x_span},
        "raw_footprint": row_boxes(raw, ymin, ymax), "freshwater_bodies": lakes,
        "requests": requested, "cuboids": tasks,
        "height_policy": "Reported shortened Y is unverified coverage; no implicit empty-air inference.",
    })
    verify_plan(plan)
    return plan


def extend_plan(parent: dict, regions: list[dict], freshwater: Path | None = None) -> dict:
    """Add explicit 3D requests while keeping the existing partition bounds."""
    tasks = [dict(b) for b in parent["cuboids"]]
    added = [box(b) for b in regions]
    lakes = []
    if freshwater:
        _, buffered = footprint(parent["geometry"])
        extra, lakes = supplemental_lakes(freshwater, buffered, **parent["requested_y"])
        added.extend(extra)
    if not added:
        raise ValueError("At least one additional height/region request is required")
    x_span = parent["partition"]["x_span"]
    for region in added:
        for piece in difference([region], tasks):
            tasks.extend(limited_boxes(piece, parent["partition"]["z_span"], x_span))
    requests, _ = partition(parent["requests"] + added, parent["partition"]["z_span"], "exact", x_span)
    payload = {k: v for k, v in parent.items() if k not in ("plan_hash", "prefix", "cuboids", "requests")}
    payload.update(parent_plan_hash=parent["plan_hash"], cuboids=tasks, requests=requests,
                   added_regions=added)
    if lakes:
        prior = {item["id"]: item for item in parent["freshwater_bodies"]}
        for item in lakes:
            if item["id"] in prior and prior[item["id"]] != item:
                raise ValueError(f"Freshwater ID {item['id']} already has a different definition; use a new ID")
            prior[item["id"]] = item
        payload["freshwater_bodies"] = list(prior.values())
    plan = seal_plan(payload)
    verify_plan(plan)
    return plan


def verify_disjoint(boxes: list[dict], label: str) -> None:
    active = []
    for b in sorted(boxes, key=lambda b: b["zmin"]):
        active = [a for a in active if a["zmax"] >= b["zmin"]]
        if any(intersection(a, b) for a in active):
            raise ValueError(f"Overlapping {label}")
        active.append(b)


def verify_plan(plan: dict) -> None:
    if plan.get("version") != VERSION:
        raise ValueError("Unsupported capture plan version")
    if plan.get("plan_hash") != digest({k: v for k, v in plan.items() if k != "plan_hash"}):
        raise ValueError("Capture plan hash mismatch; create a new plan instead of editing it")
    if plan.get("geometry_hash") != digest(plan["geometry"]):
        raise ValueError("Geometry hash mismatch")
    prefix = plan.get("prefix", "")
    if not isinstance(prefix, str) or not prefix.startswith("v12_capture_") or not prefix.replace("_", "").isalnum():
        raise ValueError("Unsafe capture prefix")
    if not plan["requests"] or not plan["cuboids"]:
        raise ValueError("Empty capture plan")
    if not all(key in plan["partition"] for key in ("x_span", "z_span")):
        raise ValueError("Capture plan requires explicit X/Z span caps")
    x_span = plan["partition"]["x_span"]
    z_span = plan["partition"]["z_span"]
    if any(type(n) is not int or not 1 <= n <= MAX_SIDE for n in (x_span, z_span)):
        raise ValueError("Invalid planned X/Z span")
    names = set()
    for b in plan["requests"] + plan["cuboids"]:
        box(b)
    for task in plan["cuboids"]:
        if (any(task[a + "max"] - task[a + "min"] + 1 > MAX_SIDE for a in AXES)
                or volume(task) > MAX_VOLUME):
            raise ValueError("Planned export exceeds packed-index or volume limits")
        if (task["xmax"] - task["xmin"] + 1 > x_span
                or task["zmax"] - task["zmin"] + 1 > z_span):
            raise ValueError("Planned export exceeds the saved X/Z span caps")
        if not isinstance(task["name"], str) or not task["name"].replace("_", "").isalnum() or task["name"] in names:
            raise ValueError("Unsafe or duplicate export name")
        names.add(task["name"])
    verify_disjoint(plan["cuboids"], "export units")
    verify_disjoint(plan["requests"], "request regions")
    if difference(plan["requests"], plan["cuboids"]):
        raise ValueError("Export plan leaves requested regions uncovered")
    if plan["partition"]["mode"] == "exact" and difference(plan["cuboids"], plan["requests"]):
        raise ValueError("Exact plan includes unrequested volume")


def load_plan(path: Path) -> dict:
    plan = read_json(path)
    verify_plan(plan)
    return plan


def column_presence_record(requested: dict, present: bytearray) -> dict:
    """Report exact X/Z columns without a block/decor record at any height.

    Absence is unknown, not proof of air. Even a positive column-presence
    result is not proof that every requested vertical chunk was loaded.
    """
    width = requested["xmax"] - requested["xmin"] + 1
    depth = requested["zmax"] - requested["zmin"] + 1
    if len(present) != width * depth:
        raise ValueError("Column-presence mask has the wrong size")
    observed = sum(present)
    rows = {}
    if observed < len(present):
        for z in range(depth):
            missing = []
            x = 0
            while x < width:
                if present[z * width + x]:
                    x += 1
                    continue
                start = x
                while x < width and not present[z * width + x]:
                    x += 1
                missing.append((requested["xmin"] + start, requested["xmin"] + x - 1))
            if missing:
                rows[requested["zmin"] + z] = missing
    return {"requested_columns": len(present), "observed_columns": observed,
            "missing_columns": len(present) - observed,
            "missing_regions": row_boxes(rows, requested["ymin"], requested["ymax"]),
            "basis": "At least one Indices or DecorIndices record at any returned height; missing columns are unverified, never inferred air."}


def inspect_export(path: Path, requested: dict, world_center: int = WORLD_CENTER) -> dict:
    """Verify sparse records and credit only the actual reported 3D envelope.

    This establishes coverage under WorldEdit's reported-envelope contract. It
    cannot independently certify chunk residency, intentional omission of air,
    or a simultaneous world snapshot. Those limits are included in every audit.
    """
    result: dict = {"file": str(path), "requested": box(requested), "problems": [],
                    "covered": None, "ceiling_blocks": 0}
    try:
        payload = path.read_bytes()
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("Export root is not an object")
        sizes = [data.get("Size" + a.upper()) for a in AXES]
        if any(type(n) is not int or not 1 <= n <= requested[a+"max"] - requested[a+"min"] + 1
               for a, n in zip(AXES, sizes)):
            raise ValueError("Returned dimensions are invalid or exceed the selection")
        origin = {"X": requested["xmin"] + world_center, "InternalY": requested["ymin"],
                  "Z": requested["zmin"] + world_center}
        actual_origin = data.get("OriginalPos")
        if not isinstance(actual_origin, dict) or any(actual_origin.get(k) != v for k, v in origin.items()):
            raise ValueError("Returned origin differs from the selection")
        sx, sy, sz = sizes
        blocks, ids = data.get("Indices"), data.get("BlockIds")
        if not isinstance(blocks, list) or not isinstance(ids, list) or len(blocks) != len(ids):
            raise ValueError("Indices and BlockIds must be matching arrays")
        column_width = requested["xmax"] - requested["xmin"] + 1
        present = bytearray(column_width * (requested["zmax"] - requested["zmin"] + 1))
        for ikey, vkey in (("Indices", "BlockIds"), ("DecorIndices", "DecorIds")):
            indices, values = data.get(ikey, []), data.get(vkey, [])
            if not isinstance(indices, list) or not isinstance(values, list) or len(indices) != len(values):
                raise ValueError(f"{ikey}/{vkey} must be matching arrays")
            if any(type(i) is not int or i < 0 or (i & 1023) >= sx or ((i >> 10) & 1023) >= sz
                   or (i >> 20) >= sy for i in indices):
                raise ValueError(f"{ikey} contains invalid or out-of-bounds packed indices")
            if any(type(i) is not int or i < 0 for i in values):
                raise ValueError(f"{vkey} contains invalid IDs")
            if ikey == "Indices":
                # BlockSchematic.Pack writes the same coordinate twice when
                # both solid and fluid layers are occupied. Preserve both IDs;
                # a coordinate alone does not identify a block-layer record.
                # https://github.com/anegostudios/vsapi/blob/master/Common/Collectible/Block/BlockSchematic.cs
                first_ids: dict[int, int] = {}
                paired: set[int] = set()
                for index, block_id in zip(indices, values):
                    present[((index >> 10) & 1023) * column_width + (index & 1023)] = 1
                    if index in paired:
                        raise ValueError("More than two block records at one coordinate")
                    previous = first_ids.get(index)
                    if previous is None:
                        first_ids[index] = block_id
                    elif previous == block_id:
                        raise ValueError("Repeated identical block record at one coordinate")
                    else:
                        paired.add(index)
                result["layered_positions"] = len(paired)
                palette = data.get("BlockCodes", {})
                if not isinstance(palette, dict):
                    raise ValueError("BlockCodes is not an object")
                result["ceiling_blocks"] += sum(
                    (i >> 20) == requested["ymax"] - requested["ymin"]
                    and palette.get(str(block_id)) not in ("game:air", "air")
                    for i, block_id in zip(indices, values))
            else:
                for index in indices:
                    present[((index >> 10) & 1023) * column_width + (index & 1023)] = 1
                result["ceiling_blocks"] += sum((i >> 20) == requested["ymax"] - requested["ymin"] for i in indices)
        result["column_presence"] = column_presence_record(requested, present)
        entities = data.get("BlockEntities", {})
        if not isinstance(entities, dict):
            raise ValueError("BlockEntities is not an object")
        for key in entities:
            i = int(key)
            if i < 0 or (i & 1023) >= sx or ((i >> 10) & 1023) >= sz or (i >> 20) >= sy:
                raise ValueError("Block entity index outside returned dimensions")
        result["covered"] = {"xmin": requested["xmin"], "xmax": requested["xmin"] + sx - 1,
                             "ymin": requested["ymin"], "ymax": requested["ymin"] + sy - 1,
                             "zmin": requested["zmin"], "zmax": requested["zmin"] + sz - 1}
        result["verified_regions"] = difference([result["covered"]], result["column_presence"]["missing_regions"])
        if not blocks and not data.get("DecorIndices"):
            raise ValueError("Empty sparse export cannot establish coverage")
        result["sha256"] = hashlib.sha256(payload).hexdigest()
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        result["problems"].append(str(exc))
        result["covered"] = None
        result["verified_regions"] = []
    return result


def render_preview(plan: dict, path: Path, gaps: list[dict] | None = None,
                   gap_label: str = "audit gaps") -> None:
    requests = plan["requests"]
    xmin = min(b["xmin"] for b in plan["cuboids"])
    xmax = max(b["xmax"] for b in plan["cuboids"])
    zmin = min(b["zmin"] for b in plan["cuboids"])
    zmax = max(b["zmax"] for b in plan["cuboids"])
    scale = min(850 / (xmax-xmin+1), 1150 / (zmax-zmin+1))
    width, height = (xmax-xmin+1)*scale, (zmax-zmin+1)*scale
    def rect(b: dict, color: str, stroke: str = "none", opacity: float = 1) -> str:
        label = f"X {b['xmin']}..{b['xmax']}; Z {b['zmin']}..{b['zmax']}; Y {b['ymin']}..{b['ymax']}"
        return (f'<rect x="{(b["xmin"]-xmin)*scale:.3f}" y="{(b["zmin"]-zmin)*scale:.3f}" '
                f'width="{(b["xmax"]-b["xmin"]+1)*scale:.3f}" height="{(b["zmax"]-b["zmin"]+1)*scale:.3f}" '
                f'fill="{color}" fill-opacity="{opacity}" stroke="{stroke}" stroke-width=".45"><title>{escape(label)}</title></rect>')
    def filled_union(boxes: list[dict], color: str, opacity: float = 1) -> str:
        # One path avoids antialias seams between thousands of adjacent rows.
        paths = []
        for b in boxes:
            x0, x1 = (b['xmin']-xmin)*scale, (b['xmax']-xmin+1)*scale
            z0, z1 = (b['zmin']-zmin)*scale, (b['zmax']-zmin+1)*scale
            paths.append(f'M{x0:.3f},{z0:.3f}H{x1:.3f}V{z1:.3f}H{x0:.3f}Z')
        return f'<path d="{" ".join(paths)}" fill="{color}" fill-opacity="{opacity}"/>'
    extra = sum(volume(b) for b in plan["cuboids"]) - sum(volume(b) for b in requests)
    layers = [f'<rect width="{width+72:.3f}" height="{height+185:.3f}" fill="#f4f7fb"/>',
              '<g font-family="sans-serif" fill="#15283c">',
              '<text x="24" y="30" font-size="21">v12 capture footprint and export mosaic</text>',
              f'<text x="24" y="53" font-size="12">{len(plan["cuboids"])} units · X ≤{plan["partition"]["x_span"]}, Z ≤{plan["partition"]["z_span"]} · {extra:,} extra block positions</text>',
              f'<text x="24" y="73" font-size="12">Blue: buffered request · dark blue: mouths/gap · orange: cuboids · red: {escape(gap_label)}</text>',
              f'<text x="24" y="93" font-size="12">X {xmin}..{xmax} → · Z {zmin}..{zmax} ↓ · requested Y {plan["requested_y"]["ymin"]}..{plan["requested_y"]["ymax"]}</text>',
              f'<text x="24" y="113" font-size="11">Plan SHA256 {plan["plan_hash"]}</text>',
              '<text x="24" y="131" font-size="11">Mouth anchors are capture limits. Height supplements can overlap in this top view.</text>',
              '<g transform="translate(36 155)">']
    layers.append(filled_union(plan["cuboids"], "#fff0cf"))
    layers.append(filled_union(requests, "#bbe1f3"))
    layers.append(filled_union(plan["raw_footprint"], "#5ba1c8"))
    layers.extend(rect(b, "none", "#c46a14") for b in plan["cuboids"])
    layers.append(filled_union(gaps or [], "#dc3545", .75))
    layers.append('</g></g>')
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width+72:.0f}" height="{height+185:.0f}" viewBox="0 0 {width+72:.3f} {height+185:.3f}">' + ''.join(layers) + '</svg>\n')


def write_plan(plan: dict, output: Path | None = None) -> Path:
    destination = output or HERE / "capture-plans" / "coastal-connection" / "plan.json"
    if destination.exists() and read_json(destination) != plan:
        raise ValueError(f"Refusing to replace existing plan: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        destination.write_text(json.dumps(plan, indent=2) + "\n")
    render_preview(plan, destination.with_suffix(".svg"))
    return destination


def print_summary(plan: dict, path: Path) -> None:
    requested = sum(volume(b) for b in plan["requests"])
    exported = sum(volume(b) for b in plan["cuboids"])
    print(f"Plan: {path}\nPreview: {path.with_suffix('.svg')}\nPrefix: {plan['prefix']}")
    print(f"{len(plan['cuboids']):,} nonoverlapping exports; {requested:,} requested block positions; {exported-requested:,} extra.")
    print(f"Initial size caps: X {plan['partition']['x_span']}, Z {plan['partition']['z_span']}; largest unit {max(volume(b) for b in plan['cuboids']):,} block positions.")
    print(f"SHA256: {plan['plan_hash']}\nNo server commands sent.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="create immutable plan and SVG before execution")
    create.add_argument("--ymin", type=int, required=True)
    create.add_argument("--ymax", type=int, required=True)
    create.add_argument("--geometry", type=Path, default=DEFAULT_GEOMETRY)
    create.add_argument("--z-span", type=int, default=50)
    create.add_argument("--x-span", type=int, default=DEFAULT_X_SPAN,
                        help="maximum initial X width (default: 450; hard maximum: 1023)")
    create.add_argument("--freshwater", type=Path, help="JSON bodies with complete integer row intervals")
    create.add_argument("--output", type=Path)
    extend = commands.add_parser("extend", help="add height/region requests in a new plan")
    extend.add_argument("--plan", type=Path, required=True)
    extend.add_argument("--regions", type=Path, help="JSON object with regions: [inclusive cuboid bounds]")
    extend.add_argument("--freshwater", type=Path, help="add whole intersecting freshwater bodies")
    extend.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "create":
            plan = create_plan(args.ymin, args.ymax, args.geometry, args.z_span,
                               freshwater=args.freshwater, x_span=args.x_span)
            path = write_plan(plan, args.output)
            print_summary(plan, path)
        elif args.command == "extend":
            if not args.regions and not args.freshwater:
                raise ValueError("extend requires --regions and/or --freshwater")
            plan = extend_plan(load_plan(args.plan), read_json(args.regions)["regions"] if args.regions else [], args.freshwater)
            path = write_plan(plan, args.output)
            print_summary(plan, path)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
