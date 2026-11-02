"""Exact selected-file inventories exclude abandoned work and protect candidates."""
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import capture_inventory as inventory
import capture_run as runs
import v12_capture_plan as p
import vintage_story_export_coordinator as c
from test_capture_run import FakeConsole, plan_for


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.exports=self.root/'WorldEdit'; self.exports.mkdir()
        self.log=self.root/'server.log'; self.log.touch()
        env=patch.dict(os.environ,{'VS_CAPTURE_RUN_INDEX':str(self.root/'state'/'runs.json')})
        env.start(); self.addCleanup(env.stop)
        self.plan=plan_for([dict(xmin=x,xmax=x+1,ymin=65,ymax=67,zmin=0,zmax=1) for x in (0,2)])

    def create(self,name='run',**console_options):
        run=runs.Run.create(self.plan,self.root/'source.json',self.exports,self.log,'Player',
            directory=self.root/name,coverage_y=66,
            wait=lambda path,issued:runs.wait_fresh(path,issued,stable_seconds=0,poll=0))
        run.console=FakeConsole(self.exports,**console_options)
        return run

    def collect(self,run):
        with patch('sys.stdout',new=io.StringIO()):
            self.assertEqual(run.collect(),0)

    def write(self,run):
        checkpoint=run.path.read_bytes()
        with run.locked(), patch('sys.stdout',new=io.StringIO()), \
             patch('subprocess.run',side_effect=AssertionError('No server commands')), \
             patch.object(p,'inspect_export',side_effect=AssertionError('Inventory must not reparse block records')):
            report=inventory.write_inventory(run)
        self.assertEqual(run.path.read_bytes(),checkpoint)
        return report

    def test_completed_capture_lists_exact_primary_files_and_supporting_probes(self):
        run=self.create(shafts={(0,0)}); self.collect(run)
        before={path.name:runs.digest_file(path) for path in self.exports.iterdir()}
        report=self.write(run)
        self.assertTrue(report['run_certified'])
        self.assertEqual(report['validated_selections'],2)
        self.assertEqual(len(report['primary']),2)
        self.assertEqual(len(report['supporting_probes']),1)
        primary={record['selected']['file'] for record in run.state['sections'].values()}
        probes={ref['file'] for record in run.state['sections'].values() for ref in record.get('probes',[])}
        self.assertEqual(set(Path(report['file_lists']['primary']['path']).read_text().splitlines()),primary)
        self.assertEqual(set(Path(report['file_lists']['required']['path']).read_text().splitlines()),primary|probes)
        self.assertEqual(Path(report['file_lists']['unused']['path']).read_text(),'')
        self.assertTrue(all(item['core'] and item['capture'] for item in report['primary']))
        self.assertEqual({path.name:runs.digest_file(path) for path in self.exports.iterdir()},before)

    def test_failed_and_superseded_attempts_are_unused_not_terrain(self):
        run=self.create(partial_first=True); self.collect(run)
        report=self.write(run)
        active={item['file'] for item in [*report['primary'],*report['supporting_probes']]}
        issued={ref['file'] for record in run.state['sections'].values() for ref in record['issued_exports']}
        self.assertTrue(report['unused_issued_files'])
        unused={item['file'] for item in report['unused_issued_files']}
        self.assertEqual(unused,issued-active)
        self.assertEqual(set(Path(report['file_lists']['unused']['path']).read_text().splitlines()),unused)
        self.assertTrue(active.isdisjoint(unused))

    def test_export_interrupted_before_primary_validation_is_unused(self):
        run=self.create(); unit=run.state['units'][0]
        with patch.object(run,'read_ref',side_effect=KeyboardInterrupt), patch('sys.stdout',new=io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                run.capture_section(unit)
        report=self.write(run)
        self.assertFalse(report['run_certified'])
        self.assertEqual(report['primary'],[])
        self.assertEqual(report['pending_candidates'],[])
        self.assertEqual(len(report['unused_issued_files']),1)

    def test_interrupted_candidate_is_excluded_from_validated_lists_and_protected(self):
        run=self.create(shafts={(0,0)}); command=run.console.command
        def interrupt(text,*args,**kwargs):
            if '/we select 0 0 ' in text:
                raise KeyboardInterrupt
            return command(text,*args,**kwargs)
        run.console.command=interrupt
        with patch('sys.stdout',new=io.StringIO()), self.assertRaises(KeyboardInterrupt):
            run.capture_section(run.state['units'][0])
        report=self.write(run)
        self.assertEqual(report['primary'],[])
        self.assertEqual(len(report['pending_candidates']),1)
        pending=report['pending_candidates'][0]['file']
        self.assertIn(pending,report['protected_files'])
        self.assertNotIn(pending,Path(report['file_lists']['unused']['path']).read_text().splitlines())
        self.assertEqual(Path(report['file_lists']['required']['path']).read_text(),'')

    def test_missing_supporting_probe_excludes_its_whole_selection(self):
        run=self.create(shafts={(0,0)}); self.collect(run)
        record=run.state['sections']['s00001']
        probe=record['probes'][0]['file']; (self.exports/probe).unlink()
        report=self.write(run)
        self.assertTrue(report['checkpoint_finished'])
        self.assertFalse(report['run_certified'])
        self.assertEqual(report['validated_selections'],1)
        self.assertNotIn(record['selected']['file'],{item['file'] for item in report['primary']})
        self.assertIn(record['selected']['file'],report['protected_files'])
        self.assertIn(probe,report['issued_without_file'])
        self.assertEqual(len(report['problems']),1)

    def test_changed_missing_or_symlinked_primary_never_enters_usable_lists(self):
        for kind in ('changed','missing','symlink'):
            with self.subTest(kind=kind):
                run=self.create(kind); self.collect(run)
                name=run.state['sections']['s00001']['selected']['file']; path=self.exports/name
                payload=path.read_bytes()
                if kind=='changed':
                    path.write_bytes(payload+b' ')
                else:
                    path.unlink()
                    if kind=='symlink':
                        target=self.root/'outside.json'; target.write_bytes(payload); path.symlink_to(target)
                report=self.write(run)
                self.assertFalse(report['run_certified'])
                self.assertEqual(report['validated_selections'],1)
                self.assertNotIn(name,Path(report['file_lists']['required']['path']).read_text().splitlines())
                self.assertIn(name,report['protected_files'])
                self.assertNotIn(name,{item['file'] for item in report['unused_issued_files']})

    def test_unknown_run_prefix_file_and_other_runs_are_not_adopted_or_marked_unused(self):
        run=self.create(); self.collect(run)
        unknown=run.state['prefix']+'_s99999_section_'+'a'*32+'.json'
        (self.exports/unknown).write_text('{}')
        (self.exports/'unrelated-export.json').write_text('{}')
        report=self.write(run)
        self.assertEqual([item['file'] for item in report['unrecognized_run_files']],[unknown])
        self.assertEqual(report['unused_issued_files'],[])
        self.assertEqual(len(report['primary']),2)

    def test_checkpoint_change_aborts_inventory_without_publishing_lists(self):
        run=self.create(); self.collect(run)
        verify=run.verified_ref_bytes
        def change_checkpoint(ref):
            result=verify(ref)
            checkpoint=p.read_json(run.path); checkpoint['external_change']=True
            runs.atomic_json(run.path,checkpoint)
            return result
        with patch.object(run,'verified_ref_bytes',side_effect=change_checkpoint), patch('sys.stdout',new=io.StringIO()):
            with self.assertRaisesRegex(runs.CaptureError,'Checkpoint changed during'):
                inventory.write_inventory(run)
        self.assertFalse(list(run.directory.glob('*-files.txt')))
        self.assertFalse(list(run.directory.glob('*-exports.json')))

    def test_cli_needs_no_pane_or_live_log_and_leaves_checkpoint_unchanged(self):
        run=self.create(); self.collect(run); self.log.unlink()
        before=run.path.read_bytes()
        args=['coordinator','--resume',str(run.directory),'--list-exports']
        with patch('sys.argv',args), patch('sys.stdout',new=io.StringIO()), \
             patch('subprocess.run',side_effect=AssertionError('No commands')), \
             patch.object(p,'inspect_export',side_effect=AssertionError('No block parsing')):
            self.assertEqual(c.main(),0)
        self.assertEqual(run.path.read_bytes(),before)
        self.assertTrue((run.directory/(run.state['run_id']+'-exports.json')).is_file())

    def test_cli_inventory_refuses_an_active_controller(self):
        run=self.create()
        with run.locked(), patch('sys.argv',['coordinator','--resume',str(run.directory),'--list-exports']), \
             patch('sys.stdout',new=io.StringIO()), patch('sys.stderr',new=io.StringIO()) as errors, \
             patch('subprocess.run',side_effect=AssertionError('No commands')):
            self.assertEqual(c.main(),1)
        self.assertIn('Another controller',errors.getvalue())
        self.assertFalse(list(run.directory.glob('*-files.txt')))

    def test_cli_returns_failure_for_missing_selected_file_without_altering_checkpoint(self):
        run=self.create(); self.collect(run)
        name=run.state['sections']['s00001']['selected']['file']
        (self.exports/name).unlink(); before=run.path.read_bytes()
        args=['coordinator','--resume',str(run.directory),'--list-exports']
        with patch('sys.argv',args), patch('sys.stdout',new=io.StringIO()), \
             patch('subprocess.run',side_effect=AssertionError('No commands')):
            self.assertEqual(c.main(),1)
        report=p.read_json(run.directory/(run.state['run_id']+'-exports.json'))
        self.assertEqual(report['validated_selections'],1)
        self.assertTrue(report['problems'])
        self.assertNotIn(name,Path(report['file_lists']['primary']['path']).read_text().splitlines())
        self.assertEqual(run.path.read_bytes(),before)


if __name__=='__main__':
    unittest.main()
