// Herald edge relay — one hibernating Durable Object per device.
//
// WHY THIS EXISTS
// ---------------
// Every Hermes install sits behind NAT, so it must dial out and hold a
// connection for the phone to reach it. On the GCE relay that connection is a
// live SSE stream in a Python process: 10k installs = 10k coroutines, 10k
// 25-second heartbeats and a Firestore write per reconnect, paid for whether
// or not anyone is talking.
//
// Here each device's connection lands on its own Durable Object and is
// accepted with the WebSocket *Hibernation* API. Once nothing is in flight
// the object is evicted from memory while the socket stays open; Cloudflare
// bills no duration for a hibernated object and nothing for incoming
// protocol pings. Cost therefore follows ACTIVE sessions (requests relayed),
// not installs — the 300-of-10k rule.
//
// Wire protocol on the agent socket (JSON text frames):
//   edge  -> agent  {t:"req", id, method, path, headers, body, stream}
//                   {t:"cancel", id}            caller went away
//                   {t:"event", data}           server push (task/approval)
//   agent -> edge   {t:"res", id, status, headers}
//                   {t:"chunk", id, data}       streaming body piece
//                   {t:"end", id}
//                   {t:"err", id, error}
//   "ping" -> "pong" is answered by the runtime itself (auto-response), so a
//   keepalive never wakes the object.

import { DurableObject } from "cloudflare:workers";

const AGENT_TAG = "agent";
const FIRST_BYTE_TIMEOUT_MS = 30_000;
// Request headers the caller may carry to the device's Hermes. Allowlist, not
// blocklist: hop-by-hop/framing headers must never be replayed locally.
const FORWARD_HEADERS = ["authorization", "accept", "content-type", "last-event-id"];

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });

async function deviceKey(token) {
  // The DO is named by a hash so the raw token never appears in object IDs,
  // logs or analytics.
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(token));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function bearer(request, url) {
  const h = request.headers.get("authorization") || "";
  const m = h.match(/^Bearer\s+(.+)$/i);
  return (m && m[1].trim()) || url.searchParams.get("token") || "";
}

// Tokens are minted by our backend; reject obvious junk before it costs a DO
// request (Workers requests are cheaper than DO requests).
const plausibleToken = (t) => typeof t === "string" && t.length >= 16 && t.length <= 256;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean);

    if (url.pathname === "/health") return json({ ok: true, service: "herald-edge" });

    // Device dials in:  GET /agent/connect  (Upgrade: websocket)
    if (parts[0] === "agent" && parts[1] === "connect") {
      if (request.headers.get("upgrade")?.toLowerCase() !== "websocket")
        return json({ error: "websocket_required" }, 426);
      const token = bearer(request, url);
      if (!plausibleToken(token)) return json({ error: "bad_token" }, 401);
      const stub = env.DEVICE.getByName(await deviceKey(token));
      return stub.fetch(new Request("https://do/agent", request));
    }

    // Server-to-device push (tasks, approvals) from the Herald backend.
    //   POST /deliver/<device_token>   Authorization: Bearer <EDGE_SERVICE_KEY>
    if (parts[0] === "deliver" && parts[1] && request.method === "POST") {
      if (!env.EDGE_SERVICE_KEY || bearer(request, url) !== env.EDGE_SERVICE_KEY)
        return json({ error: "unauthorized" }, 401);
      const stub = env.DEVICE.getByName(await deviceKey(decodeURIComponent(parts[1])));
      return stub.fetch(new Request("https://do/deliver", request));
    }

    // Caller reaches the device:  /d/<device_token>/<hermes path>
    if (parts[0] === "d" && parts[1]) {
      const token = decodeURIComponent(parts[1]);
      if (!plausibleToken(token)) return json({ error: "device_offline" }, 503);
      const rest = url.pathname.split("/").slice(3).join("/");
      const stub = env.DEVICE.getByName(await deviceKey(token));
      const inner = new URL(`https://do/relay/${rest}`);
      inner.search = url.search;
      return stub.fetch(new Request(inner, request));
    }

    return json({ error: "not_found" }, 404);
  },
};

