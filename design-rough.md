
# Voice assistant appliance: plan

## How it works

Press the button, speak, and release (or let it detect silence). The Pi records the audio, transcribes it, sends the text plus your system prompt to an LLM, and speaks the reply. If a monitor is attached, it also shows the transcript and answer.

**Button → record → speech-to-text → LLM (with tools) → text-to-speech → speaker**

A push-to-talk button is the right call. It's simpler and more private than a wake word, nothing is recorded until you press it, and it avoids false triggers. You can add a wake word later with openWakeWord.

## Materials (roughly $120-200, so check current prices)

| Part | Recommendation | Notes |
|---|---|---|
| Computer | Raspberry Pi 5, 4GB | A Pi 4 also works, since the heavy lifting is in the cloud. Pi pricing has been volatile, so compare. |
| Power | Official 27W USB-C supply | Cheap supplies cause random glitches on the Pi 5. |
| Storage | 32-64GB A1/A2 microSD | |
| Cooling/case | Case with a fan or the official Active Cooler | |
| Mic + speaker | **USB conference speakerphone** (Anker PowerConf S330, Jabra Speak 510, or similar) | Easiest option by far. It's plug-and-play and has a good mic array, echo cancellation, and a decent speaker. |
| Button | Arcade or momentary push button, with jumper wires to a GPIO pin and ground | An LED on another pin can show listening/thinking status. |
| Display cable | Micro-HDMI to HDMI (Pi 5) | |
| Network | Wi-Fi or Ethernet | |

A DIY audio alternative is a ReSpeaker mic HAT plus an I2S amp and small speaker. It's more compact but more fiddly, and the speakerphone saves a lot of headaches.

## Models and services

I'd use open-weight models behind hosted APIs, so you get open models without running a GPU. All of these providers expose OpenAI-compatible endpoints, so you can swap models by changing one config line.

- **Speech-to-text:** Whisper large-v3-turbo through Groq, Fireworks, or Together. Fast and accurate. A local fallback is whisper.cpp or faster-whisper with the `base.en` model on the Pi, which is slower but fully offline.
- **LLM:** a recent open-weight model such as Llama 3.3 70B, Qwen3, or gpt-oss, via Groq, Together, Fireworks, DeepInfra, or OpenRouter. Pick the fastest provider for the model. Latency matters more than raw smarts for a voice assistant.
- **Text-to-speech:** **Piper** runs locally on the Pi, is fast, and sounds decent. **Kokoro-82M** sounds noticeably better and is a good upgrade, hosted or local.
- **Voice activity detection:** Silero VAD, to detect when you stop talking.
- **Tools (to make it Alexa-like):** the LLM calls functions for current time, weather (Open-Meteo is free), web search (Brave Search API, Tavily, or self-hosted SearXNG), and timers. Without web search, it can't answer "what happened today" questions.

Expect a few cents a day at most for casual use.

## Harness: three options

1. **Custom Python script (my recommendation to start).** About 200 lines using `gpiozero` for the button, `sounddevice` for audio, and the `openai` client library pointed at your provider. The system prompt, voice, model, and tools live in a YAML file you edit, so configurability is trivial. It's easy to understand and modify.
2. **Pipecat or LiveKit Agents.** Open-source real-time voice frameworks with streaming and interruption ("barge-in") support. Worth it if you want a more conversational feel and don't mind more complexity.
3. **Home Assistant Assist with Wyoming satellite.** Best if you want smart-home control, but it's heavier and more than you need for a Q&A device.

## Setup steps

1. Flash **Raspberry Pi OS (64-bit, with desktop)** using Raspberry Pi Imager. Set the hostname, Wi-Fi, and SSH in the Imager settings.
2. Plug in the speakerphone. Test with `arecord -d 5 test.wav` and `aplay test.wav`, and set it as the default device in `raspi-config` or PipeWire.
3. Wire the button between a GPIO pin (say GPIO17) and ground, and test it with `gpiozero`.
4. Create a Python venv and install `openai`, `sounddevice`, `silero-vad`, `piper-tts`, `gpiozero`, and `pyyaml`.
5. Get API keys (a single provider like Groq or Together can cover STT and LLM) and store them in an `.env` file.
6. Write the main loop: button press → record until release or silence → STT → LLM → TTS → play.
7. Run it as a `systemd` service so it starts on boot, and add a simple on-screen display (a small web page in Chromium kiosk mode, or a terminal UI) for the monitor.
8. Tune the system prompt, for example: "You are a voice assistant. Answer in one to three short sentences. No markdown, lists, or emoji. Spell out numbers and units."

