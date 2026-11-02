"""Status snapshots and real signals, with no server interaction."""
from copy import deepcopy
import os
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

import vintage_story_export_coordinator as coordinator


class StatusTests(unittest.TestCase):
    def reporter(self):
        cuboids=[coordinator.Cuboid(i,0,1,65,180,0,1,f'unit{i}') for i in range(1,6)]
        state={'schema':coordinator.capture_run.SCHEMA,
               'sections':{'unit1':{'status':'complete'},'unit2':{'status':'complete'},
                           'unit3':{'status':'incomplete'},'unit4':{'status':'waiting',
                               'candidate':{'probes':[{},{}],'unverified_columns':8}}},
               'finished':False,'coverage_complete':True,'overlap_complete':False,'audit_status':'not_run'}
        reporter=coordinator.RunStatus(cuboids,state)
        reporter.set_activity('Waiting for stable export',cuboids[3])
        return reporter

    def test_snapshot_includes_counts_elapsed_activity_and_last_checkpoint(self):
        reporter=self.reporter()
        before=deepcopy(reporter.state)
        text=reporter.status_text()
        for expected in ('2/5 selections captured','(40.0%; 2 verified);',
                         '1 partial/incomplete, 1 waiting, 1 pending','Elapsed',
                         'Waiting for stable export','section 4','Saved deeper confirmations: 2',
                         '8 columns still unverified'):
            self.assertIn(expected,text)
        self.assertEqual(reporter.state,before)

    def test_snapshot_separates_captured_count_from_final_certification(self):
        reporter=self.reporter()
        text=reporter.status_text()
        self.assertIn('Run finished: False; required coverage: True; overlaps: False; audit: not_run.',text)
        self.assertNotIn('accepted',text)

    def test_actual_status_signals_use_unbuffered_output_without_mutating_state(self):
        reporter=self.reporter()
        before=deepcopy(reporter.state)
        numbers=[getattr(signal,name) for name in ('SIGINFO','SIGUSR1') if hasattr(signal,name)]
        if not numbers:
            self.skipTest('No supported status signals')
        with coordinator.status_signals(reporter), patch.object(os,'write') as write, patch('builtins.print',side_effect=AssertionError('Signal must not use buffered print')):
            for number in numbers:
                signal.raise_signal(number)
        self.assertEqual(write.call_count,len(numbers))
        self.assertTrue(all(call.args[0]==2 for call in write.call_args_list))
        self.assertIn(b'Waiting for stable export',write.call_args.args[1])
        self.assertIn(b'2 verified',write.call_args.args[1])
        self.assertEqual(reporter.state,before)

    def test_reentrant_signal_is_ignored_and_closed_stderr_does_not_abort(self):
        reporter=self.reporter()
        with patch.object(os,'write',side_effect=lambda *args:reporter.on_status_signal(None,None)) as write:
            reporter.on_status_signal(None,None)
        self.assertEqual(write.call_count,1)
        self.assertFalse(reporter.handling_signal)
        with patch.object(os,'write',side_effect=OSError('closed stream')):
            reporter.on_status_signal(None,None)
        self.assertFalse(reporter.handling_signal)

    def test_handlers_and_active_reporter_restore_on_exception(self):
        numbers=[getattr(signal,name) for name in ('SIGINFO','SIGUSR1') if hasattr(signal,name)]
        before={number:signal.getsignal(number) for number in numbers}
        interrupt_handler=signal.getsignal(signal.SIGINT)
        active=coordinator.ACTIVE_STATUS
        reporter=self.reporter()
        with self.assertRaisesRegex(RuntimeError,'test error'):
            with coordinator.status_signals(reporter):
                self.assertIs(coordinator.ACTIVE_STATUS,reporter)
                self.assertEqual(signal.getsignal(signal.SIGINT),interrupt_handler)
                coordinator.report_activity('Auditing export 10/160',clear_region=True)
                self.assertNotIn('section 4',reporter.status_text())
                raise RuntimeError('test error')
        self.assertIs(coordinator.ACTIVE_STATUS,active)
        self.assertEqual({number:signal.getsignal(number) for number in numbers},before)
        self.assertEqual(signal.getsignal(signal.SIGINT),interrupt_handler)

    def test_signal_reports_while_sleeping_and_process_continues(self):
        numbers=[getattr(signal,name) for name in ('SIGINFO','SIGUSR1') if hasattr(signal,name)]
        if not numbers:
            self.skipTest('No supported status signals')
        program='''
import time
import vintage_story_export_coordinator as c
r=c.RunStatus([], {'sections':{}})
r.set_activity('Waiting for export file')
with c.status_signals(r):
    print('ready',flush=True)
    time.sleep(1)
    print('continued',flush=True)
'''
        process=subprocess.Popen([sys.executable,'-B','-c',program],
            cwd=Path(coordinator.__file__).parent,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'ready')
            for number in numbers:
                process.send_signal(number)
            out,err=process.communicate(timeout=5)
            self.assertEqual(process.returncode,0,err)
            self.assertIn('continued',out)
            self.assertIn('Waiting for export file',err)
            self.assertIn('Status (PID',err)
            self.assertNotIn('accepted',err)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()


if __name__=='__main__':
    unittest.main()
