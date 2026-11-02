"""Plan-only restart discovers compatible runs without adopting loose exports."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import capture_run as runs
import v12_capture_plan as p
import vintage_story_export_coordinator as c


class AutoResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.exports=self.root/'WorldEdit'; self.exports.mkdir()
        self.log=self.root/'server.log'; self.log.touch()
        self.workspace=self.root/'work'; self.workspace.mkdir()
        self.index=self.root/'state'/'runs.json'
        env=patch.dict(os.environ,{'VS_CAPTURE_RUN_INDEX':str(self.index)})
        env.start(); self.addCleanup(env.stop)
        core=dict(xmin=0,xmax=1,ymin=65,ymax=67,zmin=0,zmax=1)
        self.plan=p.seal_plan(dict(version=p.VERSION,geometry={},geometry_hash=p.digest({}),geometry_version='test',
            world_center=p.WORLD_CENTER,requests=[core],cuboids=[core.copy()],raw_footprint=[core],
            requested_y={'ymin':65,'ymax':67},partition={'mode':'exact','x_span':2,'z_span':2}))
        self.source=self.root/'plans'/'A.json'; self.source.parent.mkdir()
        runs.atomic_json(self.source,self.plan)

    def prepare(self,directory):
        args=['--plan',str(self.source),'--run-dir',str(directory),'--prepare-only','--player','Player',
              '--export-dir',str(self.exports),'--server-log',str(self.log),'--coverage-y','66']
        self.assertEqual(self.invoke(args)[0],0)
        return runs.Run.resume(directory)

    def invoke(self,args,answers=None):
        output=io.StringIO()
        with patch('sys.argv',['coordinator',*args]), patch.object(Path,'cwd',return_value=self.workspace), patch('sys.stdout',new=output), patch('subprocess.run',side_effect=AssertionError('No server commands')), patch('builtins.input',side_effect=answers if answers is not None else AssertionError('No prompts expected')):
            result=c.main()
        return result,output.getvalue()

    def test_plan_only_restart_finds_custom_run_across_working_directories(self):
        directory=self.root/'outside-workspace'/'planA-run'
        original=self.prepare(directory)
        observed=[]
        def collect(run,**kwargs):
            observed.append((run.directory.resolve(),run.state['run_id']))
            return 0
        with patch.object(runs.Run,'collect',autospec=True,side_effect=collect), patch.object(runs.Run,'create',side_effect=AssertionError('Restart must not start fresh')):
            result,output=self.invoke(['--plan',str(self.source),'--target-pane','%1'])
        self.assertEqual(result,0)
        self.assertEqual(observed,[(directory.resolve(),original.state['run_id'])])
        self.assertIn('Continuing compatible run',output)
        self.assertEqual(len(runs.read_run_index()['runs']),1)

    def test_repeating_run_dir_also_continues_compatible_run(self):
        directory=self.root/'planA-run'; original=self.prepare(directory)
        with patch.object(runs.Run,'collect',return_value=0), patch.object(runs.Run,'create',side_effect=AssertionError('No new run')):
            result,_=self.invoke(['--plan',str(self.source),'--run-dir',str(directory),'--target-pane','%1'])
        self.assertEqual(result,0)
        self.assertEqual(runs.Run.resume(directory).state['run_id'],original.state['run_id'])

    def test_empty_run_directory_starts_new_run_with_or_without_new_run_flag(self):
        for flag in ([],['--new-run']):
            with self.subTest(flag=flag):
                directory=self.root/('explicit-fresh' if flag else 'empty-run')
                directory.mkdir()
                result,_=self.invoke(['--plan',str(self.source),'--run-dir',str(directory),
                    '--prepare-only','--player','Player','--export-dir',str(self.exports),
                    '--server-log',str(self.log),*flag])
                self.assertEqual(result,0)
                restored=runs.Run.resume(directory)
                self.assertEqual(restored.state['sections'],{})
                self.assertEqual(restored.plan,self.plan)

    def test_emptied_former_run_directory_gets_new_identity(self):
        directory=self.root/'recycled'
        original=self.prepare(directory)
        for path in directory.iterdir():
            path.unlink()
        replacement=self.prepare(directory)
        self.assertNotEqual(original.state['run_id'],replacement.state['run_id'])
        self.assertEqual(replacement.state['sections'],{})

    def test_nonempty_unrecognized_directory_is_rejected_without_changes(self):
        for name in ('.hidden','unrelated.json','subdirectory'):
            with self.subTest(name=name):
                directory=self.root/name.removeprefix('.')
                directory.mkdir()
                entry=directory/name
                if name=='subdirectory':
                    entry.mkdir()
                else:
                    entry.write_text('keep')
                args=['--plan',str(self.source),'--run-dir',str(directory),'--prepare-only']
                with patch('sys.stderr',new=io.StringIO()), patch.object(runs.Run,'create',side_effect=AssertionError('Not empty')):
                    with self.assertRaises(SystemExit) as stopped:
                        self.invoke(args)
                self.assertEqual(stopped.exception.code,2)
                self.assertEqual(list(directory.iterdir()),[entry])
                if entry.is_file():
                    self.assertEqual(entry.read_text(),'keep')

    def test_empty_directory_cannot_be_resumed_or_audited_as_a_run(self):
        directory=self.root/'empty'; directory.mkdir()
        for args in (['--resume',str(directory),'--prepare-only'],
                     ['--plan',str(self.source),'--run-dir',str(directory),'--report-coverage']):
            with self.subTest(args=args), patch('sys.stderr',new=io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    self.invoke(args)
                self.assertEqual(stopped.exception.code,2)
                self.assertEqual(list(directory.iterdir()),[])

    def test_cli_exhausted_run_stops_without_commands_when_input_is_noninteractive(self):
        run=self.prepare(self.root/'exhausted')
        unit=run.state['units'][0]
        run.state['sections'][unit['id']]={'status':'incomplete','selected':None,
            'error':'Chunk queues did not settle',
            'attempts':[{'id':str(index),'status':'failed'} for index in range(3)]}
        run.save()
        with patch('sys.stdin',new=io.StringIO()):
            result,output=self.invoke(['--resume',str(run.directory),'--target-pane','%1'])
        self.assertEqual(result,1)
        self.assertIn('No interactive terminal input',output)
        checkpoint=p.read_json(run.path)
        self.assertEqual(checkpoint['recovery']['status'],'unattended')
        self.assertEqual(checkpoint['recovery']['reason'],'Chunk queues did not settle')
        self.assertFalse(checkpoint['finished'])
        self.assertEqual(len(checkpoint['sections'][unit['id']]['attempts']),3)

    def test_unindexed_local_manifest_is_discovered_and_registered(self):
        directory=self.source.parent/'planA-run'
        original=runs.Run.create(self.plan,self.source,self.exports,self.log,'Player',directory=directory,coverage_y=66)
        self.assertFalse(self.index.exists())
        with patch.object(runs.Run,'collect',return_value=0), patch.object(runs.Run,'create',side_effect=AssertionError('No new run')):
            result,_=self.invoke(['--plan',str(self.source),'--target-pane','%1'])
        self.assertEqual(result,0)
        self.assertEqual(runs.read_run_index()['runs'][0]['run_id'],original.state['run_id'])

    def test_no_prior_run_creates_default_directory_without_writing_beside_plan(self):
        result,_=self.invoke(['--plan',str(self.source),'--prepare-only','--player','Player',
            '--export-dir',str(self.exports),'--server-log',str(self.log),'--coverage-y','66'])
        self.assertEqual(result,0)
        entry=runs.read_run_index()['runs'][0]
        self.assertEqual(Path(entry['directory']).parent,(self.workspace/'capture-runs').resolve())
        self.assertEqual(list(self.source.parent.iterdir()),[self.source])

    def test_new_run_derives_default_log_from_export_directory(self):
        default_log=self.exports.parent/'Logs'/'server-main.log'
        default_log.parent.mkdir(); default_log.touch()
        result,_=self.invoke(['--plan',str(self.source),'--prepare-only','--player','Player',
                             '--export-dir',str(self.exports)])
        self.assertEqual(result,0)
        entry=runs.read_run_index()['runs'][0]
        run=runs.Run.resume(entry['directory'])
        self.assertEqual(run.state['source']['server_log'],str(default_log.resolve()))
        self.assertEqual(run.state['settings'],dict(overlap=3,coverage_y=109,max_attempts=3,probe_ymin=0))

    def test_explicit_retry_budget_raise_is_honored_in_existing_run_offline_modes(self):
        for mode in ('--prepare-only','--preview'):
            with self.subTest(mode=mode):
                original=self.prepare(self.root/mode.removeprefix('--'))
                result,_=self.invoke(['--resume',str(original.directory),mode,'--max-attempts','5'])
                self.assertEqual(result,0)
                restored=runs.Run.resume(original.directory)
                self.assertEqual(restored.state['attempt_limit'],5)
                self.assertEqual(restored.state['settings']['max_attempts'],3)

    def test_new_run_flag_is_explicit_fresh_capture_and_retains_original(self):
        original=self.prepare(self.root/'first')
        old=original.path.read_bytes()
        with patch.object(c,'choose_compatible_run',side_effect=AssertionError('Must bypass discovery')):
            result,_=self.invoke(['--plan',str(self.source),'--new-run','--prepare-only','--player','Player',
                '--export-dir',str(self.exports),'--server-log',str(self.log),'--coverage-y','66'])
        self.assertEqual(result,0)
        entries=runs.read_run_index()['runs']
        self.assertEqual(len(entries),2)
        self.assertNotEqual(entries[0]['run_id'],entries[1]['run_id'])
        self.assertEqual(original.path.read_bytes(),old)

    def test_multiple_compatible_runs_prompt_instead_of_guessing(self):
        self.prepare(self.root/'first'); second=self.prepare(self.root/'second')
        observed=[]
        def collect(run,**kwargs):
            observed.append(run.state['run_id']); return 0
        with patch.object(runs.Run,'collect',autospec=True,side_effect=collect):
            result,output=self.invoke(['--plan',str(self.source),'--target-pane','%1'],answers=['2'])
        self.assertEqual(result,0)
        self.assertEqual(observed,[second.state['run_id']])
        self.assertIn('Multiple compatible runs',output)

    def test_ambiguity_can_be_cancelled_without_starting_or_overwriting(self):
        first=self.prepare(self.root/'first'); second=self.prepare(self.root/'second')
        before=[run.path.read_bytes() for run in (first,second)]
        with patch.object(runs.Run,'create',side_effect=AssertionError('No new run')):
            result,_=self.invoke(['--plan',str(self.source),'--target-pane','%1'],answers=['q'])
        self.assertEqual(result,130)
        self.assertEqual([run.path.read_bytes() for run in (first,second)],before)

    def test_model_mismatch_requires_review_and_does_not_silently_start_fresh(self):
        original=self.prepare(self.root/'prior')
        state=p.read_json(original.path); state['operational_model']='old-model'
        runs.atomic_json(original.path,state); before=original.path.read_bytes()
        with patch.object(runs.Run,'create',side_effect=AssertionError('No new run')):
            result,output=self.invoke(['--plan',str(self.source),'--target-pane','%1'],answers=['q'])
        self.assertEqual(result,130)
        self.assertIn('compatibility could not be established',output)
        self.assertEqual(original.path.read_bytes(),before)

    def test_explicit_server_instance_filters_compatible_runs(self):
        original=self.prepare(self.root/'first')
        other_exports=self.root/'OtherWorldEdit'; other_exports.mkdir()
        other=runs.Run.create(self.plan,self.source,other_exports,self.log,'Player',directory=self.root/'second',coverage_y=66)
        runs.register_run(other)
        found,blocked=runs.discover_runs(self.plan,self.source,export_dir=self.exports)
        self.assertEqual([run.state['run_id'] for run in found],[original.state['run_id']])
        self.assertFalse(blocked)

    def test_plan_only_report_uses_discovered_run_without_server_commands(self):
        original=self.prepare(self.root/'prior')
        def audit(run):
            self.assertEqual(run.state['run_id'],original.state['run_id'])
            return {'finished':False}
        with patch.object(runs.Run,'audit',autospec=True,side_effect=audit):
            result,_=self.invoke(['--plan',str(self.source),'--report-coverage'])
        self.assertEqual(result,1)

    def test_report_requires_manifest_even_when_plan_named_exports_exist(self):
        loose=self.exports/(self.plan['cuboids'][0]['name']+'.json')
        loose.write_text('{}')
        with patch('sys.stderr',new=io.StringIO()) as errors, patch.object(runs.Run,'create',side_effect=AssertionError('Reports cannot create runs')):
            with self.assertRaises(SystemExit) as stopped:
                self.invoke(['--plan',str(self.source),'--export-dir',str(self.exports),'--report-coverage'])
        self.assertEqual(stopped.exception.code,2)
        self.assertIn('requires an existing run manifest',errors.getvalue())
        self.assertFalse(self.index.exists())
        self.assertEqual(list(self.source.parent.iterdir()),[self.source])

    def test_report_cannot_create_missing_explicit_run_directory(self):
        directory=self.root/'missing-run'
        with patch('sys.stderr',new=io.StringIO()), patch.object(runs.Run,'create',side_effect=AssertionError('No new run')):
            with self.assertRaises(SystemExit) as stopped:
                self.invoke(['--plan',str(self.source),'--run-dir',str(directory),'--report-coverage'])
        self.assertEqual(stopped.exception.code,2)
        self.assertFalse(directory.exists())

    def test_export_inventory_cannot_create_a_run_or_adopt_loose_exports(self):
        (self.exports/(self.plan['cuboids'][0]['name']+'.json')).write_text('{}')
        with patch('sys.stderr',new=io.StringIO()), patch.object(runs.Run,'create',side_effect=AssertionError('No new run')):
            with self.assertRaises(SystemExit) as stopped:
                self.invoke(['--plan',str(self.source),'--export-dir',str(self.exports),'--list-exports'])
        self.assertEqual(stopped.exception.code,2)
        self.assertFalse(self.index.exists())


if __name__=='__main__':
    unittest.main()
