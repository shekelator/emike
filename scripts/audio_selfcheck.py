"""Play a 3-second tone through emike's real playback path.

Run inside the project venv (from the repo root):
    .venv/bin/python scripts/audio_selfcheck.py

If you cannot hear the tone while this runs, the problem is your output
device/volume, not emike: fix it in System Settings > Sound, or set
`hardware.output_device` in the YAML config to a specific device name,
and check the `speaker_opened` log line for what emike actually uses.
"""

from __future__ import annotations

import sys
import time

import numpy as np
import sounddevice as sd

from emike.playback import PlaybackQueue


def main() -> None:
    default_output = sd.query_devices(kind="output")
    print(f"Default output device: {default_output['name']}")
    sample_rate = 24_000
    queue = PlaybackQueue(sample_rate, prebuffer_ms=150)
    queue.start()
    sine = (np.sin(np.linspace(0, 500 * np.pi, 3600)) * 9000).astype("<i2").tobytes()
    for _ in range(20):  # 3 seconds of a 500 Hz tone at 24 kHz
        queue.put_pcm(sine)
    print("Playing 3 s tone through emike's PlaybackQueue — listen now…")
    time.sleep(4)
    queue.stop()
    print("Done. If you heard the tone, emike's playback path is fine.")


if __name__ == "__main__":
    sys.exit(main())
