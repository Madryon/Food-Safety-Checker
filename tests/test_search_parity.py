import unittest
import json
from app import app
from models import User
from extensions import db, limiter


class TestSearchParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["RATELIMIT_ENABLED"] = False
        app.config["CAPTCHA_ENABLED"] = False
        app.config["RECAPTCHA_ENABLED"] = False
        limiter.enabled = False

        with app.app_context():
            user = User.query.filter_by(username="search_tester").first()
            if not user:
                user = User(
                    username="search_tester",
                    name="Search Tester",
                    role="user",
                    is_active=True,
                )
                user.set_password("password123")
                db.session.add(user)
                db.session.commit()
            cls.user_id = user.id

    def setUp(self):
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user_id)
            sess["_fresh"] = True
            sess["user_id"] = "search_tester"
            sess["name"] = "Search Tester"
            sess["role"] = "user"

    def test_guest_search_blocked_with_401(self):
        # Guests without login must receive 401 with auth_required: True
        guest_client = app.test_client()
        resp = guest_client.post("/api/search-food", json={"query": "Mother Dairy Paneer"})
        self.assertEqual(resp.status_code, 401)
        data = resp.get_json()
        self.assertTrue(data.get("auth_required"))
        self.assertIn("registered members", data.get("error", "").lower())

        # Search suggestions must also be blocked for guests
        sugg_resp = guest_client.get("/api/search/suggest?type=food&q=paneer")
        self.assertEqual(sugg_resp.status_code, 401)
        sdata = sugg_resp.get_json()
        self.assertTrue(sdata.get("auth_required"))

    def test_out_of_context_queries_rejected(self):
        # Non-food / coding / hardware queries must return 400 with a clear warning and NO synthesized food report
        bad_queries = [
            "Python Code For Table",
            "write python code",
            "how to hack a computer",
            "laptop screen",
            "toyota corolla car"
        ]
        for query in bad_queries:
            resp = self.client.post("/api/search-food", json={"query": query})
            self.assertEqual(resp.status_code, 400, f"Expected 400 for '{query}', got {resp.status_code}")
            data = resp.get_json()
            self.assertTrue(data.get("is_out_of_context"), f"Expected is_out_of_context for '{query}'")
            self.assertIn("Out of context", data.get("error", ""))
            self.assertIn("Please check your input", data.get("error", ""))
            # Ensure NO food report / scores were synthesized
            self.assertNotIn("score", data)
            self.assertNotIn("nutrition_facts", data)

    def test_food_with_table_keyword_allowed(self):
        # "table salt" or "table butter" contains "table" but MUST be recognized as valid food
        resp = self.client.post("/api/search-food", json={"query": "table salt"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("salt", data["product_name"].lower())

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

    def test_mother_dairy_panner_typo_resilience(self):
        # Typo with double 'n' ('Mother Dairy Panner') MUST resolve to authentic Paneer (not generic 9g protein / 50g carbs)
        resp = self.client.post("/api/search-food", json={"query": "Mother Dairy Panner"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("Mother Dairy", data["product_name"])
        self.assertIn("Paneer", data["product_name"])
        self.assertNotIn("Panner", data["product_name"])
        self.assertEqual(data["brand"], "Mother Dairy")
        self.assertGreaterEqual(data["score"], 85)
        self.assertEqual(data["verdict"], "safe")
        nutr = data.get("nutrition_facts") or {}
        self.assertGreaterEqual(nutr.get("protein_g", 0), 18.0)
        self.assertLessEqual(nutr.get("carbohydrates_g", 999), 5.0)
        # Verify NO vegetable oil or generic fallback ingredients
        ing_text = data.get("ingredients_text", "")
        self.assertNotIn("Vegetable Oil", ing_text)
        self.assertIn("Milk", ing_text)

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

    def test_analyze_health_endpoint_no_busy_flags(self):
        # Direct health check (/analyze/health) must NEVER output placeholder AI_ANALYSIS_BUSY flags
        resp = self.client.post("/analyze/health", json={
            "product_name": "Britannia Good Day Butter Cookies",
            "ingredients_text": "Refined Wheat Flour (Maida), Sugar, Refined Palm Oil, Invert Sugar Syrup, Iodised Salt, Raising Agents (INS 500(ii), INS 503(ii)), Emulsifiers (INS 322), Nature Identical Flavours.",
            "nutrition_facts": {"energy_kcal": 475, "protein_g": 6.8, "total_fat_g": 18.5, "saturated_fat_g": 8.9, "trans_fat_g": 0.0, "carbohydrates_g": 70.0, "total_sugar_g": 24.5, "sodium_mg": 310, "fiber_g": 1.8}
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIsNotNone(data.get("score"))
        self.assertEqual(data.get("verdict"), "moderate")
        flags = data.get("flags", [])
        self.assertTrue(len(flags) > 0)
        flag_types = [f.get("type") for f in flags if isinstance(f, dict)]
        self.assertNotIn("AI_ANALYSIS_BUSY", flag_types)
        self.assertNotIn("AI_ANALYSIS_OFFLINE", flag_types)
        self.assertTrue(any("SUGAR" in ft or "PALM" in ft for ft in flag_types))


if __name__ == "__main__":
    unittest.main()
