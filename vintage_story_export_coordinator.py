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
import signal
import subprocess
import sys
import time
from typing import Any

import v12_capture_plan as capture_plan
import server_console
import capture_run


DEFAULT_PLAN = Path(__file__).resolve().parent / "capture-plans" / "coastal-connection" / "plan.json"
DEFAULT_PLAYER = "Xplosionist"


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


def _main(status_reporter: RunStatus) -> int:
    parser=argparse.ArgumentParser(description="Collect isolated, fresh WorldEdit exports with coverage and seam checks.")
    parser.add_argument('--plan',type=Path)
    parser.add_argument('--run-dir',type=Path,help='run directory: create if new, continue if compatible')
    parser.add_argument('--resume',type=Path,help='explicitly resume/audit an isolated run directory')
    parser.add_argument('--new-run',action='store_true',help='start a fresh run instead of automatically continuing a compatible one')
    parser.add_argument('--target-pane')
    parser.add_argument('--tmux-binary',type=Path)
    parser.add_argument('--export-dir',type=Path)
    parser.add_argument('--server-log',type=Path)
    parser.add_argument('--player')
    parser.add_argument('--overlap',type=int,default=None,help='east/south overlap blocks (new runs: 3)')
    parser.add_argument('--coverage-y',type=int,default=None,help='required coverage height (new runs: 109)')
    parser.add_argument('--max-attempts',type=int,default=None,help='whole-section attempt limit (new runs: 3)')
    parser.add_argument('--rescan-y-min',type=int,default=None)
    parser.add_argument('--preview',action='store_true')
    parser.add_argument('--prepare-only',action='store_true',help='create an isolated run and preview, then exit before sending commands')
    parser.add_argument('--report-coverage',action='store_true')
    parser.add_argument('--verbose',action='store_true')
    parser.add_argument('--step-by-step',action='store_true')
    args=parser.parse_args()
    if args.resume and args.run_dir:
        parser.error('--resume and --run-dir are mutually exclusive')
    if sum((args.preview,args.prepare_only,args.report_coverage))>1:
        parser.error('Choose only one of --preview, --prepare-only, or --report-coverage')
    if args.new_run and (args.resume or args.report_coverage or args.preview):
        parser.error('--new-run cannot be combined with --resume, --report-coverage, or --preview')
    if args.new_run and args.run_dir and args.run_dir.expanduser().exists():
        parser.error('--new-run requires a run directory that does not already exist')
    if not args.preview and not args.report_coverage and not args.prepare_only and not (args.target_pane or '').strip():
        parser.error('--target-pane is required for collection')
    resuming=False
    try:
        explicit=args.resume or (args.run_dir if args.run_dir and args.run_dir.expanduser().exists() else None)
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
                run=choose_compatible_run(candidates,blocked,allow_new=not args.report_coverage)
                resuming=run is not None
            if run is None and args.report_coverage:
                parser.error('No compatible capture run found; --report-coverage requires an existing run manifest')
            if run is None:
                player=args.player or prompt_value('Player name for /executeas',DEFAULT_PLAYER)
                export_dir=(args.export_dir or Path(prompt_value('WorldEdit export directory',str(Path.home()/'WorldEdit')))).expanduser().resolve()
                server_log=(args.server_log or export_dir.parent/'Logs'/'server-main.log').expanduser().resolve()
                if not export_dir.is_dir() or not server_log.is_file():
                    parser.error('Collection requires the actual server WorldEdit directory and readable --server-log')
                run=capture_run.Run.create(plan,plan_path,export_dir,server_log,player,
                    directory=args.run_dir.expanduser().resolve() if args.run_dir else None,
                    overlap=3 if args.overlap is None else args.overlap,
                    coverage_y=109 if args.coverage_y is None else args.coverage_y,
                    max_attempts=3 if args.max_attempts is None else args.max_attempts,
                    probe_ymin=0 if args.rescan_y_min is None else args.rescan_y_min)
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
        report_activity(description,Cuboid(unit['index'],**unit['capture'],name=unit['id']) if unit else None)
    run.activity=activity
    run.verbose=args.verbose
    print(f"Operational model: {run.state['operational_model']} ({capture_run.SCHEMA})",flush=True)
    print(f"Export prefix: {run.state['prefix']}",flush=True)
    print(f"Run ID: {run.state['run_id']}\nPlan SHA256: {run.plan['plan_hash']}\nRun directory: {run.directory}\nProgress: {run.path}",flush=True)
    print(f"Preview: {run.directory/(run.state['run_id']+'-preview.svg')}\n"
          f"{len(run.units)} raw selections; {run.state['settings']['overlap']}-block overlaps; "
          f"required coverage through Y={run.state['settings']['coverage_y']}.",flush=True)
    print_status_help()
    if args.preview or args.prepare_only:
        run.preview()
        return 0
    if not args.report_coverage:
        if not Path(run.state['source']['server_log']).is_file() or not run.export_dir.is_dir():
            parser.error('Saved run server log/export directory is unavailable')
        binary=str(args.tmux_binary.expanduser().resolve()) if args.tmux_binary else 'tmux'
        run.console=server_console.Console(binary,args.target_pane,Path(run.state['source']['server_log']),
            run.directory/'commands',paste_commands,activity=lambda description:activity(description))
    original_handler=signal.getsignal(signal.SIGINT)
    try:
        with run.locked():
            if resuming and args.max_attempts is not None:
                if args.max_attempts < run.state['attempt_limit']:
                    raise capture_run.CaptureError('Retry budget advanced while opening the run; refusing to lower it')
                run.state['attempt_limit']=args.max_attempts
                run.save()
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
