"""Chunk-sized deeper rescans and retained loading evidence for sparse exports.

No commands run on import. The coordinator supplies its tmux sender and stable
file waiter, and enables this only during collection, never during reporting.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time

import v12_capture_plan as p


class Console:
    """Match each command's acknowledgement in the local server log."""

    def __init__(self, binary, pane, logfile, work, sender):
        self.binary, self.pane, self.logfile = binary, pane, Path(logfile)
        self.work, self.sender = Path(work), sender
        self.work.mkdir(parents=True, exist_ok=True)
        self.sequence = max((int(f.stem) for f in self.work.glob('*.txt') if f.stem.isdigit()), default=0)

    def command(self, command, expected, timeout=60):
        self.sequence += 1
        command_file = self.work / f'{self.sequence:04d}.txt'
        command_file.write_text(command + '\n')
        offset = self.logfile.stat().st_size
        self.sender(command_file, self.pane, self.binary)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.logfile.open('rb') as stream:
                stream.seek(offset)
                text = stream.read().decode('utf-8', errors='replace')
            marker = 'Handling Console Command ' + command
            if marker in text:
                response = text.split(marker, 1)[1]
                if any(error in response for error in ('Exception:', 'Unknown command', 'No such player')):
                    raise RuntimeError(command + '\n' + response)
                if re.search(expected, response, re.IGNORECASE):
                    (self.work / f'{self.sequence:04d}-response.txt').write_text(text)
                    return response.strip()
            time.sleep(.25)
        raise TimeoutError('No expected acknowledgement for ' + command)


