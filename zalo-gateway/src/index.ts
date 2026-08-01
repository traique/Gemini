import { ThreadType, Zalo, type Credentials } from "zca-js";
import { callBridge } from "./bridge.js";
import { loadConfig } from "./config.js";

const config = loadConfig();
const seen = new Map<string, number>();
let directQueue = Promise.resolve();

function remember(messageId: string): boolean {
  if (seen.has(messageId)) return false;
  seen.set(messageId, Date.now());
  if (seen.size > 2_000) {
    const oldest = seen.keys().next().value;
    if (oldest) seen.delete(oldest);
  }
  return true;
}

async function main(): Promise<void> {
  const zalo = new Zalo({ selfListen: false, checkUpdate: false, logging: false });
  const api = await zalo.login({
    cookie: config.cookie,
    imei: config.imei,
    userAgent: config.userAgent,
  } as Credentials);

  const ownId = api.getOwnId();
  console.log(`[zalo] authenticated account=${ownId}`);

  const listener = api.listener as any;
  listener.on("message", (message: any) => {
    if (message.isSelf || message.type !== ThreadType.User) return;
    const text = typeof message.data?.content === "string" ? message.data.content.trim() : "";
    const senderId = String(message.data?.uidFrom || "");
    const messageId = String(message.data?.msgId || message.data?.cliMsgId || "");
    if (!text || senderId !== config.controllerId || !messageId || !remember(messageId)) return;

    directQueue = directQueue
      .then(async () => {
        const result = await callBridge(config, {
          senderId,
          conversationId: String(message.threadId),
          messageId,
          text,
        });
        for (const reply of result.messages) {
          if (!reply.trim()) continue;
          await api.sendMessage({ msg: reply }, String(message.threadId), ThreadType.User);
        }
      })
      .catch((error) => console.error("[zalo] message processing failed", error));
  });

  const stopForSupervisor = (event: string, ...args: unknown[]) => {
    console.error(`[zalo] listener ${event}; exiting for supervisor restart`, ...args);
    process.exit(1);
  };
  listener.on("disconnected", (...args: unknown[]) => stopForSupervisor("disconnected", ...args));
  listener.on("closed", (...args: unknown[]) => stopForSupervisor("closed", ...args));
  listener.on("error", (error: unknown) => console.error("[zalo] listener error", error));
  listener.start();
  console.log("[zalo] listener started");
}

main().catch((error) => {
  console.error("[zalo] fatal startup error", error);
  process.exit(1);
});
