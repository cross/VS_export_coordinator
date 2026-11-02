#!/usr/bin/env python3
# vim:et sts=4 sw=4
"""Capture a saved coastline plan in an isolated, resumable WorldEdit run.

New runs preload selections, write uniquely named immutable raw exports, retry
whole sections, and compare overlapping block-code multisets. Compatible runs
continue automatically using manifest-bound evidence. No terrain is edited.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import textwrap
import time
from typing import Any

import v12_capture_plan as capture_plan
import server_console
import capture_run
import capture_inventory


DEFAULT_PLAN = Path(__file__).resolve().parent / "capture-plans" / "coastal-connection" / "plan.json"
DEFAULT_PLAYER = "Xplosionist"
DEFAULT_RECOVERY_TIMEOUT = 300


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


class RunStatus:
    def __init__(self, cuboids: list[Cuboid], state: dict[str, Any]):
        self.cuboids = cuboids
        self.state = state
        self.started = time.monotonic()
        self.activity = ("Starting", self.started, None)
        self.handling_signal = False

    def configure(self, cuboids: list[Cuboid], state: dict[str, Any]) -> None:
        self.cuboids, self.state = cuboids, state

    def set_activity(self, description: str, region: Cuboid | None = None,
                     clear_region: bool = False) -> None:
        # One assignment gives a signal handler a consistent activity snapshot.
        self.activity = (description, time.monotonic(),
                         None if clear_region else region or self.activity[2])

    def status_text(self) -> str:
        now = time.monotonic()
        sections = self.state.get("sections", {})
        statuses = [sections.get(c.name, {}).get("status", "pending") for c in self.cuboids]
        done, total = statuses.count("complete"), len(self.cuboids)
        partial = statuses.count("incomplete")
        active = statuses.count("waiting")
        pending = total - done - partial - active
        description, since, region = self.activity
        text = (f"Status (PID {os.getpid()}): {done}/{total} selections captured "
                f"({100 * done / total if total else 0:.1f}%; {done} verified); "
                f"{partial} partial/incomplete, {active} waiting, {pending} pending. "
                f"Elapsed {now - self.started:.0f}s.\nCurrent: {description} ({now - since:.0f}s).")
        text += (f"\nRun finished: {self.state.get('finished',False)}; "
                 f"required coverage: {self.state.get('coverage_complete')}; "
                 f"overlaps: {self.state.get('overlap_complete')}; "
                 f"audit: {self.state.get('audit_status','not_run')}.")
        if region:
            text += "\n" + region_label(region)
            candidate = sections.get(region.name, {}).get("candidate")
            if candidate:
                text += f"\nSaved deeper confirmations: {len(candidate['probes'])}."
                if 'unverified_columns' in candidate:
                    text += f" {candidate['unverified_columns']:,} columns still unverified."
        return text

    def on_status_signal(self, signum, frame) -> None:
        if self.handling_signal:
            return
        self.handling_signal = True
        try:
            # A signal can interrupt print() while its buffered stream is
            # locked. Bypass Python's buffered I/O rather than reentering it.
            os.write(2, ("\n" + self.status_text() + "\n").encode("utf-8", errors="replace"))
        except OSError:
            pass  # A closed diagnostic stream must not interrupt collection.
        finally:
            self.handling_signal = False

ACTIVE_STATUS: RunStatus | None = None


def report_activity(description: str, region: Cuboid | None = None,
                    clear_region: bool = False) -> None:
    if ACTIVE_STATUS:
        ACTIVE_STATUS.set_activity(description, region, clear_region)


@contextmanager
def status_signals(reporter: RunStatus):
    """Install SIGINFO where available and SIGUSR1; restore handlers on exit."""
    global ACTIVE_STATUS
    previous_reporter, ACTIVE_STATUS = ACTIVE_STATUS, reporter
    previous_handlers = {}
    try:
        for name in ("SIGINFO", "SIGUSR1"):
            number = getattr(signal, name, None)
            if number is not None and number not in previous_handlers:
                previous_handlers[number] = signal.getsignal(number)
                signal.signal(number, reporter.on_status_signal)
        yield
    finally:
        for number, handler in previous_handlers.items():
            signal.signal(number, handler)
        ACTIVE_STATUS = previous_reporter


class UserQuit(Exception):
    """An explicit quit, distinct from Ctrl-C requesting a pause."""


def abort_immediately(signum=None, frame=None) -> None:
    """Bypass saving, cleanup, and stream flushing on a second Ctrl-C."""
    os._exit(130)


def paste_commands(command_path: Path, target_pane: str, tmux_binary: str = "tmux") -> None:
    """Use the same tmux executable for selection, export, and teleport commands."""
    subprocess.run([tmux_binary, "load-buffer", str(command_path), ";",
                    "paste-buffer", "-d", "-t", target_pane], check=True)


def prompt_value(label: str, default: str) -> str:
    report_activity("Waiting for input: " + label)
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def print_status_help() -> None:
    if hasattr(signal, "SIGINFO"):
        print(f"Ctrl-T (SIGINFO) reports status immediately; also: kill -USR1 {os.getpid()}", flush=True)
    elif hasattr(signal, "SIGUSR1"):
        print(f"Immediate status: kill -USR1 {os.getpid()} (this system has no Ctrl-T/SIGINFO).", flush=True)


def recovery_prompt(run,unit,problem,timeout):
    """Wait without sending commands; timeout/EOF/quit preserve incomplete work."""
    report_activity('Paused for investigation and a recovery decision',
                    Cuboid(unit['index'],**unit['capture'],name=unit['id']))
    print(f"\nSelection {unit['index']}/{len(run.units)} needs investigation.",flush=True)
    print('Export area: '+capture_run.bounds_label(unit['capture']),flush=True)
    print('Last problem: '+problem,flush=True)
    record=run.state['sections'][unit['id']]
    print(f"Attempts used: {len(record['attempts'])}; current limit for this selection: {run.section_attempt_limit(record)}.",flush=True)
    print(f'Progress saved: {run.path}',flush=True)
    if not sys.stdin.isatty():
        print('No interactive terminal input; stopping with progress saved. Rerun interactively to investigate/retry.',flush=True)
        return 'unattended'
    print('No server commands will be sent while paused. You may fly around or investigate the server.\n'
          'Retry authorizes ONE additional whole-selection attempt, including force-loading and teleporting again.',flush=True)
    if timeout:
        print(f'No response for {timeout}s saves and quits; w restarts that timer.',flush=True)
    else:
        print('Waiting indefinitely for your choice (--recovery-timeout 0).',flush=True)
    deadline=time.monotonic()+timeout if timeout else None
    while True:
        try:
            print('[r]etry once, [w]ait longer, [q]save and quit: ',end='',flush=True)
            if deadline is not None:
                remaining=deadline-time.monotonic()
                if remaining<=0 or not select.select([sys.stdin],[],[],remaining)[0]:
                    print('\nRecovery wait timed out; stopping with progress saved.',flush=True)
                    return 'timed_out'
            line=sys.stdin.readline()
            if not line:
                print('\nInput closed; stopping with progress saved.',flush=True)
                return 'quit'
            choice=line.strip().lower()
            if choice=='r':
                return 'retry'
            if choice=='q':
                return 'quit'
            if choice=='w':
                deadline=time.monotonic()+timeout if timeout else None
                print('Investigation wait extended; no server commands sent.',flush=True)
            else:
                print('Choose r, w, or q; the existing timeout still applies.',flush=True)
        except KeyboardInterrupt:
            print('\nStopping with progress saved.',flush=True)
            return 'quit'
        except (OSError,ValueError) as exc:
            print(f'\nCannot read recovery input: {exc}. Stopping with progress saved.',flush=True)
            return 'quit'


def choose_compatible_run(runs,blocked,*,allow_new=True):
    if len(runs)==1:
        run=runs[0]
        print(f"Continuing compatible run {run.state['run_id']} in {run.directory}",flush=True)
        return run
    if runs:
        print('Multiple compatible runs match this plan. Choose which one to continue:',flush=True)
        for index,run in enumerate(runs,1):
            source=run.state['source']
            status='finished' if run.state.get('finished') else 'unfinished'
            print(f"  {index}: {run.directory} ({status}; player {source['player']}; exports {source['export_dir']})",flush=True)
        while True:
            choice=input('Run number, '+('n for a new run, ' if allow_new else '')+'or q to stop: ').strip().lower()
            if choice=='n' and allow_new:
                return None
            if choice in ('','q'):
                raise UserQuit
            if choice.isdigit() and 1<=int(choice)<=len(runs):
                return runs[int(choice)-1]
            print('Choose a listed run number'+(', n' if allow_new else '')+', or q.',flush=True)
    if blocked:
        print('Prior runs were found, but compatibility could not be established:',flush=True)
        for item in blocked:
            print(f"  {item['directory']}: {item['problem']}",flush=True)
        if not allow_new:
            raise capture_run.CompatibilityError('No compatible run available for reporting')
        while True:
            choice=input('n to start a separate new run, or q to stop for review: ').strip().lower()
            if choice=='n':
                return None
            if choice in ('','q'):
                raise UserQuit
            print('Choose n or q.',flush=True)
    return None


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    def _split_lines(self, text, width):
        return textwrap.wrap(' '.join(text.split()),width,
                             break_long_words=False,break_on_hyphens=False)


def build_parser() -> argparse.ArgumentParser:
    parser=argparse.ArgumentParser(
        description="Capture a saved plan using loaded WorldEdit exports, whole-section retries, and overlap checks.",
        formatter_class=HelpFormatter,
        epilog="""Requirements and combinations:
  Collection is the default mode; only collection requires --target-pane.
  New runs need a player, an existing WorldEdit directory, and a readable
  server log. Omitted player/export directory values are prompted for;
  the log path is derived from the export directory unless supplied.
  Preview, preparation, reporting, and export inventories send no server commands.

  --run-dir and --resume are mutually exclusive.
  --preview, --prepare-only, --report-coverage, and --list-exports are exclusive.
  --new-run cannot be combined with --resume, --preview, --report-coverage,
  or --list-exports;
  with --run-dir it requires a nonexistent or completely empty directory.
  --report-coverage and --list-exports require an existing compatible run.
  --list-exports cannot be combined with --max-attempts; stop the collector
  before listing. It checks hashes and writes file lists, leaving the checkpoint
  and WorldEdit files unchanged. Partial runs produce partial validated lists.
  Exhausted automatic attempts pause for investigation on an interactive
  terminal: r authorizes one extra attempt, w extends the wait, q saves/quits.
  Timeout or noninteractive input stops with incomplete progress saved.