**Config example:**
```yaml
system_prompt: "You are a concise voice assistant. Reply in 1-3 spoken sentences."
stt: {provider: groq, model: whisper-large-v3-turbo}
llm: {provider: groq, model: llama-3.3-70b-versatile, max_tokens: 200}
tts: {engine: piper, voice: en_US-lessac-medium}
tools: [time, weather, web_search, timers]
```

## Latency tips

A good target is 2-3 seconds from end of speech to first audio. Stream the LLM output and start speaking as soon as the first sentence is ready rather than waiting for the full reply. Keep answers short through the prompt and a `max_tokens` cap. Also keep the HTTP connections alive between requests.

## Questions for you

1. **Scope:** Is Q&A plus timers, weather, and web search enough, or do you want music, smart-home control, or calendar integration?
2. **Privacy:** Is it okay for audio to go to a cloud STT provider, or would you rather transcribe locally on the Pi (slower but private)?
3. **Interruption:** Do you need to be able to talk over it and have it stop, or is "press the button to cut it off" fine?
4. **Skill level:** Are you comfortable running Python on the Pi over SSH, or would you like step-by-step commands for everything?
5. **Monitor use:** Just a transcript and answer on screen, or something richer like images, search results, and a clock?

Once you answer, I can write the full script, config file, wiring diagram, and `systemd` unit. In the meantime, I'd order the Pi 5, speakerphone, button, and cables, since those choices are settled either way.

---

# Build guide: push-to-talk voice assistant

Two changes from my first sketch, based on your answers:

- **Speech-to-text runs on the Pi, not in the cloud.** I didn't find a speech-to-text endpoint on Ollama Cloud, only chat models. Running Whisper locally means your voice never leaves the device. Only the transcribed text goes to Ollama, and replies are spoken by Piper, also local.
- **No tool calling yet.** For your minimal scope, the script injects the current date and time into the system prompt each turn. Web search and weather can come later.

For privacy, I only found a secondhand blog post claiming Ollama Cloud retains no data. Check Ollama's own privacy and terms pages before relying on that.

## Parts to order

- Raspberry Pi 5 (4GB) with the official 27W power supply and a case with active cooling
- 32-64GB microSD card
- USB speakerphone (Anker PowerConf S330, Jabra Speak 510, or similar)
- Momentary push button (an arcade button is nice), 2 jumper wires
- Optional status LED, a 330Ω resistor, and 2 more jumper wires
- Micro-HDMI to HDMI cable, if you want the monitor

## 1. Wiring

| Part | Connects to |
|---|---|
| Button, one leg | GPIO17 (physical pin 11) |
| Button, other leg | GND (physical pin 9) |
| LED long leg (+), via 330Ω resistor | GPIO27 (physical pin 13) |
| LED short leg (−) | GND (physical pin 14) |

The button needs no resistor, because the script enables the Pi's internal pull-up.

## 2. Flash the OS

In Raspberry Pi Imager, choose **Raspberry Pi OS (64-bit) with desktop**. The desktop version includes PipeWire audio, which makes the USB speakerphone easy. In the settings dialog, set the hostname, your username, Wi-Fi, and enable SSH. Boot it and SSH in.

## 3. Check the audio

Plug in the speakerphone, then:

```bash
wpctl status          # find your speakerphone's ID under Sinks and Sources
wpctl set-default <sink-id>
wpctl set-default <source-id>
arecord -d 5 -f S16_LE -r 16000 test.wav && aplay test.wav
```

You should hear yourself back.

