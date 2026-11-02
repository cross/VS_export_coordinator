"""Identify current terrain/evidence files and unused attempts from a run manifest."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import time

import capture_run as r
import v12_capture_plan as p


def run_filename(run,name):
    return (isinstance(name,str) and Path(name).name==name
            and name.startswith(run.state['prefix']+'_') and name.endswith('.json'))


def inventory(run):
    """Use a locked checkpoint; recheck hashes, preserving candidates and failures."""
    if not run.export_dir.is_dir():
        raise r.CaptureError('Saved WorldEdit export directory is unavailable: '+str(run.export_dir))
    checkpoint=run.path.read_bytes()
    if p.digest(json.loads(checkpoint))!=p.digest(run.state):
        raise r.CaptureError('Checkpoint changed; reopen the run before listing exports')
    primary=[]; supporting=[]; pending=[]; unresolved=[]; problems=[]
    protected=set(); issued={}; validated=[]
    selected_count=sum(bool(run.state['sections'].get(name,{}).get('selected')) for name in run.units)
    if selected_count:
        run.announce(f'Checking file hashes for {selected_count} selected primary exports and their probes; no block-record reparse or server commands.')
    else:
        run.announce('No selected exports to verify; listing pending and unused files.')

    def entry(ref,unit):
        name=ref.get('file')
        if not run_filename(run,name):
            raise r.CaptureError('Invalid export filename in run checkpoint: '+str(name))
        return {**ref,'selection':unit['id'],'path':str(run.export_dir/name),
                'exists':os.path.lexists(run.export_dir/name)}

    checked=0; last_notice=time.monotonic()
    for unit in run.state['units']:
        record=run.state['sections'].get(unit['id'],{})
        for ref in record.get('issued_exports',[]):
            if run_filename(run,ref.get('file')):
                issued[ref['file']]=ref
        if record.get('candidate'):
            candidate=record['candidate']
            refs=[candidate['selected'],*candidate['probes']]
            for ref in refs:
                item=entry(ref,unit)
                protected.add(item['file']); pending.append(item)
        selected=record.get('selected')
        if not selected:
            if record.get('status')=='complete':
                problems.append({'unit':unit['id'],'problem':'Selection marked complete without a selected primary export'})
            continue
        refs=[selected,*record.get('probes',[])]
        entries=[entry(ref,unit) for ref in refs]
        protected.update(item['file'] for item in entries)
        checked+=1; now=time.monotonic()
        if run.verbose or checked==1 or checked%10==0 or checked==selected_count or now-last_notice>=5:
            print(f'Checking selected export hashes {checked}/{selected_count}: {unit["id"]}',flush=True)
            last_notice=now
        try:
            if record.get('status')!='complete':
                raise r.CaptureError('Selection is not currently marked complete; resume or audit it before using its exports')
            if (selected.get('unit')!=unit['id'] or selected.get('purpose')!='section'
                    or selected.get('requested')!=unit['capture']):
                raise r.CaptureError('Selected primary does not match its run selection')
            if not any(attempt.get('id')==selected.get('attempt') and attempt.get('status')=='validated'
                       and attempt.get('selected')==selected and attempt.get('probes')==record.get('probes',[])
                       for attempt in record.get('attempts',[])):
                raise r.CaptureError('Selected files are not bound to a validated whole-section attempt')
            for ref in refs:
                if ref.get('unit')!=unit['id'] or ref.get('attempt')!=selected['attempt']:
                    raise r.CaptureError('Supporting file belongs to another selection or attempt')
                if ref is not selected and ref.get('purpose')!='depth':
                    raise r.CaptureError('Supporting file is not a deeper confirmation probe')
                payload,_=run.verified_ref_bytes(ref)
                del payload
            primary.append({**entries[0],'core':unit['core'],'capture':unit['capture']})
            supporting.extend(entries[1:]); validated.append(unit['id'])
        except (r.CaptureError,OSError,ValueError,KeyError,TypeError) as exc:
            problem={'unit':unit['id'],'problem':str(exc)}
            problems.append(problem); unresolved.append({**problem,'files':entries})

    unused=[]; unrecognized=[]
    for path in sorted(run.export_dir.iterdir()):
        name=path.name
        if not run_filename(run,name) or name in protected:
            continue
        info=path.lstat()
        item={'file':name,'path':str(path),'size':info.st_size}
        if name in issued and stat.S_ISREG(info.st_mode) and not path.is_symlink():
            unused.append({**issued[name],**item})
        else:
            unrecognized.append({**item,'reason':'Not an issued regular export file; not a cleanup candidate'})
    if hashlib.sha256(run.path.read_bytes()).digest()!=hashlib.sha256(checkpoint).digest():
        raise r.CaptureError('Checkpoint changed during the inventory; rerun --list-exports')
    return {'schema':'coast-capture-export-inventory/1','run_id':run.state['run_id'],
            'plan_hash':run.plan['plan_hash'],'geometry_hash':run.plan['geometry_hash'],
            'operational_model':run.state['operational_model'],'definition_hash':run.state['definition_hash'],
            'generated_utc':datetime.now(timezone.utc).isoformat(),
            'progress_file':str(run.path),'progress_sha256':hashlib.sha256(checkpoint).hexdigest(),
            'export_dir':str(run.export_dir),'world_center':run.plan['world_center'],
            'settings':run.state['settings'],'planned_selections':len(run.units),
            'validated_selections':len(validated),'checkpoint_finished':bool(run.state.get('finished')),
            'run_certified':bool(run.state.get('finished') and run.state.get('audit_status')=='complete'
                                 and len(validated)==len(run.units) and not problems),
            'primary':primary,'supporting_probes':supporting,'pending_candidates':pending,
            'unresolved_selections':unresolved,'unused_issued_files':unused,
            'unrecognized_run_files':unrecognized,'protected_files':sorted(protected),
            'issued_without_file':sorted(name for name in issued if not os.path.lexists(run.export_dir/name)),
            'problems':problems,
            'basis':'Manifest-selected completed attempts with freshly checked provenance, timestamps, and SHA256. No fresh block/coverage/overlap audit; pending candidates are excluded.',
            'usage':'Use primary files for terrain; crop to core bounds. Supporting probes preserve validation evidence and must not be merged as additional terrain. Unused means unselected, not permission to delete. Other run IDs are not inventoried.'}


def write_inventory(run):
    report=inventory(run)
    prefix=run.state['run_id']
    lists={'primary':sorted(item['file'] for item in report['primary']),
           'required':sorted({item['file'] for item in [*report['primary'],*report['supporting_probes']]}),
           'unused':sorted(item['file'] for item in report['unused_issued_files'])}
    report['file_lists']={}
    for kind,names in lists.items():
        path=run.directory/f'{prefix}-{kind}-files.txt'
        text=''.join(name+'\n' for name in names)
        temporary=path.with_suffix(path.suffix+'.tmp')
        temporary.write_text(text); temporary.replace(path)
        report['file_lists'][kind]={'path':str(path),'sha256':hashlib.sha256(text.encode()).hexdigest()}
    output=run.directory/f'{prefix}-exports.json'
    r.atomic_json(output,report)
    print(f"Export inventory: {report['validated_selections']}/{report['planned_selections']} validated selections; "
          f"{len(report['primary'])} primary files, {len(report['supporting_probes'])} supporting probes. "
          f"Run certified: {report['run_certified']}.",flush=True)
    print(f"Pending candidate files: {len(report['pending_candidates'])}; unused issued files: {len(report['unused_issued_files'])}; "
          f"unrecognized files for this run: {len(report['unrecognized_run_files'])}; problems: {len(report['problems'])}.",flush=True)
    print('Inventory: '+str(output),flush=True)
    for kind,details in report['file_lists'].items():
        print(kind.capitalize()+' file list: '+details['path'],flush=True)
    return report
