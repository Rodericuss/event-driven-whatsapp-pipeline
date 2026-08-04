import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { access, readFile, readdir } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

function digits(value) {
  return String(value ?? "").replace(/\D/g, "");
}

function stringArray(value) {
  return Array.isArray(value)
    ? value.filter((entry) => typeof entry === "string" && entry.length > 0)
    : [];
}

function resolvePersonalChatId(event, context) {
  return digits(
    context.conversationId ??
      event.conversationId ??
      event.senderId ??
      event.from,
  );
}

function normalizeGroupJid(value) {
  const match = String(value ?? "")
    .toLowerCase()
    .match(/(?:^|:)(\d+@g\.us)(?:$|:)/);
  return match?.[1] ?? "";
}

function resolveGroupJid(event, context) {
  const metadata = event.metadata ?? {};
  for (const value of [
    context.conversationId,
    event.from,
    metadata.groupId,
    metadata.originatingTo,
    event.sessionKey,
    context.sessionKey,
  ]) {
    const jid = normalizeGroupJid(value);
    if (jid) return jid;
  }
  return "";
}

export function isSafeWhatsAppIngress(
  whatsapp,
  allowedChats,
  groupIntake = {},
) {
  const configuredPersonalChats = stringArray(whatsapp?.allowFrom).map(digits);
  if (
    whatsapp?.dmPolicy !== "allowlist" ||
    configuredPersonalChats.length !== allowedChats.size ||
    !configuredPersonalChats.every((chatId) => allowedChats.has(chatId))
  ) {
    return false;
  }
  if (whatsapp?.groupPolicy === "disabled") {
    return true;
  }
  if (
    whatsapp?.groupPolicy !== "allowlist" ||
    groupIntake.enabled !== true ||
    !groupIntake.groupJid ||
    whatsapp?.ackReaction?.group !== "never"
  ) {
    return false;
  }
  const configuredGroups = Object.keys(whatsapp?.groups ?? {});
  if (
    configuredGroups.length !== 1 ||
    configuredGroups[0] !== groupIntake.groupJid ||
    whatsapp.groups[groupIntake.groupJid]?.requireMention !== false
  ) {
    return false;
  }
  const groupAllowFrom = stringArray(whatsapp?.groupAllowFrom);
  return groupAllowFrom.length === 1 && groupAllowFrom[0] === "*";
}

let whatsappRuntimePromise;
const completionWatchers = new Set();
const processingAcknowledged = new Set();
const processingTimers = new Map();
const activeProcessingWorkers = new Set();
const orphanFeedbackBatches = new Map();
const clarificationQuestionDispatches = new Set();
const actionButtonTests = new Map();
const approvalReactionTests = new Map();
const batchFlushTimers = new Map();
const ACTION_BUTTON_TEST_PREFIX = "ROMILDO_BUTTON_TEST:";
const ACTION_BUTTON_TEST_TTL_MS = 10 * 60 * 1000;
const APPROVAL_REACTION_PREFIX = "ROMILDO_APPROVAL_REACTION:";
const APPROVAL_REACTION_TEST_TTL_MS = 10 * 60 * 1000;
const BATCH_STABILITY_MS = 8_000;

export function shouldWaitForProcessing(status, processingPending) {
  return (
    status?.status === "review_required" &&
    status?.automation_failed !== true &&
    processingPending === true
  );
}

async function loadWhatsAppRuntime() {
  if (!whatsappRuntimePromise) {
    whatsappRuntimePromise = (async () => {
      const projectsRoot = path.join(os.homedir(), ".openclaw", "npm", "projects");
      const projects = (await readdir(projectsRoot))
        .filter((entry) => entry.startsWith("openclaw-whatsapp-"))
        .sort()
        .reverse();
      for (const project of projects) {
        const runtimePath = path.join(
          projectsRoot,
          project,
          "node_modules",
          "@openclaw",
          "whatsapp",
          "dist",
          "runtime-api.js",
        );
        try {
          const runtime = await import(pathToFileURL(runtimePath).href);
          if (
            typeof runtime.sendMessageWhatsApp === "function" &&
            typeof runtime.getActiveWebListener === "function" &&
            runtime.getActiveWebListener("default")
          ) {
            return runtime;
          }
        } catch {
          // Tenta a próxima instalação encontrada.
        }
      }
      throw new Error("runtime-api do plugin WhatsApp não encontrado");
    })();
  }
  try {
    return await whatsappRuntimePromise;
  } catch (error) {
    whatsappRuntimePromise = undefined;
    throw error;
  }
}

async function sendPersonalText(api, chatId, message) {
  const runtime = await loadWhatsAppRuntime();
  return runtime.sendMessageWhatsApp(`+${chatId}`, message, {
    cfg: api.runtime.config.current(),
    accountId: "default",
    verbose: false,
  });
}

