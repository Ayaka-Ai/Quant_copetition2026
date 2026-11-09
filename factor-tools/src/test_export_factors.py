import unittest
import numpy as np
import pandas as pd
from export_factors import build_scores, demo_prices


class ExportTests(unittest.TestCase):
    def test_demo_and_prefix_consistency(self):
        prices=demo_prices()
        scores=build_scores(prices)
        self.assertEqual(len(scores),195)
        self.assertEqual(int(scores.ready_all.sum()),135)
        end=prices.date.sort_values().unique()[39]
        partial=build_scores(prices[prices.date<=end])
        pd.testing.assert_frame_equal(scores[scores.date<=end].reset_index(drop=True),partial)

    def test_known_value_and_no_cross_symbol_mix(self):
        dates=pd.bdate_range('2025-01-01',periods=22)
        p=pd.DataFrame({'date':dates,'symbol':'A','close':np.arange(1.,23.)})
        p=pd.concat([p,p.assign(symbol='B',close=10.)])
        s=build_scores(p)
        self.assertAlmostEqual(s[(s.symbol=='A')&(s.date==dates[20])].custom_momentum20.iloc[0],20.)
        self.assertEqual(s[(s.symbol=='B')&(s.date==dates[20])].custom_momentum20.iloc[0],0.)

    def test_invalid_input_rejected(self):
        p=demo_prices()
        for bad in (pd.concat([p,p.iloc[:1]]),p.assign(close=0),p.assign(symbol='')):
            with self.assertRaises(ValueError): build_scores(bad)

    def test_missing_row_breaks_window(self):
        p=demo_prices(); date=p.date.sort_values().unique()[25]
        p=p[~((p.symbol=='DEMO_A')&(p.date==date))]
        s=build_scores(p)
        row=s[(s.symbol=='DEMO_A')&(s.date==date)].iloc[0]
        self.assertTrue(pd.isna(row.custom_momentum20))
        self.assertFalse(row.ready_all)


if __name__=='__main__': unittest.main()