## 4. Install dependencies

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-venv python3-gpiozero python3-lgpio libportaudio2 alsa-utils
mkdir -p ~/assistant && cd ~/assistant
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install faster-whisper sounddevice numpy requests pyyaml python-dotenv piper-tts
python -m piper.download_voices en_US-lessac-medium --data-dir voices
```

If the voice download flags differ on your version, run `python -m piper.download_voices --help`.

## 5. Ollama Cloud key and model

Create an API key at ollama.com/settings/keys, then:

```bash
cd ~/assistant
echo 'OLLAMA_API_KEY=paste-your-key-here' > .env
chmod 600 .env
set -a; source .env; set +a
curl -s https://ollama.com/api/tags -H "Authorization: Bearer $OLLAMA_API_KEY"
```

The last command lists the models available to you. Hosted catalogs change, so pick from that list. I'd start with `gpt-oss:20b` for speed. It's a reasoning model, so it adds a little thinking delay. If replies feel slow, try a smaller non-reasoning model from the list.

## 6. Config file

Create `~/assistant/config.yaml`. It's re-read on every button press, so you can edit the prompt without restarting.

```yaml
system_prompt: |
  You are a friendly voice assistant in a small appliance, like Alexa.
  Reply in one to three short spoken sentences.
  Never use markdown, lists, emoji, or special symbols.
  Spell out numbers and units the way you would say them aloud.
  If you don't know something or can't look it up, say so plainly.

llm:
  url: https://ollama.com/api/chat
  model: gpt-oss:20b        # replace with a name from the /api/tags list
  max_tokens: 250
  temperature: 0.6

stt:
  model: base.en            # small.en is more accurate but slower
  language: en

tts:
  voice: en_US-lessac-medium

button_gpio: 17
led_gpio: 27                # delete this line if you have no LED
history_turns: 6            # how many back-and-forths it remembers
conversation_timeout: 120   # seconds of silence before it forgets the conversation
```

## 7. The script

Create `~/assistant/assistant.py`:

```python
#!/usr/bin/env python3
"""Push-to-talk assistant: button -> Whisper (local) -> Ollama Cloud -> Piper (local)."""
import json, os, queue, re, subprocess, sys, tempfile, threading, time
from datetime import datetime

import numpy as np
import requests
import sounddevice as sd
import yaml
from dotenv import load_dotenv
from faster_whisper import WhisperModel
from gpiozero import Button, LED

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))
RATE = 16000
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
speech_q = queue.Queue()


def load_cfg():
    with open(os.path.join(BASE, "config.yaml")) as f:
        return yaml.safe_load(f)


def record_while_held(button):
    frames = []

    def cb(indata, n, t, status):
        frames.append(indata.copy())

    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", callback=cb):
        while button.is_pressed:
            time.sleep(0.02)
    return np.concatenate(frames)[:, 0] if frames else np.zeros(0, dtype="int16")


def transcribe(stt, pcm, language):
    if len(pcm) < RATE * 0.4:
        return ""
    audio = pcm.astype(np.float32) / 32768.0
    segments, _ = stt.transcribe(audio, language=language, vad_filter=True, beam_size=1)
    return " ".join(s.text.strip() for s in segments).strip()


def speak(text, voice):
    text = re.sub(r"[*_#`>~]", "", text).strip()
    if not text:
        return
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        subprocess.run(
            [sys.executable, "-m", "piper", "-m", voice,
             "--data-dir", os.path.join(BASE, "voices"), "-f", f.name],
            input=text.encode(), check=True, capture_output=True,
        )
        subprocess.run(["aplay", "-q", f.name], check=True)


def speaker_worker():
    while True:
        text, voice = speech_q.get()
        try:
            speak(text, voice)
        except Exception as e:
            print(f"[tts error] {e}", file=sys.stderr)
        finally:
            speech_q.task_done()


def stream_llm(cfg, history):
    llm = cfg["llm"]
    now = datetime.now().strftime("%A, %B %d, %Y, %I:%M %p")
    system = f"{cfg['system_prompt'].strip()}\nCurrent local date and time: {now}."
    body = {
        "model": llm["model"],
        "stream": True,
        "messages": [{"role": "system", "content": system}] + history,
        "options": {"num_predict": llm.get("max_tokens", 250),
                    "temperature": llm.get("temperature", 0.6)},
    }
    headers = {"Authorization": "Bearer " + os.environ["OLLAMA_API_KEY"]}
    with requests.post(llm["url"], json=body, headers=headers, stream=True, timeout=90) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            piece = chunk.get("message", {}).get("content", "")
            if piece:
                yield piece
            if chunk.get("done"):
                break