export function parseActionButtonTest(content) {
  const text = String(content ?? "").trim();
  if (!text.startsWith(ACTION_BUTTON_TEST_PREFIX)) {
    return null;
  }
  const match = text.match(
    /^ROMILDO_BUTTON_TEST:([A-Za-z0-9_-]{16,64}):(PUBLICAR|CANCELAR)$/,
  );
  if (!match) {
    return { valid: false };
  }
  return {
    valid: true,
    token: match[1],
    action: match[2],
  };
}

export function parseApprovalReaction(content) {
  const text = String(content ?? "").trim();
  if (!text.startsWith(APPROVAL_REACTION_PREFIX)) {
    return null;
  }
  const match = text.match(
    /^ROMILDO_APPROVAL_REACTION:([A-Za-z0-9._-]{5,160}):(APPROVE|CANCEL)$/,
  );
  if (!match) {
    return { valid: false };
  }
  return {
    valid: true,
    targetMessageId: match[1],
    action: match[2],
  };
}

async function sendPersonalApprovalReactionTest(
  api,
  params,
  allowedChats,
  groupIntake,
) {
  const chatId = digits(params?.chatId);
  const approval = String(params?.approval ?? "");
  if (!allowedChats.has(chatId)) {
    throw new Error("chat pessoal fora do allowlist");
  }
  if (approval !== `TEST_APPROVAL_REACTIONS:${chatId}`) {
    throw new Error("aprovação literal do teste de reações inválida");
  }
  const cfg = api.runtime.config.current();
  const whatsapp = cfg.channels?.whatsapp;
  if (!isSafeWhatsAppIngress(whatsapp, allowedChats, groupIntake)) {
    throw new Error("proteções do WhatsApp não estão ativas");
  }
  const result = await sendPersonalText(
    api,
    chatId,
    "🧪 *TESTE SEGURO DE REAÇÃO*\n\nPressione e segure esta mensagem e reaja com 👍 ou 👎. Este teste não possui anúncio associado e não pode cadastrar ou publicar nada.",
  );
  if (!result?.messageId || result.messageId === "unknown") {
    throw new Error("WhatsApp não retornou o ID da mensagem de teste");
  }
  approvalReactionTests.set(result.messageId, {
    chatId,
    expiresAt: Date.now() + APPROVAL_REACTION_TEST_TTL_MS,
  });
  return {
    chatId,
    mode: "inert_reaction_test",
    expiresInSeconds: APPROVAL_REACTION_TEST_TTL_MS / 1000,
    messageId: result.messageId,
    toJid: result.toJid,
  };
}

async function sendPersonalActionButtonTest(
  api,
  params,
  allowedChats,
  groupIntake,
) {
  const chatId = digits(params?.chatId);
  const approval = String(params?.approval ?? "");
  if (!allowedChats.has(chatId)) {
    throw new Error("chat pessoal fora do allowlist");
  }
  if (approval !== `TEST_ACTION_BUTTONS:${chatId}`) {
    throw new Error("aprovação literal do teste inválida");
  }
  const cfg = api.runtime.config.current();
  const whatsapp = cfg.channels?.whatsapp;
  if (!isSafeWhatsAppIngress(whatsapp, allowedChats, groupIntake)) {
    throw new Error("proteções do WhatsApp não estão ativas");
  }
  const runtime = await loadWhatsAppRuntime();
  const listener = runtime.getActiveWebListener("default");
  if (!listener || typeof listener.sendButtons !== "function") {
    throw new Error("adaptador WhatsApp sem suporte aos botões de ação");
  }
  const token = randomBytes(18).toString("base64url");
  actionButtonTests.set(token, {
    chatId,
    expiresAt: Date.now() + ACTION_BUTTON_TEST_TTL_MS,
  });
  try {
    const result = await listener.sendButtons(
      `${chatId}@s.whatsapp.net`,
      "🧪 *TESTE SEGURO DOS BOTÕES*\n\nOs dois botões abaixo são inofensivos. Eles não possuem anúncio associado e não podem cadastrar ou publicar nada.",
      [
        {
          label: "Publicar",
          value: `${ACTION_BUTTON_TEST_PREFIX}${token}:PUBLICAR`,
        },
        {
          label: "Cancelar",
          value: `${ACTION_BUTTON_TEST_PREFIX}${token}:CANCELAR`,
        },
      ],
      { accountId: "default" },
    );
    const pending = actionButtonTests.get(token);
    if (pending) {
      pending.messageId = result?.messageId;
    }
    return {
      chatId,
      mode: "inert_test",
      expiresInSeconds: ACTION_BUTTON_TEST_TTL_MS / 1000,
      messageId: result?.messageId,
      toJid: result?.keys?.[0]?.remoteJid,
    };
  } catch (error) {
    actionButtonTests.delete(token);
    throw error;
  }
}

function scheduleOrphanFeedback(api, chatId) {
  const previous = orphanFeedbackBatches.get(chatId);
  if (previous?.timer) clearTimeout(previous.timer);
  const count = (previous?.count ?? 0) + 1;
  const timer = setTimeout(async () => {
    orphanFeedbackBatches.delete(chatId);
    try {
      await sendPersonalText(
        api,
        chatId,
        `Recebi ${count} foto${count === 1 ? "" : "s"}, mas não encontrei um texto de anúncio válido para associar. Envie primeiro o item com o ano e depois as fotos.`,
      );
    } catch (error) {
      api.logger.warn?.(`falha ao informar mídias órfãs: ${String(error)}`);
    }
  }, 4000);
  timer.unref?.();
  orphanFeedbackBatches.set(chatId, { count, timer });
}

