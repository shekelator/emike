# emike Design Document

## Purpose
emike is a push-to-talk voice assistant appliance intended to run on a Raspberry Pi. The product goal is low-friction conversational interaction with strong user control over when audio is captured.

## Product Principles
- Capture audio only when the user explicitly presses a button.
- Keep the device behavior explainable and deterministic.
- Prefer local processing where practical for privacy and latency.
- Keep deployment lightweight enough for Raspberry Pi 5 without a GPU.
- Allow model/provider portability via configuration instead of code edits.

## User Experience
### Primary Interaction
1. User presses and holds the hardware button.
2. Device records microphone audio while held.
3. User releases the button to end utterance.
4. Device transcribes speech to text.
5. Device sends text + system prompt + short context history to configured LLM endpoint.
6. Device plays spoken response through speaker output.
7. Optional: connected monitor shows transcript and response.

### Behavioral Expectations
- No wake-word requirement in the initial release.
- Responses are concise and voice-friendly.
- Device gracefully reports transient errors and returns to ready state.
- Conversation context resets after inactivity timeout.

## Scope
### In Scope (MVP)
- Push-to-talk capture with GPIO button.
- Local STT on device (Whisper via faster-whisper).
- LLM response via OpenAI-compatible endpoint.
- Local TTS output via Piper.
- Built-in weather lookup via Open-Meteo APIs.
- Built-in web search using Brave Search (with DuckDuckGo fallback).
- Local timer creation, listing, cancellation, and spoken expiration alerts.
- Boot-time service management with systemd user service.
- YAML and environment-file based runtime configuration.
- Raspberry Pi deployment guide and local developer workflow.

### Out of Scope (MVP)
- Wake word detection.
- Barge-in interruption while TTS is speaking.
- Smart-home control integrations.
- GUI kiosk dashboard beyond terminal logs.

## System Architecture
### Components
- Input control: GPIO push-button abstraction.
- Audio capture: microphone stream at 16 kHz mono.
- Speech recognition: local Whisper model.
- Orchestration loop: state management and pipeline coordination.
- Language model client: provider-agnostic chat completion client.
- Speech synthesis: local Piper engine + audio playback.
- Configuration layer: YAML file + .env secrets.
- Service runtime: systemd user unit.

### Data Flow
- Button press starts audio capture.
- Button release stops capture and triggers STT.
- Transcription is appended to conversation state.
- LLM request is constructed from:
  - system prompt,
  - current local timestamp,
  - bounded dialogue history.
- Assistant text response is persisted in state and spoken.

## Deployment Strategy (Raspberry Pi)
### Runtime Assumptions
- Raspberry Pi OS 64-bit.
- Python 3.11+ runtime.
- USB microphone/speakerphone preferred for simplicity.
- Network access available for cloud LLM endpoint.

### Deployability Decisions
- Single Python package with minimal services.
- No container required on-device for first deployment.
- systemd user service to align with user-level audio stack.
- Separate install script for reproducible setup.

## Configuration Strategy
- Non-secret operational configuration in YAML.
- API key and sensitive values in .env file.
- Model/provider endpoint fully configurable without code changes.
- Hardware pin assignments configurable for varied wiring.

## Reliability and Observability
### Reliability
- Recover from LLM call failures without process exit.
- Keep conversation history bounded to prevent token bloat.
- Reset stale context after configurable idle timeout.

### Observability
- Print user and assistant utterances to stdout for journal capture.
- Use journalctl against systemd unit for troubleshooting.
- Keep logs human-readable for first-iteration field debugging.

## Security and Privacy
- Audio is not transmitted externally for STT/TTS in MVP path.
- Only transcribed text is sent to LLM provider.
- Secrets are isolated in environment file.
- Privacy policy of chosen LLM provider must be reviewed before production use.

## Open Questions
- Should tool invocation evolve from command routing to model-native function calling?
- Is barge-in required for first public demo, or can it wait for phase two?
- Is a richer on-screen monitor UI needed for the initial hardware build?

## Detailed Next Steps
1. Hardware validation
- Confirm button wiring and GPIO pin map on target Pi.
- Validate microphone and speaker defaults with PipeWire/ALSA.

2. Software bootstrap on device
- Run install script, create runtime config, set API key.
- Download and verify selected Piper voice model.

3. Functional validation
- Test short and long utterances.
- Verify timeout-based conversation reset behavior.
- Measure end-to-end latency (release to first spoken audio).

4. Service hardening
- Enable and validate systemd user service auto-start.
- Simulate network loss and provider errors.
- Confirm graceful recovery without manual restart.

5. Quality gate
- Add tests for config loading and message construction.
- Add CI workflow for lint/type/test checks.

6. Phase-two expansion candidates
- Tool-calling (weather/search/timers).
- Streaming response + incremental TTS.
- Barge-in during speech playback.
- Wake word option as configurable mode.
