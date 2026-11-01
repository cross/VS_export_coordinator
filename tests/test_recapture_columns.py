"""Non-server tests for repair input validation and lossless column overlay."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import v12_capture_plan as p
from tools import recapture_columns as repair


def data_for(bounds, columns, dual=False):
    indices=[x | (z<<10) for x,z in columns]
    ids=[1]*len(indices)
    if dual:
        indices.append(indices[0]); ids.append(2)
    return dict(SizeX=bounds['xmax']-bounds['xmin']+1,SizeY=3,
                SizeZ=bounds['zmax']-bounds['zmin']+1,
                OriginalPos=dict(X=bounds['xmin']+p.WORLD_CENTER,
                                 InternalY=bounds['ymin'],Z=bounds['zmin']+p.WORLD_CENTER),
                Indices=indices,BlockIds=ids,DecorIndices=[],DecorIds=[],
                BlockCodes={'1':'game:stone-granite','2':'game:water-still-7'},
                ItemCodes={},BlockEntities={},Entities=['original-entity'])


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.parent=dict(xmin=0,xmax=3,ymin=10,ymax=12,zmin=0,zmax=1,name='parent')
        self.patch=dict(xmin=1,xmax=2,ymin=10,ymax=12,zmin=0,zmax=1)
        self.original=data_for(self.parent,[(0,0),(3,0),(0,1),(3,1)])
        self.data=data_for(self.patch,[(0,0),(1,0),(0,1),(1,1)],dual=True)

    def test_overlay_fills_only_holes_and_keeps_layer_pairs(self):
        source=deepcopy(self.original)
        result=repair.overlay_empty_columns(self.parent,self.original,[(self.patch,self.data)])
        self.assertEqual(self.original,source)
        self.assertEqual(result['Indices'][:4],source['Indices'])
        self.assertEqual(result['BlockIds'][:4],source['BlockIds'])
        self.assertEqual([b for i,b in zip(result['Indices'],result['BlockIds']) if i==1],[1,2])
        self.assertEqual(result['Entities'],source['Entities'])
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/'parent.json'
            file.write_text(json.dumps(result))
            evidence=p.inspect_export(file,self.parent)
            self.assertFalse(evidence['problems'])
            self.assertEqual(evidence['column_presence']['missing_columns'],0)

    def test_existing_column_is_never_overwritten_by_patch(self):
        bounds={**self.patch,'xmin':0,'xmax':1}
        data=data_for(bounds,[(0,0),(1,0),(0,1),(1,1)])
        data['BlockIds']=[2]*4
        result=repair.overlay_empty_columns(self.parent,self.original,[(bounds,data)])
        self.assertEqual([b for i,b in zip(result['Indices'],result['BlockIds']) if i==0],[1])
        self.assertEqual([b for i,b in zip(result['Indices'],result['BlockIds']) if i==1],[2])

    def test_overlapping_or_outside_patches_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'Overlapping'):
            repair.overlay_empty_columns(self.parent,self.original,[(self.patch,self.data)]*2)
        with self.assertRaisesRegex(ValueError,'outside'):
            repair.overlay_empty_columns(self.parent,self.original,[({**self.patch,'xmax':10},self.data)])

    def test_report_hash_and_bounds_are_checked_before_commands(self):
        plan={'plan_hash':'abc','cuboids':[self.parent]}
        report={'plan_hash':'abc','empty_columns':4,'empty_column_rectangles':[
            {'xmin':1,'xmax':2,'zmin':0,'zmax':1,'columns':4,'file':'parent.json'}]}
        requests=repair.repair_requests(plan,report,'0123456789abcdef')
        self.assertEqual(requests['columns'],4)
        self.assertEqual(len(requests['jobs']),1)
        with self.assertRaisesRegex(ValueError,'different plan'):
            repair.repair_requests(plan,{**report,'plan_hash':'other'},'hash')
        report['empty_column_rectangles'][0]['xmax']=5
        with self.assertRaisesRegex(ValueError,'outside'):
            repair.repair_requests(plan,report,'hash')


if __name__=='__main__':
    unittest.main()
