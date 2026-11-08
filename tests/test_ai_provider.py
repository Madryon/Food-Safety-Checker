import os
import unittest
from unittest.mock import patch, MagicMock
from services.ai_provider import (
    get_gemini_api_keys,
    get_groq_api_keys,
    get_openrouter_api_keys,
    get_openai_api_keys,
    get_ai_status_summary,
    analyze_food_health,
    _validate_health_schema,
    _normalize_health_result,
)
from services.gemini_client import (
    get_active_gemini_keys,
    record_key_quota_exhaustion,
    _KEY_COOLDOWNS,
)


class TestAIProvider(unittest.TestCase):
    def setUp(self):
        # Save env
        self.old_env = dict(os.environ)
        # Clear AI related keys for pure testing
        for k in list(os.environ.keys()):
            if "GEMINI" in k or "GOOGLE" in k or "GROQ" in k or "OPENROUTER" in k or "OPENAI" in k:
                del os.environ[k]
        _KEY_COOLDOWNS.clear()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        _KEY_COOLDOWNS.clear()

    def test_gemini_keys_extraction_single(self):
        os.environ["GEMINI_API_KEY"] = "gem_single_key"
        keys = get_gemini_api_keys()
        self.assertEqual(keys, ["gem_single_key"])

    def test_gemini_keys_extraction_multi_comma(self):
        os.environ["GEMINI_API_KEYS"] = "key_alpha, key_beta, key_gamma"
        keys = get_gemini_api_keys()
        self.assertEqual(keys, ["key_alpha", "key_beta", "key_gamma"])

    def test_gemini_keys_extraction_numbered(self):
        os.environ["GEMINI_API_KEY_1"] = "key_one"
        os.environ["GEMINI_API_KEY_2"] = "key_two"
        keys = get_gemini_api_keys()
        self.assertEqual(keys, ["key_one", "key_two"])

    def test_gemini_key_rotation_and_cooldown(self):
        os.environ["GEMINI_API_KEYS"] = "k1, k2, k3"
        keys = get_gemini_api_keys()
        self.assertEqual(len(keys), 3)

        # Mark k1 on cooldown
        record_key_quota_exhaustion("k1", cooldown_seconds=60)
        active = get_active_gemini_keys()
        self.assertNotIn("k1", active)
        self.assertIn("k2", active)
        self.assertIn("k3", active)

    def test_groq_keys_extraction(self):
        os.environ["GROQ_API_KEYS"] = "gkey_1, gkey_2"
        keys = get_groq_api_keys()
        self.assertEqual(keys, ["gkey_1", "gkey_2"])

    def test_groq_keys_multiple_env_formats(self):
        os.environ["GROQ_API_KEY"] = "primary_groq"
        os.environ["GROQ_API_KEY_2"] = "secondary_groq"
        keys = get_groq_api_keys()
        self.assertIn("primary_groq", keys)
        self.assertIn("secondary_groq", keys)
        self.assertEqual(len(keys), 2)

    @patch("requests.post")
    def test_groq_multi_key_rotation_on_429(self, mock_post):
        from services.ai_provider import call_groq_json
        os.environ["GROQ_API_KEYS"] = "groq_key_fail, groq_key_pass"

        # Mock first response as 429, second response as 200
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.text = "Rate limit reached"

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "choices": [{"message": {"content": '{"result": "success"}'}}]
        }
        resp_200.elapsed.total_seconds.return_value = 0.25

        mock_post.side_effect = [resp_429, resp_200]

        parsed, raw, err = call_groq_json([{"role": "user", "content": "hi"}])
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.get("result"), "success")
        self.assertIsNone(err)

    def test_ai_status_summary(self):
        os.environ["GEMINI_API_KEYS"] = "gem1, gem2"
        os.environ["GROQ_API_KEY"] = "gkey1"
        summary = get_ai_status_summary()
        self.assertEqual(summary["gemini_keys_count"], 2)
        self.assertEqual(summary["groq_keys_count"], 1)
        self.assertTrue(summary["ultra_fast_groq_enabled"])
        self.assertEqual(summary["status"], "ready")

    def test_validate_and_normalize_health_schema(self):
        sample = {
            "score": "85",
            "verdict": "safe",
            "flags": [{"type": "ALLERGEN", "severity": "low", "explanation": "Contains soy"}],
            "reasoning": "Nutritious snack.",
            "alternatives": [],
        }
        self.assertTrue(_validate_health_schema(sample))
        _normalize_health_result(sample)
        self.assertEqual(sample["score"], 85)
        self.assertEqual(sample["verdict"], "safe")

    @patch("services.ai_provider.call_groq_json")
    def test_analyze_health_groq_fast_path(self, mock_groq):
        os.environ["GROQ_API_KEY"] = "g_test_key"
        mock_groq.return_value = (
            {
                "score": 75,
                "verdict": "safe",
                "flags": [],
                "reasoning": "Product has low sugar and wholesome ingredients.",
                "alternatives": [],
            },
            "",
            None,
        )

        res = analyze_food_health("Whole wheat flour, oats, water", {"total_sugar_g": 3}, "Oat Biscuits")
        self.assertEqual(res["score"], 75)
        self.assertEqual(res["verdict"], "safe")
        self.assertTrue("Groq" in res.get("_ai_provider", ""))
        self.assertIn("_latency_ms", res)


if __name__ == "__main__":
    unittest.main()
