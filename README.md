# Vintage Story Export Coordinator

This tool was written to capture an export of a large amount of data from
an existing Vintage Story world.  Given a player in creative mode with
permissions to use WorldEdit and /tp commands, this script can send commands
via the server console that will use that players interface to the server
to generate commands to select and export a large region, a little bit at
a time.

For the versioned v12 coastline capture planner, previews, execution commands,
height extensions, freshwater supplements, and coverage auditing, see
[docs/V12_CAPTURE.md](docs/V12_CAPTURE.md).
For user inputs and step-by-step plan generation/editing, start at
[Generate or revise a plan](docs/V12_CAPTURE.md#generate-or-revise-a-plan).

Repository layout:

- `vintage_story_export_coordinator.py`: collection commands, retries, and
  resume using a specified coastline plan. This is the primary tool; it
  currently defaults to `capture-plans/coastal-connection/plan.json`
- `v12_capture_plan.py`: planning, previews, and raw WorldEdit validation.
  Imported by `vintage_story_export_coordinator.py`
- `capture_run.py`: isolated capture runs, fresh immutable attempts, whole-section
  retries, deeper confirmations, and overlap/coverage certification. This is
  the default collection engine.
- `capture_inventory.py`: manifest-based inventories of selected terrain files,
  supporting probes, pending candidates, and unused export attempts.
- `server_console.py`: console commands and acknowledgements from the server log.
- `capture-plans/coastal-connection/`: current plan, coastline input, and
  changelog; Git retains earlier plan revisions.
- `docs/`: operating instructions and design information.
- `tests/`: automated tests using simulated exports; no server is contacted.

New captures use a unique run ID. Repeating the same plan command automatically
continues a compatible run; `--new-run` requests a separate fresh capture.
`--resume RUN_DIRECTORY` selects a particular run explicitly. See
[isolated runs](docs/V12_CAPTURE.md#isolated-captures-and-compatible-resume).
Only the current run format is supported. Coverage reports require a run
manifest; loose files and older progress formats are not imported.

Run the tests from the repository root:

```sh
python3 -B -m unittest discover -s tests -v
```

Generated previews and runtime records are excluded by `.gitignore`. Keep the
progress records and world exports needed for an unfinished capture locally.
