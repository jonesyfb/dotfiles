from pathlib import Path


PROFILES: dict[str, dict] = {
    "default": {"backend": "ollama", "model": "qwen3.5:27b",              "label": "qwen3.5 27b"},
    "fast":    {"backend": "ollama", "model": "qwen3.5:9b",               "label": "qwen3.5 9b"},
    "vision":  {"backend": "ollama", "model": "gemma4:31b",               "label": "gemma4 31b"},
    "reason":  {"backend": "ollama", "model": "deepseek-r1:32b",          "label": "deepseek r1", "no_tools": True},
    "smart":   {"backend": "claude", "model": "claude-sonnet-4-6",        "label": "claude sonnet"},
    "opus":    {"backend": "claude", "model": "claude-opus-4-7",          "label": "claude opus"},
    "haiku":   {"backend": "claude", "model": "claude-haiku-4-5-20251001","label": "claude haiku"},
}


class Config:
    ollama_base_url  = "http://localhost:11434"
    model            = "qwen3.5:9b"       # legacy fallback
    default_profile  = "fast"
    data_dir         = Path.home() / ".local/share/huginn"
    socket_path      = data_dir / "huginn.sock"

    # Voice
    whisper_model  = "base.en"
    whisper_device = "auto"
    piper_model    = str(Path.home() / ".local/share/piper/en_US-lessac-medium.onnx")
    tts_enabled    = True
