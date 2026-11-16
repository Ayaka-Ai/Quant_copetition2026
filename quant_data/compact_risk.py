"""Read hash-pinned compact risk artifacts with explicit strict/research clocks."""
import datetime as dt
import json
from pathlib import Path
from .public_universe import digest


class CompactRiskStore:
    def __init__(self,root,fingerprints):
        self.root=Path(root)
        for name in ('summary.json','betas.json','covariance_index.json'):
            if digest(self.root/name)!=fingerprints.get(name):raise ValueError('Risk metadata fingerprint changed: '+name)
        self.summary=json.loads((self.root/'summary.json').read_text())
        rows=json.loads((self.root/'covariance_index.json').read_text())
        beta=json.loads((self.root/'betas.json').read_text())
        self.rows={r['as_of']:r for r in rows};self.betas={r['as_of']:r for r in beta};self.cache={}
        if len(self.rows)!=len(rows) or len(self.betas)!=len(beta): raise ValueError('Ambiguous risk version')

    def snapshot(self,day,cutoff,mode='strict'):
        import numpy as np
        if mode not in ('strict','exploratory_model_time'):raise ValueError('Unknown risk availability mode')
        if cutoff.utcoffset() is None:raise ValueError('Risk cutoff must be timezone-aware')
        row=self.rows.get(day.isoformat());beta=self.betas.get(day.isoformat())
        if row is None or beta is None:return None
        clock='available_at' if mode=='strict' else 'model_available_at'
        if any(dt.datetime.fromisoformat(r[clock])>cutoff for r in (row,beta)):return None
        if any(dt.date.fromisoformat(r['estimation_end'])>day for r in (row,beta)):
            raise ValueError('Future risk estimation input')
        if row['units']!='daily_return_covariance' or beta['units']!='dimensionless_SPY_beta':
            raise ValueError('Wrong risk units')
        name=row['file']
        if Path(name).name!=name:raise ValueError('Risk artifact path must be a local filename')
        file=self.root/name
        # Check at every read, including cached matrices, to detect changed files.
        if digest(file)!=row['sha256'] or row['sha256']!=self.summary['covariance_sha256'][name]:
            raise ValueError('Covariance fingerprint changed')
        if name not in self.cache:
            with np.load(file,allow_pickle=False) as saved:
                f=saved['factors'].copy();d=saved['diagonal'].copy();symbols=tuple(str(s) for s in saved['symbols'])
            if (len(set(symbols))!=len(symbols) or f.ndim!=2 or f.shape[0]!=len(symbols)
                    or d.shape!=(len(symbols),) or not np.isfinite(f).all() or not np.isfinite(d).all() or np.any(d<0)):
                raise ValueError('Invalid compact risk artifact')
            f.flags.writeable=False;d.flags.writeable=False;self.cache[name]=(symbols,f,d)
        symbols,f,d=self.cache[name]
        betas={s:float(v) for s,v in beta['payload'].items()}
        if not all(np.isfinite(v) for v in betas.values()):raise ValueError('Nonfinite beta')
        return dict(symbols=symbols,factors=f,diagonal=d,betas=betas,mode=mode,
                    metadata=row,beta_metadata=beta)
