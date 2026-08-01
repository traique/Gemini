import { ThreadType, Zalo, type Credentials } from "zca-js";
import { ackOutbox, callBridge, fetchAllowedGroups, fetchOutbox, storeGroupMessage } from "./bridge.js";
import { loadConfig } from "./config.js";

const config = loadConfig();
const seen = new Map<string, number>();
let directQueue = Promise.resolve();
let groupQueue = Promise.resolve();
let outboxQueue = Promise.resolve();
let allowedGroups = new Set<string>();

function remember(id: string): boolean {
  if (seen.has(id)) return false;
  seen.set(id, Date.now());
  if (seen.size > 5_000) { const oldest = seen.keys().next().value; if (oldest) seen.delete(oldest); }
  return true;
}

async function sendChunks(api: any, threadId: string, chunks: string[]): Promise<void> {
  for (const chunk of chunks) if (chunk.trim()) await api.sendMessage({ msg: chunk }, threadId, ThreadType.User);
}

async function refreshGroups(): Promise<void> {
  try { allowedGroups = await fetchAllowedGroups(config); console.log(`[zalo] tracking ${allowedGroups.size} group(s)`); }
  catch (error) { console.error("[zalo] cannot refresh group allowlist", error); }
}

async function listAvailableGroups(api: any, threadId: string): Promise<void> {
  const response: any = await api.getAllGroups();
  const ids = Object.keys(response?.gridVerMap || {});
  const lines = [ids.length ? "Các nhóm tài khoản B đang tham gia:" : "Tài khoản B chưa tham gia nhóm nào."];
  for (const id of ids.slice(0, 100)) {
    try {
      const info: any = await api.getGroupInfo(id);
      const data = info?.changed_groups?.[id] || info?.gridInfoMap?.[id];
      lines.push(`• ${data?.name || "Không rõ tên"} — ${id}`);
    } catch { lines.push(`• ${id}`); }
  }
  await sendChunks(api, threadId, lines.join("\n").match(/[\s\S]{1,1800}/g) || lines);
}

async function pollOutbox(api: any): Promise<void> {
  const items = await fetchOutbox(config);
  for (const item of items) {
    const chunks = item.content.match(/[\s\S]{1,1800}/g) || [item.content];
    await sendChunks(api, config.controllerId, chunks);
    await ackOutbox(config, item.id);
  }
}

async function main(): Promise<void> {
  const api = await new Zalo({ selfListen: false, checkUpdate: false, logging: false }).login({
    cookie: config.cookie, imei: config.imei, userAgent: config.userAgent,
  } as Credentials);
  console.log(`[zalo] authenticated account=${api.getOwnId()}`);
  await refreshGroups();
  setInterval(refreshGroups, config.groupRefreshMs).unref();
  setInterval(() => { outboxQueue = outboxQueue.then(() => pollOutbox(api)).catch((e) => console.error("[zalo] outbox failed", e)); }, config.outboxPollMs).unref();

  const listener = api.listener as any;
  listener.on("message", (message: any) => {
    if (message.isSelf) return;
    const text = typeof message.data?.content === "string" ? message.data.content.trim() : "";
    const senderId = String(message.data?.uidFrom || "");
    const messageId = String(message.data?.msgId || message.data?.cliMsgId || "");
    if (!text || !messageId || !remember(messageId)) return;

    if (message.type === ThreadType.Group) {
      const groupId = String(message.threadId);
      if (!allowedGroups.has(groupId)) return;
      let sentAtMs = Number(message.data?.ts || Date.now());
      if (sentAtMs < 1_000_000_000_000) sentAtMs *= 1000;
      groupQueue = groupQueue.then(() => storeGroupMessage(config, {
        groupId, messageId, senderId, senderName: String(message.data?.dName || ""), text, sentAtMs,
      })).catch((error) => console.error("[zalo] cannot store group message", error));
      return;
    }

    if (message.type !== ThreadType.User || senderId !== config.controllerId) return;
    directQueue = directQueue.then(async () => {
      if (text.toLowerCase() === "/nhomzalo") { await listAvailableGroups(api, String(message.threadId)); return; }
      const result = await callBridge(config, { senderId, conversationId: String(message.threadId), messageId, text });
      await sendChunks(api, String(message.threadId), result.messages);
      if (/^\/(themnhom|xoanhom)\b/i.test(text)) await refreshGroups();
    }).catch((error) => console.error("[zalo] message processing failed", error));
  });

  const stop = (event: string, ...args: unknown[]) => { console.error(`[zalo] listener ${event}`, ...args); process.exit(1); };
  listener.on("disconnected", (...args: unknown[]) => stop("disconnected", ...args));
  listener.on("closed", (...args: unknown[]) => stop("closed", ...args));
  listener.on("error", (error: unknown) => console.error("[zalo] listener error", error));
  listener.start();
  console.log("[zalo] listener started");
}

main().catch((error) => { console.error("[zalo] fatal startup error", error); process.exit(1); });