def chunk_bounds(regions, ymin, ymax):
    chunks = {(x, z) for b in regions
              for x in range(b['xmin'] // 32, b['xmax'] // 32 + 1)
              for z in range(b['zmin'] // 32, b['zmax'] // 32 + 1)}
    for x, z in sorted(chunks):
        bounds = dict(xmin=x*32, xmax=x*32+31, ymin=ymin, ymax=ymax,
                      zmin=z*32, zmax=z*32+31)
        if p.volume(bounds) > p.MAX_VOLUME or ymax-ymin+1 > p.MAX_SIDE:
            raise ValueError('Deeper probe exceeds selection limits')
        yield bounds


def capture_probe(bounds, name, export_dir, player, console, stable, archive):
    response = console.command(
        f"/executeas {player} /chunk forceload {bounds['xmin']} {bounds['zmin']} {bounds['xmax']} {bounds['zmax']}",
        r'will force load \d+ chunk columns')
    x, z = bounds['xmin'] + 16, bounds['zmin'] + 16
    console.command(f"/tp {player} {x} {bounds['ymax']-2} {z}", r'teleported')
    deadline = time.monotonic() + 90
    while True:
        queues = console.command(f'/executeas {player} /chunk cit',
                                 r'Chunk in genQ: (True|False), chunkReq in Q: (True|False)')
        if 'Chunk in genQ: False, chunkReq in Q: False' in queues:
            break
        if time.monotonic() >= deadline:
            raise TimeoutError('Chunk queues did not settle before deeper rescan')
        time.sleep(1)
    path = export_dir / (name + '.json')
    archive(path)
    console.command(
        f"/executeas {player} /we select {bounds['xmin']} {bounds['ymin']} {bounds['zmin']} {bounds['xmax']} {bounds['ymax']} {bounds['zmax']}",
        r'Start and end position marked')
    console.command(f'/executeas {player} /we export {name}', r'\d+ blocks.*exported', 120)
    stable(path)
    evidence = p.inspect_export(path, bounds, allow_rescans=False)
    if evidence['problems'] or evidence['column_presence']['missing_columns']:
        raise ValueError('Deeper rescan still lacks complete column presence: ' + name)
    return {'file': path.name, 'requested': bounds, 'sha256': evidence['sha256'],
            'load_check': {'force_loaded': True, 'queues_clear': True,
                           'preload_response': response, 'queue_response': queues}}


def usable_probe(ref, export_dir, bounds):
    if not ref or ref.get('requested') != bounds:
        return False
    if not isinstance(ref.get('file'), str) or Path(ref['file']).name != ref['file']:
        return False
    path = export_dir / ref['file']
    evidence = p.inspect_export(path, bounds, allow_rescans=False)
    return (not evidence['problems'] and evidence.get('sha256') == ref.get('sha256')
            and not evidence['column_presence']['missing_columns']
            and ref.get('load_check', {}).get('force_loaded') is True
            and ref.get('load_check', {}).get('queues_clear') is True)


def fill_missing_columns(original, requested, probes):
    """Add only in-band records to previously empty columns; keep raw probes."""
    result = {**original, 'Indices': list(original['Indices']), 'BlockIds': list(original['BlockIds']),
              'DecorIndices': list(original.get('DecorIndices', [])),
              'DecorIds': list(original.get('DecorIds', [])),
              'BlockEntities': dict(original.get('BlockEntities', {})),
              'BlockCodes': dict(original.get('BlockCodes', {})),
              'ItemCodes': dict(original.get('ItemCodes', {}))}
    occupied = {(i & 1023, (i >> 10) & 1023)
                for field in ('Indices', 'DecorIndices') for i in original.get(field, [])}
    added_columns = set()
    for bounds, data in probes:
        for palette in ('BlockCodes', 'ItemCodes'):
            for key, value in data.get(palette, {}).items():
                if key in result[palette] and result[palette][key] != value:
                    raise ValueError('Conflicting rescan palette entry ' + palette + '.' + key)
                result[palette][key] = value
        def target(index):
            x = bounds['xmin'] + (index & 1023) - requested['xmin']
            z = bounds['zmin'] + ((index >> 10) & 1023) - requested['zmin']
            y = bounds['ymin'] + (index >> 20) - requested['ymin']
            if (0 <= x <= requested['xmax']-requested['xmin']
                    and 0 <= z <= requested['zmax']-requested['zmin']
                    and 0 <= y <= requested['ymax']-requested['ymin'] and (x,z) not in occupied):
                return x | (z << 10) | (y << 20), (x,z)
            return None
        for indices, ids in (('Indices', 'BlockIds'), ('DecorIndices', 'DecorIds')):
            for index, value in zip(data.get(indices, []), data.get(ids, [])):
                mapped = target(index)
                if mapped:
                    result[indices].append(mapped[0])
                    result[ids].append(value)
                    added_columns.add(mapped[1])
                    result['SizeY'] = max(result['SizeY'], (mapped[0] >> 20) + 1)
                    result['SizeX'] = max(result['SizeX'], mapped[1][0] + 1)
                    result['SizeZ'] = max(result['SizeZ'], mapped[1][1] + 1)
        for index, value in data.get('BlockEntities', {}).items():
            mapped = target(int(index))
            if mapped:
                result['BlockEntities'][str(mapped[0])] = value
    # Original entities are preserved. Probe entities remain in the raw probe
    # files; merging opaque entity blobs would risk duplicating live entities.
    return result, len(added_columns)


def rescan_export(path, requested, *, prefix, player, console, export_dir,
                  registry, save, stable, archive, ymin=0):
    """Resolve missing columns with chunk probes, preserving original bytes."""
    evidence = p.inspect_export(path, requested)
    missing = evidence.get('column_presence', {}).get('unverified_columns',
                         evidence.get('column_presence', {}).get('missing_columns', 0))
    soft_error = ['Empty sparse export cannot establish coverage']
    if not missing or (evidence['problems'] and evidence['problems'] != soft_error):
        return evidence
    if ymin >= requested['ymin']:
        raise ValueError('Rescan Y minimum must be below the selection floor')
    original_bytes = path.read_bytes()
    original = json.loads(original_bytes)
    if original.get('CapturePlanPieces'):
        raise ValueError('Rescan source leaves rather than a merged parent')
    refs = {}
    cache = registry.setdefault('probes', {})
    old_metadata = original.get('ColumnRescanEvidence', {})
    if isinstance(old_metadata, dict) and isinstance(old_metadata.get('probes'), list):
        for ref in old_metadata['probes']:
            try:
                bounds = p.box(ref['requested'])
                if usable_probe(ref, export_dir, bounds):
                    cache[p.digest(bounds)[:16]] = ref
            except (KeyError, TypeError, ValueError):
                pass
    for bounds in chunk_bounds(evidence['column_presence']['missing_regions'], ymin, requested['ymax']):
        token = p.digest(bounds)[:16]
        ref = cache.get(token)
        if not usable_probe(ref, export_dir, bounds):
            print(f"Deeper rescan: X {bounds['xmin']}..{bounds['xmax']}, Z {bounds['zmin']}..{bounds['zmax']}, Y {ymin}..{bounds['ymax']}", flush=True)
            ref = capture_probe(bounds, prefix + '_depth_' + token, export_dir, player, console, stable, archive)
            cache[token] = ref
            save()
        refs[ref['file']] = ref
    probes = [(ref['requested'], p.read_json(export_dir/ref['file'])) for ref in refs.values()]
    combined, restored = fill_missing_columns(original, requested, probes)
    combined['ColumnRescanEvidence'] = {'version': 1, 'probes': list(refs.values()),
        'source_sha256': hashlib.sha256(original_bytes).hexdigest(),
        'source_backup': path.stem + '.before-rescan-' + hashlib.sha256(original_bytes).hexdigest()[:12] + '.json',
        'entity_policy': 'Original entities retained; additional entity observations remain in raw probe files.'}
    temporary = path.with_suffix('.json.rescan-tmp')
    temporary.write_text(json.dumps(combined, separators=(',', ':')))
    checked = p.inspect_export(temporary, requested)
    if checked['problems']:
        temporary.unlink()
        raise ValueError('Rescanned export failed validation: ' + '; '.join(checked['problems']))
    if path.read_bytes() != original_bytes:
        temporary.unlink()
        raise ValueError('Export changed during rescan; refusing replacement')
    backup = path.with_name(path.stem + '.before-rescan-' + hashlib.sha256(original_bytes).hexdigest()[:12] + '.json')
    if backup.exists() and backup.read_bytes() != original_bytes:
        raise ValueError('Rescan backup hash conflict')
    if not backup.exists():
        backup.write_bytes(original_bytes)
    temporary.replace(path)
    registry.setdefault('sections', {})[path.stem] = {
        'sha256': checked['sha256'], 'source_backup': backup.name,
        'restored_columns': restored,
        'empty_columns_confirmed_by_rescan': checked['column_presence']['empty_columns_confirmed_by_rescan'],
        'unverified_columns': checked['column_presence']['unverified_columns'],
        'probes': list(refs.values())}
    save()
    print(f"Rescan result: {restored} columns restored; "
          f"{checked['column_presence']['empty_columns_confirmed_by_rescan']} empty bands corroborated; "
          f"{checked['column_presence']['unverified_columns']} unverified.", flush=True)
    return checked
