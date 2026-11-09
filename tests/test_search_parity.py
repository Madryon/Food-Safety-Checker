import unittest
import json
from app import app


class TestSearchParity(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_mother_dairy_paneer(self):
        resp = self.client.post("/api/search-food", json={"query": "Mother Dairy Paneer"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("Mother Dairy", data["product_name"])
        self.assertIn("Paneer", data["product_name"])
        self.assertEqual(data["brand"], "Mother Dairy")
        self.assertGreaterEqual(data["score"], 80)
        self.assertEqual(data["verdict"], "safe")
        self.assertIn("image_url", data)
        # Verify no bogus French product
        self.assertNotIn("Fromage", data["product_name"])
        self.assertNotIn("SODIPOL", data["brand"])
        # Check nutritional authenticity (high protein, low sugar)
        nutr = data.get("nutrition_facts") or {}
        self.assertGreaterEqual(nutr.get("protein_g", 0), 15.0)

    def test_diet_coke_no_crash(self):
        resp = self.client.post("/api/search-food", json={"query": "Diet Coke"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("Diet Coke", data["product_name"])
        self.assertIn("Coca-Cola", data["brand"])
        self.assertEqual(data["verdict"], "moderate")
        # Verify zero sugar / zero calorie authenticity
        nutr = data.get("nutrition_facts") or {}
        self.assertEqual(nutr.get("total_sugar_g"), 0.0)
        self.assertLessEqual(nutr.get("energy_kcal", 0), 5.0)
        # Check flags present for artificial sweeteners
        flag_types = [f.get("type") for f in data.get("flags", [])]
        self.assertIn("ARTIFICIAL_SWEETENERS", flag_types)
        # Verify alternatives are clean beverages
        alts = [a.get("name") for a in data.get("alternatives", [])]
        self.assertTrue(len(alts) >= 2)

    def test_fresh_paneer(self):
        resp = self.client.post("/api/search-food", json={"query": "Paneer"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("Paneer", data["product_name"])
        self.assertGreaterEqual(data["score"], 80)

    def test_amul_butter(self):
        resp = self.client.post("/api/search-food", json={"query": "Amul Butter"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("Amul", data["product_name"])
        self.assertIn("Butter", data["product_name"])
        self.assertIn("Amul", data["brand"])

    def test_maggi_noodles(self):
        resp = self.client.post("/api/search-food", json={"query": "Maggi Noodles"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("Maggi", data["product_name"])
        self.assertEqual(data["verdict"], "unsafe")

    def test_diet_pepsi_parametric_synthesis(self):
        # Even if not directly in static keywords, parametric synthesizer must accurately identify diet soda
        resp = self.client.post("/api/search-food", json={"query": "Diet Pepsi"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("Pepsi", data["product_name"])
        nutr = data.get("nutrition_facts") or {}
        self.assertEqual(nutr.get("total_sugar_g"), 0.0)


if __name__ == "__main__":
    unittest.main()
