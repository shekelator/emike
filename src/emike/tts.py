from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class TextToSpeech:
    def __init__(self, voice_model_path: str) -> None:
        self._voice_model_path = Path(voice_model_path)

    def speak(self, text: str) -> None:
        if not text.strip():
            return

        if not self._voice_model_path.exists():
            raise FileNotFoundError(
                "Piper voice model not found at "
                f"{self._voice_model_path}. "
                "Set tts.voice_model_path to an existing .onnx model or disable TTS."
            )

        with tempfile.NamedTemporaryFile(suffix=".wav") as wav_file:
            try:
                subprocess.run(
                    [
                        "piper",
                        "--model",
                        str(self._voice_model_path),
                        "--output_file",
                        wav_file.name,
                    ],
                    input=text.encode("utf-8"),
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError as exc:
                stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
                raise RuntimeError(f"Piper synthesis failed: {stderr.strip()}") from exc

            self._play_audio(wav_file.name)

    def _play_audio(self, wav_path: str) -> None:
        if shutil.which("aplay"):
            subprocess.run(["aplay", "-q", wav_path], check=True)
            return

        if shutil.which("afplay"):
            subprocess.run(["afplay", wav_path], check=True)
            return

        raise RuntimeError("No supported audio player found. Install aplay (Linux) or use macOS afplay.")
