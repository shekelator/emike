# Messaging feature design — Telegram out/inbound with wake-and-announce

Status: implemented. Phases 1–2 are shipped and verified live (cross-modal
announcement sessions work on `amazon.nova-2-5-sonic`); phase 3 (§8.3
hardening — the `announce_during_conversation` toggle plus docs) closes the
feature. On-device verification items of §7 remain on the Pi checklist.

Feature: (1) anyone at the box can dictate a message that goes to Dave ("Mort")'s phone; (2) Dave replies from his phone and the idle appliance wakes up, plays a chime, and reads the message aloud over a Nova Sonic session — no button press.

Verified against primary sources during the design pass (things that may still need on-device verification are flagged in §8 / R1–R3):

- **Cross-modal (USER-role TEXT) input is officially supported in Nova Sonic.** AWS's "Cross-modal input" page documents a three-event sequence (`contentStart` role=USER type=TEXT interactive=true → `textInput` → `contentEnd`) usable "immediately after the session starts to prompt the model to begin speaking" — i.e., the exact announcement-mode use case. Caveat from the docs: "requires an active streaming session to function properly... otherwise standard session timeouts will be applied."
  Sources: https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-cross-modal.html , https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-input-events.html , re:Post mid-session USER text: https://repost.aws/questions/QUbIwfkF5rQ_yS2ydlxr8lag/
- **Telegram Bot API `getUpdates` is free, needs only outbound HTTPS to `api.telegram.org`, no public IP/port/webhook** — long-poll with `offset`/`timeout`; unconfirmed updates retained server-side 24 h; `getUpdates` and webhooks are mutually exclusive (advance the offset for every update, even ignored ones, or they redeliver forever).
  Sources: https://core.telegram.org/bots/api/methods/getupdates , https://core.telegram.org/bots/faq/
- **AWS SMS** is billed under "AWS End User Messaging" (ex-Pinpoint); US outbound ≈ $0.00645/segment + carrier fees; inbound requires a phone number (~$2/month toll-free) and delivery to a public webhook or Firehose — unusable behind home NAT.
  Sources: https://aws.amazon.com/end-user-messaging/pricing/ , https://aws.amazon.com/sns/sms-pricing/

## 1. Transport decision

**Recommendation: Telegram Bot API, long-polling, running inside the emike process.** No webhook, no public IP, no extra daemon, no per-message cost, and both directions get an existing native phone app.

| Option | Outbound | Inbound (replies reach a NAT'd Pi) | Cost | Ops burden | Verdict |
| --- | --- | --- | --- | --- | --- |
| **Telegram Bot API** | Free (`sendMessage`), bot can DM an allowlisted user | `getUpdates` long-poll — outbound-only TLS, works behind home NAT | $0 | Token + allowlist only | **Winner** |
| ntfy.sh | Publish to topic, phone push | Inbound works, but the topic *name is the credential* on the public server (no ACLs), no chat UX | $0 | Low | Fine fallback; worse trust model |
| Pushover | Excellent push, ~$5 one-time | **No inbound reply path** | $5 + free tier | Low | Push-only; fails half the feature |
| Signal (signal-cli) | Strong E2E privacy | signal-cli needs a registered number + Java daemon + device-linking ceremony | $0 | High (second daemon) | Privacy-max; too much ops |
| SNS / End User Messaging SMS | Easy, per-segment cost + carrier fees | Inbound needs a number (~$2/mo) and a **public webhook** | Recurring; 10DLC registration if long codes | Medium-high | Inbound path is exactly what doesn't work behind NAT |
| AWS IoT Core MQTT / EventBridge→SQS | Fine (MQTT is a good NAT traversal) | Fine — **but Dave needs a sender app we'd have to build** | Under free tier | High: certs, IAM, rule engine | Solves a problem Telegram solved for free |

Decisive factors: (a) the Pi sits behind home NAT; (b) the *phone side* is the real product — a native chat app beats anything we'd build; (c) volume is a handful of messages per week, so per-message pricing is pointless complexity.

**Bot setup (one time, ~10 min):**
1. Telegram → `@BotFather` → `/newbot` (e.g. "Mike at [lastname] house") → get the HTTP token.
2. Token → `.env` as `TELEGRAM_BOT_TOKEN` (gitignored; loaded by the existing systemd `EnvironmentFile`).
3. Send the bot any message once from Dave's phone; a helper script (`scripts/telegram/print-chat-ids.py`) prints observed `chat.id` values.
4. Put chat id(s) in `config/assistant.yaml` under `messaging.chat_allowlist`.
5. No webhook anywhere → `getUpdates` works as-is.

Privacy note: bot-API messages transit Telegram's cloud (bots can't use Secret Chats, so no E2E). For a family note-passing appliance this is acceptable.

