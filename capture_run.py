"""Isolated captures: immutable attempts, whole-section retries, and seam checks.

The source plan defines nonoverlapping cores. Captures extend each core three
blocks east/south for independent overlap comparisons. No old partial export
is ever spliced into a new export. Compatible, hash-checked run manifests
can resume work automatically.
"""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

import v12_capture_plan as p

SCHEMA = 'coast-capture-run/3'
OPERATIONAL_MODEL = 'fresh-loaded-whole-section-with-overlaps/1'
INDEX_SCHEMA = 'coast-capture-run-index/1'
DEFAULT_OVERLAP = 3
DEFAULT_COVERAGE_Y = 109
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_PROBE_YMIN = 0


class CaptureError(ValueError):
    pass


class CompatibilityError(CaptureError):
    """Compatibility needs operator review; retrying cannot establish it."""


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def is_empty_directory(path):
    path=Path(path)
    return path.is_dir() and next(path.iterdir(),None) is None


def run_index_path():
    override=os.environ.get('VS_CAPTURE_RUN_INDEX')
    if override:
        return Path(override).expanduser().resolve()
    base=Path(os.environ.get('XDG_STATE_HOME') or str(Path.home()/'.local'/'state')).expanduser()
    return base/'vintage-story-export-coordinator'/'runs.json'


def read_run_index():
    path=run_index_path()
    if not path.exists():
        return {'schema':INDEX_SCHEMA,'runs':[]}
    try:
        index=p.read_json(path)
        if index.get('schema')!=INDEX_SCHEMA or not isinstance(index.get('runs'),list):
            raise ValueError('Unsupported index format')
        if any(not isinstance(item,dict) or not isinstance(item.get('directory'),str)
               or not isinstance(item.get('source'),dict) for item in index['runs']):
            raise ValueError('Malformed run entry')
        return index
    except (ValueError,TypeError,AttributeError) as exc:
        raise CompatibilityError(f'Run index needs review: {path}: {exc}')


def register_run(run):
    """Remember custom run directories without writing beside the program/plan."""
    path=run_index_path()
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.with_suffix('.lock').open('a+') as stream:
        fcntl.flock(stream.fileno(),fcntl.LOCK_EX)
        index=read_run_index()
        directory=str(run.directory.resolve())
        entry={'directory':directory,'run_id':run.state['run_id'],'plan_hash':run.plan['plan_hash'],
               'operational_model':run.state['operational_model'],'source':run.state['source'],
               'plan_source':run.state['plan_source']}
        index['runs']=[item for item in index['runs'] if item.get('directory')!=directory]+[entry]
        atomic_json(path,index)


def discover_runs(plan,plan_path,*,export_dir=None,server_log=None,player=None,settings=None):
    """Find versioned manifests, never loose exports or dimension matches."""
    source_filters={'export_dir':str(Path(export_dir).expanduser().resolve()) if export_dir else None,
                    'server_log':str(Path(server_log).expanduser().resolve()) if server_log else None,
                    'player':player}
    def matches_source(source):
        return all(value is None or source.get(key)==value for key,value in source_filters.items())
    candidates={}
    for entry in read_run_index()['runs']:
        if entry.get('plan_hash')==plan['plan_hash'] and matches_source(entry.get('source',{})):
            candidates[Path(entry['directory']).expanduser().resolve()]=True
    # Recognize current-format runs copied into this workspace as well.
    # Search only bounded, conventional local locations.
    roots={Path.cwd().resolve(),Path(plan_path).expanduser().resolve().parent}
    for root in roots:
        manifests=[root/'run.json']
        manifests.extend(root.glob('*/run.json'))
        manifests.extend((root/'capture-runs').glob('*/run.json'))
        for manifest in manifests:
            if not manifest.is_file():
                continue
            try:
                identity=p.read_json(manifest)
            except (OSError,ValueError,TypeError):
                continue
            if isinstance(identity,dict) and identity.get('plan_hash')==plan['plan_hash']:
                candidates.setdefault(manifest.parent.resolve(),False)
    compatible=[]; blocked=[]
    for directory in sorted(candidates,key=str):
        try:
            run=Run.resume(directory)
            if run.plan['plan_hash']!=plan['plan_hash'] or not matches_source(run.state['source']):
                continue
            differences=[key for key,value in (settings or {}).items()
                         if value is not None and run.state['settings'].get(key)!=value]
            if differences:
                raise CompatibilityError('Capture settings differ: '+', '.join(differences))
            compatible.append(run)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            blocked.append({'directory':str(directory),'problem':str(exc)})
    return compatible,blocked


