import type { GatewayConfig } from "./config.js";

export type IncomingDirectMessage = { senderId: string; conversationId: string; messageId: string; text: string };
export type IncomingGroupMessage = { groupId: string; messageId: string; senderId: string; senderName: string; text: string; sentAtMs: number };
type BridgeResponse = { messages: string[]; provider?: string | null };
type GroupConfig = { group_id: string; alias: string };
export type OutboxItem = { id: number; content: string };

function headers(config: GatewayConfig): Record<string, string> {
  return { "content-type": "application/json", "x-zalo-bridge-secret": config.bridgeSecret };
}

async function checked(response: Response, label: string): Promise<Response> {
  if (!response.ok) throw new Error(`${label} returned ${response.status}: ${(await response.text()).slice(0, 200)}`);
  return response;
}

export async function callBridge(config: GatewayConfig, message: IncomingDirectMessage): Promise<BridgeResponse> {
  const response = await fetch(`${config.bridgeBaseUrl}/message`, {
    method: "POST", headers: { ...headers(config), "idempotency-key": `zalo:${config.accountId}:${message.messageId}` },
    body: JSON.stringify({ account_id: config.accountId, sender_id: message.senderId, conversation_id: message.conversationId, message_id: message.messageId, text: message.text }),
    signal: AbortSignal.timeout(240_000),
  });
  return (await (await checked(response, "Bridge")).json()) as BridgeResponse;
}

export async function fetchAllowedGroups(config: GatewayConfig): Promise<Set<string>> {
  const response = await fetch(`${config.bridgeBaseUrl}/groups/${encodeURIComponent(config.accountId)}`, { headers: headers(config), signal: AbortSignal.timeout(15_000) });
  const groups = (await (await checked(response, "Group config")).json()) as GroupConfig[];
  return new Set(groups.map((group) => String(group.group_id)));
}

export async function storeGroupMessage(config: GatewayConfig, message: IncomingGroupMessage): Promise<void> {
  const response = await fetch(`${config.bridgeBaseUrl}/group-message`, {
    method: "POST", headers: headers(config),
    body: JSON.stringify({ account_id: config.accountId, group_id: message.groupId, message_id: message.messageId, sender_id: message.senderId, sender_name: message.senderName, text: message.text, sent_at_ms: message.sentAtMs }),
    signal: AbortSignal.timeout(20_000),
  });
  await checked(response, "Group message bridge");
}

export async function fetchOutbox(config: GatewayConfig): Promise<OutboxItem[]> {
  const url = `${config.bridgeBaseUrl}/outbox/${encodeURIComponent(config.accountId)}/${encodeURIComponent(config.controllerId)}`;
  const response = await fetch(url, { headers: headers(config), signal: AbortSignal.timeout(15_000) });
  return (await (await checked(response, "Outbox")).json()) as OutboxItem[];
}

export async function ackOutbox(config: GatewayConfig, itemId: number): Promise<void> {
  const response = await fetch(`${config.bridgeBaseUrl}/outbox/${itemId}/ack`, { method: "POST", headers: headers(config), signal: AbortSignal.timeout(15_000) });
  await checked(response, "Outbox ack");
}
