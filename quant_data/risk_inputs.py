"""Hash-pinned, explicitly dated external risk input contract. No downloading."""
import datetime as dt
import hashlib
import json
from pathlib import Path
from .us_calendar import exchange_sessions

KINDS=('benchmark','sectors','betas','covariance')


def load_risk_inputs(root):
    root=Path(root); path=root/'manifest.json'
    if not path.exists(): return dict(records={k:[] for k in KINDS},files=[],status='missing_manifest')
    manifest=json.loads(path.read_text())
    if manifest.get('schema_version')!='risk_inputs_v1': raise ValueError('Unsupported risk manifest')
    records={k:[] for k in KINDS}; files=[]; identities=set()
    for entry in manifest['files']:
        kind=entry['kind']
        if kind not in KINDS: raise ValueError('Unknown risk input kind')
        file=(root/entry['file']).resolve()
        try: file.relative_to(root.resolve())
        except ValueError: raise ValueError('Risk file outside input directory')
        raw=file.read_bytes(); sha=hashlib.sha256(raw).hexdigest()
        if sha!=entry['sha256']: raise ValueError('Risk input fingerprint changed')
        for row in json.loads(raw):
            day=dt.date.fromisoformat(row['as_of']); stamp=dt.datetime.fromisoformat(row['available_at'])
            if stamp.utcoffset() is None or not row['source'] or 'payload' not in row:
                raise ValueError('Risk records need source, payload and aware availability')
            expected_units={'benchmark':'NAV_fraction','sectors':'GICS_sector',
                            'betas':'dimensionless_SPY_beta','covariance':'daily_return_covariance'}
            if row.get('units')!=expected_units[kind]: raise ValueError('Missing or incompatible risk units')
            if kind in ('betas','covariance'):
                start,end=(dt.date.fromisoformat(row[k]) for k in ('estimation_start','estimation_end'))
                if start>end or end>day or end>stamp.date(): raise ValueError('Risk estimation window leaks future data')
                end_sessions=exchange_sessions(end,end+dt.timedelta(days=1))
                if not end_sessions or end_sessions[0].close_time>stamp:
                    raise ValueError('Risk estimate predates the last input session close')
            if kind=='sectors':
                if dt.date.fromisoformat(row['valid_through'])<day: raise ValueError('Invalid sector validity interval')
            identity=(kind,day,stamp)
            if identity in identities: raise ValueError('Ambiguous risk version')
            identities.add(identity); records[kind].append(dict(row,as_of=day,available_at=stamp))
        files.append(dict(kind=kind,file=entry['file'],sha256=sha))
    return dict(records=records,files=files,status='loaded')


def risk_snapshot(inputs, day, cutoff):
    chosen={}
    for kind in KINDS:
        candidates=[r for r in inputs['records'][kind] if r['available_at']<=cutoff and
            (r['as_of']<=day<=dt.date.fromisoformat(r['valid_through']) if kind=='sectors' else r['as_of']==day)]
        chosen[kind]=max(candidates,key=lambda r:(r['as_of'],r['available_at'])) if candidates else None
    return chosen
