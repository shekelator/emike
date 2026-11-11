# emike

Push-to-talk Raspberry Pi voice assistant powered by **Amazon Nova Sonic**
(speech-to-speech on Amazon Bedrock). Hold the button, talk, release — the
model streams audio back and it plays as it arrives, with tool calls executed
locally.

## How it works

- Hold the GPIO button → the Pi opens one fresh Nova Sonic bidirectional
  stream and streams microphone audio (16 kHz, 16-bit, mono PCM).
- On button release, the user turn ends and the model answers with streamed
  24 kHz speech plus transcripts (text events).
- Tool calls (`get_current_time`, `get_weather` / `get_weather_forecast`,
  `web_search`) run locally and results are returned to the model.
- Pressing the button while the assistant is speaking immediately stops
  playback and starts the new turn (barge-in).
- Each press is its own session: there is no memory of previous presses, and
  a failed turn reconnects automatically on the next press.

## Requirements

- Raspberry Pi OS 64-bit (Bookworm recommended), Raspberry Pi 5
- Python **3.12+** (installed automatically via `uv`; Pi OS ships 3.11)
- USB microphone/speaker or USB speakerphone (PipeWire/ALSA)
- GPIO push-button (default pin 17, pull-up) and optional status LED (pin 27)
- An AWS account with Nova Sonic model access (below)

## AWS setup (one time)

1. **Enable model access** in the Bedrock console for
   `amazon.nova-2-5-sonic` (and optionally the fallback
   `amazon.nova-2-sonic-v1:0`) in your chosen region
   (us-east-1, us-west-2, eu-north-1, or ap-northeast-1):
   `console.aws.amazon.com/bedrock/home?region=<region>#/modelaccess`.
