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

Before collection, startup revalidates the saved export files to decide what
can be reused. This parses their JSON, checks packed block/decor indices and
column presence, and verifies retained rescan evidence and hashes. A large
existing run can make this CPU-intensive; no server commands are sent during
this pass. Successful validation evidence is reused when updating progress,
so the block indices are scanned once per parent export during startup.

The coordinator announces the startup check before reading exports, displays
the selection number and elapsed time periodically (every selection with
`--verbose`), and prints a completion summary. The PID/status-signal instructions
appear before this pass. A status request identifies the exact startup file
being checked, even between ordinary progress messages.

### Immediate status: Ctrl-T and signals

On macOS/BSD, press **Ctrl-T** while the coordinator is running to request an
immediate status report via `SIGINFO`. The terminal's status character must
be enabled; if remapped, `stty status '^T'` restores it on those systems.

On Linux, use **`kill -USR1 PID`** from another shell. Linux does not support
the terminal `SIGINFO`/Ctrl-T mechanism. The operating system running Python
determines this, so a Mac terminal connected to a Linux server still uses the
Linux method. The coordinator prints its PID and the status command at startup.
`SIGUSR1` also works on macOS/BSD, and `kill -INFO PID` works where `SIGINFO`
is available. See the [terminal status-character documentation](https://man7.org/linux/man-pages/man3/termios.3.html).

The report includes finished/verified/accepted selection counts, partial and
pending counts, elapsed time, current operation and its duration, selection
bounds, and the last available rescan checkpoint. Export waits, server-command
acknowledgements, recovery exports, and coverage audits identify their current
operation. Status is printed to stderr without pausing or restarting work,
sending commands, writing checkpoints, or performing a fresh coverage audit.
The counts describe execution progress rather than a new proof of coverage.
Ctrl-C retains its existing pause/quit behavior.

The collector has no `--prefix` or `--z-span` option. Make geometry, height,
and section-size changes with `v12_capture_plan.py`, review the resulting plan,
then run the collector against that saved specification. A missing or invalid
plan stops collection instead of selecting a different region generator.

By default, both export commands and teleport commands use `tmux` from `PATH`.
Use `--tmux-binary /absolute/path/to/tmux` to override it. The chosen executable
is retained through retries and subdivisions.

Each plan uses its own hash-derived prefix, `<prefix>-progress.json`, command
directory, and execution coverage checkpoint beside `plan.json`. Reports write
`<prefix>-audit.json`, `<prefix>-coverage.svg`, and `<prefix>-columns.svg`.
The columns preview highlights only X/Z columns still unverified, while the
coverage preview also includes vertical gaps. The audit JSON lists every
exact uncovered cuboid, per-file validation problems and SHA256 values, and
ceiling warnings. It re-reads the exports instead of trusting a saved complete
status or the current coastline source. Exit codes: 0 means the requested 3D
union is covered by validated reported envelopes, 1 means gaps remain, and 2
means an input/error condition.

### Collection completion and audit completion

The progress file and execution coverage manifest distinguish these fields:

| Field | Meaning |
| --- | --- |
| `finished` | Every planned root selection is complete or manually accepted. Skipped selections and unresolved recovery regions keep this false. |
| `coverage_complete` | The latest completed audit verifies the entire requested 3D volume. `null` means the current audit has not produced a final result. |
| `column_coverage_complete` | The latest completed audit accounts for every requested X/Z column through records or corroborating deeper evidence. Also `null` before a final audit result. |
| `audit_status` | `not_run`, `running`, `complete`, `interrupted`, or `failed`; `complete` means the audit ran to completion, even if coverage gaps remain. |

`finished` is recomputed on every checkpoint and becomes true as soon as all
planned selections are resolved, before the final audit. Counts use only the
plan's root selections, so old recovery children cannot inflate completion.
The `completion` object records these counts and unresolved selection names.
Manual acceptance resolves an execution item without proving its coverage.

The final audit refreshes root statuses from the exports and source pieces it
actually checked, writes current completion fields to the checkpoint and
execution manifest, then prints its final summary. A coordinator
`--report-coverage` for the saved instance also refreshes that checkpoint;
reporting against a different export directory leaves the saved instance's
checkpoint untouched. Export files remain unchanged by reporting.

This is completion schema version 2 (`completion.version`). Previously,
`finished` mirrored full 3D audit success. Thus an otherwise completed capture
could have `finished: false` because omitted upper air remained unverified.
Now that case has `finished: true`, `coverage_complete: false`, and usually
`column_coverage_complete: true`. The audit JSON retains `complete` for full
3D coverage and adds `collection_finished` and `collection_completion` for
execution completion. Exit codes still follow the 3D audit result, so a
finished collection can return 1 while conservative height gaps remain.

Starting collection or an audit clears the current coverage-result fields to
`null`. Interruption/failure never turns an old audit success into a new one;
collection checkpoints remain available independently of the audit outcome.

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

Each file also has a `column_presence` result counting requested, observed,
and missing X/Z columns, with exact `missing_regions`. A column is observed
when at least one block or decor record exists at any returned height.
Columns without records or corroborating deeper-rescan evidence are removed
from credited envelopes even when the file's SizeX/SizeZ spans them.
The report's top-level `column_presence` checks
the union against the requested footprint; per-file results include the extra
padding in each selection. Missing files and invalid files receive no credit.
A positive column check does not prove every height in that column loaded.

Collection validation rejects unverified columns. A column consisting entirely
of air within the requested Y band triggers an automatic deeper rescan, as
described below, instead of repeatedly subdividing the same air space.

This proof is conditional on WorldEdit accurately reporting exported extents
and contents. Sparse JSON alone cannot establish chunk residency or distinguish
all missing terrain from intentionally omitted air. It also cannot establish a
single simultaneous world snapshot across separate captures. The audit records
these evidence limits rather than claiming stronger verification.

## Automatic deeper rescans

Normal collection keeps the saved plan's initial Y bounds (currently 65–180).
When a direct export has columns without any block/decor records, the
coordinator first force-loads the selection, teleports the player, checks the
chunk queues, and retries the selection at its original Y bounds. This avoids
turning an initially unloaded selection into a large batch of deeper probes.
Any records recovered by that retry are saved immediately into formerly empty
columns. Only holes that remain trigger 32-by-32 chunk exports from Y=0 through
the selection's requested ceiling. Output reports the remaining column count
and numbers each deeper probe within its finite batch.

This recovery happens before the usual splitting fallback. Existing exports
with holes also take this path on resume. Multiple holes and neighboring
selections reuse a deeper probe when its bounds match and its retained data
still validates. Normal-height retry exports are retained as
`<prefix>_loaded_<bounds-hash>.json` and recorded in the progress file.

Each fresh probe requests `/executeas PLAYER /chunk forceload ...`, waits for
its acknowledgement, teleports the player into that chunk, and queries
`/executeas PLAYER /chunk cit` until both queues report false. It then selects
and exports the deeper area, validates its format, and requires a block/decor
record in every probe column. It does not accept a queue response alone.
Probe failures remain unverified and fall back to bounded collection recovery.
Forced columns remain loaded until server restart; the coordinator does not
restart the server. In step-by-step mode, deeper recovery commands follow
automatically after the initial export confirmation.

The coordinator reads acknowledgements from `../Logs/server-main.log` relative
to the WorldEdit export directory. Override it for a different layout:

```sh
python3 -B vintage_story_export_coordinator.py \
  --target-pane PANE --export-dir /absolute/path/to/WorldEdit \
  --server-log /absolute/path/to/Logs/server-main.log
```

`--rescan-y-min 0` is the default. A different lower bound must be below the
selection floor and keep the probe within selection limits. It changes only
diagnostic probes, not the saved plan or its horizontal footprint. If the log
cannot be read, empty columns stay unverified; the printed message identifies
the path needed for `--server-log`. Preview and reporting never send commands.

A deeper probe can yield three outcomes:

- **In-band records recovered:** records at the requested Y heights are added
  only to previously empty parent columns. Existing records and both block
  layers are preserved; records below the requested floor remain in the probe.
- **Empty band corroborated:** every probe column has records, but the highest
  record in an affected column is below the requested floor. The retained
  deeper export and load acknowledgements corroborate the empty band. No air
  blocks are invented, and that column no longer triggers repeated retries.
- **Still unverified:** invalid output, empty probe columns, failed commands,
  or missing acknowledgements cannot establish the empty-band result.

Before changing a parent, the coordinator saves its exact original bytes as
`NAME.before-rescan-HASH.json` in the export directory. Replacement is atomic.
Each completed deeper probe is applied to the parent and checkpointed before
the next probe begins. If interrupted, an audit can credit the completed
contributions while leaving the rest of the selection unverified. A parent
with no direct block records can also retain partial, corroborated empty-band
coverage. It is not marked complete until all columns are resolved.
The parent includes `ColumnRescanEvidence` containing probe filenames, bounds,
SHA256 values, load responses, and its backup reference. The progress file's
`column_rescans` records cached probes and per-section results; commands and
responses are under `<prefix>-commands/rescans/`. Probe filenames begin with
`<prefix>_depth_`. Keep the parent, referenced probes, and source backups.
Original entity blobs remain in parents; any additional probe entities remain
in the raw probe rather than being merged without entity identity checks.

Reporting revalidates and hashes referenced probes, so saved success alone
cannot establish coverage. Missing, changed, or invalid probe files revoke
their empty-band credit; the next collection run can capture new evidence.
Per-file `column_presence.missing_columns` retains the raw no-record count,
`empty_columns_confirmed_by_rescan` counts corroborated bands, and
`unverified_columns` counts the remaining unknowns. At report level,
`columns_without_direct_records` retains the raw deficit and `missing_columns`
counts unverified columns after all sources are considered. Full-height credit
for a corroborated empty band is conditional on WorldEdit correctly returning
loaded contents; this is operational evidence, not a block-by-block air listing.
Other trimmed-height gaps remain visible in the ordinary 3D report.

Earlier versions applied probes only after the entire batch completed. An
interrupted run could therefore have valid probe files and cached hashes but
still show zero audited coverage for an empty parent. Updated collection
applies those retained probes first on resume, before sending new commands.
Reporting itself remains read-only with respect to exports: it does not merge
unapplied cached probes into their parents. A fresh run should use a separate
plan/run directory and the target instance's exports without old capture files.

## Repair reported empty columns

`tools/recapture_columns.py` is an operational repair utility for reports in
the independent audit's format. The report must contain the matching
`plan_hash`, `empty_columns` total, and `empty_column_rectangles` entries with
`file`, `xmin`, `xmax`, `zmin`, `zmax`, and `columns`. Bounds must lie inside
their named plan units and reported rectangles must not overlap.

Prepare a reviewable request without contacting the server:

```sh
python3 -B tools/recapture_columns.py \
  --report /absolute/path/to/independent-audit.json \
  --export-dir /absolute/path/to/WorldEdit \
  --work-dir repairs/coastal-connection-repair
```

Review `requests.json` in that work directory. To execute the same request,
add `--execute --target-pane PANE --player PLAYER`, plus
`--server-log /absolute/path/to/Logs/server-main.log`. If needed, add
`--tmux-binary /absolute/path/to/tmux`. Do not run another collector or change
the same player's WorldEdit selection while this utility runs.

The utility requests `/chunk forceload` for the reported regions, then uses
patches no larger than 128 X by 32 Z blocks at the plan's Y bounds. Each fresh
patch gets a teleport and `/executeas PLAYER /chunk cit` queue check before
selection/export. A queue check alone is insufficient; returned JSON must
also pass the column check. Forced chunk columns remain pinned until the
server restarts. The utility does not restart the server.

A persistent completely empty patch uses the same deeper-probe machinery as
normal collection. Probes are reused for nearby tiny holes. The utility records
below-floor observations and attaches retained evidence to the parent before
declaring its empty band corroborated. It does not fabricate air records or
alter the plan's Y bounds.

`results.json` records patch status, bounds, hashes, deeper observations, and
parent replacements. `commands/` retains commands and server responses;
`backups/` holds byte-for-byte parent originals. Repair and probe exports
remain in the WorldEdit directory. Resuming with the same arguments reuses
validated patches and unchanged completed parents.

Only previously empty columns are overlaid into a parent, preserving its
existing block/decor records and both block layers. Parent replacement is
atomic and includes `ColumnRepairProvenance`; unresolved parents stay
unchanged. Original entity blobs remain in parents; additional patch entities
are retained in patch files rather than merged without entity identity
checks. Synthetic merged parents must be repaired through their source leaves
instead; this utility refuses to overlay them.

`results.json`'s `complete` is recomputed from fresh parent validation and
requires no unverified columns. Corroborated empty bands can satisfy that check.
Run the normal coverage audit after repair to refresh the report and both
previews. It can still report unverified columns or trimmed height; those
limitations remain visible.

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
gaps, exact internal column holes, decor presence, repair overlays, and offline
coordinator reporting. They do not contact a server.

The guide is in `docs/`, tests are in `tests/`, repair utilities are in `tools/`, and the application modules
remain at the repository root. The plan JSON, coastline input, and changelog
belong together in `capture-plans/coastal-connection/`. Generated previews,
commands, progress files, coverage reports, and temporary files are ignored by
Git. Retain progress and exported data locally when needed for resume or audit;
ignoring them does not make them disposable.
