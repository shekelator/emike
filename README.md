# emike

Push-to-talk Raspberry Pi voice assistant.

## What this repository now contains
- A deployable Python package for an appliance-like assistant runtime.
- Raspberry Pi install and service setup scripts.
- A focused design document in design.md.
- Devcontainer configuration for portable development.

## Project structure
- src/emike: runtime package (button, audio, stt, llm, tts, orchestration)
- config: runtime config templates
- scripts: install, run, and test scripts
- systemd: service unit file for boot startup
- tests: basic validation tests
- .devcontainer: portable development environment settings

## Requirements
- Raspberry Pi OS 64-bit (Bookworm recommended)
- Python 3.11+
- USB microphone/speaker or USB speakerphone
- GPIO push-button wired to the configured pin
- Network access to your configured LLM endpoint

## Quick start (developer machine)
1. Create and activate a virtual environment.
2. Install editable package and dev dependencies.
3. Copy the example config and env files.
4. Run keyboard mode.

Commands:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e '.[dev]'
cp config/assistant.example.yaml config/assistant.yaml
cp .env.example .env
emike --keyboard --config config/assistant.yaml
```

Keyboard mode is intended for development and CI-like validation, not hardware deployment.

## Raspberry Pi install
1. Clone repo to the Pi user home, for example ~/emike.
2. Run install script.
3. Create runtime config and set your API key.
4. Validate audio devices and run once interactively.

Commands:

```bash
cd ~/emike
bash scripts/install-pi.sh
cp config/assistant.example.yaml config/assistant.yaml
cp .env.example .env
nano .env
source .venv/bin/activate
emike --config config/assistant.yaml
```

## Configure models and provider
Edit config/assistant.yaml:
- llm.endpoint: Ollama Cloud chat-completions endpoint
- llm.model: provider model name
- llm.api_key_env: environment variable with API key
- stt.model: local Whisper model size (base.en default)
- tts.voice_model_path: path to Piper voice model file
- tts.voice_name: Piper voice name, for example en_US-lessac-medium
- tts.voices_dir: directory where voice files are stored/downloaded
- tts.auto_download_voice: when true, missing configured voice is downloaded on startup
- tts.voice_model_path: optional legacy explicit model path override
- enabled_tools: weather, search, timers
- default_location: fallback location for weather queries

## TTS voice management
- Voice binaries are not committed to git.
- On startup, when TTS is enabled and the configured voice file is missing, emike auto-downloads it (if `tts.auto_download_voice` is true).
- Recommended config style is by voice name (`tts.voice_name`) rather than hardcoded file path.
- search_provider: auto, firecrawl, brave, or duckduckgo
- firecrawl_api_key_env: env var used for Firecrawl search
- firecrawl_search_url: Firecrawl search endpoint URL
- search_api_key_env: env var used by Brave search
- debug_mode: set true to enable DEBUG-level structured logs

Edit .env:
- set OLLAMA_API_KEY
- set FIRECRAWL_API_KEY for primary search
- optionally set BRAVE_API_KEY as secondary fallback

## Weather, search, and timer support
These capabilities are now included in runtime.

Tool invocation model:
- Weather, search, and timer actions are exposed to the LLM as callable tools.
- The LLM can call one or more tools, inspect results, and then produce a final user-facing response.

Weather:
- Uses Open-Meteo geocoding + forecast APIs.
- Requires outbound internet access.
- Temperature defaults to Fahrenheit in responses.
- Supports current conditions and forecast queries.
- Forecasts support today, tomorrow, weekday names (for example Thursday), and YYYY-MM-DD.
- Forecast responses include high and low temperatures.
- If location is omitted, weather tools use default_location (default: Jamaica Plain, MA 02130).

Search:
- Uses Firecrawl first when FIRECRAWL_API_KEY is present (or when search_provider is firecrawl).
- Falls back to Brave Search API when configured and available.
- Falls back again to DuckDuckGo instant-answer API if provider keys are unavailable.

Timers:
- Runs locally in-process.
- Supports set/list/cancel flows, with spoken completion notices.

## Run as a service on boot (systemd user service)
1. Copy the unit file.
2. Reload and enable service.
3. Enable lingering so the user service starts at boot.

Commands:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/emike.service ~/.config/systemd/user/emike.service
systemctl --user daemon-reload
systemctl --user enable --now emike
sudo loginctl enable-linger "$USER"
```

Useful service commands:

```bash
systemctl --user status emike
journalctl --user -u emike -f
systemctl --user restart emike
```

## Testing and quality checks
Run all checks:

```bash
bash scripts/test.sh
```

Or run individually:

```bash
ruff check src tests
mypy src
pytest -q
```

## Structured logging
- Logs are emitted to console as JSON objects.
- Set debug_mode: true in config/assistant.yaml to include DEBUG events.
- Useful for model-call diagnostics, including empty-response metadata.

## GitHub Actions CI pipeline
The repository includes a robust workflow in .github/workflows/ci.yml.

On every push and pull request:
- Runs lint, type checks, and tests on Python 3.11, 3.12, and 3.13.
- Builds sdist and wheel artifacts after quality checks pass.
- Uploads build artifacts for inspection.

## Devcontainer
If you use VS Code Dev Containers, open the repository in container. The container installs Python tooling and package dependencies automatically.

Notes:
- GPIO and host audio devices are not available inside the default devcontainer.
- Use keyboard mode for container-based development.

## Deployment notes for Raspberry Pi
- Prefer a USB speakerphone for easiest full-duplex audio setup.
- Keep replies short in the system prompt to reduce latency.
- Start with stt.model base.en, then tune up/down for speed vs accuracy.

## Detailed implementation roadmap
1. MVP verification
- Validate hardware button and audio path.
- Validate end-to-end flow with chosen LLM provider.
- Establish baseline latency measurements.

2. Reliability hardening
- Add retry policy for transient HTTP errors.
- Add health-check script for boot diagnostics.
- Add structured logging with log levels.

3. Conversational UX improvements
- Add streaming LLM responses.
- Add sentence-level incremental TTS.
- Add barge-in behavior during playback.

4. Productization
- Expand CI with security and release automation.
- Add versioned release process.
- Add installation bundle script and update path.

## Design document
See design.md for product scope, architecture, security/privacy assumptions, and phased next steps.

## Testing the button
```
python3 -c "from gpiozero import Button; b=Button(17); b.wait_for_press(); print('pressed'); b.wait_for_release(); print('released')"
```