Continuation and defaults:
  Without an explicit run directory, continue the compatible run matching
  the plan and supplied server/player/settings; multiple matches prompt.
  If no run matches, create ./capture-runs/RUN_ID relative to cwd.
  Omitted source/policy options use saved values when continuing a run.
  Supplied player, export directory, server log, overlap, coverage Y, and probe
  floor must match that run. The retry limit may be raised but never lowered.
  Capture-policy options do not change the source plan or its export Y bounds;
  they have no effect on a standalone --preview of the source plan.

Paths and output:
  Relative paths resolve from cwd; the default source plan is beside the script.
  Exports stay in the server's actual WorldEdit directory. The run directory
  holds its frozen plan, manifest, progress, commands, preview, and audit.
  Run index: $XDG_STATE_HOME/vintage-story-export-coordinator/runs.json
  Default XDG_STATE_HOME: ~/.local/state. VS_CAPTURE_RUN_INDEX overrides the file.

Examples:
  %(prog)s --target-pane PANE --export-dir DIRECTORY
  %(prog)s --run-dir RUN_DIRECTORY --prepare-only --export-dir DIRECTORY
  %(prog)s --resume RUN_DIRECTORY --report-coverage
  %(prog)s --resume RUN_DIRECTORY --list-exports
""")
    selection=parser.add_argument_group('plan and run selection')
    selection.add_argument('--plan',type=Path,metavar='FILE',
        help='source plan (default: capture-plans/coastal-connection/plan.json beside this script); existing runs use their frozen plan; an explicit FILE must match it')
    directories=selection.add_mutually_exclusive_group()
    directories.add_argument('--run-dir',type=Path,metavar='DIR',
        help='initialize DIR if absent or completely empty; otherwise continue it only if compatible (default: discover a matching run, otherwise ./capture-runs/RUN_ID)')
    directories.add_argument('--resume',type=Path,metavar='DIR',
        help='explicitly select an existing run for collection, preview, preparation, audit, or export inventory (default: automatic discovery)')
    selection.add_argument('--new-run',action='store_true',
        help='force a separate fresh run instead of automatic continuation (default: off); incompatible with --resume, --preview, --report-coverage, and --list-exports')

    operations=parser.add_argument_group('operation (default: collect and finish with an audit)')
    modes=operations.add_mutually_exclusive_group()
    modes.add_argument('--preview',action='store_true',
        help="render source-plan cores to FILE.svg beside the plan; with an existing --run-dir/--resume, render that run's padded mosaic inside its directory; then exit (default: off)")
    modes.add_argument('--prepare-only',action='store_true',
        help='create/open a run, freeze new-run settings, write its padded preview, and exit; new runs still need player/export/log inputs, but no pane or tmux (default: off)')
    modes.add_argument('--report-coverage',action='store_true',
        help="audit an existing run's selected files, deeper confirmations, coverage and overlaps; write RUN_ID-audit.json and update its checkpoint; no pane or live log needed (default: off)")
    modes.add_argument('--list-exports',action='store_true',
        help='inventory an existing stopped run: recheck selected file hashes and write RUN_ID-exports.json plus primary/required/unused filename lists inside the run directory; excludes pending/unvalidated data; no pane or live log needed (default: off)')

    server=parser.add_argument_group('server and console')
    server.add_argument('--target-pane',metavar='PANE',
        help='tmux pane containing the server console; REQUIRED for collection, unused for preview/preparation/reporting/inventory; no default')
    server.add_argument('--tmux-binary',type=Path,metavar='PATH',
        help='tmux executable path for all collection commands (default: tmux from PATH); unused when no commands are sent')
    server.add_argument('--export-dir',type=Path,metavar='DIR',
        help='actual server WorldEdit output directory, which must already exist; does not redirect server output (new runs: prompt, default ~/WorldEdit; continuing runs: saved directory)')
    server.add_argument('--server-log',type=Path,metavar='FILE',
        help='log for command acknowledgements (new runs: ../Logs/server-main.log relative to --export-dir; continuing runs: saved log path); readable log required to create or collect a run')
    server.add_argument('--player',metavar='NAME',
        help=f'player used by /executeas and teleports; must be online with creative mode and command permissions for collection (new runs: prompt, default {DEFAULT_PLAYER}; continuing runs: saved player)')

    policy=parser.add_argument_group('capture policy (new-run defaults below; continuation uses saved values)')
    policy.add_argument('--overlap',type=int,metavar='BLOCKS',
        help=f'east/south padding for neighboring export comparisons, 1..16 (new-run default: {capture_run.DEFAULT_OVERLAP}); may subdivide cores to obey selection limits')
    policy.add_argument('--coverage-y',type=int,metavar='Y',
        help=f"required surface/column confirmation height, nonnegative and clamped to each selection's Y range (new-run default: {capture_run.DEFAULT_COVERAGE_Y}); does not change exported Y bounds")
    policy.add_argument('--max-attempts',type=int,metavar='N',
        help=f'automatic whole-section attempt ceiling per selection, including previous attempts, N >= 1 (new-run default: {capture_run.DEFAULT_MAX_ATTEMPTS}); continuing runs may only raise it; recovery prompt can authorize one additional attempt')
    policy.add_argument('--rescan-y-min',type=int,metavar='Y',
        help=f'nonnegative floor for automatic deeper confirmation probes (new-run default: {capture_run.DEFAULT_PROBE_YMIN}); when needed, probes must start below the selection floor and fit selection limits')

    interaction=parser.add_argument_group('collection output and interaction')
    interaction.add_argument('--verbose',action='store_true',
        help='show each completed deeper probe, export/probe counts in results, successful shared-block comparisons, saved-export filenames, and every audit/inventory selection (default: off); normal output samples long probe runs; detailed stages remain available through the status signal')
    interaction.add_argument('--step-by-step',action='store_true',
        help='prompt before each primary selection needing capture/retry; loading and deeper probes remain automatic; collection only (default: off)')
    interaction.add_argument('--recovery-timeout',type=int,default=DEFAULT_RECOVERY_TIMEOUT,metavar='SECONDS',
        help=f'wait for a recovery decision after automatic attempts are exhausted (default: {DEFAULT_RECOVERY_TIMEOUT}s); 0 waits indefinitely; timeout saves and stops, noninteractive input stops immediately; collection only')
    return parser


def _main(status_reporter: RunStatus) -> int:
    parser=build_parser()
    args=parser.parse_args()
    for option,value,minimum,maximum in (
            ('--overlap',args.overlap,1,16),
            ('--coverage-y',args.coverage_y,0,None),
            ('--max-attempts',args.max_attempts,1,None),
            ('--rescan-y-min',args.rescan_y_min,0,None),
            ('--recovery-timeout',args.recovery_timeout,0,None)):
        if value is not None and (value<minimum or (maximum is not None and value>maximum)):
            constraint=f'{minimum}..{maximum}' if maximum is not None else f'>= {minimum}'
            parser.error(f'{option} must be {constraint}')
    if args.new_run and (args.resume or args.report_coverage or args.preview or args.list_exports):
        parser.error('--new-run cannot be combined with --resume, --report-coverage, --preview, or --list-exports')
    if args.list_exports and args.max_attempts is not None:
        parser.error('--list-exports cannot be combined with --max-attempts; inventory leaves the checkpoint unchanged')
    if args.new_run and args.run_dir:
        directory=args.run_dir.expanduser()
        if os.path.lexists(directory) and not capture_run.is_empty_directory(directory):
            parser.error('--new-run requires a nonexistent or completely empty run directory')
    if not (args.preview or args.report_coverage or args.prepare_only or args.list_exports) and not (args.target_pane or '').strip():
        parser.error('--target-pane is required for collection')
    resuming=False
    try:
        explicit=args.resume or (args.run_dir if args.run_dir and os.path.lexists(args.run_dir.expanduser())
                                and not capture_run.is_empty_directory(args.run_dir.expanduser()) else None)
        if explicit:
            run=capture_run.Run.resume(explicit,export_dir=args.export_dir,server_log=args.server_log,player=args.player)
            resuming=True
            if args.plan and capture_plan.load_plan(args.plan.expanduser().resolve())['plan_hash']!=run.plan['plan_hash']:
                parser.error('Supplied plan differs from the frozen run plan')
        else:
            plan_path=(args.plan or DEFAULT_PLAN).expanduser().resolve()
            plan=capture_plan.load_plan(plan_path)
            if args.preview:
                if args.run_dir:
                    parser.error('Use --prepare-only to create a run and preview')
                report_activity('Rendering plan preview',clear_region=True)
                capture_plan.render_preview(plan,plan_path.with_suffix('.svg'))
                capture_plan.print_summary(plan,plan_path)
                return 0
            run=None
            if not args.run_dir and not args.new_run:
                candidates,blocked=capture_run.discover_runs(plan,plan_path,export_dir=args.export_dir,
                    server_log=args.server_log,player=args.player,
                    settings={'overlap':args.overlap,'coverage_y':args.coverage_y,'probe_ymin':args.rescan_y_min})
                run=choose_compatible_run(candidates,blocked,allow_new=not (args.report_coverage or args.list_exports))
                resuming=run is not None
            if run is None and (args.report_coverage or args.list_exports):
                operation='--list-exports' if args.list_exports else '--report-coverage'
                parser.error(f'No compatible capture run found; {operation} requires an existing run manifest')
            if run is None:
                player=args.player or prompt_value('Player name for /executeas',DEFAULT_PLAYER)
                export_dir=(args.export_dir or Path(prompt_value('WorldEdit export directory',str(Path.home()/'WorldEdit')))).expanduser().resolve()
                server_log=(args.server_log or export_dir.parent/'Logs'/'server-main.log').expanduser().resolve()
                if not export_dir.is_dir() or not server_log.is_file():
                    parser.error('Collection requires the actual server WorldEdit directory and readable --server-log')
                run=capture_run.Run.create(plan,plan_path,export_dir,server_log,player,
                    directory=args.run_dir.expanduser().resolve() if args.run_dir else None,
                    overlap=capture_run.DEFAULT_OVERLAP if args.overlap is None else args.overlap,
                    coverage_y=capture_run.DEFAULT_COVERAGE_Y if args.coverage_y is None else args.coverage_y,
                    max_attempts=capture_run.DEFAULT_MAX_ATTEMPTS if args.max_attempts is None else args.max_attempts,
                    probe_ymin=capture_run.DEFAULT_PROBE_YMIN if args.rescan_y_min is None else args.rescan_y_min)
        if resuming:
            for key,value in (('overlap',args.overlap),('coverage_y',args.coverage_y),('probe_ymin',args.rescan_y_min)):
                if value is not None and value!=run.state['settings'][key]:
                    parser.error('Capture settings differ; use --new-run and a new directory to change '+key)
            if args.max_attempts is not None and args.max_attempts < run.state['attempt_limit']:
                parser.error('Resume may explicitly raise, but not lower, the retry budget')
        capture_run.register_run(run)
    except (KeyboardInterrupt,EOFError,UserQuit):
        print('No run started; prior outputs are unchanged.',flush=True)
        return 130
    except (OSError,ValueError,KeyError,TypeError) as exc:
        parser.error(str(exc))
    status_reporter.configure([Cuboid(unit['index'],**unit['core'],name=unit['id']) for unit in run.state['units']],run.state)
    def activity(description,unit=None):
        report_activity(description,Cuboid(unit['index'],**unit['capture'],name=unit['id']) if unit else None,
                        clear_region=unit is None)
    run.activity=activity
    run.verbose=args.verbose
    run.recovery_handler=lambda unit,problem:recovery_prompt(run,unit,problem,args.recovery_timeout)
    print(f"Operational model: {run.state['operational_model']} ({capture_run.SCHEMA})",flush=True)
    print(f"Export prefix: {run.state['prefix']}",flush=True)
    print(f"Run ID: {run.state['run_id']}\nPlan SHA256: {run.plan['plan_hash']}\nRun directory: {run.directory}\nProgress: {run.path}",flush=True)
    print(f"Preview: {run.directory/(run.state['run_id']+'-preview.svg')}\n"
          f"{len(run.units)} raw selections; {run.state['settings']['overlap']}-block overlaps; "
          f"required coverage through Y={run.state['settings']['coverage_y']}.",flush=True)
    print_status_help()
    if not (args.preview or args.prepare_only or args.report_coverage or args.list_exports):
        if not Path(run.state['source']['server_log']).is_file() or not run.export_dir.is_dir():
            parser.error('Saved run server log/export directory is unavailable')
        binary=str(args.tmux_binary.expanduser().resolve()) if args.tmux_binary else 'tmux'
        run.console=server_console.Console(binary,args.target_pane,Path(run.state['source']['server_log']),
            run.directory/'commands',paste_commands,activity=report_activity)
    original_handler=signal.getsignal(signal.SIGINT)
    try:
        with run.locked():
            if args.list_exports:
                try:
                    report=capture_inventory.write_inventory(run)
                    return 1 if report['problems'] else 0
                except KeyboardInterrupt:
                    print('\nExport inventory interrupted; checkpoint and WorldEdit files are unchanged.',flush=True)
                    return 130
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    print(f'Export inventory failed: {exc}',file=sys.stderr,flush=True)
                    return 1
            if resuming and args.max_attempts is not None:
                if args.max_attempts < run.state['attempt_limit']:
                    raise capture_run.CaptureError('Retry budget advanced while opening the run; refusing to lower it')
                run.state['attempt_limit']=args.max_attempts
                run.save()
            if args.preview or args.prepare_only:
                run.preview()
                return 0
            while True:
                try:
                    if args.report_coverage:
                        return 0 if run.audit()['finished'] else 1
                    return run.collect(confirm=args.step_by_step)
                except KeyboardInterrupt:
                    signal.signal(signal.SIGINT,abort_immediately)
                    run.state.update(finished=False,audit_status='interrupted')
                    run.save()
                    print('\nPaused; run checkpoint saved. Ctrl-C again aborts.',flush=True)
                    try:
                        choice=input('[r]etry unfinished work, [q]uit: ').strip().lower()
                    except (KeyboardInterrupt,EOFError):
                        choice='q'
                    if choice=='r':
                        signal.signal(signal.SIGINT,original_handler)
                        continue
                    print(f'Stopped. Restart with the same plan to continue, or use --resume {run.directory}',flush=True)
                    signal.signal(signal.SIGINT,original_handler)
                    return 130
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    run.state.update(finished=False,audit_status='failed',last_error=str(exc))
                    run.save()
                    print(f'Run incomplete: {exc}\nProgress: {run.path}',file=sys.stderr,flush=True)
                    return 1
                finally:
                    signal.signal(signal.SIGINT,original_handler)
    except capture_run.CaptureError as exc:
        print(str(exc),file=sys.stderr,flush=True)
        return 1


def main() -> int:
    reporter = RunStatus([], {"sections": {}})
    with status_signals(reporter):
        return _main(reporter)


if __name__ == "__main__":
    raise SystemExit(main())
