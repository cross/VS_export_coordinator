# v12 capture plans

The original route workflow is still available without `--plan`. Its prefixes,
progress files, and exports are not migrated or overwritten by v12 planning.
The v12 geometry is a local copy of the accepted
[`coastline_route_v12.py`](../capture-plans/coastal-connection/coastline_route_v12.py),
stored with the plan it defines. This is planning input, rather than a
temporary utility program.
Planning and auditing use only the Python standard library and issue no server
commands. Execute a capture only with the coordinator's `--plan` option.

## Prepared plans: Y=65–180

| Plan | Export units | Requested block positions | Extra exported positions |
| --- | ---: | ---: | ---: |
| Current: X ≤450, Z ≤50 | 160 | 311,148,656 | 8,473,916 (2.72%) |
| Previous: X ≤1023, Z ≤50 | 88 | 311,148,656 | 8,473,916 (2.72%) |

The current plan is in `capture-plans/coastal-connection/plan.json`.
Its units are at most **450 × 116 × 50** (X × Y × Z), or **2,610,000**
block positions. The first unit is 446 × 116 × 25. This smaller operational
width aims to reduce the area that must load around each player teleport;
it is not a measured guarantee of server chunk residency.

The earlier 88-unit revision is preserved in Git history at the same path.
[The plan changelog](../capture-plans/coastal-connection/CHANGELOG.md) keeps a
readable description of each revision beside the current plan. There are no
numbered or hash-named revision directories. The directory also contains a
generated `plan.svg` preview and a local PNG rendition. These previews are
ignored by Git; the SVG can be regenerated with `--preview`. Broader bands reduce command and
file-wait overhead by including edge terrain outside the exact footprint. Its
cuboids remain nonoverlapping and its audit uses the exact requested footprint.
Neither plan has been run against a server. No freshwater body definitions
have been supplied, so these prepared plans include no whole-lake supplements.

## Preview, run, resume, and audit

Run these commands from this repository. Replace `PLAN.json`, `PANE`, and
`EXPORT_DIRECTORY` with the chosen plan path, server console tmux pane, and
directory containing the WorldEdit JSON exports.

```sh
python3 vintage_story_export_coordinator.py --plan PLAN.json --preview
python3 vintage_story_export_coordinator.py --plan PLAN.json --target-pane PANE --export-dir EXPORT_DIRECTORY
python3 vintage_story_export_coordinator.py --plan PLAN.json --report-coverage --export-dir EXPORT_DIRECTORY
```

The execution command still prompts for the player name. Repeat it to resume.
The plan fixes geometry, unit names, and requested Y bounds; it does not prompt
for a different capture prefix or Y interval. Changing the tmux pane/player
does not invalidate geometric progress. Changing the export directory for an
existing progress record is rejected instead of overwriting that record.

Each plan uses its own hash-derived prefix, `<prefix>-progress.json`, command
directory, and execution coverage checkpoint beside `plan.json`. Reports write
`<prefix>-audit.json` and `<prefix>-coverage.svg`. The audit JSON lists every
exact uncovered cuboid, per-file validation problems and SHA256 values, and
ceiling warnings. It re-reads the exports instead of trusting a saved complete
status or the current coastline source. Exit codes: 0 means the requested 3D
union is covered by validated reported envelopes, 1 means gaps remain, and 2
means an input/error condition.

The saved plan embeds shoreline geometry, requested regions, export units,
geometry version/hash, source-file hash, coordinate convention, and selection
limits. A full content hash detects edits. Create a new plan to change bounds,
geometry, partitioning, or supplements. The generator refuses to overwrite a
different existing plan; generate a draft with `--output` before promoting it
to the current Git-versioned plan.

## Geometry and completeness

For integer Z=59375–59749 the west boundary is X=-34500 and the east boundary
is the east shore. For Z=59750–61994 the boundaries are the two shores. For
Z=61995–62360 the west boundary is the west shore and the east boundary is
X=-32680. These anchors describe capture coverage, not cut/fill limits.

Each west coordinate is rounded down and each east coordinate up. Cubic
Bezier intersections are enclosed using rational bisection and an exact
derivative bound rather than approximate polylines. Geometry that is not
Z-monotone is rejected. The tiny numerical enclosure can conservatively include
an additional boundary block if it straddles an integer; it cannot omit a
shoreline block. The integer footprint is widened by 100 X blocks and then
unioned over every source row within 100 Z blocks, including outside the
original Z range. The resulting Z extent is 59275–62460.

The planner normalizes the union into disjoint height slabs and row intervals,
then partitions export units. X splitting happens during planning. New plans
default to initial X widths of 450 and Z lengths of 50, saved independently
of the hard 1023-block side limit and 9,000,000-block selection-volume limit.
The coordinator refuses merges above the X/Z safety limit. Local merges carry
`CapturePlanPieces` provenance, so subsequent audits can inspect original
leaves even without the old progress file. Keep those leaf files.

Planning verifies that units do not overlap and that requested minus planned
coverage is empty. The coordinator captures broader cuboids rather than
tracking the shoreline edge: 100 blocks is the minimum horizontal margin,
and extra edge terrain is allowed. The independent audit computes requested minus validated returned
**three-dimensional** envelopes, including height gaps. Overlaps cannot inflate
coverage. Missing, unreadable, malformed, empty, or out-of-bounds export records
receive no credit. A shortened SizeY never silently establishes empty air.

