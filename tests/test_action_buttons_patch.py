from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATCH_SCRIPT = ROOT / "scripts/patch-openclaw-whatsapp-action-buttons"
INSTALLED_PLUGIN = (
    Path.home()
    / ".openclaw/npm/projects/openclaw-whatsapp-290d7f7427"
    / "node_modules/@openclaw/whatsapp"
)


class ActionButtonsPatchTests(unittest.TestCase):
    def test_patch_is_valid_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plugin = Path(temporary) / "whatsapp"
            (plugin / "dist").mkdir(parents=True)
            shutil.copy2(
                INSTALLED_PLUGIN / "package.json",
                plugin / "package.json",
            )
            for name in ("send-api-Bjn-h80j.js", "monitor-CnioHf5V.js"):
                shutil.copy2(INSTALLED_PLUGIN / "dist" / name, plugin / "dist" / name)

            env = {
                **os.environ,
                "OPENCLAW_WHATSAPP_PLUGIN_DIR": str(plugin),
            }
            first = subprocess.run(
                [str(PATCH_SCRIPT)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            second = subprocess.run(
                [str(PATCH_SCRIPT)],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

            first_result = json.loads(first.stdout)
            second_result = json.loads(second.stdout)
            self.assertIn(first_result["changed"], (True, False))
            self.assertFalse(second_result["changed"])

            send_api = (plugin / "dist/send-api-Bjn-h80j.js").read_text()
            monitor = (plugin / "dist/monitor-CnioHf5V.js").read_text()
            self.assertIn("sendButtons: async", send_api)
            self.assertIn("extractInteractiveResponseText", send_api)
            self.assertIn("ROMILDO_APPROVAL_REACTION:", send_api)
            self.assertIn("reactionMessageId", send_api)
            self.assertIn("sendTrackedInteractiveButtons", monitor)
            self.assertIn("generateWAMessageFromContent", monitor)
            self.assertIn("additionalNodes", monitor)
            self.assertIn('tag: "biz"', monitor)
            self.assertIn('currentSock.ev.on("messages.update"', monitor)
            self.assertIn("await deliveryAck", monitor)
            self.assertIn("will not be retried automatically", monitor)


if __name__ == "__main__":
    unittest.main()