def answer(cfg, history):
    voice = cfg["tts"]["voice"]
    buf, full = "", ""
    print("Assistant: ", end="", flush=True)
    for piece in stream_llm(cfg, history):
        print(piece, end="", flush=True)
        full += piece
        buf += piece
        parts = SENTENCE_END.split(buf)
        for sentence in parts[:-1]:
            speech_q.put((sentence, voice))
        buf = parts[-1]
    if buf.strip():
        speech_q.put((buf, voice))
    print("\n")
    return full.strip()


def main():
    cfg = load_cfg()
    print("Loading speech model...")
    stt = WhisperModel(cfg["stt"]["model"], device="cpu", compute_type="int8")
    button = Button(cfg["button_gpio"], pull_up=True, bounce_time=0.05)
    led = LED(cfg["led_gpio"]) if cfg.get("led_gpio") else None
    threading.Thread(target=speaker_worker, daemon=True).start()

    history, last_turn = [], 0.0
    print("Ready. Hold the button, speak, then release.\n")

    while True:
        button.wait_for_press()
        cfg = load_cfg()
        if led:
            led.on()
        pcm = record_while_held(button)
        if led:
            led.blink(0.2, 0.2)
        try:
            text = transcribe(stt, pcm, cfg["stt"].get("language", "en"))
            if not text:
                print("(didn't catch that)\n")
                continue
            print(f"You: {text}")

            if time.time() - last_turn > cfg.get("conversation_timeout", 120):
                history = []
            history.append({"role": "user", "content": text})
            reply = answer(cfg, history)
            history.append({"role": "assistant", "content": reply})
            history = history[-cfg.get("history_turns", 6) * 2:]
            speech_q.join()
            last_turn = time.time()
        except Exception as e:
            print(f"[error] {e}\n", file=sys.stderr)
            speech_q.put(("Sorry, something went wrong.", cfg["tts"]["voice"]))
            speech_q.join()
        finally:
            if led:
                led.off()


if __name__ == "__main__":
    main()
```

## 8. Test it by hand

```bash
cd ~/assistant && source .venv/bin/activate
python assistant.py
```

The first run downloads the Whisper model, which takes a minute. Hold the button, ask "What time is it?", and release. The terminal shows `You:` and `Assistant:` lines, and the speakerphone talks.

## 9. Start on boot

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/assistant.service <<'EOF'
[Unit]
Description=Voice assistant
After=network-online.target sound.target
Wants=network-online.target

[Service]
WorkingDirectory=%h/assistant
ExecStart=%h/assistant/.venv/bin/python -u assistant.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now assistant
sudo loginctl enable-linger $USER
```

To see the live transcript on the monitor, open a terminal and run `journalctl --user -u assistant -f`. It runs as a user service because that's where PipeWire audio lives.

## Troubleshooting

- **No sound or wrong device:** re-run `wpctl status` and set the defaults again.
- **Button does nothing:** check the pin numbers. Test with `python3 -c "from gpiozero import Button; b=Button(17); b.wait_for_press(); print('ok')"`.
- **401 error from Ollama:** the key in `.env` is wrong, or the service didn't pick it up. Restart it with `systemctl --user restart assistant`.
- **Model not found:** copy the name exactly from the `/api/tags` output.
- **Slow replies:** try a smaller model, lower `max_tokens`, or switch Whisper to `tiny.en`.
- **Whisper mishears you:** move to `small.en`. It costs a second or two on the Pi 5.

## Where to go next

1. **Web search and weather:** add tool calling in `stream_llm`, with Open-Meteo for weather and a search API like Brave or Tavily.
2. **Barge-in:** stop playback when the button is pressed during speech.
3. **Wake word:** openWakeWord, if you ever want hands-free use.

If you hit an error in any step, paste the output and I'll help you sort it out.
