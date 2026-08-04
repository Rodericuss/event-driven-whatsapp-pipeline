from __future__ import annotations

import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "openclaw/plugins/whatsapp-marketplace-importer/index.js"
SDK = Path.home() / ".local/lib/node_modules/openclaw/dist/plugin-sdk/plugin-entry.js"


class OpenClawPluginTests(unittest.TestCase):
    def run_plugin_probe(self) -> dict[str, object]:
        script = textwrap.dedent(
            """
            import { chmod, mkdir, readFile, writeFile } from "node:fs/promises";
            import { pathToFileURL } from "node:url";

            await mkdir(`${process.env.TEMP_ROOT}/scripts`, { recursive: true });
            await mkdir(`${process.env.TEMP_ROOT}/config`, { recursive: true });
            await writeFile(
              `${process.env.TEMP_ROOT}/config/settings.local.json`,
              JSON.stringify({
                group_publication: {
                  enabled: true,
                  groupJid: "ignored-camel-case-field",
                  group_jid: "100000000000000002@g.us",
                  group_name: "GRUPO DE PUBLICAÇÃO EXEMPLO",
                },
              }),
            );
            await writeFile(
              `${process.env.TEMP_ROOT}/scripts/stage-message`,
              `#!/usr/bin/env node
            let input = "";
            process.stdin.setEncoding("utf8");
            process.stdin.on("data", (chunk) => input += chunk);
            process.stdin.on("end", async () => {
              await import("node:fs/promises").then(({ writeFile }) =>
                writeFile(process.env.IMPORTER_ROOT + "/captured-payload.json", input)
              );
              process.stdout.write(JSON.stringify({ action: "captured", import_id: "test-import" }));
            });
            `,
            );
            await chmod(`${process.env.TEMP_ROOT}/scripts/stage-message`, 0o700);
            await writeFile(
              `${process.env.TEMP_ROOT}/scripts/handle-clarification`,
              `#!/usr/bin/env node
            let input = "";
            process.stdin.setEncoding("utf8");
            process.stdin.on("data", (chunk) => input += chunk);
            process.stdin.on("end", async () => {
              const payload = JSON.parse(input);
              if (payload.reaction_target_message_id) {
                await import("node:fs/promises").then(({ writeFile }) =>
                  writeFile(process.env.IMPORTER_ROOT + "/captured-reaction.json", input)
                );
                process.stdout.write(JSON.stringify({ handled: true, action: "reaction_ignored" }));
                return;
              }
              process.stdout.write(JSON.stringify({ handled: false }));
            });
            `,
            );
            await chmod(`${process.env.TEMP_ROOT}/scripts/handle-clarification`, 0o700);
            const source = (await readFile(process.env.PLUGIN_PATH, "utf8"))
              .replace(
                '"openclaw/plugin-sdk/plugin-entry"',
                JSON.stringify(pathToFileURL(process.env.SDK_PATH).href),
              );
            const modulePath = `${process.env.TEMP_ROOT}/plugin-under-test.mjs`;
            await writeFile(modulePath, source);
            const loadedPlugin = await import(pathToFileURL(modulePath).href);
            const plugin = loadedPlugin.default;
            const loadedGroupPublication = await loadedPlugin.loadGroupPublication(
              process.env.TEMP_ROOT,
            );
            const handlers = new Map();
            const routes = [];
            plugin.register({
              pluginConfig: {
                dryRun: false,
                projectRoot: process.env.TEMP_ROOT,
                allowedChatIds: ["5500000000000"],
                groupIntake: {
                  enabled: true,
                  groupJid: "100000000000000001@g.us",
                  approvalChatId: "5500000000000",
                  shadowMode: true,
                },
              },
              registerHttpRoute(route) {
                routes.push(route);
              },
              on(name, handler) {
                handlers.set(name, handler);
              },
              logger: { info() {}, warn() {} },
            });

            const beforeDispatch = handlers.get("before_dispatch");
            const messageReceived = handlers.get("message_received");
            const allowed = await beforeDispatch(
              { channel: "whatsapp", senderId: "+55 43 9633-6939" },
              { channelId: "whatsapp", conversationId: "5500000000000" },
            );
            const otherChat = await beforeDispatch(
              { channel: "whatsapp", senderId: "5511999999999" },
              { channelId: "whatsapp", conversationId: "5511999999999" },
            );
            const otherChannel = await beforeDispatch(
              { channel: "telegram", senderId: "5500000000000" },
              { channelId: "telegram", conversationId: "5500000000000" },
            );
            const sourceGroup = await beforeDispatch(
              { channel: "whatsapp", from: "100000000000000001@g.us" },
              {
                channelId: "whatsapp",
                conversationId: "100000000000000001@g.us",
                sessionKey: "agent:main:whatsapp:group:100000000000000001@g.us",
              },
            );
            const otherGroup = await beforeDispatch(
              { channel: "whatsapp", from: "100000000000000099@g.us" },
              {
                channelId: "whatsapp",
                conversationId: "100000000000000099@g.us",
                sessionKey: "agent:main:whatsapp:group:100000000000000099@g.us",
              },
            );
            await messageReceived(
              {
                from: "+5500000000000",
                senderId: "+5500000000000",
                messageId: "TEST-MESSAGE-ID",
                content: "Trator sintético ano 2024",
                timestamp: Date.parse("2026-07-19T12:00:00Z"),
                metadata: { mediaPath: "/tmp/test-image.png", mediaType: "image/png" },
              },
              { channelId: "whatsapp", conversationId: "5500000000000" },
            );
            const capturedPayload = JSON.parse(
              await readFile(`${process.env.TEMP_ROOT}/captured-payload.json`, "utf8"),
            );
            await messageReceived(
              {
                from: "100000000000000001@g.us",
                senderId: "+554399999999",
                messageId: "SOURCE-MESSAGE-ID",
                content: "Toyota Hilux SRX ano 2024, valor 275.000,00",
                timestamp: Date.parse("2026-08-01T12:10:00Z"),
                metadata: {
                  channelName: "GRUPO DE ORIGEM EXEMPLO",
                  groupId: "100000000000000001@g.us",
                  mediaPath: "/tmp/source-image.jpg",
                  mediaType: "image/jpeg",
                },
              },
              {
                channelId: "whatsapp",
                conversationId: "100000000000000001@g.us",
                sessionKey: "agent:main:whatsapp:group:100000000000000001@g.us",
              },
            );
            const capturedGroupPayload = JSON.parse(
              await readFile(`${process.env.TEMP_ROOT}/captured-payload.json`, "utf8"),
            );
            await messageReceived(
              {
                from: "+5500000000000",
                senderId: "+5500000000000",
                messageId: "REACTION-EVENT-ID",
                content: "ROMILDO_APPROVAL_REACTION:QUESTION-MESSAGE-ID:APPROVE",
                timestamp: Date.parse("2026-08-01T12:00:00Z"),
                metadata: {},
              },
              { channelId: "whatsapp", conversationId: "5500000000000" },
            );
            const capturedReaction = JSON.parse(
              await readFile(`${process.env.TEMP_ROOT}/captured-reaction.json`, "utf8"),
            );
            const allowedChats = new Set(["5500000000000"]);
            const groupIntake = {
              enabled: true,
              groupJid: "100000000000000001@g.us",
              approvalChatId: "5500000000000",
              shadowMode: false,
            };
            const secureWhatsApp = {
              dmPolicy: "allowlist",
              allowFrom: ["5500000000000"],
              groupPolicy: "allowlist",
              groupAllowFrom: ["*"],
              groups: {
                "100000000000000001@g.us": { requireMention: false },
              },
              ackReaction: { direct: true, group: "never" },
            };
            process.stdout.write(JSON.stringify({
              hooks: [...handlers.keys()].sort(),
              routePaths: routes.map((route) => route.path),
              allowed,
              otherChat: otherChat ?? null,
              otherChannel: otherChannel ?? null,
              sourceGroup,
              otherGroup: otherGroup ?? null,
              messageReceivedType: typeof messageReceived,
              buttonAction: loadedPlugin.parseActionButtonTest(
                "ROMILDO_BUTTON_TEST:abcdefghijklmnop:PUBLICAR",
              ),
              malformedButtonAction: loadedPlugin.parseActionButtonTest(
                "ROMILDO_BUTTON_TEST:curto:PUBLICAR",
              ),
              ordinaryTextAction: loadedPlugin.parseActionButtonTest(
                "PUBLICAR 90bc01fe",
              ),
              approvalReaction: loadedPlugin.parseApprovalReaction(
                "ROMILDO_APPROVAL_REACTION:ABCDEF0123456789ABCDEF:APPROVE",
              ),
              malformedApprovalReaction: loadedPlugin.parseApprovalReaction(
                "ROMILDO_APPROVAL_REACTION::APPROVE",
              ),
              safeAllowlistedIngress: loadedPlugin.isSafeWhatsAppIngress(
                secureWhatsApp,
                allowedChats,
                groupIntake,
              ),
              unsafeExtraGroupIngress: loadedPlugin.isSafeWhatsAppIngress(
                {
                  ...secureWhatsApp,
                  groups: {
                    ...secureWhatsApp.groups,
                    "100000000000000099@g.us": { requireMention: false },
                  },
                },
                allowedChats,
                groupIntake,
              ),
              unsafeGroupAckIngress: loadedPlugin.isSafeWhatsAppIngress(
                {
                  ...secureWhatsApp,
                  ackReaction: { direct: true, group: "always" },
                },
                allowedChats,
                groupIntake,
              ),
              transientReviewWaits: loadedPlugin.shouldWaitForProcessing(
                { status: "review_required", automation_failed: false },
                true,
              ),
              terminalReviewDoesNotWait: loadedPlugin.shouldWaitForProcessing(
                { status: "review_required", automation_failed: false },
                false,
              ),
              failedAutomationDoesNotWait: loadedPlugin.shouldWaitForProcessing(
                { status: "review_required", automation_failed: true },
                true,
              ),
              capturedReaction,
              capturedPayload,
              capturedGroupPayload,
              loadedGroupPublication,
            }));
            """
        )
        with tempfile.TemporaryDirectory() as temp_root:
            completed = subprocess.run(
                ["node", "--input-type=module", "--eval", script],
                check=True,
                capture_output=True,
                text=True,
                env={
                    "PATH": "/usr/bin:/bin",
                    "PLUGIN_PATH": str(PLUGIN),
                    "SDK_PATH": str(SDK),
                    "TEMP_ROOT": temp_root,
                },
            )
        return json.loads(completed.stdout)

    def test_personal_whatsapp_dispatch_is_suppressed(self) -> None:
        result = self.run_plugin_probe()
        self.assertEqual({"handled": True}, result["allowed"])

    def test_other_chats_and_channels_are_not_suppressed(self) -> None:
        result = self.run_plugin_probe()
        self.assertIsNone(result["otherChat"])
        self.assertIsNone(result["otherChannel"])

    def test_only_the_configured_source_group_dispatch_is_suppressed(self) -> None:
        result = self.run_plugin_probe()
        self.assertEqual({"handled": True}, result["sourceGroup"])
        self.assertIsNone(result["otherGroup"])

    def test_group_publication_accepts_only_the_exact_safe_ingress_shape(self) -> None:
        result = self.run_plugin_probe()
        self.assertTrue(result["safeAllowlistedIngress"])
        self.assertFalse(result["unsafeExtraGroupIngress"])
        self.assertFalse(result["unsafeGroupAckIngress"])

    def test_group_publication_loads_local_settings_before_legacy_path(self) -> None:
        result = self.run_plugin_probe()
        self.assertEqual(
            {
                "groupJid": "100000000000000002@g.us",
                "groupName": "GRUPO DE PUBLICAÇÃO EXEMPLO",
            },
            result["loadedGroupPublication"],
        )

    def test_capture_and_dispatch_hooks_are_both_registered(self) -> None:
        result = self.run_plugin_probe()
        self.assertIn("before_dispatch", result["hooks"])
        self.assertIn("message_received", result["hooks"])
        self.assertIn(
            "group_published_without_site", PLUGIN.read_text(encoding="utf-8")
        )
        self.assertEqual("function", result["messageReceivedType"])
        self.assertEqual(
            "TEST-MESSAGE-ID",
            result["capturedPayload"]["message_id"],
        )
        self.assertEqual(
            ["/tmp/test-image.png"],
            result["capturedPayload"]["media_paths"],
        )
        group = result["capturedGroupPayload"]
        self.assertEqual("100000000000000001", group["chat_id"])
        self.assertEqual("100000000000000001@g.us", group["chat_jid"])
        self.assertEqual("554399999999", group["sender_id"])
        self.assertEqual("5500000000000", group["approval_chat_id"])
        self.assertTrue(group["is_group"])
        self.assertTrue(group["intake_shadow_mode"])
        self.assertIn(
            "/api/romildonegocios/whatsapp/personal-album",
            result["routePaths"],
        )
        self.assertIn(
            "/api/romildonegocios/whatsapp/group-album",
            result["routePaths"],
        )
        self.assertIn(
            "/api/romildonegocios/whatsapp/action-buttons-test",
            result["routePaths"],
        )
        self.assertIn(
            "/api/romildonegocios/whatsapp/approval-reaction-test",
            result["routePaths"],
        )
        self.assertTrue(result["transientReviewWaits"])
        self.assertFalse(result["terminalReviewDoesNotWait"])
        self.assertFalse(result["failedAutomationDoesNotWait"])

    def test_action_button_callbacks_are_strictly_parsed(self) -> None:
        result = self.run_plugin_probe()
        self.assertEqual(
            {
                "valid": True,
                "token": "abcdefghijklmnop",
                "action": "PUBLICAR",
            },
            result["buttonAction"],
        )
        self.assertEqual({"valid": False}, result["malformedButtonAction"])
        self.assertIsNone(result["ordinaryTextAction"])

    def test_approval_reactions_are_strictly_parsed(self) -> None:
        result = self.run_plugin_probe()
        self.assertEqual(
            {
                "valid": True,
                "targetMessageId": "ABCDEF0123456789ABCDEF",
                "action": "APPROVE",
            },
            result["approvalReaction"],
        )
        self.assertEqual({"valid": False}, result["malformedApprovalReaction"])
        self.assertEqual(
            "QUESTION-MESSAGE-ID",
            result["capturedReaction"]["reaction_target_message_id"],
        )
        self.assertEqual("APPROVE", result["capturedReaction"]["reaction_action"])
        self.assertEqual("", result["capturedReaction"]["text"])

    def test_action_button_test_uses_phone_jid_not_lid_mapping(self) -> None:
        source = PLUGIN.read_text()
        self.assertIn('`${chatId}@s.whatsapp.net`', source)
        self.assertIn("toJid: result?.keys?.[0]?.remoteJid", source)


if __name__ == "__main__":
    unittest.main()
