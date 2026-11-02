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
    # A valid terrain fixture has at least one record in every requested X/Z
    # column; additional height can still be sparse.
    indices = [x | (z << 10) for z in range(sz) for x in range(sx)]
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

    def test_plan_without_explicit_operational_x_cap_is_rejected(self):
        malformed = deepcopy(self.banded)
        del malformed['partition']['x_span']
        malformed['plan_hash'] = p.digest({k: v for k, v in malformed.items() if k != 'plan_hash'})
        with self.assertRaisesRegex(ValueError, 'explicit X/Z span caps'):
            p.verify_plan(malformed)

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

    def test_height_extension_adds_nonoverlapping_regions_under_new_identity(self):
        a = self.banded['cuboids'][0]
        region = {**p.box(a), 'ymin': 175, 'ymax': 220}
        extended = p.extend_plan(self.banded, [region])
        self.assertNotEqual(extended['prefix'], self.banded['prefix'])
        self.assertEqual([p.box(b) for b in extended['cuboids'][:len(self.banded['cuboids'])]],
                         [p.box(b) for b in self.banded['cuboids']])
        self.assertTrue(all(b['name'].startswith(extended['prefix']) for b in extended['cuboids']))
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

    def test_freshwater_extension_adds_regions_and_retains_partition_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'lakes.json'
            path.write_text(json.dumps({'bodies': [{'id': 'whole-lake', 'rows': [[60000, -36000, -33000]]}]}))
            extended = p.extend_plan(self.banded, [], path)
            self.assertEqual([p.box(b) for b in extended['cuboids'][:len(self.banded['cuboids'])]],
                             [p.box(b) for b in self.banded['cuboids']])
            self.assertTrue(all(b['name'].startswith(extended['prefix']) for b in extended['cuboids']))
            lake = dict(xmin=-36000, xmax=-33000, ymin=65, ymax=180, zmin=60000, zmax=60000)
            self.assertFalse(p.difference([lake], extended['requests']))
            self.assertTrue(extended['freshwater_bodies'][0]['selected'])
            self.assertTrue(all(b['xmax']-b['xmin']+1 <= 450 for b in extended['cuboids']))


    def test_default_plan_preview_and_missing_plan_never_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = p.write_plan(self.banded, Path(tmp) / 'plan.json')
            with patch.object(coordinator, 'DEFAULT_PLAN', path), patch('sys.argv', ['coordinator', '--preview']), patch('subprocess.run', side_effect=AssertionError('No commands allowed')), patch('builtins.input', side_effect=AssertionError('No prompts allowed')), patch('sys.stdout', new=io.StringIO()):
                self.assertEqual(coordinator.main(), 0)
            args = ['coordinator', '--plan', str(Path(tmp)/'missing.json'), '--preview']
            with patch('sys.argv', args), patch('subprocess.run', side_effect=AssertionError('No commands allowed')), patch('builtins.input', side_effect=AssertionError('No prompts allowed')), patch('sys.stderr', new=io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    coordinator.main()
                self.assertEqual(error.exception.code, 2)

    def test_collector_rejects_removed_geometry_options(self):
        for arguments in (['--prefix', 'old_capture'], ['--z-span', '50']):
            with patch('sys.argv', ['coordinator', '--preview', *arguments]), patch('subprocess.run', side_effect=AssertionError('No commands allowed')), patch('builtins.input', side_effect=AssertionError('No prompts allowed')), patch('sys.stderr', new=io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    coordinator.main()
                self.assertEqual(error.exception.code, 2)


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

    def test_shortened_height_leaves_exact_upper_gap(self):
        result=self.inspect(export_data(self.b,size_y=100))
        self.assertFalse(result['problems'])
        self.assertEqual(result['covered'],{**self.b,'ymax':164})
        self.assertEqual(p.difference([self.b],result['verified_regions']),[{**self.b,'ymin':165}])

    def test_malformed_out_of_bounds_empty_and_decor(self):
        bad = []
        for change in [dict(Indices=[1023]), dict(Indices=[] ,BlockIds=[]), dict(DecorIndices=[0], DecorIds=[]), dict(SizeY=117), dict(OriginalPos={}), dict(Indices=[0, 0], BlockIds=[1, 1]), dict(Indices=[0, 0, 0], BlockIds=[1, 2, 3])]:
            bad.append({**export_data(self.b), **change})
        bad.append([])
        for data in bad:
            with self.subTest(data=data):
                result = self.inspect(data)
                self.assertIsNone(result['covered'])
                self.assertTrue(result['problems'])

    def test_solid_fluid_pairs_preserve_distinct_water_codes(self):
        data = export_data(self.b)
        data['BlockIds'][1] = 3
        data['Indices'].extend([0, 1])
        data['BlockIds'].extend([2, 4])
        data['BlockCodes'] = {'1': 'game:seaweed-section', '2': 'game:saltwater-still-7',
                              '3': 'game:rock-peridotite', '4': 'game:water-still-7'}
        evidence = self.inspect(data)
        self.assertFalse(evidence['problems'])
        self.assertEqual(evidence['layered_positions'], 2)
        self.assertEqual(json.loads(self.path.read_text()), data)


    def test_internal_column_holes_are_exact_and_excluded_from_audit(self):
        data = export_data(self.b)
        missing = {(2,3),(3,3),(2,4),(3,4)}
        pairs = [(i,b) for i,b in zip(data['Indices'],data['BlockIds'])
                 if (i & 1023, (i >> 10) & 1023) not in missing]
        data['Indices'],data['BlockIds'] = map(list,zip(*pairs))
        evidence = self.inspect(data)
        self.assertFalse(evidence['problems'])
        self.assertEqual(evidence['column_presence']['missing_columns'],4)
        self.assertEqual(evidence['column_presence']['missing_regions'],
            [dict(xmin=-3,xmax=-2,ymin=65,ymax=180,zmin=7,zmax=8)])
        missing_volume = sum(p.volume(b) for b in p.difference([self.b], evidence['verified_regions']))
        self.assertEqual(missing_volume,4*116)

    def test_decor_counts_as_column_presence_without_a_block_record(self):
        data = export_data(self.b)
        data['Indices'].pop()
        data['BlockIds'].pop()
        data['DecorIndices'] = [9 | (9 << 10)]
        data['DecorIds'] = [1]
        self.assertEqual(self.inspect(data)['column_presence']['missing_columns'],0)


if __name__ == '__main__':
    unittest.main()
