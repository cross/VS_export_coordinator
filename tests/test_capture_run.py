"""Clean-run isolation, fresh files, whole retries, and content seam checks."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import capture_run as runmod
import v12_capture_plan as p
import vintage_story_export_coordinator as coordinator


def plan_for(cores):
    return p.seal_plan(dict(version=p.VERSION,geometry={},geometry_hash=p.digest({}),geometry_version='test',
        world_center=p.WORLD_CENTER,requests=deepcopy(cores),cuboids=deepcopy(cores),raw_footprint=deepcopy(cores),
        requested_y={'ymin':min(b['ymin'] for b in cores),'ymax':max(b['ymax'] for b in cores)},
        partition={'mode':'exact','x_span':1023,'z_span':1023}))


class FakeConsole:
    def __init__(self,export_dir,stale_first=False,partial_first=False,shafts=(),code=1,version='1.22.7',unit_codes=None):
        self.export_dir=export_dir; self.commands=[]; self.counts={}
        self.stale_first,self.partial_first,self.shafts=stale_first,partial_first,set(shafts)
        self.code,self.version=code,version
        self.unit_codes=unit_codes or {}

    def command(self,command,expected,timeout=60):
        self.commands.append(command)
        if '/we select ' in command:
            self.bounds=dict(zip(('xmin','ymin','zmin','xmax','ymax','zmax'),map(int,command.split('/we select ')[1].split())))
        if '/we export ' in command:
            name=command.split()[-1]
            unit='s'+name.split('_s',1)[1].split('_',1)[0]
            section='_section_' in name
            if section:
                self.counts[unit]=self.counts.get(unit,0)+1
            first=section and unit=='s00001' and self.counts[unit]==1
            code=2 if first and (self.stale_first or self.partial_first) else self.code
            code=self.unit_codes.get(unit,code)
            indices=[]; ids=[]
            for z in range(self.bounds['zmin'],self.bounds['zmax']+1):
                for x in range(self.bounds['xmin'],self.bounds['xmax']+1):
                    if first and self.partial_first and (x,z)==(0,0):
                        continue
                    top=50 if (x,z) in self.shafts else 66
                    for y in range(self.bounds['ymin'],min(top,self.bounds['ymax'])+1):
                        indices.append((x-self.bounds['xmin']) | ((z-self.bounds['zmin'])<<10) | ((y-self.bounds['ymin'])<<20))
                        ids.append(code)
            data=dict(GameVersion=self.version,SizeX=max((i&1023 for i in indices),default=0)+1,
                      SizeY=max((i>>20 for i in indices),default=0)+1,
                      SizeZ=max(((i>>10)&1023 for i in indices),default=0)+1,
                      OriginalPos={'X':self.bounds['xmin']+p.WORLD_CENTER,'InternalY':self.bounds['ymin'],'Z':self.bounds['zmin']+p.WORLD_CENTER},
                      Indices=indices,BlockIds=ids,BlockCodes={'1':'game:rock-granite','2':'game:soil-low-none'},
                      DecorIndices=[],DecorIds=[],BlockEntities={},Entities=[])
            (self.export_dir/(name+'.json')).write_text(json.dumps(data))
        return 'Ok, will force load 1 chunk columns; teleported; Chunk in genQ: False, chunkReq in Q: False; Start and end position marked; 100 blocks exported'


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.exports=self.root/'WorldEdit'; self.exports.mkdir()
        self.log=self.root/'server-main.log'; self.log.touch()
        self.index_patch=patch.dict(os.environ,{'VS_CAPTURE_RUN_INDEX':str(self.root/'state'/'runs.json')})
        self.index_patch.start(); self.addCleanup(self.index_patch.stop)
        self.cores=[dict(xmin=0,xmax=1,ymin=65,ymax=67,zmin=0,zmax=1),
                    dict(xmin=2,xmax=3,ymin=65,ymax=67,zmin=0,zmax=1)]
        self.plan=plan_for(self.cores)

    def create(self,name='run',**kwargs):
        return runmod.Run.create(self.plan,self.root/'source-plan.json',self.exports,self.log,'Player',
            directory=self.root/name,coverage_y=66,wait=lambda path,issued:runmod.wait_fresh(path,issued,stable_seconds=0,poll=0),**kwargs)

    def collect(self,run,console=None):
        run.console=console or FakeConsole(self.exports)
        with patch('sys.stdout',new=io.StringIO()):
            return run.collect()

    def test_new_runs_have_distinct_id_filenames_and_progress_even_with_same_plan(self):
        first=self.create('first'); self.assertEqual(self.collect(first),0)
        before={path.name:runmod.digest_file(path) for path in self.exports.glob('*.json')}
        second=self.create('second'); console=FakeConsole(self.exports,code=2)
        self.assertEqual(self.collect(second,console),0)
        self.assertNotEqual(first.state['run_id'],second.state['run_id'])
        self.assertEqual(first.state['plan_hash'],second.state['plan_hash'])
        self.assertNotEqual(first.path.name,second.path.name)
        self.assertTrue(console.commands)
        for name,sha in before.items():
            self.assertEqual(runmod.digest_file(self.exports/name),sha)
        for record in second.state['sections'].values():
            self.assertTrue(record['selected']['file'].startswith(second.state['prefix']))
            self.assertEqual(set(p.read_json(self.exports/record['selected']['file'])['BlockIds']),{2})

    def test_compatible_resume_reuses_only_its_manifest_selected_exports(self):
        run=self.create(); self.assertEqual(self.collect(run),0)
        restored=runmod.Run.resume(run.directory)
        console=FakeConsole(self.exports)
        self.assertEqual(self.collect(restored,console),0)
        self.assertFalse(console.commands)
        self.assertTrue(restored.state['finished'])

    def test_partial_run_resumes_completed_selection_without_recapturing_it(self):
        run=self.create(); run.console=FakeConsole(self.exports)
        with patch('sys.stdout',new=io.StringIO()):
            self.assertTrue(run.capture_section(run.state['units'][0]))
        selected=run.state['sections']['s00001']['selected']['file']
        restored=runmod.Run.resume(run.directory,wait=run.wait)
        console=FakeConsole(self.exports)
        self.assertEqual(self.collect(restored,console),0)
        self.assertNotIn('s00001',console.counts)
        self.assertEqual(restored.state['sections']['s00001']['selected']['file'],selected)

    def test_interrupted_probe_batch_reuses_same_primary_and_completed_same_attempt_probes(self):
        self.plan=plan_for([dict(xmin=0,xmax=33,ymin=65,ymax=67,zmin=0,zmax=1)])
        run=self.create(); console=FakeConsole(self.exports,shafts={(0,0),(32,0)}); run.console=console
        command=console.command
        def interrupt(text,*args,**kwargs):
            if '/we select 32 0 ' in text:
                raise KeyboardInterrupt
            return command(text,*args,**kwargs)
        console.command=interrupt
        with self.assertRaises(KeyboardInterrupt), patch('sys.stdout',new=io.StringIO()):
            run.capture_section(run.state['units'][0])
        candidate=deepcopy(run.state['sections']['s00001']['candidate'])
        self.assertEqual(len(candidate['probes']),1)
        self.assertFalse(run.state['finished'])
        restored=runmod.Run.resume(run.directory,wait=run.wait)
        fresh_console=FakeConsole(self.exports,shafts={(0,0),(32,0)})
        self.assertEqual(self.collect(restored,fresh_console),0)
        record=restored.state['sections']['s00001']
        self.assertEqual(record['selected']['file'],candidate['selected']['file'])
        self.assertEqual(record['probes'][0]['file'],candidate['probes'][0]['file'])
        self.assertEqual(len(record['probes']),2)
        self.assertEqual(len(record['attempts']),1)
        self.assertFalse(fresh_console.counts)

    def test_validation_detects_file_change_between_hash_and_schema_checks(self):
        run=self.create(); self.assertEqual(self.collect(run),0)
        ref=run.state['sections']['s00001']['selected']; path=self.exports/ref['file']
        inspect=p.inspect_export
        def mutate(file,bounds,*args,**kwargs):
            data=p.read_json(file); data['Entities']=['modified during validation']; file.write_text(json.dumps(data))
            return inspect(file,bounds,*args,**kwargs)
        with patch.object(p,'inspect_export',side_effect=mutate):
            with self.assertRaisesRegex(runmod.CaptureError,'changed while'):
                run.read_ref(ref)

    def test_run_and_plan_or_operational_model_mismatch_stops_without_removing_files(self):
        run=self.create(); self.assertEqual(self.collect(run),0)
        files=set(self.exports.iterdir())
        with self.assertRaisesRegex(runmod.CaptureError,'different export_dir'):
            runmod.Run.resume(run.directory,export_dir=self.root/'other')
        state=p.read_json(run.path); state['operational_model']='old-or-unknown'
        runmod.atomic_json(run.path,state)
        with self.assertRaisesRegex(runmod.CompatibilityError,'Operational model'):
            runmod.Run.resume(run.directory)
        self.assertEqual(set(self.exports.iterdir()),files)

    def test_changed_settings_are_not_implicitly_accepted_on_resume(self):
        run=self.create(); state=p.read_json(run.path); state['settings']['coverage_y']=65
        runmod.atomic_json(run.path,state)
        with self.assertRaisesRegex(runmod.CaptureError,'settings changed'):
            runmod.Run.resume(run.directory)

    def test_create_refuses_to_replace_existing_run_directory(self):
        self.create()
        with self.assertRaisesRegex(runmod.CaptureError,'already exists'):
            self.create()

    def test_run_lock_refuses_second_writer_and_reloads_latest_checkpoint(self):
        run=self.create(); other=runmod.Run.resume(run.directory)
        with run.locked():
            with self.assertRaisesRegex(runmod.CaptureError,'Another controller'):
                with other.locked():
                    self.fail('A second writer acquired the run')
            run.state['checkpoint_marker']='newer'
            run.save()
        with other.locked():
            self.assertEqual(other.state['checkpoint_marker'],'newer')

    def test_existing_output_name_is_refused_without_archiving_or_sending_commands(self):
        run=self.create(); console=FakeConsole(self.exports); run.console=console
        name=run.state['prefix']+'_s00001_section_'+'0'*32+'.json'
        path=self.exports/name; path.write_text('old data')
        with patch.object(runmod.uuid,'uuid4',return_value=SimpleNamespace(hex='0'*32)), patch('sys.stdout',new=io.StringIO()):
            self.assertFalse(run.capture_section(run.state['units'][0]))
        self.assertFalse(console.commands)
        self.assertEqual(path.read_text(),'old data')
        self.assertIsNone(run.state['sections']['s00001']['selected'])

    def test_file_timestamp_before_command_is_rejected(self):
        path=self.exports/'stale.json'; path.write_text('{}')
        os.utime(path,ns=(1,1))
        with self.assertRaisesRegex(runmod.CaptureError,'precede'):
            runmod.wait_fresh(path,10,stable_seconds=0,poll=0)

    def test_loading_precedes_each_selection_and_export(self):
        run=self.create(); console=FakeConsole(self.exports)
        self.assertEqual(self.collect(run,console),0)
        for index,command in enumerate(console.commands):
            if '/we export ' in command:
                self.assertIn('/chunk forceload ',console.commands[index-4])
                self.assertIn('/tp ',console.commands[index-3])
                self.assertIn('/executeas Player /chunk cit',console.commands[index-2])
                self.assertIn('/we select ',console.commands[index-1])

    def test_partial_export_retry_replaces_present_records_as_well_as_missing_columns(self):
        run=self.create(); console=FakeConsole(self.exports,partial_first=True)
        self.assertEqual(self.collect(run,console),0)
        record=run.state['sections']['s00001']
        self.assertEqual(len(record['attempts']),2)
        self.assertEqual(record['attempts'][0]['status'],'failed')
        data=p.read_json(self.exports/record['selected']['file'])
        self.assertEqual(set(data['BlockIds']),{1})
        self.assertTrue(any(set(p.read_json(path).get('BlockIds',[]))=={2} for path in self.exports.glob('*_section_*.json')))

    def test_overlap_mismatch_triggers_fresh_whole_section_and_gates_finished(self):
        run=self.create(); console=FakeConsole(self.exports,stale_first=True); run.console=console
        with patch('sys.stdout',new=io.StringIO()):
            for unit in run.state['units']:
                self.assertTrue(run.capture_section(unit))
            report=run.audit()
        self.assertFalse(report['finished'])
        self.assertFalse(run.state['overlap_complete'])
        self.assertTrue(any(pair['status']=='mismatch' for pair in report['overlap_checks']))
        self.assertEqual(self.collect(run,console),0)
        self.assertEqual(len(run.state['sections']['s00001']['attempts']),2)
        self.assertTrue(run.state['finished'])

    def test_genuine_shaft_has_fresh_probe_evidence_without_splicing_records(self):
        run=self.create(); console=FakeConsole(self.exports,shafts={(0,0)})
        self.assertEqual(self.collect(run,console),0)
        record=run.state['sections']['s00001']
        self.assertEqual(record['coverage']['corroborated_columns'],1)
        self.assertEqual(len(record['probes']),1)
        data=p.read_json(self.exports/record['selected']['file'])
        self.assertFalse(any((index&1023)==0 and ((index>>10)&1023)==0 for index in data['Indices']))
        self.assertTrue(run.state['finished'])
        self.assertFalse(run.state['full_3d_coverage_complete'])

    def test_requested_upper_ceiling_hits_remain_visible(self):
        self.plan=plan_for([{**core,'ymax':66} for core in self.cores])
        run=self.create(); self.assertEqual(self.collect(run),0)
        report=p.read_json(run.directory/(run.state['run_id']+'-audit.json'))
        self.assertTrue(report['warnings'])
        self.assertTrue(all(record['coverage']['ceiling_records']>0 for record in run.state['sections'].values()))

    def test_modified_selected_file_revokes_finished_and_requires_new_attempt(self):
        run=self.create(); self.assertEqual(self.collect(run),0)
        record=run.state['sections']['s00001']; old=record['selected']['file']
        path=self.exports/old; data=p.read_json(path); data['Entities']=['changed']; path.write_text(json.dumps(data))
        with patch('sys.stdout',new=io.StringIO()):
            report=run.audit()
        self.assertFalse(report['finished']); self.assertFalse(run.state['files_verified'])
        self.assertEqual(self.collect(run),0)
        self.assertNotEqual(record['selected']['file'],old)

    def test_game_version_change_requires_review_instead_of_reusing_or_deleting(self):
        run=self.create(); run.console=FakeConsole(self.exports)
        with patch('sys.stdout',new=io.StringIO()):
            self.assertTrue(run.capture_section(run.state['units'][0]))
        previous=run.state['sections']['s00001']['selected']['file']
        run.console=FakeConsole(self.exports,version='1.23.0')
        with self.assertRaisesRegex(runmod.CompatibilityError,'Game version changed'), patch('sys.stdout',new=io.StringIO()):
            run.capture_section(run.state['units'][1])
        self.assertTrue((self.exports/previous).is_file())
        self.assertFalse(run.state['finished'])

    def test_overlap_padding_is_split_before_packed_limits_are_exceeded(self):
        plan=plan_for([dict(xmin=0,xmax=1022,ymin=65,ymax=67,zmin=0,zmax=1022)])
        units=runmod.run_units(plan,3)
        self.assertGreater(len(units),1)
        p.verify_disjoint([u['core'] for u in units],'test cores')
        self.assertFalse(p.difference(plan['requests'],[u['core'] for u in units]))
        self.assertTrue(all(u['capture']['xmax']-u['capture']['xmin']+1<=1023 and
                            u['capture']['zmax']-u['capture']['zmin']+1<=1023 and p.volume(u['capture'])<=p.MAX_VOLUME for u in units))

    def test_overlap_fingerprints_compare_codes_and_preserve_duplicate_layers_and_decor_faces(self):
        bounds=dict(xmin=0,xmax=0,ymin=65,ymax=65,zmin=0,zmax=0)
        a={'Indices':[0,0],'BlockIds':[1,2],'BlockCodes':{'1':'game:seaweed','2':'game:water-still-7','3':'game:vine'},'DecorIndices':[0],'DecorIds':[(5<<24)|3]}
        b={'Indices':[0,0],'BlockIds':[8,7],'BlockCodes':{'7':'game:seaweed','8':'game:water-still-7','9':'game:vine'},'DecorIndices':[0],'DecorIds':[(5<<24)|9]}
        self.assertEqual(runmod.fingerprint(a,bounds,bounds),runmod.fingerprint(b,bounds,bounds))
        b['Indices'].pop(); b['BlockIds'].pop()
        self.assertNotEqual(runmod.fingerprint(a,bounds,bounds)['sha256'],runmod.fingerprint(b,bounds,bounds)['sha256'])
        b=deepcopy(a); b['DecorIds']=[(6<<24)|3]
        self.assertNotEqual(runmod.fingerprint(a,bounds,bounds)['sha256'],runmod.fingerprint(b,bounds,bounds)['sha256'])

    def test_surface_record_does_not_hide_missing_lower_vertical_chunk_stratum(self):
        bounds=dict(xmin=0,xmax=1,ymin=65,ymax=180,zmin=0,zmax=0)
        data={'Indices':[(109-65)<<20,1 | ((109-65)<<20),1],
              'BlockIds':[1,1,1],'BlockCodes':{'1':'game:rock-granite'}}
        self.assertEqual(runmod.missing_anchor_columns(data,bounds,109),set())
        self.assertEqual(runmod.columns_needing_confirmation(data,bounds,109),{(0,0)})

    def test_batched_overlap_comparison_matches_independent_strip_fingerprints(self):
        bounds=dict(xmin=0,xmax=2,ymin=65,ymax=65,zmin=0,zmax=1)
        data={'Indices':[0,0,1,2,1024,1025,1026],'BlockIds':[1,2,1,1,1,1,1],
              'BlockCodes':{'1':'game:rock-granite','2':'game:water-still-7'},'DecorIndices':[],'DecorIds':[]}
        pairs=[{'id':'xstrip','bounds':{**bounds,'xmin':1}},
               {'id':'zstrip','bounds':{**bounds,'zmin':1}}]
        result=runmod.section_fingerprints(data,bounds,pairs)
        for pair in pairs:
            self.assertEqual(result[pair['id']],runmod.fingerprint(data,bounds,pair['bounds']))

    def test_persistent_seam_difference_stops_at_retry_limit_without_finishing(self):
        run=self.create(); console=FakeConsole(self.exports,unit_codes={'s00001':2,'s00002':1})
        with self.assertRaisesRegex(runmod.CaptureError,'Retry limit'), patch('sys.stdout',new=io.StringIO()):
            self.collect(run,console)
        self.assertFalse(run.state['finished'])
        self.assertTrue(run.state['seam_failures'])
        self.assertGreater(run.state['seam_failures'][0]['difference']['positions'],0)
        self.assertTrue(all(len(record['attempts'])<=3 for record in run.state['sections'].values()))

    def test_final_audit_recomputes_overlap_fingerprints_instead_of_trusting_cache(self):
        run=self.create(); self.assertEqual(self.collect(run),0)
        pair=run.pairs[0]
        run.state['sections'][pair['left']]['fingerprints'][pair['id']]['sha256']='incorrect cache'
        with patch('sys.stdout',new=io.StringIO()):
            report=run.audit()
        self.assertTrue(report['finished'])
        self.assertTrue(report['overlap_complete'])

    def test_resume_requires_a_current_run_manifest(self):
        directory=self.root/'not-a-run'; directory.mkdir()
        with self.assertRaisesRegex(runmod.CompatibilityError,'Missing run.json'):
            runmod.Run.resume(directory)

    def test_prepare_only_creates_preview_and_run_without_a_pane_or_server_commands(self):
        source=self.root/'source-plan.json'; runmod.atomic_json(source,self.plan)
        directory=self.root/'prepared'
        args=['coordinator','--plan',str(source),'--run-dir',str(directory),'--prepare-only',
              '--player','Player','--export-dir',str(self.exports),'--server-log',str(self.log),'--coverage-y','66']
        with patch('sys.argv',args), patch('subprocess.run',side_effect=AssertionError('No commands')), patch('builtins.input',side_effect=AssertionError('No prompts')), patch('sys.stdout',new=io.StringIO()):
            self.assertEqual(coordinator.main(),0)
        run=runmod.Run.resume(directory)
        self.assertTrue((directory/(run.state['run_id']+'-preview.svg')).is_file())
        self.assertFalse(run.state['finished'])
        self.assertFalse(run.state['sections'])
        self.assertFalse(list(self.exports.iterdir()))

    def test_isolated_report_uses_its_run_without_sending_commands(self):
        run=self.create(); self.assertEqual(self.collect(run),0)
        args=['coordinator','--resume',str(run.directory),'--report-coverage']
        with patch('sys.argv',args), patch('subprocess.run',side_effect=AssertionError('No commands')), patch('sys.stdout',new=io.StringIO()):
            self.assertEqual(coordinator.main(),0)
        self.assertTrue(p.read_json(run.path)['finished'])


if __name__=='__main__':
    unittest.main()
