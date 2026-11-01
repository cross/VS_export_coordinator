#!/usr/bin/env python3
"""Repair reported empty columns with preloaded, independently checked exports.

Without --execute this only writes a reviewable requests.json. With --execute,
preload the reported chunk columns, teleport/query chunk status through the
player, export small patches, and fill only formerly empty parent columns.
Original exports, patch files, command responses, and results are retained.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v12_capture_plan as p
import capture_rescan
import vintage_story_export_coordinator as coordinator


def atomic_json(path: Path, value: dict) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n')
    tmp.replace(path)


def repair_requests(plan: dict, report: dict, report_hash: str) -> dict:
    if report.get('plan_hash') != plan['plan_hash']:
        raise ValueError('Independent report belongs to a different plan')
    parents = {b['name']+'.json': b for b in plan['cuboids']}
    regions, jobs = [], []
    for entry in report['empty_column_rectangles']:
        parent = parents[entry['file']]
        bounds = p.box({**entry, 'ymin': parent['ymin'], 'ymax': parent['ymax']})
        if p.intersection(parent, bounds) != bounds:
            raise ValueError('Reported hole lies outside its parent')
        count = (bounds['xmax']-bounds['xmin']+1)*(bounds['zmax']-bounds['zmin']+1)
        if count != entry['columns']:
            raise ValueError('Reported column count differs from rectangle bounds')
        regions.append({**bounds, 'parent': entry['file'], 'columns': count})
        for part in p.limited_boxes(bounds, 32, 128):
            jobs.append({**part, 'parent': entry['file'],
                'name': f"v12_column_repair_{report_hash[:12]}_{len(jobs)+1:04d}"})
    p.verify_disjoint(regions, 'repair regions')
    if sum(r['columns'] for r in regions) != report['empty_columns']:
        raise ValueError('Independent report total differs from its rectangles')
    return {'plan_hash': plan['plan_hash'], 'independent_report_sha256': report_hash,
            'regions': regions, 'jobs': jobs, 'columns': sum(r['columns'] for r in regions),
            'maximum_patch': {'x': 128, 'z': 32},
            'load_policy': 'forceload report regions, teleport to each patch, executeas chunk cit until not queued, then require column presence'}


def overlay_empty_columns(parent: dict, original: dict, patches: list[tuple[dict, dict]]) -> dict:
    """Preserve original records; add patch data only where no column record exists."""
    if original.get('CapturePlanPieces'):
        raise ValueError('Repair merged leaves rather than a synthetic merged parent')
    width = parent['xmax']-parent['xmin']+1
    occupied = {(i & 1023, (i >> 10) & 1023)
                for key in ('Indices', 'DecorIndices') for i in original.get(key, [])}
    result = {**original, 'Indices': list(original['Indices']), 'BlockIds': list(original['BlockIds']),
              'DecorIndices': list(original.get('DecorIndices', [])),
              'DecorIds': list(original.get('DecorIds', [])),
              'BlockEntities': dict(original.get('BlockEntities', {})),
              'BlockCodes': dict(original.get('BlockCodes', {})),
              'ItemCodes': dict(original.get('ItemCodes', {}))}
    claimed = set()
    for bounds, data in patches:
        if p.intersection(parent, bounds) != p.box(bounds):
            raise ValueError('Patch outside parent selection')
        dx, dy, dz = (bounds[a+'min']-parent[a+'min'] for a in p.AXES)
        patch_columns = {(x+dx,z+dz) for x in range(bounds['xmax']-bounds['xmin']+1)
                         for z in range(bounds['zmax']-bounds['zmin']+1)} - occupied
        if claimed & patch_columns:
            raise ValueError('Overlapping repair patches')
        claimed.update(patch_columns)
        for palette in ('BlockCodes', 'ItemCodes'):
            for key, value in data.get(palette, {}).items():
                if key in result[palette] and result[palette][key] != value:
                    raise ValueError(f'Conflicting palette entry {palette}.{key}')
                result[palette][key] = value
        def mapped(index):
            x, y, z = (index & 1023)+dx, (index >> 20)+dy, ((index >> 10)&1023)+dz
            if not (0 <= x < width and parent['ymin'] <= y+parent['ymin'] <= parent['ymax']
                    and 0 <= z <= parent['zmax']-parent['zmin']):
                raise ValueError('Patch index outside parent')
            return x | (z << 10) | (y << 20), (x,z)
        for indices, ids in (('Indices','BlockIds'),('DecorIndices','DecorIds')):
            for index, block_id in zip(data.get(indices, []),data.get(ids, [])):
                target, column = mapped(index)
                if column in patch_columns:
                    result[indices].append(target)
                    result[ids].append(block_id)
        for index, value in data.get('BlockEntities', {}).items():
            target, column = mapped(int(index))
            if column in patch_columns:
                key = str(target)
                if key in result['BlockEntities'] and result['BlockEntities'][key] != value:
                    raise ValueError('Conflicting block entity in repair')
                result['BlockEntities'][key] = value
        result['SizeY'] = max(result['SizeY'],dy+data['SizeY'])
    result['SizeX'] = width
    result['SizeZ'] = parent['zmax']-parent['zmin']+1
    # Entity blobs lack accessible IDs here. Keep the original entity list;
    # additional entity observations remain in the retained patch JSON files.
    return result


class Console:
    def __init__(self, binary, pane, logfile, work):
        self.binary, self.pane, self.logfile, self.work = binary, pane, logfile, work
        self.sequence = max((int(f.stem) for f in (work/'commands').glob('*.txt')
                             if f.stem.isdigit()),default=0)

    def command(self, command, expected, timeout=60):
        self.sequence += 1
        command_file = self.work/'commands'/f'{self.sequence:04d}.txt'
        command_file.parent.mkdir(exist_ok=True)
        command_file.write_text(command+'\n')
        offset = self.logfile.stat().st_size
        coordinator.paste_commands(command_file, self.pane, self.binary)
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            with self.logfile.open('r',encoding='utf-8',errors='replace') as stream:
                stream.seek(offset)
                text = stream.read()
            marker = 'Handling Console Command '+command
            if marker in text:
                response = text.split(marker,1)[1]
                if 'Exception:' in response or 'Unknown command' in response or 'No such player' in response:
                    raise RuntimeError(command+'\n'+response)
                if re.search(expected,response,re.IGNORECASE):
                    (self.work/'commands'/f'{self.sequence:04d}-response.txt').write_text(text)
                    return response
            time.sleep(.25)
        raise TimeoutError('No expected acknowledgement for '+command)


def wait_stable(path: Path, timeout=90):
    deadline, previous, stable = time.monotonic()+timeout, None, 0
    while time.monotonic() < deadline:
        try:
            stat = path.stat()
            current = stat.st_size, stat.st_mtime_ns
        except FileNotFoundError:
            current = None
        stable = stable+1 if current and current[0] and current == previous else 0
        if stable >= 2:
            return
        previous = current
        time.sleep(.5)
    raise TimeoutError('No stable repair export: '+str(path))


def probe_capture_floor(job, console, args, work, results):
    """Use the coordinator's shared, hash-checked probe cache for diagnostics."""
    registry=results.setdefault('column_rescans', {'probes':{}, 'sections':{}})
    observations=[]
    for bounds in capture_rescan.chunk_bounds([job],0,job['ymax']):
        token=p.digest(bounds)[:16]
        ref=registry['probes'].get(token)
        if not capture_rescan.usable_probe(ref,args.export_dir,bounds):
            ref=capture_rescan.capture_probe(bounds,'v12_column_depth_'+token,
                args.export_dir,args.player,console,wait_stable,coordinator.archive_invalid)
            registry['probes'][token]=ref
            atomic_json(work/'results.json',results)
        data=p.read_json(args.export_dir/ref['file'])
        columns=defaultdict(list)
        for field in ('Indices','DecorIndices'):
            for index in data.get(field,[]):
                columns[(bounds['xmin']+(index&1023),bounds['zmin']+((index>>10)&1023))].append(bounds['ymin']+(index>>20))
        for (x,z),ys in columns.items():
            if job['xmin'] <= x <= job['xmax'] and job['zmin'] <= z <= job['zmax']:
                observations.append({'x':x,'z':z,'minimum_record_y':min(ys),'maximum_record_y':max(ys),
                                     'probe':str(args.export_dir/ref['file'])})
    expected=(job['xmax']-job['xmin']+1)*(job['zmax']-job['zmin']+1)
    if len(observations)==expected and all(o['maximum_record_y'] < job['ymin'] for o in observations):
        return observations
    return None