def digest_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def wait_fresh(path, issued_ns, timeout=90, stable_seconds=2, poll=.25):
    """Require a regular file absent before issuance, new timestamps, and stability."""
    deadline, previous, since = time.monotonic()+timeout, None, None
    while time.monotonic() < deadline:
        try:
            info = path.lstat()
        except FileNotFoundError:
            time.sleep(poll)
            continue
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise CaptureError('Export is not a regular file: '+str(path))
        if info.st_mtime_ns < issued_ns or info.st_ctime_ns < issued_ns:
            raise CaptureError('Export timestamps precede its command: '+str(path))
        current = info.st_size, info.st_mtime_ns, info.st_ctime_ns
        now = time.monotonic()
        if current == previous and info.st_size:
            if since is not None and now-since >= stable_seconds:
                return info
        else:
            since = now
        previous = current
        time.sleep(poll)
    raise CaptureError('Timed out waiting for a new stable export: '+str(path))


def padded(core, overlap):
    return {**p.box(core), 'xmax':core['xmax']+overlap, 'zmax':core['zmax']+overlap}


def run_units(plan, overlap):
    if type(overlap) is not int or not 1 <= overlap <= 16:
        raise CaptureError('Overlap must be 1..16 blocks')
    cores=[]
    for original in plan['cuboids']:
        pending=[p.box(original)]
        while pending:
            core=pending.pop()
            capture=padded(core,overlap)
            if (all(capture[a+'max']-capture[a+'min']+1 <= p.MAX_SIDE for a in p.AXES)
                    and p.volume(capture) <= p.MAX_VOLUME):
                cores.append(core)
                continue
            axis=max(('x','z'),key=lambda a:core[a+'max']-core[a+'min'])
            if core[axis+'max']==core[axis+'min']:
                raise CaptureError('Cannot fit padded capture within selection limits')
            mid=(core[axis+'min']+core[axis+'max'])//2
            pending.extend([{**core,axis+'max':mid},{**core,axis+'min':mid+1}])
    cores.sort(key=lambda b:(b['zmin'],b['xmin'],b['ymin']))
    p.verify_disjoint(cores,'run cores')
    if p.difference(plan['requests'],cores):
        raise CaptureError('Run cores do not cover the requested footprint')
    return [{'id':f's{index:05d}','index':index,'core':core,'capture':padded(core,overlap)}
            for index,core in enumerate(cores,1)]


def overlap_pairs(units):
    pairs=[]
    for index,left in enumerate(units):
        for right in units[index+1:]:
            bounds=p.intersection(left['capture'],right['capture'])
            if bounds:
                pairs.append({'id':left['id']+'_'+right['id'],'left':left['id'],
                              'right':right['id'],'bounds':bounds})
    return pairs


def records(data, bounds):
    """Yield world positions and canonical solid/fluid/decor tokens, preserving multiplicity."""
    palette=data.get('BlockCodes',{})
    for indices,ids,decor in (('Indices','BlockIds',False),('DecorIndices','DecorIds',True)):
        for index,block_id in zip(data.get(indices,[]),data.get(ids,[])):
            # BlockSchematic packs decor face/subposition above the low 24-bit ID.
            code=palette.get(str(block_id & 0xffffff if decor else block_id))
            if code is None and block_id==0:
                code='game:air'
            if not isinstance(code,str):
                raise CaptureError(f'Missing block code for {block_id}')
            if code in ('air','game:air'):
                continue
            position=(bounds['xmin']+(index&1023),bounds['ymin']+(index>>20),
                      bounds['zmin']+((index>>10)&1023))
            yield position, ('decor',code,str(block_id>>24)) if decor else ('block',code)


def canonical_map(data, bounds, region, columns=None):
    result=defaultdict(list)
    for position,token in records(data,bounds):
        x,y,z=position
        if (region['xmin'] <= x <= region['xmax'] and region['ymin'] <= y <= region['ymax']
                and region['zmin'] <= z <= region['zmax'] and (columns is None or (x,z) in columns)):
            result[position].append(token)
    return {position:tuple(sorted(tokens)) for position,tokens in result.items()}


def fingerprint(data, bounds, region):
    content=canonical_map(data,bounds,region)
    return fingerprint_map(content,region)


def fingerprint_map(content,region):
    return {'sha256':p.digest({'bounds':region,'records':sorted(content.items())}),
            'positions':len(content),'records':sum(len(tokens) for tokens in content.values())}