## 2. Architecture overview

One process still owns everything (as today). Three new pieces plus one seam in `AssistantRuntime`:

```
                          ┌────────────────────────┐
Telegram API ◄──────────► │ TelegramPoller thread  │ (long-poll, offset, backoff)
   sendMessage ◄──────────┴───────┬────────────────┘
                                  │ call_soon_threadsafe
┌────────────────────────────  2. WakeBus  ───────────────┼─────────────────────┐
│ ButtonWakePump thread (button.wait_for_press loop)      │                     │
│        + TelegramPoller                                 ▼                     │
│ AssistantRuntime._run(): await wake_bus.next()  → inbox list                   │
│        press wake + inbox non-empty → chime → _run_announcement(msgs)          │
│        press wake                → existing push-to-talk/conversation path     │
└────────────────────────────────────────────────────────────────────────────────┘
```

- **`TelegramPoller`** — daemon thread: loops `transport.poll()` (long-poll `timeout=25`, HTTP timeout 35 s), filters to the chat allowlist, feeds each surviving update into the bus *and* the inbox via `loop.call_soon_threadsafe(...)`, advances the offset, sleeps with exponential backoff on errors (WiFi blips self-heal; never crashes the service).
- **`WakeBus`** — replaces the direct `await asyncio.to_thread(self._button.wait_for_press)` in `AssistantRuntime._run()`. A `ButtonWakePump` thread emits press events exactly like `ReleaseCounter` emits release counts; the poller emits message events. All existing semantics (push-to-talk barge-in, keyboard conversation toggle) are preserved because the main loop still sees every press.
- **`TelegramTransport`** (`src/emike/messaging.py`) — interface with two methods so tests never touch the network:

```python
class MessageTransport(Protocol):
    def send(self, chat_id: str, text: str) -> None: ...   # raises TransportError
    def poll(self) -> list[IncomingMessage]: ...           # long-poll, blocks ~25 s in caller thread

@dataclass(frozen=True)
class IncomingMessage:
    chat_id: str
    text: str
    update_id: int
    received_at: float
```

`TelegramTransport` implements it with `requests` (already a dependency): `POST /bot<token>/sendMessage` (truncate to 4000 chars; API limit 4096) and `GET /bot<token>/getUpdates` with `offset=last+1, timeout=25, allowed_updates=["message"]`. Advancing the offset for **every** update (including rejected-chat ones) is mandatory — otherwise redelivery loops forever.

**Offset persistence**: last confirmed `update_id` in `~/.cache/emike/telegram-offset`, rewritten when a batch is processed (at-most-once). Prevents re-announcing yesterday's messages after an appliance reboot.

## 3. Inbound wake-and-announce

### 3.1 Priority model

- **Idle + message** → chime, then one Nova Sonic "announcement session" reads it.
- **Active conversation + message** → buffered in the inbox; announced when the current turn ends. `announce_during_conversation: false` default (interrupting to read a note is hostile UX). When the toggle is true, the runtime cancels the running turn/conversation through the same cancel path a button press takes and the loop drain announces immediately — it never cancels an announcement in progress, which is itself the thing that will read the newly arrived message.
- **Idle + press while inbox non-empty** → the press wins (a real person takes precedence); pending messages announce after that turn ends.
- **Multiple queued messages** → batched into *one* announcement session ("You have 2 new messages… First: … Second: …") — conserves the Bedrock session quota and is better to listen to.
- **Cancellation**: pressing mid-announcement cancels it (barge-in semantics); unannounced messages return to the front of the inbox in original order, with a 3-attempt cap per message before it's dropped and logged (`announcement_dropped`).

### 3.2 Wake seam in `AssistantRuntime._run()`

```python
while True:
    self._led.set_state(LEDState.IDLE)
    if self._active is None and self._inbox:            # drain pending when idle
        msgs = self._drain_inbox()
        self._active = asyncio.create_task(self._run_announcement(request_id, msgs))
        continue
    wake = await self._wakes.next()                      # press or message
    if wake is PressWake:
        # existing logic unchanged: cancel active (barge-in / toggle), start turn
    # MessageWake: already buffered; the top of the loop announces it
```

The `ButtonWakePump` thread mirrors `ReleaseCounter`. LED: `LISTENING` before the chime; `SPEAKING` when the announcement's AUDIO content starts (existing `_receive` path).

