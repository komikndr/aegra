# Aegra Message Gateway

Direct Telegram-first messaging gateway for Aegra. It owns Telegram polling,
Keycloak device-code account linking, per-user assistant selection, durable
Aegra thread mapping, text streaming, bounded image preprocessing, and internal
ECharts PNG rendering for Telegram.

The service is not intended to be published directly. Telegram uses outbound
polling, so the gateway only exposes an internal readiness endpoint for Compose.
Every Telegram user must be on the numeric allowlist and must link an individual
Aegra identity before agent requests are accepted. No external gateway runtime
or proxy service is required.

Set these values in the local, ignored `.env` before starting the messaging
profile. Generate and retain the encryption and HMAC keys: changing either key
invalidates existing links or external identity mappings.

```bash
MESSAGE_GATEWAY_ENCRYPTION_KEY=<base64 encoded 32-byte key>
MESSAGE_GATEWAY_HMAC_KEY=<base64 encoded 32-byte key>
TELEGRAM_BOT_TOKEN=<BotFather token>
TELEGRAM_ALLOWED_USERS=<comma-separated numeric Telegram user IDs>
```

Start the core stack and optional messaging profile:

```bash
docker compose up -d
docker compose \
  -f docker-compose.yml \
  -f docker-compose.messaging.yml \
  --profile messaging up -d
```

Send `/link` to the bot, complete the Keycloak device flow in a browser, and
send `/link status`. `/agent` lists the assistants currently permitted for that
linked Aegra user. `/new` starts a fresh Aegra thread for the selected assistant,
and `/stop` cancels that conversation's active Aegra stream.

The transport processes conversations independently while preserving ordering
within each direct-message chat. Static and streaming output is split into
lossless Telegram-sized messages. Database processing leases recover messages
after crashes, and terminal idempotency records are retained for 30 days by
default.

Responses may include one strict fenced chart block:

````markdown
```echarts
{"xAxis":{"type":"category","data":["Q1"]},"yAxis":{},"series":[{"type":"bar","data":[42]}]}
```
````

The gateway withholds the raw JSON, sends the preceding prose, renders the
pure-data option through the internal `echarts-renderer` sidecar, then sends a
PNG before any following prose. Only lowercase `echarts` fences containing a
single strict JSON object are rendered. Invalid, oversized, or unavailable
charts are replaced with a short notice; the renderer is not published on a
host port. `MESSAGE_GATEWAY_CHART_RENDERER_TIMEOUT_SECONDS` and
`MESSAGE_GATEWAY_CHART_RENDERER_MAX_IMAGE_BYTES` bound renderer use.
