# Zero-secrets Discord: when the token has to be in a WebSocket frame

Most agent platforms say "we handle credentials for you" and then hand the agent a file with the credentials in it. The agent dumps `os.environ`, reads the file, or asks its owner to paste a key — and the promise quietly breaks. On Slate, the architectural goal is stricter: **the agent VM holds zero real credentials, ever, for any integration.** Auth happens at the transport layer, injected by a proxy between the agent and the outside world. Dump memory, grep disk, read outbound traffic — you won't find a key, because there isn't one.

This works cleanly for HTTP APIs. A per-agent integration at `https://github-<vm>.int.exe.xyz` forwards to `api.github.com` and adds `Authorization: Bearer <token>` on the way through. The agent calls the integration URL with no auth header; GitHub sees the injected one. The agent can't leak the token because it's never in the agent's process.

Then we tried to add Discord, and the architecture didn't fit.

## The thing that made Discord special

Discord bots talk over two channels:

1. **REST** — `https://discord.com/api/v10/...`. Auth is `Authorization: Bot <token>`. Standard HTTP header. Normal reverse proxy pattern, same shape as every other integration we have.
2. **Gateway** — `wss://gateway.discord.gg`. Persistent WebSocket for real-time events (message received, reaction added, member joined). Auth is a JSON field inside the first WS frame the client sends after connecting, an `IDENTIFY` payload:

```json
{"op": 2, "d": {"token": "<bot-token>", "intents": 53608189, ...}}
```

The REST side is easy — existing integration pattern, one CLI call to wire up. The gateway side broke the architecture. An HTTP reverse proxy can add or overwrite headers on a request; it can't reach into a WebSocket frame's JSON body and rewrite a field. The token has to be in the frame, and the only thing that can put it there is the client that builds the frame.

Which is the client. Which is the agent VM. Which is not supposed to hold the token.

## The cheap-but-leaky options we considered

- **Put `DISCORD_BOT_TOKEN` in `.env`.** Standard path. Works in 90 seconds. Breaks the zero-secrets claim outright — the agent now has the token on disk, can read it, can be social-engineered into revealing it, can leak it through any of the normal ways credentials leak.
- **Use Discord's Interactions Endpoint URL (HTTP-only bot mode).** Discord calls *you* for slash commands and interactions, no WebSocket needed. Covers slash commands. Doesn't cover organic message events, which is most of what a conversational bot does. Useful but insufficient.
- **Monkey-patch discord.py to skip IDENTIFY.** Can't — Discord won't serve events to an unauthenticated gateway session.

None of those get us to "bot that listens to messages + zero tokens on the VM."

## What actually works

Split the two channels and handle the gateway specifically:

**REST (unchanged pattern).** Per-agent exe.dev integration `discord-<vm>.int.exe.xyz` targets `discord.com`, injects `Authorization: Bot <real>`. Agent calls the integration URL with no Authorization header of its own. exe.dev adds the bot header on the way to Discord.

**Gateway (custom proxy, protocol-aware).** We run a WebSocket proxy on our infra — `dg-proxy`, ~150 lines of Python — at `wss://discord-gateway.slate.ceo`. The agent opens a WS to that URL. `dg-proxy` opens its own WS to `gateway.discord.gg`, bidirectionally pumps frames, and does exactly one surgical rewrite: when the agent sends an `IDENTIFY` frame (op 2) with a placeholder token, the proxy replaces `d.token` with the real bot token before forwarding. Every other frame passes through byte-for-byte. The agent never knows what token Discord actually received; Discord never knows the agent sent anything other than the real token.

The real bot token lives in one place: the platform's SQLite database on the provisioner host, read only by `dg-proxy` at the moment it needs to stamp an IDENTIFY. The agent VM's `.env` has `DISCORD_BOT_TOKEN=proxy-managed-<vm>` — a deliberately useless string that would authenticate nothing if you tried to use it directly. It exists only because discord.py expects a non-empty token value; all it ever does is get replaced.

## How auth is not in the client's outbound traffic