This proof is conditional on WorldEdit accurately reporting exported extents
and contents. Sparse JSON alone cannot establish chunk residency or distinguish
all missing terrain from intentionally omitted air. It also cannot establish a
single simultaneous world snapshot across separate captures. The audit records
these evidence limits rather than claiming stronger verification.

## New plans and partition choices

Y bounds are required independently of the fixed horizontal buffer:

```sh
python3 v12_capture_plan.py create --ymin 65 --ymax 180
python3 v12_capture_plan.py create --ymin 65 --ymax 180 --x-span 256 --z-span 25 --output /tmp/coastal-connection-revised.json
```

The default is broader 50-block Z bands split into widths of at most 450.
Use `--x-span` and `--z-span` to reduce initial selections further if needed;
each must be between 1 and 1023 and all units also obey the volume limit.
Changing these settings creates a new plan hash/prefix. The default destination
is always `capture-plans/coastal-connection/plan.json`. Git records revisions;
the adjacent `CHANGELOG.md` describes why each revision changed. The smaller X cap
changes the partition only: it retains exactly the same requested and exported
footprints as the previous 88-unit plan. Optional `--geometry PATH` reads literal shore
definitions without executing that Python file. `--output PATH` chooses the
plan destination. Identical regeneration is allowed. For a changed revision,
write a draft outside the tracked plan directory, review it, replace the current
`plan.json`, regenerate its preview, update `CHANGELOG.md`, and commit the two
files together. Retrieve earlier plan contents with:

```sh
git log --oneline -- capture-plans/coastal-connection/plan.json
git show COMMIT:capture-plans/coastal-connection/plan.json
```

Keep a frozen copy of a plan needed by an unfinished run, alongside its
progress records. Historical run records and world exports should be retained
locally rather than exposed as extra maintained plan revisions.

## Additional height coverage

Blocks/decor at a requested upper ceiling are flagged during execution and in
the audit. A ceiling hit is a warning that the chosen height may truncate the
terrain; it does not change the requested bounds or prove that more height is
unnecessary. Choose additional bounds explicitly after inspection.

Supply a JSON file such as `height-regions.json`:

```json
{"regions": [{"xmin": -33600, "xmax": -33000, "ymin": 181, "ymax": 220,
              "zmin": 61500, "zmax": 62000}]}
```

```sh
python3 v12_capture_plan.py extend --plan PLAN.json --regions height-regions.json --output /tmp/coastal-connection-revised.json
```

This creates a draft revision with a separate plan/progress identity. It retains old export unit
names so existing JSON files in the same export directory can be reused, and
adds only the region outside existing planned units. Overlapping new height
requests are deduplicated. The old plan and progress remain untouched.
New units inherit the parent plan's saved X/Z caps, including freshwater
extensions. Older plans without an X cap retain their previous 1023-block
policy when extended; use the new 160-unit plan for smaller sections.
`requested_y` records the initial bounds; `added_regions` and `requests` record
all additional bounds. Extending already-planned space is not a retry of a
failed or shortened export: the audit reports those gaps separately.

## Whole freshwater bodies

Create a JSON file with complete body footprints as integer row intervals:

```json
{"bodies": [
  {"id": "lake-north", "rows": [[60000, -34200, -34000],
                                  [60001, -34210, -33990]],
   "ymin": 65, "ymax": 180}
]}
```

List **every** row of each complete body, with multiple intervals on a row if
needed. The example illustrates the format, not an actual mapped lake. If any
body column intersects the buffered coastline footprint, the entire supplied
body is added to the requested union. Body-specific Y bounds are optional;
they default to the initial capture Y bounds. Nonintersecting bodies are
recorded but not selected. Overlapping bodies are unioned before partitioning.
No claim is made that the supplied body is complete or freshwater: those
attributes must come from mapping/world analysis outside this coastline planner.

```sh
python3 v12_capture_plan.py create --ymin 65 --ymax 180 --freshwater freshwater-bodies.json --output /tmp/coastal-connection-revised.json
python3 v12_capture_plan.py extend --plan PLAN.json --freshwater freshwater-bodies.json --output /tmp/coastal-connection-revised.json
```

The extension command adds newly mapped lakes in a draft plan revision while
retaining old export unit names and reusing their files. It exports only body
regions outside the previously planned units. Repeated body IDs must keep the
same definition; use a new ID for revised boundaries. Height extension is also
available for rectangular supplemental regions, but does not discover lake
boundaries.

## Validation

```sh
python3 -B -m unittest discover -s tests -v
```

Tests include exact subtraction against a voxel oracle, independent high
precision curve inversion, all three mouth/gap transitions and buffer ends,
selection limits, disjoint partitions, hash integrity, lake inclusion, height
extensions, corrupt exports, ceiling flags, shortened Y, merged-leaf height
gaps, and offline coordinator reporting. They do not contact a server.

The guide is in `docs/`, tests are in `tests/`, and the two application modules
remain at the repository root. The plan JSON, coastline input, and changelog
belong together in `capture-plans/coastal-connection/`. Generated previews,
commands, progress files, coverage reports, and temporary files are ignored by
Git. Retain progress and exported data locally when needed for resume or audit;
ignoring them does not make them disposable.
