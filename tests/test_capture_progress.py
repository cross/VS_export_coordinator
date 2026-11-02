"""Audit progress stays visible during work and leaves clean terminal/log output."""
import io
import threading
import unittest

from capture_progress import AuditProgress


class Terminal(io.StringIO):
    def isatty(self):
        return True


class HeartbeatStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.heartbeat=threading.Event()

    def flush(self):
        if 'elapsed' in self.getvalue():
            self.heartbeat.set()


class AuditProgressTests(unittest.TestCase):
    def test_terminal_redraws_current_phase_then_finishes_the_original_line(self):
        stream=Terminal(); now=[0.0]
        progress=AuditProgress(stream,clock=lambda:now[0])
        progress.begin('Checking saved selection 1/44','primary + 4 probes')
        self.assertNotIn('\n',stream.getvalue())
        progress.update('comparing deeper probe 2/4')
        now[0]=5.0; progress.refresh()
        self.assertIn('\r  Checking saved selection 1/44: comparing deeper probe 2/4 (5s elapsed).',stream.getvalue())
        now[0]=12.4; progress.finish('validated')
        self.assertEqual(stream.getvalue().split('\r')[-1].rstrip(),
                         '  Checking saved selection 1/44: primary + 4 probes ... validated (12.4s).')
        self.assertEqual(stream.getvalue().count('\n'),1)

    def test_redirected_output_samples_progress_without_terminal_control_characters(self):
        stream=io.StringIO(); now=[0.0]
        progress=AuditProgress(stream,clock=lambda:now[0])
        progress.begin('Checking selection','primary + 2 probes')
        initial=stream.getvalue()
        progress.update('parsing primary export')
        now[0]=4.9; progress.refresh()
        self.assertEqual(stream.getvalue(),initial)
        now[0]=5.0; progress.refresh()
        progress.update('comparing deeper probe 1/2')
        now[0]=9.9; progress.refresh()
        self.assertEqual(stream.getvalue().count('elapsed'),1)
        now[0]=10.0; progress.refresh()
        progress.finish('validated')
        text=stream.getvalue()
        self.assertNotIn('\r',text)
        self.assertNotIn('\x1b',text)
        self.assertEqual(len(text.splitlines()),4)
        self.assertIn('parsing primary export (5s elapsed)',text)
        self.assertIn('comparing deeper probe 1/2 (10s elapsed)',text)

    def test_worker_reports_during_a_long_step_without_waiting_for_completion(self):
        stream=HeartbeatStream()
        with AuditProgress(stream,interval=0.02) as progress:
            progress.begin('Checking selection','primary export')
            progress.update('checking column evidence')
            self.assertTrue(stream.heartbeat.wait(1),'No progress was emitted while the audit step was still active')
            self.assertIn('checking column evidence',stream.getvalue())
            self.assertNotIn('validated',stream.getvalue())
            progress.finish('validated')
        self.assertFalse(progress._thread.is_alive())

    def test_interruption_closes_terminal_line_and_stops_the_worker(self):
        stream=Terminal()
        with self.assertRaises(KeyboardInterrupt):
            with AuditProgress(stream,clock=lambda:0.0) as progress:
                progress.begin('Computing requested coverage and gaps')
                raise KeyboardInterrupt
        self.assertTrue(stream.getvalue().endswith('interrupted (0.0s).\n'))
        self.assertFalse(progress._thread.is_alive())
