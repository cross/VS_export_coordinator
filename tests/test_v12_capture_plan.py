"""Geometry, coverage, and offline coordinator regression tests (no server)."""
from copy import deepcopy
from decimal import Decimal, localcontext
import io
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

import v12_capture_plan as p
import vintage_story_export_coordinator as coordinator


def voxels(boxes):
    return {(x, y, z) for b in boxes for x in range(b['xmin'], b['xmax']+1)
            for y in range(b['ymin'], b['ymax']+1) for z in range(b['zmin'], b['zmax']+1)}


def export_data(b, size_y=None, ceiling=False):
    sx, sy, sz = [b[a+'max'] - b[a+'min'] + 1 for a in p.AXES]
    sy = size_y or sy
    indices = [0]
    if ceiling and sy > 1:
        indices.append((sy-1) << 20)
    return dict(SizeX=sx, SizeY=sy, SizeZ=sz,
                OriginalPos=dict(X=b['xmin']+p.WORLD_CENTER, InternalY=b['ymin'], Z=b['zmin']+p.WORLD_CENTER),
                Indices=indices, BlockIds=[1]*len(indices), BlockCodes={'1': 'game:stone-granite'},
                DecorIndices=[], DecorIds=[], BlockEntities={})


class RegionTests(unittest.TestCase):
    def test_box_difference_against_independent_voxel_oracle(self):
        rng = random.Random(20261101)
        for _ in range(120):
            def random_box():
                return {k: v for a in p.AXES for k, v in zip((a+'min', a+'max'), sorted(rng.sample(range(-3, 5), 2)))}
            a, b = random_box(), random_box()
            parts = p.subtract(a, b)
            self.assertEqual(voxels(parts), voxels([a]) - voxels([b]))
            self.assertEqual(sum(p.volume(part) for part in parts), len(voxels(parts)))

    def test_multiple_subtractions_and_y_gap(self):
        a = dict(xmin=0, xmax=3, ymin=0, ymax=5, zmin=0, zmax=3)
        lower = {**a, 'ymax': 1}
        upper = {**a, 'ymin': 4}
        self.assertEqual(p.difference([a], [lower, upper]), [{**a, 'ymin': 2, 'ymax': 3}])

    def test_interval_union_preserves_disconnected_intervals(self):
        self.assertEqual(p.union_intervals([(8, 10), (0, 1), (2, 4), (9, 11)]), [(0, 4), (8, 11)])

    def test_limits_wide_deep_tall_volume(self):
        b = dict(xmin=-2000, xmax=120, ymin=0, ymax=1300, zmin=0, zmax=1100)
        tasks = p.limited_boxes(b, 1023)
        self.assertEqual(sum(p.volume(t) for t in tasks), p.volume(b))
        p.verify_disjoint(tasks, 'test units')
        for t in tasks:
            self.assertLessEqual(p.volume(t), p.MAX_VOLUME)
            self.assertTrue(all(t[a+'max'] - t[a+'min'] + 1 <= 1023 for a in p.AXES))

    def test_partition_exact_and_banded_small_oracle(self):
        regions = [dict(xmin=-4, xmax=2, ymin=0, ymax=2, zmin=-2, zmax=0),
                   dict(xmin=0, xmax=5, ymin=1, ymax=4, zmin=-1, zmax=3)]
        for mode in ('exact', 'banded'):
            requests, tasks = p.partition(regions, 2, mode)
            p.verify_disjoint(requests, 'requests')
            p.verify_disjoint(tasks, 'tasks')
            self.assertEqual(voxels(requests), voxels(regions))
            self.assertTrue(voxels(regions) <= voxels(tasks))
            if mode == 'exact':
                self.assertEqual(voxels(requests), voxels(tasks))


class PlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.geometry = p.DEFAULT_GEOMETRY
        cls.exact = p.create_plan(65, 180, cls.geometry, mode='exact')
        cls.banded = p.create_plan(65, 180, cls.geometry, mode='banded')

    def test_mouth_anchors_endpoints_and_buffer(self):
        raw, buffered = p.footprint(p.read_shores(self.geometry))
        self.assertEqual(raw[59375], [(-34500, -34281)])
        self.assertEqual(raw[62360], [(-33576, -32680)])
        self.assertEqual((min(buffered), max(buffered)), (59275, 62460))
        self.assertEqual(buffered[59275], [(-34600, -34181)])
        self.assertEqual(buffered[62460], [(-33676, -32580)])
        for z in (59275, 59375, 59650, 59749, 59850, 61994, 61995, 62360, 62460):
            expected = p.union_intervals([(a-100, b+100) for zz, intervals in raw.items()
                                          if abs(zz-z) <= 100 for a, b in intervals])
            self.assertEqual(buffered[z], expected)

    def test_certified_bezier_enclosures_against_decimal_reference(self):
        with localcontext() as ctx:
            ctx.prec = 70
            for shore in p.read_shores(self.geometry).values():
                segments = p.shore_segments(shore)
                for controls, _, _ in segments:
                    if len(controls) != 4:
                        continue
                    z = int((controls[0][1] + controls[-1][1]) / 2)
                    lo, hi = p.shore_interval(segments, z)
                    pts = [(Decimal(v.numerator)/Decimal(v.denominator), Decimal(w.numerator)/Decimal(w.denominator))
                           for v, w in controls]
                    def cubic(axis, t):
                        u = 1-t
                        return pts[0][axis]*u**3 + 3*pts[1][axis]*u*u*t + 3*pts[2][axis]*u*t*t + pts[3][axis]*t**3
                    a, b = Decimal(0), Decimal(1)
                    for _ in range(200):
                        t = (a+b)/2
                        if cubic(1, t) < z:
                            a = t
                        else:
                            b = t
                    x = cubic(0, (a+b)/2)
                    # Decimal root inversion is approximate even when the
                    # rational implementation finds an exact root (e.g. 1/2).
                    tolerance = Decimal('1e-50')
                    self.assertLessEqual(Decimal(lo.numerator)/Decimal(lo.denominator), x + tolerance)
                    self.assertGreaterEqual(Decimal(hi.numerator)/Decimal(hi.denominator), x - tolerance)

    def test_exact_and_banded_cover_identical_requests(self):
        self.assertEqual(self.exact['requests'], self.banded['requests'])
        self.assertFalse(p.difference(self.exact['requests'], self.banded['cuboids']))
        self.assertEqual(sum(p.volume(b) for b in self.exact['cuboids']), sum(p.volume(b) for b in self.exact['requests']))
        self.assertEqual(len(self.banded['cuboids']), 160)

    def test_smaller_units_preserve_the_previous_exported_footprint(self):
        wide = p.create_plan(65, 180, self.geometry, x_span=1023)
        self.assertEqual(self.banded['requests'], wide['requests'])
        self.assertFalse(p.difference(wide['cuboids'], self.banded['cuboids']))
        self.assertFalse(p.difference(self.banded['cuboids'], wide['cuboids']))
        self.assertEqual(self.banded['partition']['x_span'], 450)
        for task in self.banded['cuboids']:
            self.assertLessEqual(task['xmax'] - task['xmin'] + 1, 450)
            self.assertLessEqual(task['zmax'] - task['zmin'] + 1, 50)
            self.assertLessEqual(p.volume(task), 2_610_000)

    def test_older_saved_plan_without_operational_x_cap_still_loads(self):
        old = p.create_plan(65, 180, self.geometry, x_span=1023)
        del old['partition']['x_span']
        old['plan_hash'] = p.digest({k: v for k, v in old.items() if k != 'plan_hash'})
        p.verify_plan(old)
        self.assertEqual(len(old['cuboids']), 88)

    def test_operational_caps_are_checked_even_below_hard_limits(self):
        modified = deepcopy(self.banded)
        modified['partition']['x_span'] = 100
        modified['plan_hash'] = p.digest({k: v for k, v in modified.items() if k != 'plan_hash'})
        with self.assertRaisesRegex(ValueError, 'saved X/Z span caps'):
            p.verify_plan(modified)
        b = dict(xmin=-1000, xmax=1000, ymin=65, ymax=180, zmin=0, zmax=49)
        parts = p.limited_boxes(b, 25, 128)
        self.assertEqual(sum(p.volume(t) for t in parts), p.volume(b))
        self.assertTrue(all(t['xmax']-t['xmin']+1 <= 128 for t in parts))
        for cap in (0, 1024):
            with self.assertRaises(ValueError):
                p.create_plan(65, 180, self.geometry, x_span=cap)

    def test_southern_x_split_at_planning_time(self):
        requests = p.rows_for_boxes(self.exact['requests'])
        self.assertGreater(max(hi-lo+1 for intervals in requests.values() for lo, hi in intervals), 1023)
        for task in self.banded['cuboids']:
            self.assertLessEqual(task['xmax'] - task['xmin'] + 1, 1023)

    def test_bad_shore_fails_instead_of_sampling_ambiguity(self):
        with self.assertRaises(ValueError):
            p.shore_segments([('M', [(0, 0)]), ('C', [(1, 3), (2, 1), (3, 4)])])

    def test_plan_hash_and_immutability(self):
        modified = deepcopy(self.exact)
        modified['cuboids'][0]['xmax'] += 1
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            p.verify_plan(modified)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'plan.json'
            p.write_plan(self.exact, path)
            self.assertEqual(p.load_plan(path), self.exact)
            p.write_plan(self.exact, path)  # Identical regeneration is safe.
            with self.assertRaisesRegex(ValueError, 'Refusing'):
                p.write_plan(self.banded, path)

    def test_default_output_is_one_stable_directory(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(p, 'HERE', Path(tmp)):
            expected = Path(tmp) / 'capture-plans' / 'coastal-connection' / 'plan.json'
            self.assertEqual(p.write_plan(self.banded), expected)
            self.assertEqual(p.write_plan(self.banded), expected)
            with self.assertRaisesRegex(ValueError, 'Refusing'):
                p.write_plan(self.exact)
            self.assertEqual(list(expected.parent.parent.iterdir()), [expected.parent])
            self.assertEqual(p.load_plan(expected), self.banded)

    def test_height_extension_is_separate_nonoverlapping_and_reuses_names(self):
        a = self.banded['cuboids'][0]
        region = {**p.box(a), 'ymin': 175, 'ymax': 220}
        extended = p.extend_plan(self.banded, [region])
        self.assertNotEqual(extended['prefix'], self.banded['prefix'])
        self.assertEqual(extended['cuboids'][:len(self.banded['cuboids'])], self.banded['cuboids'])
        self.assertEqual(extended['parent_plan_hash'], self.banded['plan_hash'])
        self.assertFalse(p.difference([region], extended['cuboids']))
        self.assertEqual(self.banded['requested_y'], dict(ymin=65, ymax=180))
        self.assertEqual(extended['partition']['x_span'], 450)
        self.assertTrue(all(b['xmax']-b['xmin']+1 <= 450 for b in extended['cuboids']))

    def test_freshwater_includes_entire_intersecting_body_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'lakes.json'
            path.write_text(json.dumps({'bodies': [
                {'id': 'touching', 'rows': [[0, 0, 2], [1, -10, 15], [2, -20, 20]]},
                {'id': 'distant', 'rows': [[15, 100, 120]]}]}))
            requests, records = p.supplemental_lakes(path, {0: [(2, 3)]}, 65, 180)
            self.assertEqual([b['selected'] for b in records], [True, False])
            self.assertEqual(p.rows_for_boxes(requests)[2], [(-20, 20)])

    def test_freshwater_extension_preserves_old_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'lakes.json'
            path.write_text(json.dumps({'bodies': [{'id': 'whole-lake', 'rows': [[60000, -36000, -33000]]}]}))
            extended = p.extend_plan(self.banded, [], path)
            self.assertEqual(extended['cuboids'][:len(self.banded['cuboids'])], self.banded['cuboids'])
            lake = dict(xmin=-36000, xmax=-33000, ymin=65, ymax=180, zmin=60000, zmax=60000)
            self.assertFalse(p.difference([lake], extended['requests']))
            self.assertTrue(extended['freshwater_bodies'][0]['selected'])
            self.assertTrue(all(b['xmax']-b['xmin']+1 <= 450 for b in extended['cuboids']))

    def test_full_audit_short_y_corruption_and_offline_coordinator(self):
        plan = self.banded
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = p.write_plan(plan, directory / 'plan.json')
            for task in plan['cuboids']:
                (directory / (task['name'] + '.json')).write_text(json.dumps(export_data(task)))
            full = p.audit_coverage(plan, {}, directory)
            self.assertTrue(full['complete'])
            self.assertEqual(full['verified_envelope_blocks'], full['requested_blocks'])
            task = plan['cuboids'][0]
            target = directory / (task['name'] + '.json')
            target.write_text(json.dumps(export_data(task, size_y=100)))
            partial = p.audit_coverage(plan, {}, directory)
            self.assertFalse(partial['complete'])
            self.assertTrue(all(g['ymin'] == 165 and g['ymax'] == 180 for g in partial['gaps']))
            lost = sum(p.volume(o) for r in plan['requests'] if (o := p.intersection(r, {**p.box(task), 'ymin': 165})))
            self.assertEqual(full['requested_blocks']-partial['verified_envelope_blocks'], lost)
            target.write_text('{broken')
            corrupt = p.audit_coverage(plan, {}, directory)
            self.assertFalse(corrupt['complete'])
            args = ['coordinator', '--plan', str(path), '--export-dir', str(directory), '--report-coverage']
            with patch('sys.argv', args), patch('subprocess.run', side_effect=AssertionError('No commands allowed')), patch('builtins.input', side_effect=AssertionError('No prompts allowed')), patch('sys.stdout', new=io.StringIO()):
                with patch.object(coordinator, 'route_corridor_slices', side_effect=AssertionError('Saved geometry only')):
                    self.assertEqual(coordinator.main(), 1)

    def test_coordinator_reuses_existing_exports_and_resumes_without_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = p.write_plan(self.banded, directory / 'plan.json')
            sentinel = directory / 'channel_capture-progress.json'
            sentinel.write_text('legacy run must stay untouched')
            for task in self.banded['cuboids']:
                (directory / (task['name']+'.json')).write_text(json.dumps(export_data(task)))
            args = ['coordinator', '--plan', str(path), '--target-pane', '%9', '--export-dir', str(directory)]
            for _ in range(2):
                with patch('sys.argv', args), patch('subprocess.run', side_effect=AssertionError('No commands allowed')), patch('builtins.input', return_value=''), patch('sys.stdout', new=io.StringIO()):
                    self.assertEqual(coordinator.main(), 0)
            state = p.read_json(directory / (self.banded['prefix']+'-progress.json'))
            self.assertTrue(state['finished'])
            self.assertEqual(state['signature']['capture_plan_hash'], self.banded['plan_hash'])
            self.assertEqual(sentinel.read_text(), 'legacy run must stay untouched')


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'piece.json'
        self.b = dict(xmin=-5, xmax=4, ymin=65, ymax=180, zmin=4, zmax=13)

    def inspect(self, data):
        self.path.write_text(json.dumps(data))
        return p.inspect_export(self.path, self.b)

    def test_ceiling_and_air(self):
        data = export_data(self.b, ceiling=True)
        self.assertEqual(self.inspect(data)['ceiling_blocks'], 1)
        data['BlockCodes']['1'] = 'game:air'
        self.assertEqual(self.inspect(data)['ceiling_blocks'], 0)

    def test_malformed_out_of_bounds_empty_and_decor(self):
        bad = []
        for change in [dict(Indices=[1023]), dict(Indices=[] ,BlockIds=[]), dict(DecorIndices=[0], DecorIds=[]), dict(SizeY=117), dict(OriginalPos={}), dict(Indices=[0, 0], BlockIds=[1, 1])]:
            bad.append({**export_data(self.b), **change})
        bad.append([])
        for data in bad:
            with self.subTest(data=data):
                result = self.inspect(data)
                self.assertIsNone(result['covered'])
                self.assertTrue(result['problems'])

    def test_oversized_merge_rejected_before_writing(self):
        c = coordinator.Cuboid(1, 0, 1023, 0, 5, 0, 5, 'too_wide')
        with self.assertRaisesRegex(ValueError, 'Cannot merge'):
            coordinator.merge_exports(c, [], self.path)
        self.assertFalse(self.path.exists())

    def test_command_execution_keeps_existing_protocol_and_records_height(self):
        directory = Path(self.temp.name)
        c = coordinator.Cuboid(1, **self.b, name='v12_test', plan_mode=True)
        output = directory / 'v12_test.json'
        def simulate_server(*args, **kwargs):
            output.write_text(json.dumps(export_data(self.b, ceiling=True)))
        state = {'signature': {'capture_plan_hash': 'test'}, 'sections': {}}
        state_path = directory / 'progress.json'
        with patch('subprocess.run', side_effect=simulate_server) as sent, patch.object(coordinator, 'stable_file', return_value=True), patch('sys.stdout', new=io.StringIO()):
            status, problems = coordinator.run_export_attempt(c, 'Player', '%9', directory, directory, state, state_path, confirm=False, verbose=False)
        self.assertEqual((status, problems), ('complete', []))
        self.assertEqual(sent.call_count, 1)
        command = (directory / 'v12_test.txt').read_text()
        self.assertEqual(command, '/executeas Player /we select -5 65 4 4 180 13\n/executeas Player /we export v12_test\n')
        self.assertEqual(state['sections'][c.name]['ceiling_blocks'], 1)

    def test_merged_provenance_cannot_hide_child_y_gap_without_progress(self):
        # One X half is short, the other reaches full height. A merged SizeY
        # alone would falsely imply the first half's upper volume was captured.
        parent = coordinator.Cuboid(1, **self.b, name='parent', plan_mode=True)
        left = coordinator.Cuboid(1, **{**self.b, 'xmax': -1}, name='left', plan_mode=True)
        right = coordinator.Cuboid(1, **{**self.b, 'xmin': 0}, name='right', plan_mode=True)
        directory = Path(self.temp.name)
        pieces = []
        for c, sy in ((left, 100), (right, 116)):
            path = directory / (c.name+'.json')
            path.write_text(json.dumps(export_data(coordinator.cuboid_record(c), size_y=sy)))
            pieces.append((c, path))
        coordinator.merge_exports(parent, pieces, directory / 'parent.json')
        plan = dict(cuboids=[{**self.b, 'name': 'parent'}], requests=[self.b], world_center=p.WORLD_CENTER,
                    plan_hash='test', geometry_hash='test')
        audit = p.audit_coverage(plan, {}, directory)
        self.assertEqual(audit['gaps'], [{**self.b, 'xmax': -1, 'ymin': 165}])


if __name__ == '__main__':
    unittest.main()
