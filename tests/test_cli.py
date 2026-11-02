"""CLI documentation and rejection of invalid input before any run writes."""
import io
from itertools import combinations
import tempfile
import unittest
from unittest.mock import patch

import vintage_story_export_coordinator as c


class CliTests(unittest.TestCase):
    def rejected(self,args,expected):
        errors=io.StringIO()
        with patch('sys.argv',['coordinator',*args]), patch('sys.stderr',new=errors), \
             patch('builtins.input',side_effect=AssertionError('Must fail before prompting')), \
             patch.object(c.capture_plan,'load_plan',side_effect=AssertionError('Must fail before reading a plan')), \
             patch.object(c.capture_run.Run,'create',side_effect=AssertionError('Must not create a run')), \
             patch.object(c.capture_run,'register_run',side_effect=AssertionError('Must not update the index')), \
             patch('subprocess.run',side_effect=AssertionError('Must not send commands')):
            with self.assertRaises(SystemExit) as stopped:
                c.main()
        self.assertEqual(stopped.exception.code,2)
        self.assertIn(expected,errors.getvalue())

    def test_help_explains_every_option_and_dynamic_defaults(self):
        parser=c.build_parser()
        text=' '.join(parser.format_help().split())
        for action in parser._actions:
            with self.subTest(option=action.option_strings):
                self.assertTrue(action.help)
                for option in action.option_strings:
                    self.assertIn(option,text)
        for expected in ('REQUIRED for collection','default Xplosionist','default ~/WorldEdit',
                         '../Logs/server-main.log relative to --export-dir','tmux from PATH',
                         './capture-runs/RUN_ID','capture-plans/coastal-connection/plan.json',
                         'existing runs use their frozen plan','saved values when continuing',
                         'mutually exclusive','does not change exported Y bounds',
                         'VS_CAPTURE_RUN_INDEX'):
            self.assertIn(expected,text)
        for option,value in (('--overlap',c.capture_run.DEFAULT_OVERLAP),
                             ('--coverage-y',c.capture_run.DEFAULT_COVERAGE_Y),
                             ('--max-attempts',c.capture_run.DEFAULT_MAX_ATTEMPTS),
                             ('--rescan-y-min',c.capture_run.DEFAULT_PROBE_YMIN)):
            action=next(action for action in parser._actions if option in action.option_strings)
            self.assertIn(f'new-run default: {value}',action.help)
            self.assertIsNone(action.default)  # Omission must preserve saved values.

    def test_operation_modes_are_mutually_exclusive_before_side_effects(self):
        for left,right in combinations(('--preview','--prepare-only','--report-coverage'),2):
            with self.subTest(left=left,right=right):
                self.rejected([left,right],'not allowed with argument')

    def test_explicit_run_selectors_are_mutually_exclusive(self):
        self.rejected(['--resume','some-run','--run-dir','some-run','--preview'],
                      'not allowed with argument')

    def test_new_run_rejects_incompatible_modes_and_existing_directory(self):
        for arguments in (['--resume','some-run'],['--preview'],['--report-coverage']):
            with self.subTest(arguments=arguments):
                self.rejected(['--new-run',*arguments],'--new-run cannot be combined')
        with tempfile.TemporaryDirectory() as directory:
            with open(directory+'/unrelated.txt','w') as stream:
                stream.write('keep')
            self.rejected(['--new-run','--run-dir',directory,'--prepare-only'],
                          'nonexistent or completely empty run directory')

    def test_collection_requires_pane_before_reading_or_writing_run(self):
        self.rejected([],'--target-pane is required for collection')
        self.rejected(['--target-pane','  '],'--target-pane is required for collection')

    def test_numeric_bounds_are_checked_before_prompts_and_run_creation(self):
        for option,value in (('--overlap','0'),('--overlap','17'),('--max-attempts','0'),
                             ('--coverage-y','-1'),('--rescan-y-min','-1')):
            with self.subTest(option=option,value=value):
                self.rejected(['--prepare-only',option,value],option+' must be')
        self.rejected(['--prepare-only','--overlap','1.5'],'invalid int value')


if __name__=='__main__':
    unittest.main()
