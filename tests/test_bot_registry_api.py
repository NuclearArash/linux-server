import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

PANEL_DIR = Path(__file__).resolve().parents[1] / "panel"
sys.path.insert(0, str(PANEL_DIR))
import app as panel_app
from bot_registry import empty_registry, save_registry, set_bot_credentials


class BotRegistryApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.original_base_dir = panel_app.BASE_DIR
        self.original_registry_path = panel_app.BOT_REGISTRY_PATH
        self.original_credentials_key = panel_app.BOT_CREDENTIALS_KEY
        panel_app.BASE_DIR = str(self.root)
        panel_app.BOT_REGISTRY_PATH = str(self.root / "bot-registry.json")
        panel_app.BOT_CREDENTIALS_KEY = Fernet.generate_key().decode("ascii")
        self.addCleanup(setattr, panel_app, "BASE_DIR", self.original_base_dir)
        self.addCleanup(setattr, panel_app, "BOT_REGISTRY_PATH", self.original_registry_path)
        self.addCleanup(setattr, panel_app, "BOT_CREDENTIALS_KEY", self.original_credentials_key)
        panel_app.app.config["TESTING"] = True
        self.client = panel_app.app.test_client()
        with panel_app.BOT_INSTALL_JOBS_LOCK:
            original_jobs = dict(panel_app.BOT_INSTALL_JOBS)
            panel_app.BOT_INSTALL_JOBS.clear()
        self.addCleanup(self._restore_jobs, original_jobs)
        original_inputs = dict(panel_app.BOT_INSTALL_PAYLOADS)
        panel_app.BOT_INSTALL_PAYLOADS.clear()
        self.addCleanup(setattr, panel_app, "BOT_INSTALL_PAYLOADS", original_inputs)
        with self.client.session_transaction() as session:
            session["logged_in"] = True

    @staticmethod
    def _restore_jobs(jobs):
        with panel_app.BOT_INSTALL_JOBS_LOCK:
            panel_app.BOT_INSTALL_JOBS.clear()
            panel_app.BOT_INSTALL_JOBS.update(jobs)

    def add_bot(self):
        definition = {
            "id": "a" * 32,
            "name": "Example Bot",
            "repository": "https://github.com/example/telegram-bot.git",
            "ref": "main",
            "entrypoint": "src/bot.py",
            "enabled": False,
        }
        registry = empty_registry()
        registry["bots"].append(definition)
        return registry, definition

    def test_status_reports_empty_fleet_without_default_bots(self):
        with (
            patch.object(panel_app.psutil, "cpu_percent", return_value=0),
            patch.object(panel_app.psutil, "virtual_memory", return_value=SimpleNamespace(used=0, total=1, percent=0)),
            patch.object(panel_app.psutil, "disk_usage", return_value=SimpleNamespace(percent=0)),
            patch.object(panel_app, "get_panel_url", return_value="unavailable"),
            patch.object(panel_app, "get_ssh_cmd", return_value="unavailable"),
        ):
            response = self.client.get("/api/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["fleet"], {"online": 0, "total": 0})
        self.assertEqual(response.json["bots"], {})

    def test_environment_endpoint_returns_env_values_but_not_pat(self):
        registry, definition = self.add_bot()
        set_bot_credentials(
            registry,
            definition["id"],
            {
                "pat": "synthetic-private-token",
                "environment": {"BOT_TOKEN": "synthetic-bot-token"},
            },
            panel_app.BOT_CREDENTIALS_KEY,
        )
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)

        response = self.client.get(f"/api/env/{definition['id']}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["env"], {"BOT_TOKEN": "synthetic-bot-token"})
        self.assertNotIn("synthetic-private-token", response.get_data(as_text=True))

    def test_add_bot_route_starts_background_install(self):
        with patch.object(panel_app.threading.Thread, "start"):
            response = self.client.post(
                "/api/bots",
                json={
                    "name": "Example Bot",
                    "repository": "https://github.com/example/telegram-bot.git",
                    "ref": "main",
                    "entrypoint": "src/bot.py",
                    "pat": "synthetic-token",
                    "environment": "BOT_TOKEN=synthetic-token OTHER_VAR = value-two",
                },
            )

        self.assertEqual(response.status_code, 202)
        self.assertIn("job", response.json)
        self.assertIn("bot", response.json)
        self.assertEqual(response.json["job"]["bot_name"], "Example Bot")
        self.assertEqual(response.json["job"]["definition"]["entrypoint"], "src/bot.py")
        self.assertNotIn("synthetic-token", response.get_data(as_text=True))

    def test_failed_install_can_be_edited_and_retried_without_resending_secrets(self):
        with patch.object(panel_app.threading.Thread, "start"):
            created = self.client.post(
                "/api/bots",
                json={
                    "name": "Example Bot",
                    "repository": "https://github.com/example/telegram-bot.git",
                    "ref": "main",
                    "entrypoint": "missing.py",
                    "pat": "synthetic-private-token",
                    "environment": "BOT_TOKEN=synthetic-bot-token",
                },
            )

        job_id = created.json["job"]["id"]
        panel_app._record_bot_job(job_id, "failed", 100, "Missing entrypoint", step="error")
        self.assertNotIn("synthetic-private-token", self.client.get("/api/bots/jobs").get_data(as_text=True))
        self.assertNotIn("synthetic-bot-token", self.client.get("/api/bots/jobs").get_data(as_text=True))

        with patch.object(panel_app.threading.Thread, "start"):
            retried = self.client.post(
                f"/api/bots/jobs/{job_id}/retry",
                json={"entrypoint": "src/bot.py", "pat": "", "environment": ""},
            )

        self.assertEqual(retried.status_code, 202)
        self.assertEqual(retried.json["job"]["status"], "queued")
        self.assertEqual(retried.json["job"]["definition"]["entrypoint"], "src/bot.py")
        saved_input = panel_app.BOT_INSTALL_PAYLOADS[job_id]
        self.assertEqual(saved_input["pat"], "synthetic-private-token")
        self.assertEqual(saved_input["environment"], {"BOT_TOKEN": "synthetic-bot-token"})
        self.assertNotIn("synthetic-private-token", retried.get_data(as_text=True))
        self.assertNotIn("synthetic-bot-token", retried.get_data(as_text=True))

    def test_retry_rejects_non_object_json(self):
        with patch.object(panel_app.threading.Thread, "start"):
            created = self.client.post(
                "/api/bots",
                json={
                    "name": "Example Bot",
                    "repository": "https://github.com/example/telegram-bot.git",
                    "ref": "main",
                    "entrypoint": "src/bot.py",
                },
            )
        job_id = created.json["job"]["id"]
        panel_app._record_bot_job(job_id, "failed", 100, "failed", step="error")

        response = self.client.post(f"/api/bots/jobs/{job_id}/retry", json=[])

        self.assertEqual(response.status_code, 400)

    def test_successful_install_discards_in_memory_retry_secrets(self):
        with patch.object(panel_app.threading.Thread, "start"):
            created = self.client.post(
                "/api/bots",
                json={
                    "name": "Example Bot",
                    "repository": "https://github.com/example/telegram-bot.git",
                    "ref": "main",
                    "entrypoint": "src/bot.py",
                    "pat": "synthetic-private-token",
                    "environment": "BOT_TOKEN=synthetic-bot-token",
                },
            )
        job_id = created.json["job"]["id"]
        payload = panel_app.BOT_INSTALL_PAYLOADS[job_id]

        with (
            patch.object(panel_app, "_clone_bot_repository"),
            patch.object(panel_app, "_install_bot_dependencies"),
            patch.object(panel_app, "_validate_repo_entrypoint"),
        ):
            panel_app._run_bot_install(job_id, payload["definition"], payload["pat"], payload["environment"])

        self.assertNotIn(job_id, panel_app.BOT_INSTALL_PAYLOADS)
        self.assertEqual(panel_app.BOT_INSTALL_JOBS[job_id]["status"], "success")

    def test_control_route_rejects_unknown_bot_id(self):
        response = self.client.post(f"/api/bot/{'b' * 32}/start")

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()