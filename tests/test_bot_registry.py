import tempfile
import unittest
import json
from pathlib import Path

from cryptography.fernet import Fernet

from panel.bot_registry import (
    empty_registry,
    get_bot_credentials,
    load_registry,
    save_registry,
    set_bot_credentials,
    validate_bot_definition,
)


class BotRegistryTests(unittest.TestCase):
    def setUp(self):
        self.definition = {
            "id": "a" * 32,
            "name": "Example Bot",
            "repository": "https://github.com/example/telegram-bot.git",
            "ref": "main",
            "entrypoint": "src/bot.py",
            "enabled": False,
        }

    def test_empty_registry_has_no_default_bots(self):
        self.assertEqual(
            empty_registry(),
            {"version": 1, "bots": [], "encrypted_credentials": {}},
        )

    def test_accepts_a_valid_bot_definition(self):
        validate_bot_definition(self.definition)

    def test_rejects_non_github_repository_urls(self):
        invalid_urls = [
            "http://github.com/example/bot.git",
            "https://example.com/example/bot.git",
            "https://user:password@github.com/example/bot.git",
            "https://github.com/example/bot.git?token=secret",
        ]

        for repository in invalid_urls:
            with self.subTest(repository=repository):
                definition = {**self.definition, "repository": repository}
                with self.assertRaises(ValueError):
                    validate_bot_definition(definition)

    def test_rejects_entrypoints_outside_the_repository(self):
        invalid_entrypoints = ["../bot.py", "/tmp/bot.py", "src/../../bot.py"]

        for entrypoint in invalid_entrypoints:
            with self.subTest(entrypoint=entrypoint):
                definition = {**self.definition, "entrypoint": entrypoint}
                with self.assertRaises(ValueError):
                    validate_bot_definition(definition)

    def test_saves_and_loads_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_path = Path(directory) / "registry.json"
            registry = empty_registry()
            registry["bots"].append(self.definition)

            save_registry(registry_path, registry)

            self.assertEqual(load_registry(registry_path), registry)

    def test_encrypts_pat_and_environment_outside_bot_metadata(self):
        encryption_key = Fernet.generate_key()
        registry = empty_registry()
        registry["bots"].append(self.definition)
        credentials = {
            "pat": "synthetic-private-token",
            "environment": {"BOT_TOKEN": "synthetic-bot-token"},
        }

        set_bot_credentials(registry, self.definition["id"], credentials, encryption_key)

        serialized_registry = json.dumps(registry)
        self.assertNotIn(credentials["pat"], serialized_registry)
        self.assertNotIn(credentials["environment"]["BOT_TOKEN"], serialized_registry)
        self.assertEqual(
            get_bot_credentials(registry, self.definition["id"], encryption_key),
            credentials,
        )

    def test_rejects_credential_write_without_encryption_key(self):
        registry = empty_registry()
        registry["bots"].append(self.definition)

        with self.assertRaises(ValueError):
            set_bot_credentials(
                registry,
                self.definition["id"],
                {"pat": "synthetic-private-token", "environment": {}},
                "",
            )


if __name__ == "__main__":
    unittest.main()