import tempfile
import unittest
from pathlib import Path

from scripts.model_catalog_service import (
    PREVIEW_EXTENSIONS,
    ModelCatalogEntry,
    ModelFileCatalog,
    find_companion_file,
)


class ModelFileCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.catalog = ModelFileCatalog()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _model(self, relative_path: str) -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"model")
        return path

    def test_finds_entry_by_stem_filename_relative_path_and_alias(self):
        path = self._model("characters/Hero Style.safetensors")
        entry = ModelCatalogEntry(path, alias="hero_alias", network_hash="abc123")
        self.catalog.replace("lora", self.root, [entry])

        for identifier in (
            "Hero Style",
            "hero style.safetensors",
            "characters/Hero Style",
            "characters/Hero Style.safetensors",
            "hero_alias",
        ):
            with self.subTest(identifier=identifier):
                self.assertEqual(self.catalog.find("lora", identifier), entry)

    def test_duplicate_stems_keep_the_first_catalog_entry(self):
        first = ModelCatalogEntry(self._model("a/shared.safetensors"))
        second = ModelCatalogEntry(self._model("b/shared.safetensors"))
        self.catalog.replace("lora", self.root, [first, second])

        self.assertEqual(self.catalog.find("lora", "shared"), first)
        self.assertEqual(self.catalog.find("lora", "b/shared"), second)

    def test_replace_removes_stale_entries(self):
        old_entry = ModelCatalogEntry(self._model("old.safetensors"))
        new_entry = ModelCatalogEntry(self._model("new.safetensors"))
        self.catalog.replace("lora", self.root, [old_entry])
        self.catalog.replace("lora", self.root, [new_entry])

        self.assertIsNone(self.catalog.find("lora", "old"))
        self.assertEqual(self.catalog.find("lora", "new"), new_entry)

    def test_model_types_are_isolated(self):
        lora = ModelCatalogEntry(self._model("lora/item.safetensors"))
        lyco = ModelCatalogEntry(self._model("lyco/item.safetensors"))
        self.catalog.replace("lora", self.root, [lora])
        self.catalog.replace("lyco", self.root, [lyco])

        self.assertEqual(self.catalog.find("lora", "item"), lora)
        self.assertEqual(self.catalog.find("lyco", "item"), lyco)

    def test_native_entry_outside_primary_root_is_still_indexed(self):
        other_root = self.root.parent / (self.root.name + "-additional")
        try:
            other_root.mkdir()
            path = other_root / "external.safetensors"
            path.write_bytes(b"model")
            entry = ModelCatalogEntry(path, alias="external_alias")
            self.catalog.replace("lora", self.root, [entry])

            self.assertEqual(self.catalog.find("lora", "external"), entry)
            self.assertEqual(self.catalog.find("lora", "external_alias"), entry)
        finally:
            if other_root.exists():
                for child in other_root.iterdir():
                    child.unlink()
                other_root.rmdir()

    def test_find_companion_uses_exact_model_stem(self):
        model = self._model("styles/example.safetensors")
        preview = model.with_name("example.preview.webp")
        preview.write_bytes(b"preview")
        unrelated = model.with_name("other.png")
        unrelated.write_bytes(b"other")

        self.assertEqual(find_companion_file(model, PREVIEW_EXTENSIONS), preview)
        self.assertIsNone(find_companion_file(model, (".json",)))


if __name__ == "__main__":
    unittest.main()
