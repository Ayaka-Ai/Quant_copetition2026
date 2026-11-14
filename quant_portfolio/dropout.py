"""Stateful weekly replacements using actual filled holdings, never prior targets."""
from decimal import Decimal as D
from quant_backtest import TargetWeights


class TopKDropStrategy:
    def __init__(self, decisions, *, maximum_positions=20, weight=D('.04'), sector_cap=D('.20'),
                 replacement_fraction=D('.20'), score_margin_std=D('.15'),
                 trade_fraction=None, liquidity_caps=None):
        self.decisions = {d['signal_session']: d for d in decisions}
        if len(self.decisions) != len(decisions):
            raise ValueError('Duplicate decision session')
        if (maximum_positions < 1 or not D(0) < weight <= sector_cap <= 1
                or maximum_positions*weight > 1 or not D(0) <= replacement_fraction <= 1
                or score_margin_std < 0 or trade_fraction is not None and not D(0) <= trade_fraction <= 1):
            raise ValueError('Invalid dropout/risk parameters')
        self.maximum_positions, self.weight, self.sector_cap = maximum_positions, weight, sector_cap
        self.replacement_fraction, self.margin = replacement_fraction, score_margin_std
        self.trade_fraction, self.liquidity_caps = trade_fraction, liquidity_caps
        self.audit = []

    def __call__(self, state):
        d = self.decisions.get(state.session_date)
        if d is None:
            return None
        audit = dict(signal_session=state.session_date, actual_holdings=dict(state.holdings),
                     status='skipped', reason='', forced_exits=[], vacancy_buys=[], replacements=[])
        self.audit.append(audit)
        if d['status'] not in ('target_ready', 'cash_target_ready'):
            audit['reason'] = d['status']
            return None
        if d['signal_time'] > state.available_at:
            audit['reason'] = 'score_not_yet_available'
            return None
        unsafe = {c['symbol'] for c in d['candidates']
                  if 'unresolved_current_price_or_action_basis' in c['data_reasons']}
        if not state.valuation_valid or state.equity is None or unsafe.intersection(state.holdings):
            audit['reason'] = 'unsafe_actual_holding_data'
            return None
        candidates = {c['symbol']: c for c in d['candidates'] if c['eligible']}
        if any(c.get('score') is None for c in candidates.values()):
            raise ValueError('Dropout requires explicit scores')
        ranked = sorted(candidates, key=lambda s: (-candidates[s]['score'], s))

        def fits(symbol, selected):
            c = candidates[symbol]
            return (len(selected) < self.maximum_positions
                and not any(candidates[s]['issuer_cik'] == c['issuer_cik'] for s in selected)
                and (sum(candidates[s]['sector'] == c['sector'] for s in selected)+1)*self.weight <= self.sector_cap)

        selected = []
        # Preserve eligible holdings first, using actual shares after all fills/actions.
        for s in ranked:
            if s in state.holdings and fits(s, selected):
                selected.append(s)
        retained = set(selected)
        forced = set(state.holdings)-retained
        audit['forced_exits'] = sorted(forced)
        for s in ranked:
            if s not in selected and s not in forced and fits(s, selected):
                selected.append(s)
                audit['vacancy_buys'].append(s)
        values = [c['score'] for c in candidates.values()]
        average = sum(values, D(0))/len(values) if values else D(0)
        std = (sum(((v-average)**2 for v in values), D(0))/len(values)).sqrt() if values else D(0)
        threshold = self.margin*std
        limit = int(D(len(state.holdings))*self.replacement_fraction)
        for _ in range(limit):
            pair = None
            old = sorted(retained, key=lambda s: (candidates[s]['score'], s))
            for new in ranked:
                if new in selected or new in forced or new in state.holdings:
                    continue
                for sell in old:
                    advantage = candidates[new]['score']-candidates[sell]['score']
                    rest = [s for s in selected if s != sell]
                    if advantage > 0 and advantage >= threshold and fits(new, rest):
                        pair = sell, new, advantage
                        break
                if pair:
                    break
            if pair is None:
                break
            sell, new, advantage = pair
            selected.remove(sell)
            selected.append(new)
            retained.remove(sell)
            forced.add(sell)  # Do not repurchase a replacement within this decision.
            audit['replacements'].append(dict(sell=sell, buy=new, score_advantage=advantage))
        selected.sort(key=lambda s: (-candidates[s]['score'], s))
        budget = state.equity*self.trade_fraction if self.trade_fraction is not None else None
        caps = self.liquidity_caps.get(state.session_date, {}) if self.liquidity_caps is not None else {}
        if self.liquidity_caps is not None:
            caps = {s: caps.get(s, D(0)) for s in set(selected) | set(state.holdings)}
        audit.update(status='target_ready', reason='', selected_symbols=selected,
            maximum_regular_replacements=limit, score_population_std=std, score_threshold=threshold,
            maximum_trade_notional=budget, symbol_trade_notionals=caps)
        return TargetWeights(state.session_date, d['signal_time'], {s: self.weight for s in selected},
                             tuple(selected), tuple(unsafe), budget, caps)
