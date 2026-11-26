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

    def test_gemini_keys_primary_plus_secondary(self):
        os.environ["GEMINI_API_KEY"] = "primary_gemini"
        os.environ["GEMINI_API_KEY_2"] = "secondary_gemini"
        keys = get_gemini_api_keys()
        self.assertEqual(keys, ["primary_gemini", "secondary_gemini"])

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

    def test_groq_vision_default_not_decommissioned(self):
        from services.ai_provider import DEFAULT_GROQ_VISION_MODEL, GROQ_VISION_MODELS
        self.assertNotIn("llama-3.2-11b-vision-preview", DEFAULT_GROQ_VISION_MODEL)
        self.assertNotIn("llama-3.2-11b-vision-preview", GROQ_VISION_MODELS)
        self.assertIn("meta-llama/llama-4-scout-17b-16e-instruct", GROQ_VISION_MODELS)

    @patch("services.gemini_client._GEMINI_SESSION.get")
    def test_gemini_model_discovery_excludes_slow_non_vision_models(self, mock_get):
        from services.gemini_client import discover_available_models, CACHED_AVAILABLE_MODELS
        CACHED_AVAILABLE_MODELS.clear()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [
                {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/deep-research-pro-preview", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-flash-latest", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemma-3-27b-it", "supportedGenerationMethods": ["generateContent"]},
            ]
        }
        mock_get.return_value = mock_resp

        models = discover_available_models("AIzaTestDiscoveryKey123456")
        self.assertEqual(models, ["gemini-2.5-flash", "gemini-flash-latest"])
        self.assertNotIn("gemini-2.5-pro", models)
        self.assertNotIn("gemini-2.5-flash-preview-tts", models)

    @patch("services.ai_provider.call_groq_json")
    @patch("services.ai_provider.generate_content_with_retry")
    def test_vision_parallel_race_returns_fastest_and_surfaces_auth_errors(self, mock_gem, mock_groq):
        import io
        from PIL import Image
        from services.ai_provider import extract_food_vision_bytes

        os.environ["GEMINI_API_KEY"] = "AQ.InvalidTestKey"
        os.environ["GROQ_API_KEY"] = "gsk_test_key"

        buf = io.BytesIO()
        Image.new("RGB", (200, 150), "white").save(buf, format="JPEG")
        raw_bytes = buf.getvalue()

        # Case 1: Gemini raises 401 auth error, Groq succeeds -> user immediately gets result!
        mock_gem.side_effect = RuntimeError("HTTP 401 UNAUTHENTICATED: API key not valid [ACCESS_TOKEN_TYPE_UNSUPPORTED]")
        mock_groq.return_value = (
            {
                "brand": "Nestle",
                "product_name": "Maggi Noodles",
                "ingredients_text": "Wheat flour, Palm oil",
                "nutrition_facts": {"energy_kcal": 427},
                "data_source": "image",
            },
            "{}",
            None,
        )
        res = extract_food_vision_bytes(raw_bytes)
        self.assertEqual(res.get("product_name"), "Maggi Noodles")

        # Case 2: Both Gemini & Groq fail with 401 -> exact API auth error surfaces (NOT fake timeout!)
        mock_groq.return_value = (None, None, "HTTP 401 (meta-llama/llama-4-scout-17b-16e-instruct): Invalid API Key")
        err_res = extract_food_vision_bytes(raw_bytes)
        self.assertEqual(err_res.get("error_type"), "auth_error")
        self.assertIn("401", err_res.get("error", ""))
        self.assertTrue(len(err_res.get("provider_errors", [])) >= 2)


if __name__ == "__main__":
    unittest.main()
