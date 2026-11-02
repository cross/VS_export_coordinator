"""Investigation prompts wait without commanding the server and stop safely."""
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import vintage_story_export_coordinator as c


class Terminal(io.StringIO):
    def isatty(self):
        return True


class RecoveryPromptTests(unittest.TestCase):
    def setUp(self):
        self.unit={'id':'s00001','index':1,'capture':dict(xmin=0,xmax=449,ymin=65,ymax=180,zmin=0,zmax=49)}
        self.run=SimpleNamespace(units={'s00001':self.unit},path=Path('/tmp/test-progress.json'),
            state={'sections':{'s00001':{'attempts':[{}, {}, {}]}}},section_attempt_limit=lambda record:3)

    def prompt(self,stdin,timeout=300,clock=None,ready=None):
        output=io.StringIO()
        with patch('sys.stdin',stdin), patch('sys.stdout',new=output), \
             patch.object(c.select,'select',side_effect=ready) as selected, \
             patch.object(c.time,'monotonic',side_effect=clock or (lambda:0)), \
             patch('subprocess.run',side_effect=AssertionError('No server commands while investigating')):
            choice=c.recovery_prompt(self.run,self.unit,'Chunk queues did not settle',timeout)
        return choice,output.getvalue(),selected

    def test_retry_and_quit_are_explicit_choices(self):
        for text,expected in (('r\n','retry'),('q\n','quit')):
            stdin=Terminal(text)
            with self.subTest(text=text):
                choice,output,selected=self.prompt(stdin,ready=lambda *args:([stdin],[],[]))
                self.assertEqual(choice,expected)
                self.assertIn('ONE additional whole-selection attempt',output)
                self.assertIn('No server commands will be sent while paused',output)
                self.assertIn('X 0..449, Y 65..180, Z 0..49',output)
                self.assertIn('Attempts used: 3',output)
                self.assertEqual(selected.call_args.args[3],300)

    def test_timeout_stops_without_authorizing_retry(self):
        choice,output,_=self.prompt(Terminal('r\n'),ready=lambda *args:([],[],[]))
        self.assertEqual(choice,'timed_out')
        self.assertIn('timed out',output)

    def test_wait_extends_deadline_and_invalid_input_does_not(self):
        stdin=Terminal('invalid\nw\nr\n')
        choice,output,selected=self.prompt(stdin,timeout=10,clock=iter([0,1,3,4,5]).__next__,
                                           ready=lambda *args:([stdin],[],[]))
        self.assertEqual(choice,'retry')
        self.assertEqual([call.args[3] for call in selected.call_args_list],[9,7,9])
        self.assertIn('existing timeout still applies',output)
        self.assertIn('Investigation wait extended',output)

    def test_zero_disables_timeout_and_eof_quits(self):
        for text,expected in (('r\n','retry'),('','quit')):
            with self.subTest(text=text):
                choice,output,selected=self.prompt(Terminal(text),timeout=0)
                self.assertEqual(choice,expected)
                self.assertIn('Waiting indefinitely',output)
                selected.assert_not_called()

    def test_noninteractive_input_stops_immediately(self):
        choice,output,selected=self.prompt(io.StringIO('r\n'))
        self.assertEqual(choice,'unattended')
        selected.assert_not_called()
        self.assertIn('No interactive terminal input',output)

    def test_ctrl_c_at_recovery_prompt_quits(self):
        stdin=Terminal()
        with patch.object(stdin,'readline',side_effect=KeyboardInterrupt):
            choice,_,_=self.prompt(stdin,timeout=0)
        self.assertEqual(choice,'quit')


if __name__=='__main__':
    unittest.main()
