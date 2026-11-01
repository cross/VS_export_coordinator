"""Automatic deeper rescans with a simulated server; no live commands."""
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import capture_rescan as rescan
import v12_capture_plan as p
import vintage_story_export_coordinator as coordinator
from tools import recapture_columns as repair


def data_for(bounds, indices):
    return dict(SizeX=bounds['xmax']-bounds['xmin']+1,
                SizeY=bounds['ymax']-bounds['ymin']+1,
                SizeZ=bounds['zmax']-bounds['zmin']+1,
                OriginalPos=dict(X=bounds['xmin']+p.WORLD_CENTER,
                                 Z=bounds['zmin']+p.WORLD_CENTER, InternalY=bounds['ymin']),
                Indices=indices, BlockIds=[1]*len(indices), BlockCodes={'1':'game:rock-granite'},
                DecorIndices=[], DecorIds=[], BlockEntities={}, Entities=['original'])


class FakeConsole:
    def __init__(self, root, in_band=False, incomplete=False):
        self.root, self.in_band, self.incomplete = root, in_band, incomplete
        self.commands = []

    def command(self, command, expected, timeout=60):
        self.commands.append(command)
        if '/we select ' in command:
            values = list(map(int,command.split('/we select ')[1].split()))
            self.bounds = dict(zip(('xmin','ymin','zmin','xmax','ymax','zmax'),values))
        if '/we export ' in command:
            indices=[x | (z<<10) for z in range(32) for x in range(32)]
            if self.incomplete:
                indices.pop()
            if self.in_band:
                indices.extend([1 | (4<<20), 1 | (4<<20)])
            data=data_for(self.bounds,indices)
            if self.in_band:
                data['BlockIds'][-1]=2
                data['BlockCodes']['2']='game:water-still-7'
            (self.root/(command.split()[-1]+'.json')).write_text(json.dumps(data))
        return 'Ok, will force load 1 chunk columns; teleported; Chunk in genQ: False, chunkReq in Q: False'


class RescanTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.bounds=dict(xmin=0,xmax=2,ymin=3,ymax=5,zmin=0,zmax=0)
        self.path=self.root/'parent.json'
        self.original=data_for(self.bounds,[0,2])
        self.path.write_text(json.dumps(self.original))
        self.registry={}

    def run_rescan(self, console=None, registry=None):
        console=console or FakeConsole(self.root)
        with patch('sys.stdout',new=io.StringIO()):
            result=rescan.rescan_export(self.path,self.bounds,prefix='test',player='Player',
                console=console,export_dir=self.root,registry=self.registry if registry is None else registry,
                save=lambda:None,stable=lambda path:None,archive=coordinator.archive_invalid)
        return result,console

    def plan(self):
        return dict(plan_hash='test',geometry_hash='geometry',world_center=p.WORLD_CENTER,
                    requests=[self.bounds],cuboids=[{**self.bounds,'name':'parent'}])

    def test_empty_band_is_corroborated_and_audited_without_inventing_blocks(self):
        before=self.path.read_bytes()
        result,console=self.run_rescan()
        self.assertFalse(result['problems'])
        self.assertEqual(result['column_presence']['missing_columns'],1)
        self.assertEqual(result['column_presence']['empty_columns_confirmed_by_rescan'],1)
        self.assertEqual(result['column_presence']['unverified_columns'],0)
        actual=p.read_json(self.path)
        self.assertEqual(actual['Indices'],self.original['Indices'])
        self.assertEqual(actual['Entities'],self.original['Entities'])
        self.assertEqual(next(self.root.glob('*.before-rescan-*.json')).read_bytes(),before)
        self.assertEqual(console.commands[0],'/executeas Player /chunk forceload 0 0 31 31')
        self.assertEqual(console.commands[2],'/executeas Player /chunk cit')
        self.assertIn('/we select 0 0 0 31 5 31',console.commands[3])
        c=coordinator.Cuboid(1,**self.bounds,name='parent')
        self.assertFalse(coordinator.validate_export(self.path,c))
        audit=p.audit_coverage(self.plan(),{},self.root)
        self.assertTrue(audit['complete'])
        self.assertEqual(audit['column_presence']['columns_without_direct_records'],1)
        self.assertEqual(audit['column_presence']['empty_columns_confirmed_by_rescan'],1)
        self.assertEqual(audit['column_presence']['missing_columns'],0)

    def test_recovered_in_band_records_fill_only_missing_column_and_preserve_layers(self):
        result,_=self.run_rescan(FakeConsole(self.root,in_band=True))
        self.assertEqual(result['column_presence']['missing_columns'],0)
        data=p.read_json(self.path)
        self.assertEqual(data['Indices'][:2],[0,2])
        self.assertEqual(data['Indices'][2:],[1 | (1<<20)]*2)
        self.assertEqual(data['BlockIds'][2:],[1,2])
        self.assertEqual(data['Entities'],['original'])
        self.assertEqual(self.registry['sections']['parent']['restored_columns'],1)

    def test_missing_or_changed_probe_revokes_confirmation_and_can_be_recaptured(self):
        self.run_rescan()
        probe=next(iter(self.registry['probes'].values()))
        file=self.root/probe['file']
        file.unlink()
        audit=p.audit_coverage(self.plan(),{},self.root)
        self.assertFalse(audit['complete'])
        self.assertEqual(audit['column_presence']['missing_columns'],1)
        self.assertTrue(audit['files'][0]['rescan_problems'])
        result,console=self.run_rescan()
        self.assertEqual(result['column_presence']['unverified_columns'],0)
        self.assertEqual(len(console.commands),5)
        data=p.read_json(file); data['Entities'].append('changed')
        file.write_text(json.dumps(data))
        self.assertEqual(p.inspect_export(self.path,self.bounds)['column_presence']['unverified_columns'],1)

    def test_resume_and_nearby_holes_reuse_one_checked_probe(self):
        _,console=self.run_rescan()
        first=len(console.commands)
        self.run_rescan(console)
        self.assertEqual(len(console.commands),first)
        # Another selection in the same chunk uses the cached deeper export.
        self.path=self.root/'neighbor.json'
        self.bounds={**self.bounds,'xmin':4,'xmax':4}
        self.path.write_text(json.dumps(data_for(self.bounds,[])))
        result,_=self.run_rescan(console)
        self.assertFalse(result['problems'])
        self.assertEqual(result['column_presence']['empty_columns_confirmed_by_rescan'],1)
        self.assertEqual(len(console.commands),first)

    def test_deeper_export_with_empty_column_cannot_confirm_loading(self):
        before=self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'still lacks'):
            self.run_rescan(FakeConsole(self.root,incomplete=True))
        self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse(self.registry['probes'])

    def test_missing_load_acknowledgement_cannot_confirm_columns(self):
        self.run_rescan()
        data=p.read_json(self.path)
        data['ColumnRescanEvidence']['probes'][0]['load_check']['queues_clear']=False
        self.path.write_text(json.dumps(data))
        self.assertEqual(p.inspect_export(self.path,self.bounds)['column_presence']['unverified_columns'],1)

    def test_console_requires_command_acknowledgement(self):
        log=self.root/'server.log'; log.write_text('unrelated old messages')
        sender_calls=[]
        console=rescan.Console('tmux','%1',log,self.root/'commands',
            lambda *args:sender_calls.append(args))
        with patch.object(rescan.time,'monotonic',side_effect=[0,61]):
            with self.assertRaisesRegex(TimeoutError,'acknowledgement'):
                console.command('/executeas Player /chunk cit',r'queues clear')
        self.assertEqual(len(sender_calls),1)

    def test_source_change_during_probe_cannot_overwrite_export(self):
        console=FakeConsole(self.root)
        command=console.command
        changed=json.dumps({**self.original,'Entities':['changed concurrently']})
        def mutate(*args,**kwargs):
            result=command(*args,**kwargs)
            if '/we export ' in args[0]:
                self.path.write_text(changed)
            return result
        console.command=mutate
        with self.assertRaisesRegex(ValueError,'changed during rescan'):
            self.run_rescan(console)
        self.assertEqual(self.path.read_text(),changed)

    def test_negative_coordinates_group_by_chunk_and_enforce_limits(self):
        b={**self.bounds,'xmin':-33,'xmax':-31,'zmin':-1,'zmax':0}
        chunks=list(rescan.chunk_bounds([b,b],0,180))
        self.assertEqual(len(chunks),4)
        self.assertEqual(chunks[0]['xmin'],-64)
        self.assertEqual(chunks[0]['zmin'],-32)
        with self.assertRaisesRegex(ValueError,'limits'):
            list(rescan.chunk_bounds([b],0,9000))

    def test_coordinator_rescans_existing_holes_before_retrying_large_selection(self):
        c=coordinator.Cuboid(1,**self.bounds,name='parent')
        log=self.root/'server-main.log'; log.write_text('')
        state=dict(sections={},signature={'prefix':'test'},rescan_settings={'server_log':str(log),'ymin':0})
        fake=FakeConsole(self.root)
        with patch.object(rescan,'Console',return_value=fake), patch.object(coordinator,'stable_file',return_value=True), patch('sys.stdout',new=io.StringIO()):
            pieces,gaps=coordinator.recover_automatically(c,0,'Player','%1',self.root,self.root,
                state,self.root/'test-progress.json',coordinator.RunStatus([c],state))
        self.assertEqual(pieces,[(c,self.path)])
        self.assertFalse(gaps)
        self.assertEqual(state['sections']['parent']['status'],'complete')
        self.assertEqual(len(fake.commands),5)
        self.assertFalse(list(self.root.glob('*.previous-invalid*.json')))

    def test_repair_utility_shares_probe_and_attaches_auditable_evidence(self):
        console=FakeConsole(self.root)
        job={**self.bounds,'xmin':1,'xmax':1}
        results={}
        args=SimpleNamespace(export_dir=self.root,player='Player')
        with patch.object(repair,'wait_stable',return_value=None), patch('sys.stdout',new=io.StringIO()):
            observed=repair.probe_capture_floor(job,console,args,self.root,results)
            confirmed=repair.corroborate_parent(self.path,self.bounds,console,args,self.root,results)
        self.assertEqual(len(observed),1)
        self.assertEqual(observed[0]['maximum_record_y'],0)
        self.assertEqual(confirmed['column_presence']['unverified_columns'],0)
        self.assertEqual(len(console.commands),5)
        self.assertTrue(p.read_json(self.path)['ColumnRescanEvidence'])


if __name__=='__main__':
    unittest.main()
