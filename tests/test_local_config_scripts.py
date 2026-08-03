from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "bootstrap-local-config"
VALIDATE = ROOT / "scripts" / "validate-local-config"


class LocalConfigurationScriptsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        (self.root / "src").mkdir()
        shutil.copy2(ROOT / ".env.example", self.root / ".env.example")
        shutil.copy2(
            ROOT / "config" / "settings.example.json",
            self.root / "config" / "settings.example.json",
        )
        shutil.copytree(
            ROOT / "src" / "whatsapp_importer",
            self.root / "src" / "whatsapp_importer",
        )
        self.environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "IMPORTER_ROOT": str(self.root),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_script(
        self, script: Path, *arguments: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(script), *arguments],
            env=self.environment,
            text=True,
            capture_output=True,
            check=check,
        )

    def test_bootstrap_creates_private_files_and_legacy_link(self) -> None:
        self.run_script(BOOTSTRAP)
        local = self.root / "config" / "settings.local.json"
        dotenv = self.root / ".env"
        legacy = self.root / "config" / "settings.json"
        self.assertTrue(local.is_file())
        self.assertTrue(dotenv.is_file())
        self.assertTrue(legacy.is_symlink())
        self.assertEqual("settings.local.json", os.readlink(legacy))
        self.assertEqual(0o600, stat.S_IMODE(local.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(dotenv.stat().st_mode))
        result = self.run_script(VALIDATE)
        self.assertIn("Configuração local válida", result.stdout)

    def test_bootstrap_is_idempotent(self) -> None:
        self.run_script(BOOTSTRAP)
        before = (self.root / "config" / "settings.local.json").read_bytes()
        self.run_script(BOOTSTRAP)
        self.assertEqual(
            before, (self.root / "config" / "settings.local.json").read_bytes()
        )

    def test_bootstrap_refuses_an_unexpected_legacy_file(self) -> None:
        (self.root / "config" / "settings.json").write_text(
            json.dumps({"unsafe": True}), encoding="utf-8"
        )
        result = self.run_script(BOOTSTRAP, check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("não é o link", result.stderr)

    def test_public_example_validation_fails_closed(self) -> None:
        safe = self.run_script(VALIDATE, "--public-example")
        self.assertIn("Exemplo público seguro", safe.stdout)
        path = self.root / "config" / "settings.example.json"
        settings = json.loads(path.read_text())
        settings["group_publication"]["enabled"] = True
        path.write_text(json.dumps(settings), encoding="utf-8")
        unsafe = self.run_script(VALIDATE, "--public-example", check=False)
        self.assertNotEqual(0, unsafe.returncode)
        self.assertIn("sem destinos e sem escritas", unsafe.stderr)


if __name__ == "__main__":
    unittest.main()
