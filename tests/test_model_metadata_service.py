import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.model_metadata_service import (
    preferred_network_name,
    read_model_artifacts,
    read_model_display_info,
    resolve_model_metadata,
)


SHA_A = "A" * 64
SHA_B = "B" * 64


class ModelMetadataServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.model = self.root / "example.safetensors"
        self.model.write_bytes(b"not-a-real-safetensors-file")
        self.cache = self.root / "prompt-studio-cache.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_json(self, suffix, data):
        path = self.root / f"example{suffix}"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def resolve(self, **kwargs):
        return resolve_model_metadata(
            self.model,
            model_type="lora",
            cache_path=self.cache,
            allow_network=False,
            calculate_hash=False,
            **kwargs,
        )

    def test_reads_browser_json_without_modifying_it(self):
        browser_json = self.write_json(
            ".json",
            {
                "sha256": SHA_A.lower(),
                "modelId": 101,
                "modelVersionId": 202,
                "baseModel": "Illustrious",
                "activation text groups": ["hero, armor", "night scene"],
            },
        )
        original_bytes = browser_json.read_bytes()

        metadata = self.resolve()

        self.assertEqual(metadata["sha256"], SHA_A)
        self.assertEqual(metadata["base_model"], "Illustrious")
        self.assertEqual(metadata["trigger_words"], ["hero", "armor", "night scene"])
        self.assertIn("browser_neo_json", metadata["sources"])
        self.assertEqual(browser_json.read_bytes(), original_bytes)
        self.assertTrue(self.cache.is_file())

    def test_reads_browser_api_info_for_matching_model(self):
        self.write_json(".json", {"modelId": 101, "modelVersionId": 202})
        self.write_json(
            ".api_info.json",
            {
                "id": 101,
                "modelVersions": [
                    {
                        "id": 202,
                        "baseModel": "Pony",
                        "trainedWords": ["score_9", "hero"],
                        "files": [{"hashes": {"SHA256": SHA_A}}],
                    }
                ],
            },
        )

        metadata = self.resolve()

        self.assertEqual(metadata["base_model"], "Pony")
        self.assertEqual(metadata["trigger_words"], ["score_9", "hero"])
        self.assertEqual(metadata["model_version_id"], 202)
        self.assertIn("browser_neo_api_info", metadata["sources"])

        display = read_model_display_info(self.model)
        self.assertEqual(display["name"], "")
        self.assertEqual(display["model"]["id"], 101)
        self.assertEqual(display["trainedWords"], ["score_9", "hero"])

    def test_ignores_api_info_for_another_model(self):
        self.write_json(".json", {"modelId": 101, "activation text": "local trigger"})
        self.write_json(
            ".api_info.json",
            {
                "id": 999,
                "modelVersions": [
                    {"id": 202, "baseModel": "Wrong", "trainedWords": ["wrong trigger"]}
                ],
            },
        )

        metadata = self.resolve()

        self.assertEqual(metadata["base_model"], "")
        self.assertEqual(metadata["trigger_words"], ["local trigger"])
        self.assertNotIn("browser_neo_api_info", metadata["sources"])

    def test_civitai_info_is_an_independent_external_source(self):
        self.write_json(
            ".civitai.info",
            {
                "modelId": 303,
                "modelVersionId": 404,
                "baseModel": "SDXL 1.0",
                "trainedWords": ["external trigger"],
            },
        )

        artifacts = read_model_artifacts(self.model)

        self.assertEqual(artifacts["base_model"], "SDXL 1.0")
        self.assertEqual(artifacts["trigger_words"], ["external trigger"])
        self.assertIn("external_civitai_info", artifacts["sources"])
        self.assertNotIn("browser_neo_json", artifacts["sources"])

    def test_supports_legacy_civitai_info_after_full_filename(self):
        path = self.root / "example.safetensors.civitai.info"
        path.write_text(
            json.dumps({"baseModel": "SD 1.5", "trainedWords": ["legacy external"]}),
            encoding="utf-8",
        )

        artifacts = read_model_artifacts(self.model)

        self.assertEqual(artifacts["base_model"], "SD 1.5")
        self.assertEqual(artifacts["trigger_words"], ["legacy external"])
        self.assertIn("external_civitai_info", artifacts["sources"])

    def test_ignores_external_info_when_browser_model_id_conflicts(self):
        self.write_json(".json", {"modelId": 101, "activation text": "local trigger"})
        self.write_json(
            ".civitai.info",
            {"modelId": 999, "baseModel": "Wrong", "trainedWords": ["wrong trigger"]},
        )

        artifacts = read_model_artifacts(self.model)

        self.assertEqual(artifacts["base_model"], "")
        self.assertEqual(artifacts["trigger_words"], ["local trigger"])
        self.assertNotIn("external_civitai_info", artifacts["sources"])

    def test_migrates_legacy_tac_fields_without_rewriting_sidecar(self):
        browser_json = self.write_json(
            ".json",
            {"civitai_sha256": SHA_B.lower(), "civitai_trained_words": "old, trigger"},
        )
        original_bytes = browser_json.read_bytes()

        metadata = self.resolve()

        self.assertEqual(metadata["sha256"], SHA_B)
        self.assertEqual(metadata["trigger_words"], ["old", "trigger"])
        self.assertIn("legacy_tac_json", metadata["sources"])
        self.assertEqual(browser_json.read_bytes(), original_bytes)

    def test_preserves_existing_sha256_sidecar_support(self):
        (self.root / "example.sha256").write_text(SHA_A.lower(), encoding="utf-8")

        metadata = self.resolve()

        self.assertEqual(metadata["sha256"], SHA_A)
        self.assertIn("sha256_sidecar", metadata["sources"])

    def test_civitai_result_is_reused_from_prompt_studio_cache(self):
        self.write_json(".json", {"sha256": SHA_A})
        calls = []

        def fetcher(sha256):
            calls.append(sha256)
            return {
                "id": 202,
                "modelId": 101,
                "baseModel": "NoobAI",
                "trainedWords": ["first", "second"],
            }

        first = resolve_model_metadata(
            self.model,
            model_type="lora",
            required_fields=("trigger_words",),
            cache_path=self.cache,
            fetcher=fetcher,
        )
        second = resolve_model_metadata(
            self.model,
            model_type="lora",
            required_fields=("trigger_words",),
            cache_path=self.cache,
            fetcher=fetcher,
        )

        self.assertEqual(first["trigger_words"], ["first", "second"])
        self.assertEqual(second["trigger_words"], ["first", "second"])
        self.assertEqual(calls, [SHA_A])
        self.assertFalse((self.root / "example.api_info.json").exists())

    def test_preferred_network_name_matches_forge_setting(self):
        header = json.dumps({"__metadata__": {"ss_output_name": "hero-alias"}}).encode()
        self.model.write_bytes(len(header).to_bytes(8, "little") + header)

        self.assertEqual(
            preferred_network_name(self.model, preferred_name="Alias from file"),
            "hero-alias",
        )
        self.assertEqual(
            preferred_network_name(self.model, preferred_name="Filename"),
            "example",
        )
        self.assertEqual(
            preferred_network_name(
                self.model,
                preferred_name="Alias from file",
                forbidden_aliases={"HERO-ALIAS"},
            ),
            "example",
        )

    def test_unchanged_metadata_does_not_rewrite_prompt_studio_cache(self):
        self.write_json(".json", {"sha256": SHA_A, "baseModel": "Anima"})
        self.resolve()

        with patch("scripts.model_metadata_service._save_cache") as save_cache:
            metadata = self.resolve()

        self.assertEqual(metadata["base_model"], "Anima")
        save_cache.assert_not_called()


if __name__ == "__main__":
    unittest.main()
