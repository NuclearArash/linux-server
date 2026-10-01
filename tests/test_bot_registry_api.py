import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

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

    def test_file_search_returns_matching_files_and_folders_below_current_path(self):
        current = self.root / "workspace"
        nested = current / "nested"
        match_folder = current / "Needle Folder"
        nested.mkdir(parents=True)
        match_folder.mkdir()
        root_match = current / "needle.txt"
        nested_match = nested / "needles.py"
        outside_match = self.root / "outside-needle.txt"
        root_match.write_text("root", encoding="utf-8")
        nested_match.write_text("nested", encoding="utf-8")
        outside_match.write_text("outside", encoding="utf-8")

        response = self.client.get(
            "/api/files/search",
            query_string={"path": str(current), "q": "needle"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            {entry["path"] for entry in response.json["entries"]},
            {str(root_match), str(nested_match), str(match_folder)},
        )
        self.assertFalse(response.json["truncated"])

    def test_delete_file_explorer_entry_removes_file_or_folder(self):
        current = self.root / "workspace"
        current.mkdir()
        file_path = current / "remove.txt"
        folder_path = current / "remove-folder"
        nested_path = folder_path / "nested.txt"
        file_path.write_text("remove", encoding="utf-8")
        folder_path.mkdir()
        nested_path.write_text("remove", encoding="utf-8")

        for target in (file_path, nested_path, folder_path):
            response = self.client.post(
                "/api/files/delete",
                json={"path": str(target), "current_path": str(current)},
            )
            self.assertEqual(response.status_code, 200)
            self.assertFalse(target.exists())

    def test_delete_file_explorer_entry_rejects_paths_outside_current_directory(self):
        current = self.root / "workspace"
        current.mkdir()
        sibling = self.root / "keep.txt"
        sibling.write_text("keep", encoding="utf-8")

        response = self.client.post(
            "/api/files/delete",
            json={"path": str(sibling), "current_path": str(current)},
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(sibling.exists())

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

    def test_environment_parser_ignores_full_line_comments(self):
        environment = panel_app.parse_environment_assignments(
            "# credentials for the bot\nBOT_TOKEN = one two\n   # ignored too\nOTHER = value"
        )

        self.assertEqual(environment, {"BOT_TOKEN": "one two", "OTHER": "value"})

    def test_repository_metadata_returns_branches_and_default_branch_files(self):
        responses = [
            Mock(status_code=200, json=Mock(return_value={"default_branch": "main"})),
            Mock(status_code=200, json=Mock(return_value=[{"name": "main"}, {"name": "feature/ui"}])),
            Mock(status_code=200, json=Mock(return_value={
                "truncated": False,
                "tree": [
                    {"path": "src", "type": "tree"},
                    {"path": "src/bot.py", "type": "blob"},
                    {"path": "README.md", "type": "blob"},
                ],
            })),
        ]
        with patch.object(panel_app.requests, "get", side_effect=responses) as github_get:
            response = self.client.post(
                "/api/github/repository-metadata",
                json={"repository": "https://github.com/example/bot.git", "pat": "synthetic-pat"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["default_branch"], "main")
        self.assertEqual(response.json["branches"], ["main", "feature/ui"])
        self.assertEqual(response.json["files"], ["src/bot.py", "README.md"])
        self.assertFalse(response.json["files_truncated"])
        self.assertEqual(github_get.call_count, 3)
        self.assertEqual(github_get.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer synthetic-pat")
        self.assertNotIn("synthetic-pat", response.get_data(as_text=True))

    def test_repository_metadata_uses_saved_pat_for_editing_bot(self):
        registry, definition = self.add_bot()
        set_bot_credentials(
            registry,
            definition["id"],
            {"pat": "synthetic-saved-pat", "environment": {"BOT_TOKEN": "synthetic-bot-token"}},
            panel_app.BOT_CREDENTIALS_KEY,
        )
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)
        responses = [
            Mock(status_code=200, json=Mock(return_value={"default_branch": "main"})),
            Mock(status_code=200, json=Mock(return_value=[{"name": "main"}])),
            Mock(status_code=200, json=Mock(return_value={"truncated": False, "tree": [{"path": "src/bot.py", "type": "blob"}]})),
        ]
        with patch.object(panel_app.requests, "get", side_effect=responses) as github_get:
            response = self.client.post(
                "/api/github/repository-metadata",
                json={
                    "repository": definition["repository"],
                    "bot_id": definition["id"],
                    "pat": "",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(github_get.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer synthetic-saved-pat")
        self.assertNotIn("synthetic-saved-pat", response.get_data(as_text=True))
        self.assertNotIn("synthetic-bot-token", response.get_data(as_text=True))

    def test_repository_metadata_rejects_non_github_hosts(self):
        with patch.object(panel_app.requests, "get") as github_get:
            response = self.client.post(
                "/api/github/repository-metadata",
                json={"repository": "https://example.com/org/repo", "pat": "synthetic-pat"},
            )

        self.assertEqual(response.status_code, 400)
        github_get.assert_not_called()

    def test_repository_metadata_loads_files_for_selected_branch(self):
        responses = [
            Mock(status_code=200, json=Mock(return_value={"default_branch": "main"})),
            Mock(status_code=200, json=Mock(return_value={
                "truncated": False,
                "tree": [{"path": "src/feature_bot.py", "type": "blob"}],
            })),
        ]
        with patch.object(panel_app.requests, "get", side_effect=responses) as github_get:
            response = self.client.post(
                "/api/github/repository-metadata",
                json={
                    "repository": "https://github.com/example/bot.git",
                    "ref": "feature/ui",
                    "include_branches": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["ref"], "feature/ui")
        self.assertEqual(response.json["branches"], [])
        self.assertEqual(response.json["files"], ["src/feature_bot.py"])
        self.assertEqual(github_get.call_count, 2)
        self.assertIn("/git/trees/feature/ui", github_get.call_args_list[1].args[0])

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
        self.assertEqual(response.json["bot"]["directory_name"], "Example Bot")
        self.assertNotIn("synthetic-token", response.get_data(as_text=True))

    def test_bot_info_uses_named_directory_and_legacy_id_fallback(self):
        _, definition = self.add_bot()
        named_definition = {**definition, "directory_name": "Example Bot"}

        self.assertEqual(
            panel_app.get_bot_info(named_definition)["dir"],
            str(self.root / "bots" / "Example Bot"),
        )
        self.assertEqual(
            panel_app.get_bot_info(definition)["dir"],
            str(self.root / "bots" / definition["id"]),
        )

    def test_add_bot_rejects_directory_name_already_reserved_by_queued_install(self):
        payload = {
            "name": "Example Bot",
            "repository": "https://github.com/example/telegram-bot.git",
            "ref": "main",
            "entrypoint": "src/bot.py",
        }
        with patch.object(panel_app.threading.Thread, "start"):
            first = self.client.post("/api/bots", json=payload)
            second = self.client.post("/api/bots", json=payload)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)

    def test_edit_bot_updates_fields_env_and_icon_without_revealing_pat(self):
        registry, definition = self.add_bot()
        set_bot_credentials(
            registry,
            definition["id"],
            {"pat": "synthetic-private-token", "environment": {"BOT_TOKEN": "old-token"}},
            panel_app.BOT_CREDENTIALS_KEY,
        )
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)

        with patch.object(panel_app, "get_bot_proc", return_value=None):
            response = self.client.put(
                f"/api/bots/{definition['id']}",
                json={
                    "name": "Renamed Bot",
                    "repository": "https://github.com/example/new-bot.git",
                    "ref": "feature/branch",
                    "entrypoint": "package/main.py",
                    "icon": "🛰️",
                    "pat": "",
                    "environment": "BOT_TOKEN=new-token\n# ignored comment",
                },
            )

        self.assertEqual(response.status_code, 200)
        updated_registry = panel_app.get_bot_registry()
        updated = updated_registry["bots"][0]
        self.assertEqual(updated["name"], "Renamed Bot")
        self.assertEqual(updated["repository"], "https://github.com/example/new-bot.git")
        self.assertEqual(updated["ref"], "feature/branch")
        self.assertEqual(updated["entrypoint"], "package/main.py")
        self.assertEqual(updated["icon"], "🛰️")
        credentials = panel_app.get_bot_credentials(updated_registry, definition["id"], panel_app.BOT_CREDENTIALS_KEY)
        self.assertEqual(credentials["pat"], "synthetic-private-token")
        self.assertEqual(credentials["environment"], {"BOT_TOKEN": "new-token"})
        self.assertNotIn("synthetic-private-token", response.get_data(as_text=True))

    def test_update_bot_queues_stopped_update_using_saved_credentials(self):
        registry, definition = self.add_bot()
        definition["enabled"] = True
        set_bot_credentials(
            registry,
            definition["id"],
            {"pat": "synthetic-private-token", "environment": {"BOT_TOKEN": "synthetic-bot-token"}},
            panel_app.BOT_CREDENTIALS_KEY,
        )
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)

        with patch.object(panel_app, "stop_bot_process", return_value=True), patch.object(panel_app.threading.Thread, "start"):
            response = self.client.post(f"/api/bots/{definition['id']}/update")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["job"]["operation"], "update")
        self.assertFalse(response.json["job"]["definition"]["enabled"])
        saved_payload = panel_app.BOT_INSTALL_PAYLOADS[response.json["job"]["id"]]
        self.assertEqual(saved_payload["pat"], "synthetic-private-token")
        self.assertEqual(saved_payload["environment"], {"BOT_TOKEN": "synthetic-bot-token"})
        self.assertNotIn("synthetic-private-token", response.get_data(as_text=True))
        self.assertFalse(panel_app.get_bot_registry()["bots"][0]["enabled"])

    def test_start_bot_process_launches_bot_and_marks_it_enabled(self):
        registry, definition = self.add_bot()
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)
        bot_dir = self.root / "bots" / definition["id"]
        entrypoint = bot_dir / "src" / "bot.py"
        entrypoint.parent.mkdir(parents=True)
        entrypoint.write_text("print('started')", encoding="utf-8")
        venv_python = bot_dir / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.write_text("", encoding="utf-8")
        info = {
            **definition,
            "dir": str(bot_dir),
            "entry": definition["entrypoint"],
            "pid_path": str(self.root / "bot.pid"),
            "log_path": str(self.root / "bot.log"),
            "venv_python": str(venv_python),
        }

        with (
            patch.object(panel_app, "get_bot_info", return_value=info),
            patch.object(panel_app, "get_bot_proc", return_value=None),
            patch.object(panel_app.subprocess, "Popen", return_value=SimpleNamespace(pid=12345)) as process_start,
        ):
            status, message = panel_app.start_bot_process(definition["id"], definition)

        self.assertEqual(status, 200)
        self.assertEqual(message, "Example Bot started")
        self.assertTrue(panel_app.get_bot_registry()["bots"][0]["enabled"])
        self.assertEqual((self.root / "bot.pid").read_text(encoding="ascii"), "12345")
        process_start.assert_called_once()

    def test_update_worker_swaps_in_complete_staged_checkout(self):
        registry, definition = self.add_bot()
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)
        current_dir = self.root / "bots" / definition["id"]
        current_dir.mkdir(parents=True)
        (current_dir / "old-version.txt").write_text("old", encoding="utf-8")
        job_id = "update-test-job"
        panel_app._record_bot_job(job_id, "queued", 0, "Queued", bot_id=definition["id"])

        def clone_to_staging(_job_id, update_definition, _pat, repo_dir=None):
            staging_dir = Path(repo_dir)
            entrypoint = staging_dir / update_definition["entrypoint"]
            entrypoint.parent.mkdir(parents=True, exist_ok=True)
            entrypoint.write_text("new", encoding="utf-8")

        def start_updated_bot(bot_key, _definition):
            panel_app.set_bot_enabled(bot_key, True)
            return 200, "Example Bot started"

        with (
            patch.object(panel_app, "_clone_bot_repository", side_effect=clone_to_staging),
            patch.object(panel_app, "_install_bot_dependencies"),
            patch.object(panel_app, "start_bot_process", side_effect=start_updated_bot, create=True) as start_bot,
        ):
            panel_app._run_bot_update(job_id, definition, "", {})

        self.assertFalse((current_dir / "old-version.txt").exists())
        self.assertEqual((current_dir / "src" / "bot.py").read_text(encoding="utf-8"), "new")
        self.assertEqual(panel_app.BOT_INSTALL_JOBS[job_id]["status"], "success")
        start_bot.assert_called_once_with(definition["id"], definition)
        self.assertTrue(panel_app.get_bot_registry()["bots"][0]["enabled"])

    def test_update_worker_preserves_existing_checkout_when_clone_fails(self):
        registry, definition = self.add_bot()
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)
        current_dir = self.root / "bots" / definition["id"]
        current_dir.mkdir(parents=True)
        existing_file = current_dir / "working-state.txt"
        existing_file.write_text("keep me", encoding="utf-8")
        job_id = "failed-update-job"
        panel_app._record_bot_job(job_id, "queued", 0, "Queued", bot_id=definition["id"])

        with patch.object(panel_app, "_clone_bot_repository", side_effect=RuntimeError("network timeout")):
            panel_app._run_bot_update(job_id, definition, "", {})

        self.assertEqual(existing_file.read_text(encoding="utf-8"), "keep me")
        self.assertFalse(any(self.root.joinpath("bots").glob(f".{definition['id']}.update-*")))
        self.assertEqual(panel_app.BOT_INSTALL_JOBS[job_id]["status"], "failed")

    def test_get_bot_edit_metadata_does_not_reveal_credentials(self):
        registry, definition = self.add_bot()
        set_bot_credentials(
            registry,
            definition["id"],
            {"pat": "synthetic-private-token", "environment": {"BOT_TOKEN": "synthetic-bot-token"}},
            panel_app.BOT_CREDENTIALS_KEY,
        )
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)

        response = self.client.get(f"/api/bots/{definition['id']}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["bot"], definition)
        self.assertNotIn("synthetic-private-token", response.get_data(as_text=True))
        self.assertNotIn("synthetic-bot-token", response.get_data(as_text=True))

    def test_icon_only_edit_preserves_running_bot_state(self):
        registry, definition = self.add_bot()
        definition["enabled"] = True
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)

        with patch.object(panel_app, "stop_bot_process") as stop_bot:
            response = self.client.put(
                f"/api/bots/{definition['id']}",
                json={"icon": "🚀"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(panel_app.get_bot_registry()["bots"][0]["enabled"])
        self.assertEqual(panel_app.get_bot_registry()["bots"][0]["icon"], "🚀")
        stop_bot.assert_not_called()

    def test_delete_bot_removes_local_registry_credentials_and_directory_but_preserves_shared_cache(self):
        registry, definition = self.add_bot()
        set_bot_credentials(
            registry,
            definition["id"],
            {"pat": "synthetic-private-token", "environment": {"BOT_TOKEN": "synthetic-bot-token"}},
            panel_app.BOT_CREDENTIALS_KEY,
        )
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)
        bot_dir = self.root / "bots" / definition["id"]
        bot_dir.mkdir(parents=True)
        (bot_dir / "cached-state.txt").write_text("state", encoding="utf-8")

        with patch.object(panel_app, "stop_bot_process", return_value=True):
            response = self.client.delete(f"/api/bots/{definition['id']}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["deleted_cache_snapshots"], 0)
        self.assertIn("shared cache snapshots were preserved", response.json["message"].lower())
        self.assertEqual(panel_app.get_bot_registry()["bots"], [])
        self.assertEqual(panel_app.get_bot_registry()["encrypted_credentials"], {})
        self.assertFalse(bot_dir.exists())
        self.assertNotIn("synthetic-private-token", response.get_data(as_text=True))

    def test_delete_bot_rolls_back_directory_when_registry_save_fails(self):
        registry, definition = self.add_bot()
        save_registry(panel_app.BOT_REGISTRY_PATH, registry)
        bot_dir = self.root / "bots" / definition["id"]
        bot_dir.mkdir(parents=True)
        (bot_dir / "cached-state.txt").write_text("preserve", encoding="utf-8")

        with patch.object(panel_app, "stop_bot_process", return_value=True), patch.object(
            panel_app, "save_registry", side_effect=OSError("registry save failed")
        ):
            response = self.client.delete(f"/api/bots/{definition['id']}")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(panel_app.get_bot_registry()["bots"], [definition])
        self.assertEqual((bot_dir / "cached-state.txt").read_text(encoding="utf-8"), "preserve")

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

    def test_failed_install_error_redacts_pat(self):
        with patch.object(panel_app.threading.Thread, "start"):
            created = self.client.post(
                "/api/bots",
                json={
                    "name": "Example Bot",
                    "repository": "https://github.com/example/telegram-bot.git",
                    "ref": "main",
                    "entrypoint": "src/bot.py",
                    "pat": "synthetic-private-token",
                },
            )
        job = created.json["job"]
        payload = panel_app.BOT_INSTALL_PAYLOADS[job["id"]]

        with patch.object(
            panel_app,
            "_clone_bot_repository",
            side_effect=RuntimeError("Authentication failed for https://synthetic-private-token@github.com/example/repo.git"),
        ):
            panel_app._run_bot_install(job["id"], payload["definition"], payload["pat"], payload["environment"])

        failure = self.client.get("/api/bots/jobs").get_data(as_text=True)
        self.assertNotIn("synthetic-private-token", failure)
        self.assertIn("[REDACTED]", failure)

    def test_dependency_install_uses_extended_pip_timeout_and_retries(self):
        bot_id = "d" * 32
        repo_dir = self.root / "bots" / bot_id
        python_executable = repo_dir / ".venv" / "bin" / "python"
        python_executable.parent.mkdir(parents=True)
        python_executable.touch()
        (repo_dir / "requirements.txt").write_text("example-package\n", encoding="utf-8")
        definition = {"id": bot_id}

        with patch.object(panel_app, "_run_install_command") as run_command:
            panel_app._install_bot_dependencies("test-job", definition)

        command = run_command.call_args.args[0]
        self.assertIn("--timeout", command)
        self.assertIn("120", command)
        self.assertIn("--retries", command)
        self.assertIn("5", command)

    def test_dependency_read_timeout_is_reported_concisely(self):
        error = """ERROR: Exception: Traceback (most recent call last):
TimeoutError: The read operation timed out
pip._vendor.urllib3.exceptions.ReadTimeoutError: HTTPSConnectionPool(host='files.pythonhosted.org', port=443): Read timed out."""

        message = panel_app.format_bot_install_error(error)

        self.assertIn("Dependency download timed out", message)
        self.assertIn("retry the installation", message)
        self.assertNotIn("Traceback", message)
        self.assertNotIn("files.pythonhosted.org", message)

    def test_other_long_dependency_tracebacks_are_not_shown_to_user(self):
        error = "ERROR: Exception: Traceback (most recent call last):\n" + ("pip internal stack frame\n" * 80)

        message = panel_app.format_bot_install_error(error)

        self.assertEqual(message, "Installation failed with lengthy output. Check the dependency settings and retry.")
        self.assertNotIn("Traceback", message)

    def test_failed_dependency_job_does_not_return_pip_traceback(self):
        with patch.object(panel_app.threading.Thread, "start"):
            created = self.client.post(
                "/api/bots",
                json={
                    "name": "Slow Download Bot",
                    "repository": "https://github.com/example/telegram-bot.git",
                    "ref": "main",
                    "entrypoint": "src/bot.py",
                },
            )
        job_id = created.json["job"]["id"]
        payload = panel_app.BOT_INSTALL_PAYLOADS[job_id]
        read_timeout = "Traceback (most recent call last): ReadTimeoutError: HTTPSConnectionPool(host='files.pythonhosted.org', port=443): Read timed out."

        with patch.object(panel_app, "_clone_bot_repository"), patch.object(
            panel_app,
            "_install_bot_dependencies",
            side_effect=RuntimeError(read_timeout),
        ):
            panel_app._run_bot_install(job_id, payload["definition"], payload["pat"], payload["environment"])

        response = self.client.get(f"/api/bots/jobs/{job_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Dependency download timed out", response.json["job"]["message"])
        self.assertNotIn("Traceback", response.get_data(as_text=True))

    def test_control_route_rejects_unknown_bot_id(self):
        response = self.client.post(f"/api/bot/{'b' * 32}/start")

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()