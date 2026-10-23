from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from emike.config import TTSConfig


class TextToSpeech:
    def __init__(self, config: TTSConfig) -> None:
        self._voice_name = config.voice_name
        self._voices_dir = Path(config.voices_dir)
        self._auto_download_voice = config.auto_download_voice
        if config.voice_model_path:
            self._voice_model_path = Path(config.voice_model_path)
        else:
            self._voice_model_path = self._voices_dir / f"{self._voice_name}.onnx"

    def speak(self, text: str) -> None:
        if not text.strip():
            return

        if not self._voice_model_path.exists() and self._auto_download_voice:
            self._download_voice()

        if not self._voice_model_path.exists():
            raise FileNotFoundError(
                "Piper voice model not found at "
                f"{self._voice_model_path}. "
                "Set tts.voice_name/tts.voices_dir, provide tts.voice_model_path, "
                "or disable TTS."
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

    def _download_voice(self) -> None:
        self._voices_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "piper.download_voices",
                    self._voice_name,
                    "--download-dir",
                    str(self._voices_dir),
                ],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            raise RuntimeError(f"Piper voice download failed: {stderr.strip()}") from exc

    def _play_audio(self, wav_path: str) -> None:
        if shutil.which("aplay"):
            subprocess.run(["aplay", "-q", wav_path], check=True)
            return

        if shutil.which("afplay"):
            subprocess.run(["afplay", wav_path], check=True)
            return

        raise RuntimeError("No supported audio player found. Install aplay (Linux) or use macOS afplay.")
