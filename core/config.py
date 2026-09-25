"""Configuration: .env into the process, config.yaml into typed settings."""

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent


def load_env(path=None):
    """Upstream's loader: KEY=VALUE lines, never overriding a real environment variable."""
    path = Path(path) if path else ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.split("#")[0].strip())


class AudioConfig(BaseModel):
    sample_rate: int = 16000
    frame_ms: int = 60


class SttConfig(BaseModel):
    url: str = "wss://streaming.assemblyai.com/v3/ws"
    speech_model: str = "universal-streaming-english"
    sample_rate: int = 16000
    encoding: str = "pcm_s16le"
    format_turns: bool = True
    end_of_turn_confidence_threshold: float = 1.0
    max_turn_silence_ms: int = 2500
    keyterms_from_gazetteer: int = 50
    # Streaming is billed on connection time, idle included. Push-to-talk opens
    # the socket on key down -- a whole utterance before the transcript is
    # needed -- so holding it open while idle buys no latency, only cost.
    idle_close_s: float = 30.0


class Thresholds(BaseModel):
    intent_min_confidence: float = 0.6
    risk_threshold: float = 0.3


class Budgets(BaseModel):
    max_steps: int = 40
    max_model_calls: int = 100
    confirm_timeout_s: int = 60


class NavigationConfig(BaseModel):
    open_in: str = "current_tab"
    # Bring the agent's tab to the front. The whole premise is watching your own
    # Chrome do the work, and a tab you cannot see looks like nothing happened.
    activate: bool = True
    sites_file: str = "data/sites.yaml"
    profile_file: str = "data/profile.yaml"
    start_here_threshold: float = 0.6
    start_site_min_confidence: float = 0.6
    follow_new_tabs: bool = True


class Settings(BaseModel):
    hotkey: str = "f9"
    min_press_ms: int = 150
    audio: AudioConfig = Field(default_factory=AudioConfig)
    stt: SttConfig = Field(default_factory=SttConfig)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    budgets: Budgets = Field(default_factory=Budgets)
    navigation: NavigationConfig = Field(default_factory=NavigationConfig)
    page_text_chars: int = 6000
    history_actions: int = 10
    timezone: str = "Asia/Kolkata"
    status_port: int = 8766
    save_audio: bool = False

    def path(self, relative):
        return ROOT / relative


_SETTINGS = None


def settings(reload=False):
    """Process-wide settings. config.yaml is optional; every key has a default."""
    global _SETTINGS
    if _SETTINGS is None or reload:
        path = ROOT / "config.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
        loaded = Settings(**(raw or {}))
        loaded.save_audio = os.environ.get("SAVE_AUDIO", "").lower() == "true" or loaded.save_audio
        _SETTINGS = loaded
    return _SETTINGS
