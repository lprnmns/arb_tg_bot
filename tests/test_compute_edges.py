import unittest

from bot.hl_client import compute_edges


class ComputeEdgesTests(unittest.TestCase):
    def setUp(self):
        self.fees = {
            "perp": {"maker": 1.5, "taker": 4.5},
            "spot": {"maker": 4.0, "taker": 7.0},
        }

    def test_balanced_books_penalized_by_fees(self):
        edges = compute_edges(100.0, 100.0, 100.0, 100.0, self.fees)
        fee_mm = self.fees["perp"]["maker"] + self.fees["spot"]["maker"]
        fee_tt = self.fees["perp"]["taker"] + self.fees["spot"]["taker"]

        self.assertAlmostEqual(edges["ps_mm"], -fee_mm)
        self.assertAlmostEqual(edges["sp_mm"], -fee_mm)
        self.assertAlmostEqual(edges["ps_tt"], -fee_tt)
        self.assertAlmostEqual(edges["sp_tt"], -fee_tt)
        self.assertAlmostEqual(edges["mid_ref"], 100.0)

    def test_positive_perp_to_spot_edge(self):
        edges = compute_edges(101.0, 101.2, 99.5, 99.7, self.fees)
        mid = (101.0 + 101.2 + 99.5 + 99.7) / 4.0
        raw_ps = ((101.0 - 99.7) / mid) * 1e4
        raw_sp = ((99.5 - 101.2) / mid) * 1e4
        fee_mm = self.fees["perp"]["maker"] + self.fees["spot"]["maker"]
        fee_tt = self.fees["perp"]["taker"] + self.fees["spot"]["taker"]

        self.assertAlmostEqual(edges["mid_ref"], mid)
        self.assertAlmostEqual(edges["ps_mm"], raw_ps - fee_mm)
        self.assertAlmostEqual(edges["sp_mm"], raw_sp - fee_mm)
        self.assertAlmostEqual(edges["ps_tt"], raw_ps - fee_tt)
        self.assertAlmostEqual(edges["sp_tt"], raw_sp - fee_tt)

    def test_extreme_negative_spot_to_perp_edge(self):
        edges = compute_edges(50.0, 50.2, 51.5, 51.7, self.fees)
        mid = (50.0 + 50.2 + 51.5 + 51.7) / 4.0
        raw_sp = ((51.5 - 50.2) / mid) * 1e4
        fee_mm = self.fees["perp"]["maker"] + self.fees["spot"]["maker"]

        self.assertTrue(raw_sp > 0)
        self.assertAlmostEqual(edges["sp_mm"], raw_sp - fee_mm)
        self.assertAlmostEqual(edges["mid_ref"], mid)


if __name__ == "__main__":
    unittest.main()