2. **IAM permission.** The Pi's credentials need exactly one Bedrock action:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": ["bedrock:InvokeModelWithBidirectionalStream"],
       "Resource": [
         "arn:aws:bedrock:<region>:<account>:foundation-model/amazon.nova-2-5-sonic",
         "arn:aws:bedrock:<region>:<account>:foundation-model/amazon.nova-2-sonic-v1:0"
       ]
     }]
   }
   ```

   `scripts/aws/emike-sonic-policy.json` contains this policy (edit the
   account/region ARNs or leave `*`).
3. **Quick setup script** (creates the IAM user, attaches the policy, checks
   model availability, prints keys for `.env`):

   ```bash
   bash scripts/aws/setup-aws.sh us-east-1
   ```

   Or by hand with the AWS CLI:

   ```bash
   aws iam create-user --user-name emike-assistant
   aws iam create-policy --policy-name EmikeNovaSonicInvoke \
     --policy-document file://scripts/aws/emike-sonic-policy.json
   aws iam attach-user-policy --user-name emike-assistant \
     --policy-arn <policy-arn-from-create-policy>
   aws iam create-access-key --user-name emike-assistant
   ```

4. Put the resulting keys in `.env` (see `.env.example`): `AWS_ACCESS_KEY_ID`,
   `AWS_SECRET_ACCESS_KEY`, and if needed `AWS_SESSION_TOKEN`. emike resolves
   credentials through the standard SDK chain: environment variables first
   (what `.env` sets on the Pi — loaded by the systemd unit), then the usual
   `~/.aws` profile files (handy on a dev machine).

## Quick start (developer machine)

Requires Python **3.12 or newer** — the system Python of a stock machine
(e.g. 3.9.6 on macOS) will not work directly, so create a venv first. If you
have `uv`, it downloads a matching interpreter itself:

```bash
uv venv --python 3.12 .venv      # or: python3.12 -m venv .venv
source .venv/bin/activate
uv pip install -e '.[dev]'       # or: python -m pip install --upgrade pip && pip install -e '.[dev]'
cp config/assistant.example.yaml config/assistant.yaml
cp .env.example .env && nano .env   # add AWS credentials
emike --keyboard --config config/assistant.yaml
```

Keyboard mode: press Enter to start capture, Enter again to release. GPIO is
not needed for development. Do not call the bare system `pip3` — inside the
activated venv, `python`/`pip` resolve to 3.12.

## Raspberry Pi install

```bash
cd ~/emike
bash scripts/install-pi.sh                         # apt deps, uv, Python 3.12 venv
bash scripts/aws/setup-aws.sh                      # or the manual steps above
cp config/assistant.example.yaml config/assistant.yaml
cp .env.example .env && nano .env                  # AWS keys
emike --config config/assistant.yaml               # foreground test run
bash scripts/install-service.sh                    # then enable systemd at boot
```

Service/logs:

```bash
systemctl --user status emike
journalctl --user -u emike -f
```

## Configuration (`config/assistant.yaml`)

`sonic:` section:

| Key | Meaning |
| --- | --- |
| `aws_region` | Bedrock region for the stream (must have the model) |
| `model_id` | Primary model ID, default `amazon.nova-2-5-sonic` |
| `fallback_model_id` | Retried once if the primary fails (`amazon.nova-2-sonic-v1:0`) |
| `voice_id` | e.g. `tiffany`, `matthew`, `amy`, `olivia`, `lupe` … |
| `input_sample_rate_hz` | Mic format — must be 16000 |
| `output_sample_rate_hz` | Response audio: 8000/16000/24000 (default 24000) |
| `chunk_ms` | Mic chunk length sent per `audioInput` event |
| `release_tail_ms` | Silence streamed after button release (the model's endpointing needs a beat of audio after speech stops before it commits the turn) |
| `max_tokens` / `temperature` / `top_p` | Session inference configuration |
| `endpointing_sensitivity` | Model-side turn detection: LOW/MEDIUM/HIGH |
| `prebuffer_ms` | Audio buffered before playback starts |
| `open_timeout_seconds` / `turn_timeout_seconds` | Watchdogs (turn cap stays under Bedrock's 8-minute connection limit) |

Other keys: `system_prompt` (sent verbatim at session start, prepended with
the current local timestamp), `hardware.button_gpio` / `hardware.led_gpio`,
`enabled_tools` (`weather`, `search`, `time`), `default_location`, and search
keys (see below).

## Tools

- `get_current_time` — local device time (no API call).
- Weather (`get_weather`, `get_weather_forecast`) — Open-Meteo; forecasts
  include daily high/low and support today/tomorrow/weekday/date.
- Web search (`web_search`) — Firecrawl → Brave → DuckDuckGo, keyed from
  `FIRECRAWL_API_KEY` / `BRAVE_API_KEY` in `.env`.
- Timers are implemented and dormant: set/list/cancel code ships in
  `src/emike/tools.py` but is not registered unless `timers` is added to
  `enabled_tools`.

Adding a tool = one entry in `ToolRouter.get_tool_definitions()` + one branch
in `execute_tool_call` (and expose it in `enabled_tools`); the schema converts
to Nova Sonic's `toolSpec` format automatically.

## LED states

| Pattern | Meaning |
| --- | --- |
| Off | Idle, waiting for button |
| Solid | Listening (button held) |
| Slow blink | Thinking (model processing / tool executing) |
| Fast blink | Speaking (audio playing) |
| Rapid blink | Error (logged to journal; returns to idle; next press retries) |

Errors never crash the service: the turn fails, the error is logged
(`turn_error` event), the LED shows the error pattern until the next press.

## Testing and quality checks

```bash
bash scripts/test.sh      # ruff check src tests && mypy src && pytest -q
```

## Structured logging

JSON lines on stdout (journalctl-friendly) with events: `assistant_ready`,
`user_text`, `assistant_text`, `tool_call`, `tool_error`, `turn_completed`,
`turn_error`, `turn_interrupt`, `sonic_model_fallback`. Set
`debug_mode: true` for DEBUG-level output.

## CI

`.github/workflows/ci.yml` runs lint, type check, and tests on Python
3.12/3.13 (tests are pure unit tests — no network, audio devices, or AWS).
Push-tagged releases build wheels via `release.yml`.

## Latency notes

Old pipeline: full transcription → full LLM completion → full TTS synthesis →
playback. The Nova Sonic path begins streaming audio while it generates, and
session handshakes overlap your own speech (they happen while the button is
held). Measured improvement is documented as a manual check — see the
verification checklist in the design notes.

## Design document

See design.md for product scope, architecture, security/privacy assumptions,
and phased next steps.