One more detail, because "the placeholder is in the IDENTIFY frame on the wire" is still uncomfortable. At the REST layer, discord.py normally builds an `Authorization: Bot <token>` header from whatever token it was configured with. Even with a placeholder, that's a credential-shaped thing going out the client's socket. So we monkey-patch discord.py's `HTTPClient.request` to force `self.token = None` for the duration of every request. The library's own code checks `if self.token is not None: headers['Authorization'] = ...`; when it's None, no header is attached. The REST request leaves the agent with no authentication header whatsoever. The integration adds the real one between `sf1` and `discord.com`.

Net effect on the wire leaving the agent VM:

- **REST to `discord-<vm>.int.exe.xyz`:** no Authorization header at all.
- **WebSocket IDENTIFY frame to `dg-proxy`:** contains the placeholder string, which isn't a credential.

An attacker dumping the agent's memory, reading its filesystem, or sniffing its outbound traffic finds no real bot token. The real token only exists server-side, and the agent has no mechanism for reaching it.

## Making discord.py do this without forking it

Everything above is a runtime monkey-patch. We don't fork `discord.py`, we don't touch its source. A small module (`dg_patch.py`) installed in the agent's Python environment does four things at import time:

1. Sets `discord.http.Route.BASE` to the per-agent integration URL. Every REST call discord.py makes now goes to `https://discord-<vm>.int.exe.xyz/api/v10` instead of `discord.com`.
2. Wraps `HTTPClient.request` so `self.token` is cleared around every request. No `Authorization` header is ever built or sent.
3. Wraps `DiscordWebSocket.from_client` to mint a fresh single-use ticket from the provisioner API (via the per-agent admin integration) and use the returned `wss://discord-gateway.slate.ceo/tkt/<ticket>` URL instead of Discord's gateway URL. Tickets are scoped to (agent, service), 60-second TTL, single-use.
4. Forces WebSocket compression off (our proxy doesn't decode zlib frames — we kept it simple).

The module gets loaded at Python startup via a one-line `.pth` trick: a file `dg_patch.pth` in site-packages containing `import dg_patch`. Python processes every `.pth` file in site-packages at interpreter start and executes lines starting with `import`. This bypasses the "only one sitecustomize.py wins" problem that loses to Debian's system file, and runs our patches before `discord.py` is imported anywhere by hermes.

## What a single message round-trip looks like

Someone DMs the bot:

1. Discord delivers the message to an open WS session somewhere. That session is `dg-proxy` on `sf1`, not the agent VM.
2. `dg-proxy` forwards the frame byte-for-byte over the WS it holds with the agent. The agent's discord.py receives `MESSAGE_CREATE`.
3. Hermes processes the message, produces a reply, and calls `channel.send(...)` — which discord.py turns into `POST /channels/<id>/messages`.
4. The REST call goes to `discord-<vm>.int.exe.xyz` with no Authorization header. exe.dev's integration adds `Authorization: Bot <real>` and forwards to `discord.com`.
5. Discord posts the message to the channel.

Two proxies, one for each channel; both stateless with respect to the agent's identity aside from the ticket and the pre-provisioned integration.

## What this generalizes to

The pattern — "split auth between a transport-layer proxy and a protocol-aware proxy based on where the protocol puts its credentials" — applies to any service whose auth lives inside a payload rather than an HTTP header. Slack's RTM API, any custom WS protocol with a handshake message, anything that tunnels credentials in application-layer framing. The HTTP-reverse-proxy pattern covers the majority case; a protocol-aware shim covers the long tail.

The cost is real — `dg-proxy` is infrastructure we own and operate, it has to scale with gateway connection count, and every new gateway protocol is a new proxy. For now, one proxy per protocol family is fine. If we ever add a third protocol that can't be solved with a header injection, it'll be worth building a more general protocol-aware-proxy framework rather than n separate services.

## One more thing

Don't let the agent restart itself mid-debugging. When we first stood this up, sal (the canary agent) was helping its owner configure Discord over Telegram. Every time it thought something was wrong, it called `sudo systemctl restart hermes`, which tore down the WS mid-IDENTIFY and produced a series of confusing `Close 4000` traces in the proxy logs. The architecture was fine; the agent was flapping itself. A debug tip that's specific to public agents that can run sudo.