function assertPersonalAlbumParams(params, allowedChats) {
  const importId = String(params.importId ?? "");
  const chatId = digits(params.chatId);
  const approval = String(params.approval ?? "");
  const message = String(params.message ?? "");
  const mediaUrls = stringArray(params.mediaUrls).map((entry) =>
    path.resolve(entry),
  );
  if (!/^[0-9a-f-]{36}$/i.test(importId)) {
    throw new Error("importId inválido");
  }
  if (!allowedChats.has(chatId)) {
    throw new Error("chat pessoal fora do allowlist");
  }
  if (approval !== `PUBLISH_PERSONAL:${importId}:${chatId}`) {
    throw new Error("aprovação literal inválida");
  }
  if (!message.trim() || mediaUrls.length < 1 || mediaUrls.length > 30) {
    throw new Error("publicação pessoal exige texto e entre 1 e 30 mídias");
  }
  const allowedRoot = path.resolve(
    os.homedir(),
    ".openclaw",
    "media",
    "outbound",
    "romildonegocios",
    importId,
  );
  for (const mediaUrl of mediaUrls) {
    if (
      mediaUrl !== allowedRoot &&
      !mediaUrl.startsWith(`${allowedRoot}${path.sep}`)
    ) {
      throw new Error("mídia fora da área de saída autorizada");
    }
  }
  return { importId, chatId, message, mediaUrls };
}

function assertMediaParams(params) {
  const importId = String(params.importId ?? "");
  const message = String(params.message ?? "");
  const mediaUrls = stringArray(params.mediaUrls).map((entry) =>
    path.resolve(entry),
  );
  if (!/^[0-9a-f-]{36}$/i.test(importId)) {
    throw new Error("importId inválido");
  }
  if (!message.trim() || mediaUrls.length < 1 || mediaUrls.length > 30) {
    throw new Error("publicação exige texto e entre 1 e 30 mídias");
  }
  const allowedRoot = path.resolve(
    os.homedir(),
    ".openclaw",
    "media",
    "outbound",
    "romildonegocios",
    importId,
  );
  for (const mediaUrl of mediaUrls) {
    if (
      mediaUrl !== allowedRoot &&
      !mediaUrl.startsWith(`${allowedRoot}${path.sep}`)
    ) {
      throw new Error("mídia fora da área de saída autorizada");
    }
  }
  return { importId, message, mediaUrls };
}

