"""
Huginn Voice — STT via faster-whisper, TTS via piper binary.

Setup:
  uv add faster-whisper          (STT)
  yay -S piper-tts-bin           (TTS binary)
  # Download a Piper voice:
  mkdir -p ~/.local/share/piper
  cd ~/.local/share/piper
  wget https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx
  wget https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json
"""
import asyncio
import shlex
from pathlib import Path

from config import Config


class VoiceEngine:
    def __init__(self):
        self._whisper = None

    def _load_whisper(self):
        if self._whisper is None:
            try:
                from faster_whisper import WhisperModel
                import ctranslate2
                device = Config.whisper_device
                if device == "auto":
                    device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
                compute = "float16" if device == "cuda" else "int8"
                self._whisper = WhisperModel(Config.whisper_model, device=device, compute_type=compute)
                print(f"Whisper loaded: {Config.whisper_model} on {device} ({compute})", flush=True)
            except ImportError:
                raise RuntimeError("faster-whisper not installed — run: uv add faster-whisper")
        return self._whisper

    def _transcribe_sync(self, audio_path: str) -> str:
        model = self._load_whisper()
        segments, _ = model.transcribe(audio_path, language="en", vad_filter=True)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text or ""

    async def transcribe(self, audio_path: str) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._transcribe_sync, audio_path)

    async def speak(self, text: str) -> None:
        model_path = Config.piper_model
        if not Path(model_path).exists():
            print(f"Piper model not found: {model_path}", flush=True)
            return
        cmd = (
            f"echo {shlex.quote(text)} | "
            f"piper-tts --model {shlex.quote(model_path)} --output_raw | "
            f"aplay -r 22050 -f S16_LE -c 1 -q"
        )
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0 and err:
            print(f"TTS error: {err.decode().strip()}", flush=True)
