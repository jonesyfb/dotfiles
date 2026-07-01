"""
Huginn Voice — STT via faster-whisper, TTS via piper + ffmpeg.

Setup:
  uv add faster-whisper
  yay -S piper-tts-bin ffmpeg
"""
import asyncio
import re
import shlex
import shutil
from pathlib import Path

from config import Config

_RATE = 22050

_FFMPEG_AF = (
    "equalizer=f=80:width_type=o:width=1.5:g=5,"
    "equalizer=f=2500:width_type=o:width=1.5:g=-3,"
    "equalizer=f=6000:width_type=o:width=2:g=-5,"
    "lowpass=f=7000,"
    "aecho=0.8:0.5:45:0.10,"
    "volume=1.3"
)


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
        return " ".join(seg.text.strip() for seg in segments).strip()

    async def transcribe(self, audio_path: str) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._transcribe_sync, audio_path)

    @staticmethod
    def _clean_for_tts(text: str) -> str:
        # Markdown
        text = re.sub(r'\*+|_+|`+|~+', '', text)
        text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
        # Currency
        text = re.sub(r'\$(\d[\d,]*(?:\.\d+)?)', lambda m: m.group(1).replace(',', '') + ' dollars', text)
        text = re.sub(r'£(\d[\d,]*(?:\.\d+)?)', lambda m: m.group(1).replace(',', '') + ' pounds', text)
        text = re.sub(r'€(\d[\d,]*(?:\.\d+)?)', lambda m: m.group(1).replace(',', '') + ' euros', text)
        # Percentages
        text = re.sub(r'(\d+(?:\.\d+)?)%', r'\1 percent', text)
        # Dates like 2024-04-17
        text = re.sub(r'\b(\d{4})-(\d{2})-(\d{2})\b', r'\1 \2 \3', text)
        # Remove leftover URLs
        text = re.sub(r'https?://\S+', 'a link', text)
        return re.sub(r'\s+', ' ', text).strip()

    async def speak(self, text: str) -> None:
        if not Config.tts_enabled:
            return
        text = self._clean_for_tts(text)
        if not text:
            return

        model_path  = Config.piper_model
        use_effects = Path(model_path).exists()

        if not use_effects:
            for fallback in [
                str(Path.home() / ".local/share/piper/en_US-ryan-high.onnx"),
                str(Path.home() / ".local/share/piper/en_US-lessac-medium.onnx"),
            ]:
                if Path(fallback).exists():
                    model_path  = fallback
                    use_effects = True
                    print(f"Piper fallback: {Path(fallback).stem}", flush=True)
                    break
            else:
                print("No piper model found.", flush=True)
                return

        piper_cmd = f"echo {shlex.quote(text)} | piper-tts --model {shlex.quote(model_path)} --output_raw"

        if use_effects and shutil.which("ffmpeg"):
            raw_in = f"-f s16le -ar {_RATE} -ac 1 -i pipe:0"
            cmd = (
                f"{piper_cmd} | "
                f"ffmpeg -loglevel quiet {raw_in} -af {shlex.quote(_FFMPEG_AF)} -f wav pipe:1 | "
                f"aplay -q"
            )
        else:
            cmd = f"{piper_cmd} | aplay -r {_RATE} -f S16_LE -c 1 -q"

        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode != 0 and err:
            print(f"TTS error: {err.decode().strip()}", flush=True)
