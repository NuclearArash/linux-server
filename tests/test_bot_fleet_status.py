import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bot_fleet_status.py"
SPEC = importlib.util.spec_from_file_location("bot_fleet_status", SCRIPT_PATH)
bot_fleet_status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bot_fleet_status)


class BotFleetStatusTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.registry_path = self.root / "bot-registry.json"
        self.pid_directory = self.root / "pids"
        self.pid_directory.mkdir()

    def write_registry(self, bots):
        self.registry_path.write_text(
            json.dumps({"version": 1, "bots": bots}),
            encoding="utf-8",
        )

    def test_lists_every_bot_with_live_status_and_escapes_names(self):
        running_id = "a" * 32
        stopped_id = "b" * 32
        self.write_registry([
            {"id": running_id, "name": "Orbit <Alerts>", "enabled": True},
            {"id": stopped_id, "name": "Paused Bot", "enabled": False},
        ])
        (self.pid_directory / f"bot-{running_id}.pid").write_text("4321\n", encoding="utf-8")
        (self.pid_directory / f"bot-{stopped_id}.pid").write_text("4322\n", encoding="utf-8")

        def check_process(pid, _signal):
            if pid != 4321:
                raise ProcessLookupError()

        with patch.object(bot_fleet_status.os, "kill", side_effect=check_process):
            lines = bot_fleet_status.render_bot_status_lines(
                self.registry_path,
                self.pid_directory,
            )

        self.assertEqual(
            lines,
            [
                "🟢 Orbit &lt;Alerts&gt;: RUNNING (PID 4321)",
                "🔴 Paused Bot: STOPPED",
            ],
        )

    def test_empty_registry_reports_no_configured_bots(self):
        self.write_registry([])

        lines = bot_fleet_status.render_bot_status_lines(
            self.registry_path,
            self.pid_directory,
        )

        self.assertEqual(lines, ["No bots configured"])

    def test_invalid_registry_fails_instead_of_reporting_an_empty_fleet(self):
        self.registry_path.write_text("{not-json", encoding="utf-8")

        with self.assertRaises(ValueError):
            bot_fleet_status.render_bot_status_lines(
                self.registry_path,
                self.pid_directory,
            )


class HermesTelegramConfigTests(unittest.TestCase):
    def test_configure_script_disables_telegram_restart_notifications(self):
        import subprocess

        bash = "bash"
        if os.name == "nt":
            self.skipTest("Hermes config shell integration requires a Bash environment")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hermes = root / "hermes"
            calls_file = root / "calls.txt"
            hermes.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HERMES_CALLS\"\n",
                encoding="utf-8",
            )
            hermes.chmod(0o755)
            environment = os.environ.copy()
            environment.update({
                "HERMES_HOME": str(root / "home"),
                "HERMES_BIN": str(hermes),
                "HERMES_CALLS": str(calls_file),
                "HERMES_TELEGRAM_BOT_TOKEN": "123456:" + "a" * 35,
                "HERMES_TELEGRAM_ALLOWED_USERS": "12345",
            })

            result = subprocess.run(
                [bash, str(Path(__file__).resolve().parents[1] / "scripts" / "configure-hermes-telegram.sh")],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn(
                "config set gateway.platforms.telegram.gateway_restart_notification false",
                calls_file.read_text(encoding="utf-8"),
            )

    def test_configure_script_disables_restart_notifications_without_telegram_secrets(self):
        import subprocess

        if os.name == "nt":
            self.skipTest("Hermes config shell integration requires a Bash environment")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hermes = root / "hermes"
            calls_file = root / "calls.txt"
            hermes.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HERMES_CALLS\"\n",
                encoding="utf-8",
            )
            hermes.chmod(0o755)
            environment = os.environ.copy()
            environment.pop("HERMES_TELEGRAM_BOT_TOKEN", None)
            environment.update({
                "HERMES_HOME": str(root / "home"),
                "HERMES_BIN": str(hermes),
                "HERMES_CALLS": str(calls_file),
            })

            result = subprocess.run(
                [bash, str(Path(__file__).resolve().parents[1] / "scripts" / "configure-hermes-telegram.sh")],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn(
                "config set gateway.platforms.telegram.gateway_restart_notification false",
                calls_file.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
