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


def capture_loaded_export(bounds, name, export_dir, player, console, stable, archive):
    """Force-load and retry a selection without touching its source export."""
    response = console.command(
        f"/executeas {player} /chunk forceload {bounds['xmin']} {bounds['zmin']} {bounds['xmax']} {bounds['zmax']}",
        r'will force load \d+ chunk columns')
    x, z = (bounds['xmin'] + bounds['xmax']) // 2, (bounds['zmin'] + bounds['zmax']) // 2
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
    if evidence['problems'] and evidence['problems'] != ['Empty sparse export cannot establish coverage']:
        raise ValueError('Loaded export failed validation: ' + name + ': ' + '; '.join(evidence['problems']))
    return {'file': path.name, 'requested': bounds, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'load_check': {'force_loaded': True, 'queues_clear': True,
                           'preload_response': response, 'queue_response': queues}}


def capture_probe(bounds, name, export_dir, player, console, stable, archive):
    ref = capture_loaded_export(bounds, name, export_dir, player, console, stable, archive)
    evidence = p.inspect_export(export_dir / ref['file'], bounds, allow_rescans=False)
    if evidence['problems'] or evidence['column_presence']['missing_columns']:
        raise ValueError('Deeper rescan still lacks complete column presence: ' + name)
    return ref