def corroborate_parent(parent_path, parent, console, args, work, results):
    registry=results.setdefault('column_rescans', {'probes':{}, 'sections':{}})
    return capture_rescan.rescan_export(parent_path,p.box(parent),prefix='v12_column_repair',
        player=args.player,console=console,export_dir=args.export_dir,registry=registry,
        save=lambda:atomic_json(work/'results.json',results),stable=wait_stable,
        archive=coordinator.archive_invalid)


def execute(plan, requests, args, work):
    parents = {b['name']+'.json': b for b in plan['cuboids']}
    console = Console(str(args.tmux_binary.expanduser()),args.target_pane,args.server_log,work)
    state_path = work/'results.json'
    results = p.read_json(state_path) if state_path.exists() else {
        'plan_hash': plan['plan_hash'], 'jobs': {}, 'parents': {}, 'preloads': [],
        'entity_policy': 'Original parent entities retained; patch entity blobs remain in patch files.'}
    if results['plan_hash'] != plan['plan_hash']:
        raise ValueError('Repair results belong to another plan')
    atomic_json(state_path,results)
    for region in requests['regions']:
        command = (f"/executeas {args.player} /chunk forceload {region['xmin']} {region['zmin']} "
                   f"{region['xmax']} {region['zmax']}")
        response = console.command(command,r'will force load \d+ chunk columns')
        results['preloads'].append({'region':region,'response':response.strip()})
    atomic_json(state_path,results)
    print(f"Server acknowledged {len(requests['regions'])} preload regions.",flush=True)
    by_parent = defaultdict(list)
    for job in requests['jobs']:
        by_parent[job['parent']].append(job)
    (work/'backups').mkdir(exist_ok=True)
    for filename,jobs in by_parent.items():
        parent = parents[filename]
        parent_path = args.export_dir/filename
        before = parent_path.read_bytes()
        source_hash = hashlib.sha256(before).hexdigest()
        previous_hash=results['parents'].get(filename,{}).get('sha256')
        current=p.inspect_export(parent_path,parent)
        if previous_hash == source_hash and not current['problems'] and not current['column_presence']['unverified_columns']:
            print('Already repaired:',filename,flush=True)
            continue
        backup = work/'backups'/filename
        if not backup.exists():
            backup.write_bytes(before)
        elif (backup.read_bytes() != before and previous_hash != source_hash
              and results.get('column_rescans', {}).get('sections', {}).get(parent_path.stem, {}).get('sha256') != source_hash):
            raise ValueError('Parent changed since backup; refusing to overlay it again: '+filename)
        backup_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
        patches = []
        all_valid = True
        below_floor = []
        for job in jobs:
            output = args.export_dir/(job['name']+'.json')
            bounds = p.box(job)
            evidence = None
            if output.exists():
                old_evidence = p.inspect_export(output,bounds)
                if not old_evidence['problems'] and not old_evidence['column_presence']['missing_columns']:
                    evidence = old_evidence
                    results['jobs'][job['name']] = {'requested':bounds,'parent':filename,
                        'file':str(output),'evidence':evidence,'status':'validated'}
                elif old_evidence['problems'] == ['Empty sparse export cannot establish coverage']:
                    observed=probe_capture_floor(job,console,args,work,results)
                    if observed:
                        below_floor.extend(observed)
                        results['jobs'][job['name']]={'requested':bounds,'parent':filename,'file':str(output),
                            'status':'recaptured_empty_above_floor','lower_records':observed}
                        atomic_json(state_path,results)
                        print(f"{job['name']}: {len(observed)} columns have only below-floor records in the deeper probe.",flush=True)
                        continue
            for attempt in range(1,2) if evidence is None else []:
                x,z = (job['xmin']+job['xmax'])//2,(job['zmin']+job['zmax'])//2
                console.command(f'/tp {args.player} {x} {job["ymax"]-2} {z}',r'teleported')
                deadline = time.monotonic()+90
                while True:
                    response = console.command(f'/executeas {args.player} /chunk cit',
                                               r'Chunk in genQ: (True|False), chunkReq in Q: (True|False)')
                    if 'Chunk in genQ: False, chunkReq in Q: False' in response:
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError('Chunk requests did not settle at repair position')
                    time.sleep(1)
                if output.exists():
                    coordinator.archive_invalid(output)
                console.command(f"/executeas {args.player} /we select {job['xmin']} {job['ymin']} {job['zmin']} {job['xmax']} {job['ymax']} {job['zmax']}",
                                r'Start and end position marked')
                console.command(f"/executeas {args.player} /we export {job['name']}",r'\d+ blocks.*exported',120)
                wait_stable(output)
                evidence = p.inspect_export(output,bounds)
                missing = evidence.get('column_presence',{}).get('missing_columns')
                results['jobs'][job['name']] = {'requested':bounds,'parent':filename,
                    'file':str(output),'attempt':attempt,'evidence':evidence,
                    'status':'validated' if not evidence['problems'] and missing == 0 else 'unresolved'}
                atomic_json(state_path,results)
                print(f"{job['name']}: {missing} missing columns; problems={evidence['problems']}",flush=True)
                if not evidence['problems'] and missing == 0:
                    break
                if evidence['problems'] == ['Empty sparse export cannot establish coverage']:
                    observed=probe_capture_floor(job,console,args,work,results)
                    if observed:
                        below_floor.extend(observed)
                        results['jobs'][job['name']].update(status='recaptured_empty_above_floor',lower_records=observed)
                        atomic_json(state_path,results)
                        break
            if results['jobs'].get(job['name'],{}).get('status') == 'recaptured_empty_above_floor':
                continue
            if evidence is None or evidence['problems'] or evidence.get('column_presence',{}).get('missing_columns'):
                all_valid = False
            else:
                patches.append((bounds,p.read_json(output)))
        if not all_valid:
            checked=corroborate_parent(parent_path,parent,console,args,work,results)
            if not checked['problems'] and not checked['column_presence']['unverified_columns']:
                results['parents'][filename]={'status':'rescanned','source_sha256':backup_hash,
                    'sha256':checked['sha256'],'backup':str(backup),
                    'column_presence':checked['column_presence']}
                atomic_json(state_path,results)
                print('Parent recovered through chunk-sized deeper rescans:',filename,flush=True)
                continue
            print('Parent remains unverified:',filename,flush=True)
            continue
        combined = overlay_empty_columns(parent,json.loads(before),patches)
        if not patches:
            confirmed=corroborate_parent(parent_path,parent,console,args,work,results)
            if confirmed['problems'] or confirmed['column_presence']['unverified_columns']:
                print('Parent remains unverified:',filename,flush=True)
                continue
            results['parents'][filename]={'status':'empty_band_corroborated','source_sha256':backup_hash,
                'sha256':confirmed['sha256'],'backup':str(backup),'below_floor_columns':below_floor}
            atomic_json(state_path,results)
            print('Parent retains original block records; empty bands corroborated by retained deeper evidence:',filename,flush=True)
            continue
        combined['ColumnRepairProvenance'] = {'plan_hash':plan['plan_hash'],
            'source_sha256':backup_hash,'source_backup':str(backup),
            'patches':[{'file':job['name']+'.json','requested':p.box(job)} for job in jobs],
            'repaired_utc':datetime.now(timezone.utc).isoformat()}
        temporary = parent_path.with_suffix('.json.repair-tmp')
        temporary.write_text(json.dumps(combined,separators=(',',':')))
        checked = p.inspect_export(temporary,parent)
        if checked['problems'] or checked['column_presence']['missing_columns'] != len(below_floor):
            raise ValueError('Repaired parent still fails validation: '+filename+' '+str(checked))
        if parent_path.read_bytes() != before:
            raise ValueError('Parent changed while collecting repair; refusing replacement')
        temporary.replace(parent_path)
        if below_floor:
            checked=corroborate_parent(parent_path,parent,console,args,work,results)
        results['parents'][filename] = {'status':'repaired','source_sha256':backup_hash,
            'sha256':checked['sha256'],'missing_columns':checked['column_presence']['missing_columns'],
            'below_floor_columns':below_floor,'backup':str(backup)}
        atomic_json(state_path,results)
        print('Repaired parent:',filename,flush=True)
    results['complete'] = all(
        not (check:=p.inspect_export(args.export_dir/filename,parents[filename]))['problems']
        and not check['column_presence']['unverified_columns'] for filename in by_parent)
    atomic_json(state_path,results)
    return 0 if results['complete'] else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--plan',type=Path,default=coordinator.DEFAULT_PLAN)
    parser.add_argument('--export-dir',type=Path,required=True)
    parser.add_argument('--work-dir',type=Path,required=True)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--target-pane')
    parser.add_argument('--player',default='Xplosionist')
    parser.add_argument('--tmux-binary',type=Path,default=Path('tmux'))
    parser.add_argument('--server-log',type=Path)
    args=parser.parse_args()
    plan=p.load_plan(args.plan)
    report_bytes=args.report.read_bytes()
    requests=repair_requests(plan,json.loads(report_bytes),hashlib.sha256(report_bytes).hexdigest())
    args.work_dir=args.work_dir.resolve()
    args.export_dir=args.export_dir.expanduser().resolve()
    args.work_dir.mkdir(parents=True,exist_ok=True)
    path=args.work_dir/'requests.json'
    if path.exists() and p.read_json(path) != requests:
        parser.error('Work directory belongs to a different repair')
    atomic_json(path,requests)
    print(f"Prepared {len(requests['jobs'])} patches for {len(requests['regions'])} rectangles, {requests['columns']} columns.",flush=True)
    if not args.execute:
        return 0
    if not args.target_pane or not args.server_log:
        parser.error('--execute requires --target-pane and --server-log')
    return execute(plan,requests,args,args.work_dir)


if __name__=='__main__':
    raise SystemExit(main())
