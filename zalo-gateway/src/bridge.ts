import type { GatewayConfig } from "./config.js";

export type IncomingDirectMessage = {
  senderId: string;
  conversationId: string;
  messageId: string;
  text: string;
};

export type IncomingGroupMessage = {
  groupId: string;
  messageId: string;
  senderId: string;
  senderName: string;
  text: string;
  sentAtMs: number;
};

type BridgeResponse = { messages: string[]; provider?: string | null };
type GroupConfig = { group_id: string; alias: string };

function headers(config: GatewayConfig): Record<string, string> {
  return {
    "content-type": "application/json",
    "x-zalo-bridge-secret": config.bridgeSecret,
  };
}

export async function callBridge(
  config: GatewayConfig,
  message: IncomingDirectMessage,
): Promise<BridgeResponse> {
  const response = await fetch(`${config.bridgeBaseUrl}/message`, {
    method: "POST",
    headers: {
      ...headers(config),
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
  if (!response.ok) throw new Error(`Bridge returned ${response.status}: ${(await response.text()).slice(0, 200)}`);
  return (await response.json()) as BridgeResponse;
}

export async function fetchAllowedGroups(config: GatewayConfig): Promise<Set<string>> {
  const response = await fetch(
    `${config.bridgeBaseUrl}/groups/${encodeURIComponent(config.accountId)}`,
    { headers: headers(config), signal: AbortSignal.timeout(15_000) },
  );
  if (!response.ok) throw new Error(`Group config returned ${response.status}`);
  const groups = (await response.json()) as GroupConfig[];
  return new Set(groups.map((group) => String(group.group_id)));
}

export async function storeGroupMessage(
  config: GatewayConfig,
  message: IncomingGroupMessage,
): Promise<void> {
  const response = await fetch(`${config.bridgeBaseUrl}/group-message`, {
    method: "POST",
    headers: headers(config),
    body: JSON.stringify({
      account_id: config.accountId,
      group_id: message.groupId,
      message_id: message.messageId,
      sender_id: message.senderId,
      sender_name: message.senderName,
      text: message.text,
      sent_at_ms: message.sentAtMs,
    }),
    signal: AbortSignal.timeout(20_000),
  });
  if (!response.ok) throw new Error(`Group message bridge returned ${response.status}`);
}
