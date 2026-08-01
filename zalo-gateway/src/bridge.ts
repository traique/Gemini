import type { GatewayConfig } from "./config.js";

export type IncomingDirectMessage = {
  senderId: string;
  conversationId: string;
  messageId: string;
  text: string;
};

type BridgeResponse = {
  messages: string[];
  provider?: string | null;
};

export async function callBridge(
  config: GatewayConfig,
  message: IncomingDirectMessage,
): Promise<BridgeResponse> {
  const response = await fetch(config.bridgeUrl, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-zalo-bridge-secret": config.bridgeSecret,
      "idempotency-key": `zalo:${config.accountId}:${message.messageId}`,
    },
    body: JSON.stringify({
      account_id: config.accountId,
      sender_id: message.senderId,
      conversation_id: message.conversationId,
      message_id: message.messageId,
      text: message.text,
    }),
    signal: AbortSignal.timeout(120_000),
  });

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`Bridge returned ${response.status}: ${detail.slice(0, 200)}`);
  }
  return (await response.json()) as BridgeResponse;
}