export class Device extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    // In-flight requests only. A pending fetch keeps the object in memory, so
    // this map never has to survive hibernation: if it is empty we are idle.
    this.pending = new Map();
    this.ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
  }

  agent() {
    const socks = this.ctx.getWebSockets(AGENT_TAG);
    return socks.length ? socks[socks.length - 1] : null;
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/agent") return this.acceptAgent();
    if (url.pathname === "/deliver") return this.deliver(request);
    if (url.pathname.startsWith("/relay/")) return this.relay(request, url);
    return json({ error: "not_found" }, 404);
  }

  acceptAgent() {
    // One live agent per device: a reconnect replaces the old socket so a
    // half-open connection can never shadow the new one.
    for (const old of this.ctx.getWebSockets(AGENT_TAG)) {
      try { old.close(4000, "replaced"); } catch {}
    }
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    this.ctx.acceptWebSocket(server, [AGENT_TAG]);
    server.serializeAttachment({ since: Date.now() });
    return new Response(null, { status: 101, webSocket: client });
  }

  async deliver(request) {
    const ws = this.agent();
    if (!ws) return json({ delivered: false, error: "device_offline" }, 503);
    const data = await request.json().catch(() => null);
    ws.send(JSON.stringify({ t: "event", data }));
    return json({ delivered: true });
  }

  async relay(request, url) {
    const path = "/" + url.pathname.slice("/relay/".length) + url.search;

    if (path === "/_edge/status") {
      const ws = this.agent();
      const att = ws ? ws.deserializeAttachment() : null;
      return json({ online: !!ws, connected_since: att?.since ?? null });
    }

    const ws = this.agent();
    if (!ws) return json({ error: "device_offline" }, 503);

    const headers = {};
    for (const name of FORWARD_HEADERS) {
      const v = request.headers.get(name);
      if (v) headers[name] = v;
    }
    const alias = request.headers.get("x-hermes-key");
    if (alias && !headers.authorization) headers.authorization = `Bearer ${alias}`;

    let body = null;
    if (!["GET", "HEAD"].includes(request.method)) {
      const text = await request.text();
      body = text.length ? text : null;
    }

    const id = crypto.randomUUID();
    const stream = new TransformStream();
    const writer = stream.writable.getWriter();
    const enc = new TextEncoder();

    const head = new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.finish(id);
        resolve(json({ error: "device_timeout" }, 504));
      }, FIRST_BYTE_TIMEOUT_MS);
      this.pending.set(id, { resolve, timer, writer, enc, ws, started: false });
    });

    try {
      ws.send(JSON.stringify({ t: "req", id, method: request.method, path, headers, body }));
    } catch {
      this.finish(id);
      return json({ error: "device_offline" }, 503);
    }

    // If the phone/backend hangs up mid-stream, tell the device to stop
    // reading the local stream. The Hermes RUN keeps going; the caller resumes
    // it later with Last-Event-ID.
    request.signal?.addEventListener?.("abort", () => this.cancel(id));

    const res = await head;
    if (res.__stream) {
      return new Response(stream.readable, { status: res.status, headers: res.headers });
    }
    return res;
  }

  cancel(id) {
    const p = this.pending.get(id);
    if (!p) return;
    try { p.ws.send(JSON.stringify({ t: "cancel", id })); } catch {}
    this.finish(id);
  }

  finish(id) {
    const p = this.pending.get(id);
    if (!p) return;
    clearTimeout(p.timer);
    this.pending.delete(id);
    p.writer.close().catch(() => {});
  }

  async webSocketMessage(ws, message) {
    if (typeof message !== "string") return;
    let msg;
    try { msg = JSON.parse(message); } catch { return; }
    const p = msg.id ? this.pending.get(msg.id) : null;
    if (!p) return;

    if (msg.t === "res") {
      clearTimeout(p.timer);
      p.started = true;
      const h = new Headers(msg.headers || {});
      h.set("cache-control", "no-store");
      const res = { __stream: true, status: msg.status || 200, headers: h };
      p.resolve(res);
    } else if (msg.t === "chunk") {
      p.writer.write(p.enc.encode(msg.data || "")).catch(() => this.cancel(msg.id));
    } else if (msg.t === "end") {
      this.finish(msg.id);
    } else if (msg.t === "err") {
      if (!p.started) {
        clearTimeout(p.timer);
        p.resolve(json({ error: "device_error", detail: String(msg.error || "") }, 502));
      }
      this.finish(msg.id);
    }
  }

  async webSocketClose(ws) {
    this.failAll(ws);
  }

  async webSocketError(ws) {
    this.failAll(ws);
  }

  failAll(ws) {
    for (const [id, p] of this.pending) {
      if (p.ws !== ws) continue;
      if (!p.started) {
        clearTimeout(p.timer);
        p.resolve(json({ error: "device_offline" }, 503));
      }
      this.finish(id);
    }
  }
}