### 3.3 Announcement session (`_run_announcement`)

Modeled on `_run_conversation` with two differences:

1. **Chime first** — before opening the Bedrock session (opens take ~1–3 s; the attention cue must not wait). `src/emike/chime.py` synthesizes a two-tone "ding" (E5→A5, ~0.6 s, soft attack) into 24 kHz PCM in numpy, played through a short-lived `PlaybackQueue` (`wait_until_drained(5.0)` via `asyncio.to_thread`) — no asset files, trivially testable.
2. **Silence streaming instead of mic audio**: the sender coroutine streams encoded silence (`encode_pcm_chunk(b"\x00\x00" * samples_per_chunk)`) at the normal chunk cadence into an open USER AUDIO content, like `_stream_mic_continuously` minus the button. This keeps the session streaming per the docs' cross-modal-text requirement, can't trip voice activity detection, and the real mic is never opened.

Then ~0.5 s after open, inject the scripted prompt via the documented cross-modal text path — new builder `build_user_text_events(prompt_name, content_name, text)` in `sonic_events.py` (`contentStart` role=USER type=TEXT interactive=true → `textInput` → plain `contentEnd`, fresh content name). Announcement script (verbatim-message policy, kid-safe):

```
Announce to the household that you have {n} new message{s} from Dad on their
phone — say that before anything else. Then read each one aloud, verbatim —
do not summarize, answer, or comment on them. Do not reveal anything else
about the phone conversation. Stop after reading the last one.
First message: "<text1>"
Second message: "<text2>"
```

Everything else reuses existing machinery verbatim: `_open_with_deadline` (10 s timeout + fallback-model retry + `_abandon_open`), `PlaybackQueue`, `_receive` non-conversation mode, `_close_session`. Watchdog via `turn_timeout_seconds`; failures take the existing `turn_error`/LED-ERROR path; failed announcements return to the inbox and retry on the next wake or a slow timer.

### 3.4 Risks and fallbacks

- **R1 — does `amazon.nova-2-5-sonic` accept cross-modal USER TEXT?** ~~AWS's doc targets "Nova 2 Sonic" while our primary model is nova-2-5. **Verify early, in phase 2 item 1.**~~ **Verified live: it does.** The announcement injects the script as USER text half a second into the silence stream (`ANNOUNCE_TEXT_DELAY_SECONDS = 0.5`); no fallback was needed (Fallback A: inject as additional SYSTEM content on the same stream; Fallback B (robust, still no local TTS): pre-render announcements with **Amazon Polly** (same AWS credentials, boto3 optional dep, effectively free at household volume) and play rendered PCM through `PlaybackQueue` with no Nova session at all).
- **R2 — model treats injected text as instructions.** Mitigations: allowlist, send rate limits, and the verbatim-reading prompt framing messages as data.
- **R3 — silence-stream semantics** (does the model respond only when audio flows?). Phase-2 verification; Fallback B sidesteps streaming entirely.

## 4. Outbound: the `send_message` tool

New tool in `ToolRouter`, registered when `messaging` is in `enabled_tools`, wired with an injected `MessageTransport` (like the existing `on_timer_elapsed` hook) so tests use a fake.

- Schema:

```json
{"type": "function", "function": {
  "name": "send_message",
  "description": "Send a short text message from Mike to someone's phone. Use only when the user at the box clearly asks to send a message to a person.",
  "parameters": {"type": "object", "properties": {
    "recipient": {"type": "string", "description": "Who to send to, e.g. 'mort'. Use exactly these names from the config."},
    "message": {"type": "string", "description": "The message text to send, in the sender's own words."}
  }, "required": ["recipient", "message"]}
}}
```

- Execution: validate `recipient` against `messaging.recipients` (unknown name → result string "Unknown recipient. Known people: mort." so the model self-corrects conversationally); truncate to 4000 chars; `transport.send(chat_id, text)`; success result `"Message sent to Mort's phone."` Errors take the existing `tool_error` → `{"error": ...}` → tool-result path.
- **Hallucinated-repeat safety**: per-process token bucket (default 5 messages / rolling 10 min, configurable). Empty bucket → tool returns "Send limit reached. Try again in a few minutes." so the model converses instead of erroring.
- **Decision (Dave): read back + confirm.** No separate confirm tool — the model's flow is: repeat the dictated message verbatim ("So: pizza night, right? Should I send it?") and wait; call `send_message` only after the user confirms ("yes", "send it"). The system prompt instructs this; the rate bucket remains as a backstop.
- System prompt additions:

```
When someone at the box asks to send a message to a person's phone, repeat the
message back exactly and get them to confirm before calling send_message. Only
send a message after that confirmation. When reading an announcement, read the
message verbatim and add nothing else about the phone conversation.
```

## 5. Security and privacy

- **Token**: `TELEGRAM_BOT_TOKEN` in `.env` (gitignored; systemd `EnvironmentFile`). Never logged — the transport redacts the token substring from exceptions/logs (`transport_error` event).
- **Chat allowlist, both directions**: inbound — poller drops updates whose `chat.id` isn't allowlisted (log `message_rejected` with chat id + text length, never the text). Outbound — `send_message` resolves only configured recipients, whose ids live inside the allowlist.
- **Arbitrary text read aloud to children**: only allowlisted senders can trigger announcements; the verbatim-read prompt minimizes model embellishment; no inbound message is ever executed as an intent.
- **Prompt injection from message bodies**: bounded by the allowlist and the send bucket; possible future hardening caps announcement length.
- **Transit**: outbound/inbound text passes Telegram's cloud without E2E (one line in README's design notes).

## 6. Config surface

`.env` (one new key): `TELEGRAM_BOT_TOKEN=…`

`config/assistant.yaml` new section; parsed into a `MessagingConfig` dataclass (`enabled: false` default → no yaml change required to keep running):

```yaml
messaging:
  enabled: true
  poll_timeout_seconds: 25
  chat_allowlist: [123456789]
  recipients:
    mort: "123456789"
  send_rate_per_minute: 5
  send_window_seconds: 600
  announce_chime: true
  announce_during_conversation: false
  max_announcements: 10
```

`enabled_tools` gains `messaging` (one entry registers `send_message`).

## 7. Testing and verification

Unit tests (no network/audio/AWS — CI-safe):

- `tests/test_messaging.py`: `FakeTransport`; offset advancing for rejected updates; allowlist filtering; token redaction in exception text; send truncation; token-bucket refill.
- `tests/test_tools.py` additions: `send_message` shape, unknown-recipient result.
- `tests/test_sonic_events.py` additions: `build_user_text_events` triplet shape.
- `tests/test_config.py` additions: `messaging:` parsing + defaults + backward compat.
- Optional `WakeBus` ordering test with a stub `ButtonInterface`.

Manual checklist:

- **Dev machine (macOS, `--keyboard`)**: print-chat-ids shows the right chat; "send a message to Mort saying pizza at six" → tool call lands; unknown recipient → graceful correction; send-limit exhaustion → polite result; allowed message while idle → chime + verbatim announcement; message during conversation → queued then announced; press mid-announcement → cancels, requeues; token never appears in logs.
- **Pi (systemd)**: WiFi blip (unplug 60 s) → poller recovers with backoff; reboot → persisted offset prevents duplicates; Bedrock 8-min/20-session quotas untouched (announcement sessions are short).

## 8. Phasing

1. **Phase 1 — outbound + transport plumbing (~1 day).** `messaging.py` (interface + TelegramTransport + poller skeleton + offset persistence + redaction), `MessagingConfig`, `send_message` in `ToolRouter`, system prompt line, unit tests, `.env.example`/README. Poller runs when enabled but stays silent (buffers only). **Shipped.**
2. **Phase 2 — WakeBus + chime + announcement session (~2 days).** `WakeBus`/`ButtonWakePump` (behavior-preserving; verify keyboard toggle + GPIO barge-in first), `chime.py`, `build_user_text_events`, `_run_announcement`, inbox + drain, **early R1 verification on `amazon.nova-2-5-sonic`** (pivot to Fallback B if cross-modal text is rejected). **Shipped and verified live** (see R1). Batching and requeue-on-cancel with the attempt cap landed here, ahead of phase 3.
3. **Phase 3 — hardening (~0.5 day).** Requeue-on-cancel with attempt cap and batching (done in phase 2), the `announce_during_conversation` toggle, README/design.md — **this change**. Remaining: the on-device part of the §7 manual checklist (Pi: WiFi-blip recovery, reboot offset persistence, quota check).

## 9. Open questions — decided by Dave

1. **Allowlist chats:** *both* Dave's chat and a second family chat (partner) — announcements and send_message recipients come from the allowlist.
2. **Send confirmation:** *read back + confirm* — the model repeats the dictated message and sends only after a yes (see §4).
3. **Escalation:** *once* — chime and read a single time, no re-chiming.
4. **Chime:** *soft synthesized ding* (two-tone E5→A5, ~0.6 s, `src/emike/chime.py`).
5. **Phone messages:** read-only announcements (default kept; no intent prefixes).