def usable_loaded_export(ref, export_dir, bounds):
    """An acknowledged normal-height retry may legitimately still be empty."""
    if not ref or ref.get('requested') != bounds:
        return False
    if not isinstance(ref.get('file'), str) or Path(ref['file']).name != ref['file']:
        return False
    path = export_dir / ref['file']
    evidence = p.inspect_export(path, bounds, allow_rescans=False)
    return (not evidence['problems'] or evidence['problems'] == ['Empty sparse export cannot establish coverage']) and (
        hashlib.sha256(path.read_bytes()).hexdigest() == ref.get('sha256')
        and ref.get('load_check', {}).get('force_loaded') is True
        and ref.get('load_check', {}).get('queues_clear') is True)


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
                  registry, save, stable, archive, ymin=0, retry_loaded=True):
    """Reuse saved probes, retry after loading, then checkpoint each deeper probe."""
    evidence = p.inspect_export(path, requested)
    columns = evidence.get('column_presence', {})
    missing = columns.get('unverified_columns', columns.get('missing_columns', 0))
    soft_error = ['Empty sparse export cannot establish coverage']
    if not missing or (evidence['problems'] and evidence['problems'] != soft_error):
        return evidence
    print(f"{path.stem}: {missing:,} unverified X/Z columns out of "
          f"{columns['requested_columns']:,}.", flush=True)
    current_bytes = path.read_bytes()
    current = json.loads(current_bytes)
    if current.get('CapturePlanPieces'):
        raise ValueError('Rescan source leaves rather than a merged parent')
    source_hash = hashlib.sha256(current_bytes).hexdigest()
    backup = path.with_name(path.stem + '.before-rescan-' + source_hash[:12] + '.json')
    refs = {}
    cache = registry.setdefault('probes', {})
    old_metadata = current.get('ColumnRescanEvidence', {})
    if isinstance(old_metadata, dict):
        old_backup = old_metadata.get('source_backup')
        if isinstance(old_backup, str) and Path(old_backup).name == old_backup:
            candidate = path.parent / old_backup
            if candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == old_metadata.get('source_sha256'):
                backup, source_hash = candidate, old_metadata['source_sha256']
        if isinstance(old_metadata.get('probes'), list):
            for ref in old_metadata['probes']:
                try:
                    bounds = p.box(ref['requested'])
                    if usable_probe(ref, export_dir, bounds):
                        cache[p.digest(bounds)[:16]] = ref
                        refs[ref['file']] = ref
                except (KeyError, TypeError, ValueError):
                    pass
    prior = registry.get('sections', {}).get(path.stem, {})
    restored = prior.get('restored_columns', 0) if prior.get('sha256') == hashlib.sha256(current_bytes).hexdigest() else 0
    loaded_ref = None

    def remaining():
        columns = evidence.get('column_presence', {})
        return columns.get('unverified_columns', columns.get('missing_columns', 0))

    def checkpoint(ref, deeper):
        nonlocal current_bytes, current, evidence, restored
        bounds = ref['requested']
        combined, added = fill_missing_columns(current, requested, [(bounds, p.read_json(export_dir/ref['file']))])
        if deeper:
            refs[ref['file']] = ref
        if not added and not deeper:
            return
        metadata = {'version': 1, 'probes': list(refs.values()),
            'source_sha256': source_hash, 'source_backup': backup.name,
            'entity_policy': 'Original entities retained; additional entity observations remain in raw export files.'}
        if loaded_ref:
            metadata['loaded_retry'] = loaded_ref
        elif isinstance(old_metadata, dict) and old_metadata.get('loaded_retry'):
            metadata['loaded_retry'] = old_metadata['loaded_retry']
        combined['ColumnRescanEvidence'] = metadata
        temporary = path.with_suffix('.json.rescan-tmp')
        temporary.write_text(json.dumps(combined, separators=(',', ':')))
        checked = p.inspect_export(temporary, requested)
        if checked['problems']:
            temporary.unlink()
            raise ValueError('Rescanned export failed validation: ' + '; '.join(checked['problems']))
        if path.read_bytes() != current_bytes:
            temporary.unlink()
            raise ValueError('Export changed during rescan; refusing replacement')
        if not backup.exists():
            backup.write_bytes(current_bytes)
        elif hashlib.sha256(backup.read_bytes()).hexdigest() != source_hash:
            temporary.unlink()
            raise ValueError('Rescan backup hash conflict')
        temporary.replace(path)
        current_bytes, current = path.read_bytes(), combined
        checked['file'] = str(path)
        evidence = checked
        restored += added
        registry.setdefault('sections', {})[path.stem] = {
            'status': 'rescanning' if remaining() else 'resolved',
            'sha256': checked['sha256'], 'source_backup': backup.name,
            'restored_columns': restored,
            'empty_columns_confirmed_by_rescan': checked['column_presence']['empty_columns_confirmed_by_rescan'],
            'unverified_columns': remaining(), 'probes': list(refs.values())}
        save()
        print(f"Saved rescan checkpoint: +{added:,} columns restored; "
              f"{checked['column_presence']['empty_columns_confirmed_by_rescan']:,} empty bands corroborated; "
              f"{remaining():,} unverified.", flush=True)

    # Older versions cached probes before applying the batch. Recover those
    # files first, so upgrading/resuming does not throw away completed work.
    if ymin < requested['ymin']:
        for bounds in chunk_bounds(columns.get('unverified_regions', columns['missing_regions']), ymin, requested['ymax']):
            ref = cache.get(p.digest(bounds)[:16])
            if usable_probe(ref, export_dir, bounds):
                print(f"Applying retained deeper export: {ref['file']}", flush=True)
                checkpoint(ref, True)
    if not remaining():
        return evidence

    if retry_loaded:
        token = p.digest(requested)[:16]
        retries = registry.setdefault('loaded_retries', {})
        loaded_ref = retries.get(token)
        if not usable_loaded_export(loaded_ref, export_dir, requested):
            print('Loading the selection and retrying at the requested Y bounds before deeper scans.', flush=True)
            loaded_ref = capture_loaded_export(requested, prefix + '_loaded_' + token,
                export_dir, player, console, stable, archive)
            retries[token] = loaded_ref
            save()
        checkpoint(loaded_ref, False)
        if not remaining():
            return evidence

    if ymin >= requested['ymin']:
        raise ValueError('Rescan Y minimum must be below the selection floor')
    columns = evidence['column_presence']
    planned = list(chunk_bounds(columns.get('unverified_regions', columns['missing_regions']), ymin, requested['ymax']))
    print(f"After loading/retry: {remaining():,} columns still unverified; "
          f"checking {len(planned)} affected chunk columns for deeper evidence.", flush=True)
    for index, bounds in enumerate(planned, 1):
        token = p.digest(bounds)[:16]
        ref = cache.get(token)
        if not usable_probe(ref, export_dir, bounds):
            print(f"Deeper rescan {index}/{len(planned)}: X {bounds['xmin']}..{bounds['xmax']}, "
                  f"Z {bounds['zmin']}..{bounds['zmax']}, Y {ymin}..{bounds['ymax']}", flush=True)
            ref = capture_probe(bounds, prefix + '_depth_' + token, export_dir, player, console, stable, archive)
            cache[token] = ref
            save()
        checkpoint(ref, True)
    return evidence