def section_fingerprints(data,bounds,pairs):
    """Decode each source record once for all incident overlap strips."""
    maps={pair['id']:defaultdict(list) for pair in pairs}
    by_z=defaultdict(list)
    for pair in pairs:
        for z in range(pair['bounds']['zmin'],pair['bounds']['zmax']+1):
            by_z[z].append(pair)
    for position,token in records(data,bounds):
        x,y,z=position
        for pair in by_z.get(z,()):
            region=pair['bounds']
            if region['xmin']<=x<=region['xmax'] and region['ymin']<=y<=region['ymax']:
                maps[pair['id']][position].append(token)
    return {pair['id']:fingerprint_map({pos:tuple(sorted(tokens)) for pos,tokens in maps[pair['id']].items()},pair['bounds'])
            for pair in pairs}


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


def missing_anchor_columns(data, bounds, anchor):
    present={(x,z) for (x,y,z),token in records(data,bounds) if y==anchor}
    return {(x,z) for x in range(bounds['xmin'],bounds['xmax']+1)
            for z in range(bounds['zmin'],bounds['zmax']+1)}-present


def columns_needing_confirmation(data,bounds,anchor):
    """Check the required surface and every intersecting 32-high chunk stratum."""
    anchor_present=set(); strata=set()
    for (x,y,z),token in records(data,bounds):
        if bounds['ymin'] <= y <= anchor:
            strata.add((x,z,y//32))
            if y==anchor:
                anchor_present.add((x,z))
    bands=range(bounds['ymin']//32,anchor//32+1)
    return {(x,z) for x in range(bounds['xmin'],bounds['xmax']+1)
            for z in range(bounds['zmin'],bounds['zmax']+1)
            if (x,z) not in anchor_present or any((x,z,band) not in strata for band in bands)}


def mismatches(left, right, limit=20):
    different=sorted(position for position in left.keys()|right.keys() if left.get(position,())!=right.get(position,()))
    return {'positions':len(different),'samples':[
        {'position':position,'left':left.get(position,()),'right':right.get(position,())}
        for position in different[:limit]]}


class Run:
    def __init__(self, directory, plan, state, console=None, activity=None, wait=wait_fresh):
        self.directory,self.plan,self.state=Path(directory),plan,state
        self.export_dir=Path(state['source']['export_dir'])
        self.path=self.directory/(state['run_id']+'-progress.json')
        self.console,self.wait=console,wait
        self.verbose=False
        self.activity=activity or (lambda description,unit=None:None)
        self.units={unit['id']:unit for unit in state['units']}
        self.pairs=overlap_pairs(state['units'])
        self.incident=defaultdict(list)
        for pair in self.pairs:
            for name in (pair['left'],pair['right']):
                self.incident[name].append(pair)

    @classmethod
    def create(cls, plan, plan_path, export_dir, server_log, player, directory=None,
               overlap=DEFAULT_OVERLAP, coverage_y=DEFAULT_COVERAGE_Y,
               max_attempts=DEFAULT_MAX_ATTEMPTS, probe_ymin=DEFAULT_PROBE_YMIN, **kwargs):
        p.verify_plan(plan)
        if type(max_attempts) is not int or max_attempts < 1:
            raise CaptureError('Maximum attempts must be positive')
        if any(type(value) is not int or value < 0 for value in (coverage_y, probe_ymin)):
            raise CaptureError('Coverage Y and probe Y minimum must be nonnegative integers')
        run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:12]
        directory=Path(directory) if directory else Path.cwd()/'capture-runs'/run_id
        if os.path.lexists(directory) and not is_empty_directory(directory):
            raise CaptureError('Run directory already exists and is not empty; choose an empty/new directory or use --resume: '+str(directory))
        units=run_units(plan,overlap)
        state={'schema':SCHEMA,'operational_model':OPERATIONAL_MODEL,'run_id':run_id,'prefix':'v12_capture_'+run_id,
               'plan_hash':plan['plan_hash'],'geometry_hash':plan['geometry_hash'],
               'plan_source':str(Path(plan_path).resolve()),'created_utc':datetime.now(timezone.utc).isoformat(),
               'source':{'export_dir':str(Path(export_dir).resolve()),'server_log':str(Path(server_log).resolve()),'player':player},
               'settings':{'overlap':overlap,'coverage_y':coverage_y,'max_attempts':max_attempts,'probe_ymin':probe_ymin},
               'units':units,'sections':{},'finished':False,'collection_finished':False,
               'attempt_limit':max_attempts,
               'coverage_complete':None,'overlap_complete':None,'files_verified':None,'audit_status':'not_run'}
        definition=p.digest({key:state[key] for key in ('operational_model','run_id','prefix','plan_hash','geometry_hash','source','settings','units')})
        state['definition_hash']=definition
        directory.mkdir(parents=True,exist_ok=True)
        if not is_empty_directory(directory):
            raise CaptureError('Run directory is no longer empty; refusing to initialize it: '+str(directory))
        # Claim initialization before writing any other files. Concurrent
        # creators cannot replace each other's run identity or frozen plan.
        identity={'schema':SCHEMA,'run_id':run_id,'plan_hash':plan['plan_hash'],
                  'operational_model':OPERATIONAL_MODEL,
                  'progress_file':run_id+'-progress.json','definition_hash':definition}
        with (directory/'run.json').open('x') as stream:
            stream.write(json.dumps(identity,indent=2)+'\n')
        with (directory/'plan.json').open('x') as stream:
            stream.write(json.dumps(plan,indent=2)+'\n')
        run=cls(directory,plan,state,**kwargs)
        run.save()
        run.preview()
        return run

    @classmethod
    def resume(cls,directory,export_dir=None,server_log=None,player=None,**kwargs):
        directory=Path(directory).resolve()
        if not (directory/'run.json').is_file():
            raise CompatibilityError('Missing run.json; this is not a capture run directory')
        identity=p.read_json(directory/'run.json')
        if identity.get('schema')!=SCHEMA or not re.fullmatch(r'\d{8}T\d{6}Z_[0-9a-f]{12}',identity.get('run_id','')):
            raise CaptureError('Not a version 3 isolated capture run')
        filename=identity['run_id']+'-progress.json'
        if identity.get('progress_file')!=filename:
            raise CaptureError('Invalid progress filename')
        state=p.read_json(directory/filename)
        if state.get('operational_model')!=OPERATIONAL_MODEL or identity.get('operational_model')!=OPERATIONAL_MODEL:
            raise CompatibilityError('Operational model changed or is unknown. Preserve this run and review compatibility before reuse; no outputs were removed.')
        plan=p.load_plan(directory/'plan.json')
        if (state.get('schema')!=SCHEMA or state.get('run_id')!=identity['run_id']
                or state.get('plan_hash')!=plan['plan_hash'] or identity.get('plan_hash')!=plan['plan_hash']):
            raise CaptureError('Run identity or frozen plan hash does not match')
        if state['units']!=run_units(plan,state['settings']['overlap']):
            raise CaptureError('Saved capture bounds differ from the frozen plan')
        definition=p.digest({key:state[key] for key in ('operational_model','run_id','prefix','plan_hash','geometry_hash','source','settings','units')})
        if definition!=identity.get('definition_hash') or definition!=state.get('definition_hash'):
            raise CaptureError('Immutable run source/settings changed')
        for key,value in (('export_dir',export_dir),('server_log',server_log)):
            if value is not None and str(Path(value).resolve())!=state['source'][key]:
                raise CaptureError('Resume belongs to a different '+key)
        if player is not None and player!=state['source']['player']:
            raise CaptureError('Resume belongs to a different player')
        return cls(directory,plan,state,**kwargs)

    def save(self):
        self.state['collection_finished']=all(self.state['sections'].get(name,{}).get('status')=='complete' for name in self.units)
        self.state['finished']=bool(self.state['collection_finished'] and self.state.get('coverage_complete')
                                    and self.state.get('overlap_complete') and self.state.get('files_verified')
                                    and self.state.get('audit_status')=='complete')
        self.state['updated_utc']=datetime.now(timezone.utc).isoformat()
        atomic_json(self.path,self.state)

    @contextmanager
    def locked(self):
        """A run has one controller/writer, including while producing reports."""
        with (self.directory/'controller.lock').open('a+') as stream:
            try:
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise CaptureError('Another controller is using this run; stop it before resuming or auditing')
            try:
                stream.seek(0); stream.truncate(); stream.write(str(os.getpid())+'\n'); stream.flush()
                # The checkpoint may have advanced between opening the run
                # and acquiring its lock. Keep existing observer references.
                latest=type(self).resume(self.directory)
                self.state.clear(); self.state.update(latest.state)
                yield
            finally:
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)

    def preview(self):
        display={**self.plan,'cuboids':[{**unit['capture'],'name':unit['id']} for unit in self.state['units']]}
        path=self.directory/(self.state['run_id']+'-preview.svg')
        p.render_preview(display,path)
        text=path.read_text().replace('v12 capture footprint and export mosaic',
            'Capture run '+self.state['run_id']+' (overlapping raw selections)')
        path.write_text(text)

    def load_area(self,bounds):
        player=self.state['source']['player']
        force=self.console.command(f"/executeas {player} /chunk forceload {bounds['xmin']} {bounds['zmin']} {bounds['xmax']} {bounds['zmax']}",r'will force load \d+ chunk columns')
        self.console.command(f"/tp {player} {(bounds['xmin']+bounds['xmax'])//2} {bounds['ymax']-2} {(bounds['zmin']+bounds['zmax'])//2}",r'teleported')
        deadline=time.monotonic()+90
        while True:
            queues=self.console.command(f'/executeas {player} /chunk cit',r'Chunk in genQ: (True|False), chunkReq in Q: (True|False)')
            if 'Chunk in genQ: False, chunkReq in Q: False' in queues:
                return {'force_loaded':True,'queues_clear':True,'preload_response':force,'queue_response':queues}
            if time.monotonic()>=deadline:
                raise CaptureError('Chunk queues did not settle')
            time.sleep(1)

    def capture(self,unit,bounds,purpose,attempt):
        name=f"{self.state['prefix']}_{unit['id']}_{purpose}_{uuid.uuid4().hex}"
        path=self.export_dir/(name+'.json')
        if os.path.lexists(path):
            raise CaptureError('Refusing an existing export filename: '+str(path))
        loaded=self.load_area(bounds)
        player=self.state['source']['player']
        self.console.command(f"/executeas {player} /we select {bounds['xmin']} {bounds['ymin']} {bounds['zmin']} {bounds['xmax']} {bounds['ymax']} {bounds['zmax']}",r'Start and end position marked')
        if os.path.lexists(path):
            raise CaptureError('Export name appeared before its command: '+str(path))
        issued=time.time_ns()
        ref={'run_id':self.state['run_id'],'unit':unit['id'],'purpose':purpose,'attempt':attempt,
             'file':path.name,'requested':p.box(bounds),'command_issued_ns':issued,'load_check':loaded}
        record=self.state['sections'][unit['id']]
        record.setdefault('issued_exports',[]).append(ref)
        self.save()
        self.console.command(f'/executeas {player} /we export {name}',r'\d+ blocks.*exported',120)
        self.activity('Waiting for fresh stable export: '+name,unit)
        self.wait(path,issued)
        info=path.lstat()
        if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
                or info.st_mtime_ns < issued or info.st_ctime_ns < issued):
            raise CaptureError('Export freshness check failed: '+path.name)
        ref.update(sha256=digest_file(path),size=info.st_size,mtime_ns=info.st_mtime_ns,ctime_ns=info.st_ctime_ns)
        self.save()
        return ref

    def read_ref(self,ref):
        name=ref.get('file','')
        if (Path(name).name!=name or not name.startswith(self.state['prefix']+'_')
                or ref.get('run_id')!=self.state['run_id']):
            raise CaptureError('Export belongs to another run: '+name)
        if ref.get('unit') not in self.units or not ref.get('load_check',{}).get('queues_clear') or not ref['load_check'].get('force_loaded'):
            raise CaptureError('Export lacks run/load provenance: '+name)
        issued=self.state['sections'].get(ref['unit'],{}).get('issued_exports',[])
        if not any(item==ref for item in issued):
            raise CaptureError('Export was not issued by this run: '+name)
        path=self.export_dir/name
        info=path.lstat()
        if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
                or info.st_mtime_ns < ref['command_issued_ns'] or info.st_ctime_ns < ref['command_issued_ns']
                or info.st_size!=ref['size'] or info.st_mtime_ns!=ref['mtime_ns'] or info.st_ctime_ns!=ref['ctime_ns']):
            raise CaptureError('Export changed or has stale timestamps: '+name)
        payload=path.read_bytes()
        if hashlib.sha256(payload).hexdigest()!=ref['sha256']:
            raise CaptureError('Export hash changed: '+name)
        evidence=p.inspect_export(path,ref['requested'],self.plan['world_center'])
        after=path.lstat()
        if ((evidence.get('sha256') is not None and evidence['sha256']!=ref['sha256'])
                or (after.st_size,after.st_mtime_ns,after.st_ctime_ns)!=(info.st_size,info.st_mtime_ns,info.st_ctime_ns)):
            raise CaptureError('Export changed while being validated: '+name)
        if evidence['problems'] and evidence['problems']!=['Empty sparse export cannot establish coverage']:
            raise CaptureError(name+': '+'; '.join(evidence['problems']))
        data=json.loads(payload)
        version=data.get('GameVersion')
        if not isinstance(version,str) or not version:
            raise CompatibilityError('Export game version is unknown; compatibility needs review')
        previous=self.state.get('observed_game_version')
        if previous is not None and version!=previous:
            raise CompatibilityError(f'Game version changed from {previous} to {version}; compatibility needs review before this run can continue')
        if previous is None:
            self.state['observed_game_version']=version
            self.save()
        return data,evidence

    def anchor(self,unit):
        return min(unit['capture']['ymax'],max(unit['capture']['ymin'],self.state['settings']['coverage_y']))

    def check_probe(self,unit,selected,data,probe,pending):
        if probe['purpose']!='depth' or probe['unit']!=unit['id'] or probe['attempt']!=selected['attempt']:
            raise CaptureError('Probe does not belong to the selected whole-section attempt')
        fresh,proof=self.read_ref(probe)
        bounds=probe['requested']
        if (proof['problems'] or proof['column_presence']['missing_columns']
                or bounds['ymin']!=self.state['settings']['probe_ymin']
                or bounds['ymax']!=unit['capture']['ymax'] or bounds['ymin']>=unit['capture']['ymin']
                or bounds['xmin']%32 or bounds['zmin']%32
                or bounds['xmax']-bounds['xmin']!=31 or bounds['zmax']-bounds['zmin']!=31):
            raise CaptureError('Deeper probe does not establish loaded column presence')
        affected={(x,z) for x,z in pending if bounds['xmin']<=x<=bounds['xmax'] and bounds['zmin']<=z<=bounds['zmax']}
        left=canonical_map(data,unit['capture'],unit['capture'],affected)
        right=canonical_map(fresh,bounds,unit['capture'],affected)
        if left!=right:
            raise CaptureError('Fresh deeper data differs from the main section; retry the whole section: '+json.dumps(mismatches(left,right)))
        return affected

    def check_section(self,unit,selected,probes):
        if selected['requested']!=unit['capture'] or selected['purpose']!='section' or selected['unit']!=unit['id']:
            raise CaptureError('Selected export does not match the run selection')
        data,evidence=self.read_ref(selected)
        missing=columns_needing_confirmation(data,unit['capture'],self.anchor(unit))
        pending=set(missing)
        for probe in probes:
            pending-=self.check_probe(unit,selected,data,probe,pending)
        if pending:
            raise CaptureError(f'{len(pending)} columns lack evidence through Y={self.anchor(unit)}')
        return data,evidence,{'anchor_y':self.anchor(unit),'columns':(unit['capture']['xmax']-unit['capture']['xmin']+1)*(unit['capture']['zmax']-unit['capture']['zmin']+1),
                              'direct_coverage_columns':(unit['capture']['xmax']-unit['capture']['xmin']+1)*(unit['capture']['zmax']-unit['capture']['zmin']+1)-len(missing),
                              'corroborated_columns':len(missing),'unverified_columns':0,
                              'ceiling_records':evidence['ceiling_blocks'],
                              'basis':'Direct anchor record and a record in every required vertical chunk stratum, or a matching fresh loaded deeper-column export.'}

    def capture_section(self,unit):
        record=self.state['sections'].setdefault(unit['id'],{'status':'pending','attempts':[]})
        candidate=record.get('candidate')
        if candidate:
            attempt=candidate['attempt']
            if not record['attempts'] or record['attempts'][-1]['id']!=attempt or record['attempts'][-1]['status']!='capturing':
                raise CaptureError('Partial candidate is not the active attempt')
        else:
            if len(record['attempts'])>=self.state['attempt_limit']:
                raise CaptureError('Retry limit reached for '+unit['id']+'; run remains incomplete')
            attempt=uuid.uuid4().hex
            record['attempts'].append({'id':attempt,'status':'capturing'})
        record.update(status='waiting',selected=None,fingerprints={})
        self.state.update(finished=False,audit_status='not_run',coverage_complete=None,overlap_complete=None,files_verified=None)
        self.save()
        try:
            if candidate:
                selected=candidate['selected']; probes=list(candidate['probes'])
                print(f"Reusing immutable candidate for {unit['id']} and {len(probes)} same-attempt probes.",flush=True)
            else:
                selected=self.capture(unit,unit['capture'],'section',attempt)
                probes=[]
            if (selected['requested']!=unit['capture'] or selected['purpose']!='section'
                    or selected['unit']!=unit['id'] or selected['attempt']!=attempt):
                raise CaptureError('Partial primary candidate does not match the active section attempt')
            data,evidence=self.read_ref(selected)
            if not data.get('Indices') and len(record['attempts'])==1:
                raise CaptureError('Zero-block export; retry the entire loaded selection')
            record['candidate']={'attempt':attempt,'selected':selected,'probes':probes}
            self.save()
            missing=columns_needing_confirmation(data,unit['capture'],self.anchor(unit))
            pending=set(missing)
            for probe in probes:
                pending-=self.check_probe(unit,selected,data,probe,pending)
            if pending:
                rows=defaultdict(list)
                for x,z in pending:
                    rows[z].append((x,x))
                boxes=p.row_boxes({z:p.union_intervals(xs) for z,xs in rows.items()},unit['capture']['ymin'],unit['capture']['ymax'])
                if self.state['settings']['probe_ymin']>=unit['capture']['ymin']:
                    raise CaptureError('Probe Y minimum must be below the selection floor')
                planned=list(chunk_bounds(boxes,self.state['settings']['probe_ymin'],unit['capture']['ymax']))
                print(f"{unit['id']}: {len(pending)} columns need deeper confirmation; {len(planned)} probes.",flush=True)
                for index,bounds in enumerate(planned,1):
                    self.activity(f'Deeper confirmation {index}/{len(planned)}',unit)
                    ref=self.capture(unit,bounds,'depth',attempt)
                    pending-=self.check_probe(unit,selected,data,ref,pending)
                    probes.append(ref)
                    record['candidate']['probes']=probes
                    record['candidate']['unverified_columns']=len(pending)
                    self.save()
            data,evidence,coverage=self.check_section(unit,selected,probes)
            strips=section_fingerprints(data,unit['capture'],self.incident[unit['id']])
            record.update(status='complete',selected=selected,probes=probes,coverage=coverage,fingerprints=strips)
            record.pop('error',None)
            record.pop('candidate',None)
            record['attempts'][-1].update(status='validated',selected=selected,probes=probes)
            self.save()
            return True
        except CompatibilityError as exc:
            record.update(status='incomplete',error=str(exc),selected=None)
            record['attempts'][-1].update(status='review_required',error=str(exc))
            self.save()
            raise
        except (CaptureError,OSError,ValueError,TimeoutError) as exc:
            record.pop('candidate',None)
            record.update(status='incomplete',error=str(exc),selected=None)
            record['attempts'][-1].update(status='failed',error=str(exc))
            self.save()
            print(f"{unit['id']}: {exc}",flush=True)
            return False

    def seams(self):
        checks=[]
        for pair in self.pairs:
            left=self.state['sections'].get(pair['left'],{})
            right=self.state['sections'].get(pair['right'],{})
            a=left.get('fingerprints',{}).get(pair['id'])
            b=right.get('fingerprints',{}).get(pair['id'])
            status='pending' if not a or not b or not left.get('selected') or not right.get('selected') else ('match' if a['sha256']==b['sha256'] else 'mismatch')
            checks.append({**pair,'status':status,'left_file':(left.get('selected') or {}).get('file'),
                           'right_file':(right.get('selected') or {}).get('file'),'left':pair['left'],'right':pair['right'],
                           'left_sha256':(left.get('selected') or {}).get('sha256'),
                           'right_sha256':(right.get('selected') or {}).get('sha256'),
                           'left_fingerprint':a,'right_fingerprint':b})
        return checks

    def seam_detail(self,pair):
        left=self.state['sections'][pair['left']]['selected']
        right=self.state['sections'][pair['right']]['selected']
        a,_=self.read_ref(left); b,_=self.read_ref(right)
        return {**pair,'difference':mismatches(canonical_map(a,left['requested'],pair['bounds']),
                                               canonical_map(b,right['requested'],pair['bounds']))}

    def audit(self):
        self.state.update(audit_status='running',coverage_complete=None,overlap_complete=None,files_verified=None,finished=False)
        self.save()
        covered,formal,files,errors,warnings=[],[],[],[],[]
        for index,unit in enumerate(self.state['units'],1):
            self.activity(f'Auditing selection {index}/{len(self.units)}',unit)
            if index==1 or index%10==0 or index==len(self.units):
                print(f"Auditing run selection {index}/{len(self.units)}: {unit['id']}",flush=True)
            record=self.state['sections'].get(unit['id'],{})
            try:
                if not record.get('selected'):
                    raise CaptureError('No validated whole-section export selected')
                data,evidence,coverage=self.check_section(unit,record['selected'],record.get('probes',[]))
                record['fingerprints']=section_fingerprints(data,unit['capture'],self.incident[unit['id']])
                record.update(status='complete',coverage=coverage)
                record.pop('error',None)
                if coverage['ceiling_records']:
                    warnings.append(f"{unit['id']}: {coverage['ceiling_records']} records reach requested Y ceiling {unit['capture']['ymax']}")
                covered.append(unit['core'])
                if evidence['covered']:
                    intersection=p.intersection(evidence['covered'],unit['core'])
                    if intersection:
                        formal.extend(p.difference([intersection],evidence['column_presence']['missing_regions']))
                files.extend([record['selected'],*record.get('probes',[])])
            except (CaptureError,OSError,ValueError,KeyError) as exc:
                errors.append({'unit':unit['id'],'problem':str(exc),'selected':record.get('selected'),
                               'candidate':record.get('candidate')})
                record.update(status='incomplete',error=str(exc),fingerprints={})
        checks=self.seams()
        checks=[self.seam_detail(check) if check['status']=='mismatch' else check for check in checks]
        required=[{**bounds,'ymax':max(bounds['ymin'],min(bounds['ymax'],self.state['settings']['coverage_y']))} for bounds in self.plan['requests']]
        gaps=p.difference(required,covered)
        formal_gaps=p.difference(self.plan['requests'],formal)
        overlaps_ok=all(check['status']=='match' for check in checks)
        self.state.update(coverage_complete=not gaps,column_coverage_complete=not gaps,overlap_complete=overlaps_ok,
                          files_verified=not errors)
        report={'schema':SCHEMA,'run_id':self.state['run_id'],'plan_hash':self.plan['plan_hash'],
                'operational_model':OPERATIONAL_MODEL,'definition_hash':self.state['definition_hash'],
                'observed_game_version':self.state.get('observed_game_version'),
                'source':self.state['source'],'settings':self.state['settings'],'units':self.state['units'],
                'geometry_hash':self.plan['geometry_hash'],'required_through_y':self.state['settings']['coverage_y'],
                'coverage_complete':not gaps,'column_coverage_complete':not gaps,'gaps':gaps,
                'full_3d_coverage_complete':not formal_gaps,'full_3d_gaps':formal_gaps,
                'overlap_complete':overlaps_ok,'overlap_checks':checks,'file_errors':errors,'files':files,
                'warnings':warnings,
                'evidence_limits':'Loaded-export evidence and matching sparse block-code multisets; not an atomic world snapshot. Omitted upper air stays formally unverified.'}
        self.state['full_3d_coverage_complete']=not formal_gaps
        report['finished']=bool(not gaps and overlaps_ok and not errors and all(
            self.state['sections'].get(name,{}).get('status')=='complete' for name in self.units))
        self.preview()
        atomic_json(self.directory/(self.state['run_id']+'-audit.json'),report)
        self.state['audit_status']='complete'
        if report['finished']:
            self.state.pop('last_error',None)
        self.save()
        print(f"Run {self.state['run_id']}: finished={self.state['finished']}; "
              f"required coverage={not gaps}; overlaps={sum(c['status']=='match' for c in checks)}/{len(checks)}; "
              f"file errors={len(errors)}.",flush=True)
        for warning in warnings:
            print('Height warning: '+warning,flush=True)
        return report

    def collect(self,confirm=False):
        self.state.update(finished=False,audit_status='not_run',files_verified=None,
                          coverage_complete=None,column_coverage_complete=None,overlap_complete=None)
        self.save()
        queue=deque(self.units)
        print(f"Checking {len(self.units)} selections from compatible run {self.state['run_id']} for reuse.",flush=True)
        last_progress=time.monotonic()
        while True:
            while queue:
                name=queue.popleft(); unit=self.units[name]
                record=self.state['sections'].get(name,{})
                self.activity('Checking run selection',unit)
                if record.get('selected'):
                    self.activity('Checking saved export: '+record['selected']['file'],unit)
                    now=time.monotonic()
                    if self.verbose or unit['index']==1 or unit['index']%10==0 or now-last_progress>=5:
                        print(f"Checking saved run selection {unit['index']}/{len(self.units)}: {name}",flush=True)
                        last_progress=now
                    try:
                        self.check_section(unit,record['selected'],record.get('probes',[]))
                        continue
                    except CompatibilityError:
                        raise
                    except (CaptureError,OSError,ValueError) as exc:
                        print(f"Cannot reuse {name}: {exc}. Keeping its old attempt and recapturing the whole selection.",flush=True)
                        record.update(selected=None,status='incomplete')
                print(f"Selection {unit['index']}/{len(self.units)}: {unit['core']}",flush=True)
                if confirm:
                    if input('Return to capture; q to stop: ').strip().lower()=='q':
                        return 130
                if not self.capture_section(unit):
                    queue.appendleft(name)
                    continue
                conflicts=[pair for pair in self.seams() if pair['status']=='mismatch' and name in (pair['left'],pair['right'])]
                for pair in conflicts:
                    other=pair['right'] if pair['left']==name else pair['left']
                    self.state.setdefault('seam_failures',[]).append(self.seam_detail(pair))
                    print(f"Overlap mismatch {pair['id']}; requesting a fresh whole export of {other}.",flush=True)
                    self.state['sections'][other].update(selected=None,status='incomplete')
                    if other not in queue:
                        queue.appendleft(other)
                    self.save()
            report=self.audit()
            if report['finished']:
                return 0
            affected={error['unit'] for error in report['file_errors']}
            for pair in report['overlap_checks']:
                if pair['status']=='mismatch':
                    affected.add(pair['left'])
            if not affected:
                raise CaptureError('Run failed coverage or overlap validation')
            for name in sorted(affected):
                self.state['sections'][name].update(selected=None,status='incomplete')
                queue.append(name)
            self.save()