export async function loadGroupPublication(projectRoot) {
  const configuredPath = String(process.env.IMPORTER_SETTINGS_PATH ?? "").trim();
  const candidates = [
    configuredPath
      ? path.resolve(projectRoot, configuredPath)
      : null,
    path.join(projectRoot, "config", "settings.local.json"),
    path.join(projectRoot, "config", "settings.json"),
    path.join(projectRoot, "config", "settings.example.json"),
  ].filter(Boolean);
  let settings;
  for (const candidate of candidates) {
    try {
      settings = JSON.parse(await readFile(candidate, "utf8"));
      break;
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }
  if (!settings) throw new Error("configuração do projeto não encontrada");
  const publication = { ...(settings.group_publication ?? {}) };
  if (process.env.GROUP_PUBLICATION_ENABLED) {
    publication.enabled = ["1", "true", "yes", "on"].includes(
      process.env.GROUP_PUBLICATION_ENABLED.trim().toLowerCase(),
    );
  }
  if (process.env.PUBLICATION_GROUP_JID) {
    publication.group_jid = process.env.PUBLICATION_GROUP_JID.trim();
  }
  if (process.env.PUBLICATION_GROUP_NAME) {
    publication.group_name = process.env.PUBLICATION_GROUP_NAME.trim();
  }
  const groupJid = String(publication?.group_jid ?? "").trim();
  const groupName = String(publication?.group_name ?? "").trim();
  if (
    publication?.enabled !== true ||
    !/^\d+@g\.us$/.test(groupJid) ||
    !groupName
  ) {
    throw new Error("publicação no grupo não está configurada");
  }
  return { groupJid, groupName };
}

async function assertGroupAlbumParams(params, projectRoot) {
  const album = assertMediaParams(params);
  const configured = await loadGroupPublication(projectRoot);
  const groupJid = String(params.groupJid ?? "").trim();
  const groupName = String(params.groupName ?? "").trim();
  const approval = String(params.approval ?? "");
  if (
    groupJid !== configured.groupJid ||
    groupName !== configured.groupName
  ) {
    throw new Error("grupo fora do destino autorizado");
  }
  if (approval !== `PUBLISH_GROUP:${album.importId}:${groupJid}`) {
    throw new Error("aprovação literal do grupo inválida");
  }
  return { ...album, groupJid, groupName };
}

async function readJsonRequest(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 128 * 1024) {
      throw new Error("payload excede 128 KiB");
    }
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function sendJson(response, status, payload) {
  response.statusCode = status;
  response.setHeader("content-type", "application/json; charset=utf-8");
  response.end(JSON.stringify(payload));
}

async function sendPersonalAlbum(api, params, allowedChats, groupIntake) {
  const album = assertPersonalAlbumParams(params ?? {}, allowedChats);
  const cfg = api.runtime.config.current();
  const whatsapp = cfg.channels?.whatsapp;
  if (!isSafeWhatsAppIngress(whatsapp, allowedChats, groupIntake)) {
    throw new Error("proteções do WhatsApp não estão ativas");
  }
  const runtime = await loadWhatsAppRuntime();
  const result = await runtime.sendMessageWhatsApp(
    `+${album.chatId}`,
    album.message,
    {
      cfg,
      mediaUrls: album.mediaUrls,
      accountId: "default",
      verbose: false,
    },
  );
  return {
    importId: album.importId,
    chatId: album.chatId,
    deliveryMode: album.mediaUrls.length > 1 ? "native_album" : "single_media",
    mediaCount: album.mediaUrls.length,
    messageId: result.messageId,
    toJid: result.toJid,
  };
}

async function sendGroupAlbum(
  api,
  params,
  projectRoot,
  allowedChats,
  groupIntake,
) {
  const album = await assertGroupAlbumParams(params ?? {}, projectRoot);
  const cfg = api.runtime.config.current();
  const whatsapp = cfg.channels?.whatsapp;
  if (
    !isSafeWhatsAppIngress(whatsapp, allowedChats, groupIntake) ||
    (groupIntake.enabled === true && groupIntake.groupJid === album.groupJid)
  ) {
    throw new Error("proteções de entrada do WhatsApp não estão ativas");
  }
  const runtime = await loadWhatsAppRuntime();
  const result = await runtime.sendMessageWhatsApp(
    album.groupJid,
    album.message,
    {
      cfg,
      mediaUrls: album.mediaUrls,
      accountId: "default",
      verbose: false,
    },
  );
  return {
    importId: album.importId,
    groupJid: album.groupJid,
    groupName: album.groupName,
    deliveryMode: album.mediaUrls.length > 1 ? "native_album" : "single_media",
    mediaCount: album.mediaUrls.length,
    messageId: result.messageId,
    toJid: result.toJid,
  };
}

function runProjectJsonScript(projectRoot, scriptName, payload) {
  return new Promise((resolve, reject) => {
    const executable = path.join(projectRoot, "scripts", scriptName);
    const child = spawn(executable, scriptName === "ingest-message" ? ["--stdin-json"] : [], {
      cwd: projectRoot,
      env: {
        ...process.env,
        DRY_RUN: "false",
        IMPORTER_ROOT: projectRoot,
      },
      stdio: ["pipe", "pipe", "pipe"],
      timeout: 15_000,
    });

    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.once("error", reject);
    child.once("close", (code) => {
      if (code !== 0) {
        reject(
          new Error(
            `${scriptName} terminou com código ${code}: ${stderr || stdout}`.slice(
              0,
              1200,
            ),
          ),
        );
        return;
      }
      try {
        resolve(JSON.parse(stdout));
      } catch (error) {
        reject(new Error(`resposta inválida de ${scriptName}: ${String(error)}`));
      }
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

function runImporter(projectRoot, payload) {
  return runProjectJsonScript(projectRoot, "ingest-message", payload);
}

function stageImporterEvent(projectRoot, payload) {
  return runProjectJsonScript(projectRoot, "stage-message", payload);
}

function flushImporterEvents(projectRoot, payload) {
  return runProjectJsonScript(projectRoot, "flush-staged", payload);
}

function runClarification(projectRoot, payload) {
  return runProjectJsonScript(projectRoot, "handle-clarification", payload);
}

async function sendClarificationQuestion(
  api,
  projectRoot,
  clarification,
  fallbackChatId,
) {
  const importId = String(clarification.import_id ?? "");
  const chatId = digits(clarification.chat_id ?? fallbackChatId);
  if (
    !importId ||
    !chatId ||
    clarification.status !== "pending" ||
    clarification.question_sent_at ||
    clarificationQuestionDispatches.has(importId)
  ) {
    return;
  }
  clarificationQuestionDispatches.add(importId);
  try {
    const sent = await sendPersonalText(
      api,
      chatId,
      String(clarification.question ?? ""),
    );
    await runClarification(projectRoot, {
      _internal_action: "mark_question_sent",
      import_id: importId,
      question_message_id: sent?.messageId,
    });
  } finally {
    clarificationQuestionDispatches.delete(importId);
  }
}

async function resumePendingClarifications(api, projectRoot, allowedChats) {
  const pendingRoot = path.join(projectRoot, "anuncios", "pendentes");
  let entries;
  try {
    entries = await readdir(pendingRoot, { withFileTypes: true });
  } catch {
    return false;
  }
  let retryNeeded = false;
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const clarificationPath = path.join(
      pendingRoot,
      entry.name,
      "clarification.json",
    );
    try {
      await access(clarificationPath);
    } catch {
      continue;
    }
    try {
      const clarification = JSON.parse(
        await readFile(clarificationPath, "utf8"),
      );
      const chatId = digits(clarification.chat_id);
      if (allowedChats.has(chatId)) {
        await sendClarificationQuestion(
          api,
          projectRoot,
          clarification,
          chatId,
        );
      }
    } catch (error) {
      retryNeeded = true;
      api.logger.warn?.(
        `falha ao retomar esclarecimento ${entry.name}: ${String(error)}`,
      );
    }
  }
  return retryNeeded;
}

async function resumePendingClarificationsWithRetry(
  api,
  projectRoot,
  allowedChats,
) {
  for (let attempt = 1; attempt <= 12; attempt += 1) {
    const retryNeeded = await resumePendingClarifications(
      api,
      projectRoot,
      allowedChats,
    );
    if (!retryNeeded) return;
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
}

function enqueueProcessing(projectRoot, importId, delayMs = 15000) {
  if (!importId) return;
  const old = processingTimers.get(importId);
  if (old) clearTimeout(old);
  const workerPath = path.join(projectRoot, "scripts", "queue-worker");
  access(workerPath).catch(() => {});
  const timer = setTimeout(() => {
    processingTimers.delete(importId);
    activeProcessingWorkers.add(importId);
    const worker = spawn(workerPath, [importId], {
      cwd: projectRoot,
      env: { ...process.env, IMPORTER_ROOT: projectRoot, DRY_RUN: "false" },
      detached: true,
      stdio: "ignore",
    });
    const finished = () => activeProcessingWorkers.delete(importId);
    worker.once("error", finished);
    worker.once("exit", finished);
    worker.unref();
  }, delayMs);
  timer.unref?.();
  processingTimers.set(importId, timer);
}

function watchForCompletion(api, projectRoot, importId, chatId) {
  if (completionWatchers.has(importId)) return;
  completionWatchers.add(importId);
  const timer = setInterval(async () => {
    try {
      const status = JSON.parse(await (await import("node:fs/promises")).readFile(path.join(projectRoot, "anuncios", "pendentes", importId, "status.json"), "utf8"));
      if (
        status.status === "awaiting_clarification" ||
        status.status === "awaiting_publication_confirmation"
      ) {
        clearInterval(timer); completionWatchers.delete(importId);
        const clarification = JSON.parse(
          await (await import("node:fs/promises")).readFile(
            path.join(projectRoot, "anuncios", "pendentes", importId, "clarification.json"),
            "utf8",
          ),
        );
        await sendClarificationQuestion(
          api,
          projectRoot,
          clarification,
          chatId,
        );
        return;
      }
      const processingPending =
        processingTimers.has(importId) || activeProcessingWorkers.has(importId);
      if (shouldWaitForProcessing(status, processingPending)) {
        return;
      }
      if (status.status === "review_required" || status.automation_failed === true) {
        clearInterval(timer); completionWatchers.delete(importId);
        const runtime = await loadWhatsAppRuntime();
        const cfg = api.runtime.config.current();
        const reason = status.automation_error ?? (status.errors ?? status.warnings ?? ["revisão manual necessária"])[0];
        await runtime.sendMessageWhatsApp(`+${chatId}`, `Não foi possível preparar o anúncio ${importId.slice(0, 8)}: ${reason}`, { cfg, accountId: "default", verbose: false });
        return;
      }
      if (
        status.status === "personal_test_published" ||
        status.status === "group_published" ||
        status.status === "group_published_without_site" ||
        status.status === "group_published_site_pending"
      ) {
        clearInterval(timer); completionWatchers.delete(importId);
        if (status.status === "group_published_site_pending") {
          await sendPersonalText(
            api,
            chatId,
            `Anúncio ${importId.slice(0, 8)} publicado no grupo ${status.group_publication_name ?? "autorizado"} sem link. O cadastro no site ficou pendente.`,
          );
        } else if (status.status === "group_published_without_site") {
          await sendPersonalText(
            api,
            chatId,
            `Anúncio ${importId.slice(0, 8)} publicado no grupo ${status.group_publication_name ?? "autorizado"} sem link. O site está desativado neste fluxo.`,
          );
        } else if (status.status === "group_published") {
          await sendPersonalText(
            api,
            chatId,
            `Anúncio ${importId.slice(0, 8)} cadastrado como visível e publicado no grupo ${status.group_publication_name ?? "autorizado"}.`,
          );
        }
      }
    } catch { /* próxima verificação */ }
  }, 5000);
  timer.unref?.();
  const expiry = setTimeout(() => { clearInterval(timer); completionWatchers.delete(importId); }, 30 * 60 * 1000);
  expiry.unref?.();
}

async function handleFlushedBatch(api, projectRoot, response, fallbackChatId) {
  const batches = Array.isArray(response?.batches)
    ? response.batches
    : [response];
  for (const batch of batches) {
    const chatId = digits(batch?.approval_chat_id ?? fallbackChatId);
    if (!chatId) {
      api.logger.warn?.("lote sem chat pessoal de aprovação; feedback bloqueado");
      continue;
    }
    const results = Array.isArray(batch?.results) ? batch.results : [];
    const readyImportIds = new Set();
    let skippedMedia = 0;
    let rejectedEvents = 0;
    for (const result of results) {
      skippedMedia += Number(result?.media_skipped ?? 0);
      if (result?.action === "event_rejected") rejectedEvents += 1;
      if (result?.action === "orphan_media_stored") {
        scheduleOrphanFeedback(api, chatId);
      }
      if (result?.action === "media_attached" && result?.import_id) {
        readyImportIds.add(String(result.import_id));
      }
      if (
        result?.action === "clarification_media_attached" &&
        result?.import_id
      ) {
        await sendPersonalText(
          api,
          chatId,
          `As novas fotos foram anexadas ao anúncio ${String(result.import_id).slice(0, 8)}. Ainda preciso da resposta à pergunta pendente.`,
        );
      }
      api.logger.info?.(
        `whatsapp-marketplace-importer: ${result?.action ?? "capturado"} import_id=${result?.import_id ?? "-"}`,
      );
    }
    for (const importId of readyImportIds) {
      enqueueProcessing(projectRoot, importId, 0);
      watchForCompletion(api, projectRoot, importId, chatId);
    }
    if (readyImportIds.size > 0) {
      await sendPersonalText(
        api,
        chatId,
        `Recebi e separei ${readyImportIds.size} candidato${readyImportIds.size === 1 ? "" : "s"}. Vou validar cada um e enviarei os cartões de aprovação individualmente.`,
      );
    }
    if (skippedMedia > 0) {
      await sendPersonalText(
        api,
        chatId,
        `Ignorei ${skippedMedia} mídia${skippedMedia === 1 ? "" : "s"} que não era imagem. Isso não interrompeu os anúncios.`,
      );
    }
    if (rejectedEvents > 0) {
      await sendPersonalText(
        api,
        chatId,
        `${rejectedEvents} mensagem${rejectedEvents === 1 ? "" : "ens"} não puderam ser processadas e foram mantidas no diagnóstico.`,
      );
    }
  }
}

function scheduleBatchFlush(api, projectRoot, payload, approvalChatId) {
  const streamKey = `${payload.chat_id}:${payload.sender_id}`;
  const previous = batchFlushTimers.get(streamKey);
  if (previous) clearTimeout(previous);
  const timer = setTimeout(async () => {
    batchFlushTimers.delete(streamKey);
    try {
      const response = await flushImporterEvents(projectRoot, {
        chat_id: payload.chat_id,
        sender_id: payload.sender_id,
      });
      await handleFlushedBatch(api, projectRoot, response, approvalChatId);
    } catch (error) {
      api.logger.warn?.(`falha ao consolidar lote: ${String(error)}`);
      await sendPersonalText(
        api,
        approvalChatId,
        `Não consegui consolidar o lote recebido: ${String(error).slice(0, 500)}`,
      );
    }
  }, BATCH_STABILITY_MS);
  timer.unref?.();
  batchFlushTimers.set(streamKey, timer);
}

export default definePluginEntry({
  id: "whatsapp-marketplace-importer",
  name: "WhatsApp Marketplace Importer",
  description: "Captura anúncios pessoais, finaliza produtos visíveis e publica no grupo autorizado.",
  register(api) {
    const config = api.pluginConfig ?? {};
    const projectRoot = path.resolve(String(config.projectRoot ?? ""));
    const allowedChats = new Set(
      stringArray(config.allowedChatIds).map(digits).filter(Boolean),
    );
    const configuredGroupIntake = config.groupIntake ?? {};
    const groupIntake = {
      enabled: configuredGroupIntake.enabled === true,
      groupJid: normalizeGroupJid(configuredGroupIntake.groupJid),
      approvalChatId: digits(configuredGroupIntake.approvalChatId),
      shadowMode: configuredGroupIntake.shadowMode === true,
    };
    if (!projectRoot || allowedChats.size === 0) {
      throw new Error("projectRoot e allowedChatIds são obrigatórios");
    }
    if (
      groupIntake.enabled &&
      (!groupIntake.groupJid || !allowedChats.has(groupIntake.approvalChatId))
    ) {
      throw new Error(
        "groupIntake exige groupJid válido e approvalChatId pessoal permitido",
      );
    }

    api.registerHttpRoute({
      path: "/api/romildonegocios/whatsapp/personal-album",
      auth: "gateway",
      match: "exact",
      handler: async (request, response) => {
        if (request.method !== "POST") {
          sendJson(response, 405, { error: "método não permitido" });
          return true;
        }
        try {
          const result = await sendPersonalAlbum(
            api,
            await readJsonRequest(request),
            allowedChats,
            groupIntake,
          );
          sendJson(response, 200, result);
        } catch (error) {
          sendJson(response, 400, {
            error: String(error instanceof Error ? error.message : error),
          });
        }
        return true;
      },
    });

    api.registerHttpRoute({
      path: "/api/romildonegocios/whatsapp/group-album",
      auth: "gateway",
      match: "exact",
      handler: async (request, response) => {
        if (request.method !== "POST") {
          sendJson(response, 405, { error: "método não permitido" });
          return true;
        }
        try {
          const result = await sendGroupAlbum(
            api,
            await readJsonRequest(request),
            projectRoot,
            allowedChats,
            groupIntake,
          );
          sendJson(response, 200, result);
        } catch (error) {
          sendJson(response, 400, {
            error: String(error instanceof Error ? error.message : error),
          });
        }
        return true;
      },
    });

    api.registerHttpRoute({
      path: "/api/romildonegocios/whatsapp/action-buttons-test",
      auth: "gateway",
      match: "exact",
      handler: async (request, response) => {
        if (request.method !== "POST") {
          sendJson(response, 405, { error: "método não permitido" });
          return true;
        }
        try {
          const result = await sendPersonalActionButtonTest(
            api,
            await readJsonRequest(request),
            allowedChats,
            groupIntake,
          );
          sendJson(response, 200, result);
        } catch (error) {
          sendJson(response, 400, {
            error: String(error instanceof Error ? error.message : error),
          });
        }
        return true;
      },
    });

    api.registerHttpRoute({
      path: "/api/romildonegocios/whatsapp/approval-reaction-test",
      auth: "gateway",
      match: "exact",
      handler: async (request, response) => {
        if (request.method !== "POST") {
          sendJson(response, 405, { error: "método não permitido" });
          return true;
        }
        try {
          const result = await sendPersonalApprovalReactionTest(
            api,
            await readJsonRequest(request),
            allowedChats,
            groupIntake,
          );
          sendJson(response, 200, result);
        } catch (error) {
          sendJson(response, 400, {
            error: String(error instanceof Error ? error.message : error),
          });
        }
        return true;
      },
    });

    api.on("before_dispatch", (event, context) => {
      if ((context.channelId ?? event.channel) !== "whatsapp") {
        return;
      }
      const chatId = resolvePersonalChatId(event, context);
      const groupJid = resolveGroupJid(event, context);
      if (groupIntake.enabled && groupJid === groupIntake.groupJid) {
        api.logger.info?.(
          `whatsapp-marketplace-importer suprimiu dispatch do agente no grupo ${groupJid}`,
        );
        return { handled: true };
      }
      if (!allowedChats.has(chatId)) return;
      api.logger.info?.(
        `whatsapp-marketplace-importer suprimiu dispatch do agente no chat pessoal ${chatId}`,
      );
      return { handled: true };
    });

    api.on(
      "message_received",
      async (event, context) => {
        if (context.channelId !== "whatsapp") {
          return;
        }

        const metadata = event.metadata ?? {};
        const groupJid = resolveGroupJid(event, context);
        const isGroup = Boolean(groupJid);
        const personalChatId =
          resolvePersonalChatId(event, context) || digits(metadata.originatingTo);
        if (isGroup && (!groupIntake.enabled || groupJid !== groupIntake.groupJid)) {
          api.logger.warn?.(
            `whatsapp-marketplace-importer recusou grupo fora do allowlist: ${groupJid}`,
          );
          return;
        }
        if (!isGroup && !allowedChats.has(personalChatId)) {
          api.logger.warn?.(
            `whatsapp-marketplace-importer recusou chat fora do allowlist: ${personalChatId || "desconhecido"}`,
          );
          return;
        }
        const chatId = isGroup ? digits(groupJid) : personalChatId;
        const approvalChatId = isGroup
          ? groupIntake.approvalChatId
          : personalChatId;

        const buttonTest = isGroup ? null : parseActionButtonTest(event.content);
        if (buttonTest) {
          if (!buttonTest.valid) {
            api.logger.warn?.(
              "whatsapp-marketplace-importer recusou resposta de botão de teste malformada",
            );
            return;
          }
          const pending = actionButtonTests.get(buttonTest.token);
          actionButtonTests.delete(buttonTest.token);
          if (
            !pending ||
            pending.chatId !== chatId ||
            pending.expiresAt < Date.now()
          ) {
            await sendPersonalText(
              api,
              chatId,
              "Esse botão de teste expirou ou já foi usado. Nenhuma ação foi executada.",
            );
            return;
          }
          await sendPersonalText(
            api,
            chatId,
            `✅ Teste concluído: recebi o clique em “${buttonTest.action === "PUBLICAR" ? "Publicar" : "Cancelar"}”. Nenhum anúncio foi cadastrado ou publicado.`,
          );
          api.logger.info?.(
            `whatsapp-marketplace-importer: action_button_test_received action=${buttonTest.action}`,
          );
          return;
        }

        const approvalReaction = isGroup
          ? null
          : parseApprovalReaction(event.content);
        if (approvalReaction && !approvalReaction.valid) {
          api.logger.warn?.(
            "whatsapp-marketplace-importer recusou reação de aprovação malformada",
          );
          return;
        }
        if (approvalReaction) {
          const pendingTest = approvalReactionTests.get(
            approvalReaction.targetMessageId,
          );
          if (pendingTest) {
            approvalReactionTests.delete(approvalReaction.targetMessageId);
            if (
              pendingTest.chatId !== chatId ||
              pendingTest.expiresAt < Date.now()
            ) {
              await sendPersonalText(
                api,
                chatId,
                "Esse teste de reação expirou ou já foi usado. Nenhuma ação foi executada.",
              );
              return;
            }
            await sendPersonalText(
              api,
              chatId,
              `✅ Teste concluído: recebi a reação ${approvalReaction.action === "APPROVE" ? "👍" : "👎"}. Nenhum anúncio foi cadastrado ou publicado.`,
            );
            api.logger.info?.(
              `whatsapp-marketplace-importer: approval_reaction_test_received action=${approvalReaction.action}`,
            );
            return;
          }
        }

        const mediaPaths = stringArray(metadata.mediaPaths);
        if (mediaPaths.length === 0 && typeof metadata.mediaPath === "string") {
          mediaPaths.push(metadata.mediaPath);
        }
        const mediaTypes = stringArray(metadata.mediaTypes);
        if (mediaTypes.length === 0 && typeof metadata.mediaType === "string") {
          mediaTypes.push(metadata.mediaType);
        }

        const payload = {
          source: "whatsapp",
          chat_id: chatId,
          chat_jid: isGroup ? groupJid : "",
          chat_name: String(metadata.channelName ?? ""),
          sender_id: digits(
            event.senderId ?? metadata.senderE164 ?? event.from ?? chatId,
          ),
          sender_name: String(metadata.senderName ?? ""),
          message_id: String(event.messageId ?? context.messageId ?? ""),
          received_at: event.timestamp
            ? new Date(event.timestamp).toISOString()
            : new Date().toISOString(),
          text: approvalReaction ? "" : String(event.content ?? ""),
          reaction_target_message_id:
            approvalReaction?.targetMessageId ?? "",
          reaction_action: approvalReaction?.action ?? "",
          media_paths: mediaPaths,
          media_types: mediaTypes,
          is_group: isGroup,
          approval_chat_id: approvalChatId,
          approval_sender_id: approvalChatId,
          intake_shadow_mode: isGroup && groupIntake.shadowMode,
          is_forwarded: false,
        };

        let clarificationResult = { handled: false };
        if (!isGroup) {
          try {
            clarificationResult = await runClarification(projectRoot, payload);
          } catch (error) {
            api.logger.warn?.(`falha ao tratar esclarecimento: ${String(error)}`);
            await sendPersonalText(
              api,
              approvalChatId,
              `Não consegui registrar sua resposta: ${String(error).slice(0, 500)}`,
            );
            return;
          }
        }
        if (clarificationResult.handled === true) {
          if (clarificationResult.reply) {
            await sendPersonalText(api, chatId, clarificationResult.reply);
          }
          if (
            clarificationResult.action === "clarification_recorded" ||
            clarificationResult.action === "publication_confirmed"
          ) {
            enqueueProcessing(projectRoot, clarificationResult.import_id, 0);
            watchForCompletion(
              api,
              projectRoot,
              clarificationResult.import_id,
              approvalChatId,
            );
          }
          api.logger.info?.(
            `whatsapp-marketplace-importer: ${clarificationResult.action} import_id=${clarificationResult.import_id ?? "-"}`,
          );
          return;
        }
        if (approvalReaction) {
          api.logger.info?.(
            `whatsapp-marketplace-importer ignorou reação sem confirmação pendente target=${approvalReaction.targetMessageId}`,
          );
          return;
        }

        let staged;
        try {
          staged = await stageImporterEvent(projectRoot, payload);
        } catch (error) {
          api.logger.warn?.(`falha ao persistir mensagem: ${String(error)}`);
          await sendPersonalText(
            api,
            approvalChatId,
            `Não consegui guardar esta mensagem: ${String(error).slice(0, 500)}`,
          );
          return;
        }
        scheduleBatchFlush(api, projectRoot, payload, approvalChatId);
        api.logger.info?.(
          `whatsapp-marketplace-importer: ${staged.action ?? "event_staged"} sequence=${staged.observed_sequence ?? "-"}`,
        );
      },
      { timeoutMs: 20_000 },
    );

    const resumeTimer = setTimeout(() => {
      resumePendingClarificationsWithRetry(
        api,
        projectRoot,
        allowedChats,
      ).catch((error) => {
        api.logger.warn?.(`falha ao retomar esclarecimentos: ${String(error)}`);
      });
    }, 1500);
    resumeTimer.unref?.();

    const stagedResumeTimer = setTimeout(() => {
      flushImporterEvents(projectRoot, { all: true })
        .then((response) =>
          handleFlushedBatch(api, projectRoot, response, ""),
        )
        .catch((error) => {
          api.logger.warn?.(`falha ao retomar lotes persistidos: ${String(error)}`);
        });
    }, BATCH_STABILITY_MS);
    stagedResumeTimer.unref?.();
  },
});
