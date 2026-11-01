# v12 capture plans

The coordinator uses only a saved coastline capture plan. By default it loads
`capture-plans/coastal-connection/plan.json` relative to the script, regardless
of the current working directory. `--plan` selects a different saved plan.
There is no centerline/corridor fallback or separate Y/Z prompt. The previous
implementation remains available in Git history; its capture files are untouched.
The v12 geometry is a local copy of the accepted
[`coastline_route_v12.py`](../capture-plans/coastal-connection/coastline_route_v12.py),
stored with the plan it defines. This is planning input, rather than a
temporary utility program.
Planning and auditing use only the Python standard library and issue no server
commands. Execute a capture only with the coordinator's `--plan` option.

`plan.json` is generated output, rather than a file users need to author by
hand. The generator is [`v12_capture_plan.py`](../v12_capture_plan.py). Its
small inputs are the shoreline definitions, Y bounds, section-size options,
and optional height/lake supplements. It expands those inputs into the
requested regions, export cuboids, names, embedded geometry, and hashes needed
for repeatable collection and coverage auditing.

For instructions, see [Generate or revise a plan](#generate-or-revise-a-plan).

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

The collector has no `--prefix` or `--z-span` option. Make geometry, height,
and section-size changes with `v12_capture_plan.py`, review the resulting plan,
then run the collector against that saved specification. A missing or invalid
plan stops collection instead of selecting a different region generator.

By default, both export commands and teleport commands use `tmux` from `PATH`.
Use `--tmux-binary /absolute/path/to/tmux` to override it. The chosen executable
is retained through retries and subdivisions.

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

## Interpreting export failures and height messages

WorldEdit's sparse block list can contain two entries at the same packed
coordinate: one solid-layer block and one fluid-layer block. For example, a
seaweed block and salt water can share a position. The
[official schematic packing code](https://github.com/anegostudios/vsapi/blob/master/Common/Collectible/Block/BlockSchematic.cs)
deliberately writes both entries. A validator error saying only
`Duplicate block indices` was a coordinator bug, not evidence that teleporting
failed to load that section.

The validator now accepts two distinct block IDs at one coordinate, preserves
both, and rejects repeated identical records or more than two block records
at a coordinate. It validates the export format, not the game rules governing
which specific solid/fluid combinations should occur. The JSON palette gives
block codes but does not provide all block classes, placement rules, or mod
behavior. No hardcoded plant/water compatibility list is used. Different fresh,
salt, still, and flowing water codes are retained; the collector does not
simulate their behavior or substitute one type for another. Merges retain
both block-layer records.

After updating from the version with the duplicate-index bug, restart with
the same plan and export directory. Existing exports are revalidated. A usable
direct parent export can supersede its earlier failed-split checkpoint;
there is no need to delete the progress file or discard valid export data.

`export validated (height trimmed)` means the JSON passed the file checks but
its stored height is shorter than the requested height. WorldEdit omits air
and computes dimensions from stored blocks, so this alone does not indicate
a failed export. The final 3D audit conservatively leaves the omitted upper
height unverified. A valid file and proven coverage of the entire requested
height are different results.

Teleport recovery currently waits a fixed 1.5 seconds before retrying. That
delay is not an acknowledgement from the server that every required chunk is
loaded. An initial empty export may still require chunk loading, while a
format-validation error should be diagnosed from its printed reason. Reasons
are now printed in normal automatic output as well as verbose output.

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

## Generate or revise a plan

Run the examples from the repository root. A Python interpreter is sufficient;
there are no third-party Python dependencies. The generator does not use tmux,
contact the game, or send server commands.

### Reproduce the current plan

This is the tool and configuration used to generate the current 160-section
plan. A fresh temporary output directory lets you inspect the result without
changing the maintained plan:

```sh
plan_draft_dir=$(mktemp -d)
python3 v12_capture_plan.py create \
  --geometry capture-plans/coastal-connection/coastline_route_v12.py \
  --ymin 65 --ymax 180 \
  --x-span 450 --z-span 50 \
  --output "$plan_draft_dir/plan.json"
```

The command prints its output locations and writes `plan.json` and `plan.svg`
in that temporary directory. With the current source and options, its content
hash matches the committed 160-section plan. You can compare the complete JSON:

```sh
cmp "$plan_draft_dir/plan.json" capture-plans/coastal-connection/plan.json
```

`cmp` prints nothing and exits successfully when the files match.

### Choose the change you need

| Desired change | Inputs and command |
| --- | --- |
| Change the requested height for the entire footprint | `create --ymin MIN --ymax MAX` |
| Use smaller or larger initial sections | `create --x-span WIDTH --z-span DEPTH` |
| Change the shoreline shape within the existing v12 area | Edit a geometry-file copy and use `create --geometry FILE` |
| Add height over selected regions while retaining existing export unit names | `extend --plan FILE --regions REGIONS.json` |
| Include whole intersecting freshwater bodies | `create --freshwater BODIES.json`, or `extend --plan FILE --freshwater BODIES.json` |

Every `create` command also requires `--ymin` and `--ymax`. Use `--output` to
write a draft. `--x-span` defaults to 450 and `--z-span` to 50; these are maximum
sizes, so edge sections can be smaller. Both must be integers from 1 to 1023,
and the generator also enforces the selection-volume limit. Y bounds are
inclusive: 65 through 180 is 116 blocks high.

For example, generate smaller sections without changing the requested height:

```sh
plan_draft_dir=$(mktemp -d)
python3 v12_capture_plan.py create \
  --ymin 65 --ymax 180 \
  --x-span 256 --z-span 25 \
  --output "$plan_draft_dir/plan.json"
```

The geometry file defaults to the v12 shoreline source beside the maintained
plan. Without `--output`, the destination is the maintained
`capture-plans/coastal-connection/plan.json`. Both defaults are relative to the
generator's location, regardless of cwd. Explicit relative paths are relative
to cwd; use absolute paths when invoking the program from another directory.
The generator allows identical regeneration but refuses to overwrite an
existing plan with different contents. Use a fresh draft path for each variant.

### Edit the shoreline geometry

Start with a copy of the human-maintained source, not the generated JSON:

```sh
geometry_draft_dir=$(mktemp -d)
cp capture-plans/coastal-connection/coastline_route_v12.py \
  "$geometry_draft_dir/coastline.py"
```

Edit `coastline.py` in that directory. The two literal lists `WEST_SHORE` and
`EAST_SHORE` describe paths in relative world X/Z block coordinates:

| Operation | Format and meaning |
| --- | --- |
| `M` | One `(X, Z)` point: the start of the shore. Each path starts with one `M`. |
| `L` | One `(X, Z)` point: a straight segment from the preceding endpoint. |
| `C` | Three `(X, Z)` points: two control points and the endpoint of a cubic Bezier curve, starting from the preceding endpoint. |

For example, this existing west-shore segment has two control points followed
by its endpoint:

```python
('C', [(-33728, 61470), (-33678, 61503), (-33665, 61525)])
```

Use numeric literals, including decimals where needed. The loader reads these
definitions as data and does not execute the Python file or evaluate imports,
functions, or calculated expressions.

Each segment must move forward in Z. Its control points must have
nondecreasing Z values, and its endpoint must have a greater Z than its start.
The west shore must cover Z=59750–62360 and the east shore Z=59375–61994. The
generator rejects unsupported paths, missing row coverage, and reversed
west/east capture boundaries. Regenerate after editing:

```sh
plan_draft_dir=$(mktemp -d)
python3 v12_capture_plan.py create \
  --geometry "$geometry_draft_dir/coastline.py" \
  --ymin 65 --ymax 180 \
  --x-span 450 --z-span 50 \
  --output "$plan_draft_dir/plan.json"
```

This generator is currently specific to the v12 coastal connection. The Z
bands, mouth anchors, 100-block buffer, coordinate offset, and v12 geometry
label are fixed in the program; they have no CLI options. `--geometry` permits
shore-shape revisions within those constraints. Planning a completely
different world region or changing those rules requires modifying the planner.

### Review and use a draft

Open the emitted `plan.svg` in an image viewer or browser. It shows the
required footprint and export mosaic. Generation also checks that export
units do not overlap, obey size/volume limits, and cover every requested region.
The printed summary gives section counts, maximum unit volume, and extra
captured volume.

You can explicitly preview the draft with the coordinator, without sending
commands or changing the maintained plan:

```sh
python3 vintage_story_export_coordinator.py \
  --plan "$plan_draft_dir/plan.json" --preview
```

For a deliberate server test of that draft, choose the pane and actual WorldEdit
export directory, then run:

```sh
python3 vintage_story_export_coordinator.py \
  --plan "$plan_draft_dir/plan.json" \
  --target-pane PANE --export-dir EXPORT_DIRECTORY \
  --step-by-step --verbose
```

That last command sends server commands after your confirmation. Progress and
reports are written beside the selected draft, while world exports go in the
configured export directory. Move a draft you intend to run into durable local
storage first; a temporary directory is suitable for review, not long-term
resume records.

### Make a reviewed revision the maintained plan

Retain a frozen plan copy with the progress records of any unfinished run
before changing the maintained specification. Then promote the reviewed JSON:

```sh
cp "$plan_draft_dir/plan.json" capture-plans/coastal-connection/plan.json
python3 vintage_story_export_coordinator.py --preview
```

If you edited the shoreline copy in the geometry example, also promote that
reviewed source so future generation uses the same geometry:

```sh
cp "$geometry_draft_dir/coastline.py" \
  capture-plans/coastal-connection/coastline_route_v12.py
```

Commit changed geometry or supplement input files along with the plan so the
result can be reproduced. Update the adjacent
`CHANGELOG.md` with what changed and why, then commit it together with
`plan.json`. Earlier full plan contents belong in Git history:

```sh
git log --oneline -- capture-plans/coastal-connection/plan.json
git show COMMIT:capture-plans/coastal-connection/plan.json
```

The preview command regenerates SVG only; a local PNG snapshot is not updated
automatically. Regenerate or discard an outdated PNG after changing the plan.

Do not hand-edit `requests`, `cuboids`, export names, or hash fields in the
expanded JSON. Editing it invalidates its content hash; changing the input and
regenerating keeps geometry, coverage, and identifiers consistent. `create`
assigns a new set of export names when inputs change. Use `extend` for additive
height/lake coverage when you want existing export unit names retained; it
does not change the existing section sizes or remove requested coverage.

Historical run records and world exports should be retained locally rather
than exposed as extra maintained plan revisions. A plan file does not itself
contain the collected terrain data.

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
