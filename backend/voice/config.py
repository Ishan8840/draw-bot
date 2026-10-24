import os
from pathlib import Path
from typing import Literal
from pydantic import Field, model_validator
from ..models import Model

ROOT = Path(__file__).resolve().parents[2]


class VoiceSettings(Model):
    enabled: bool = True
    stt_model: str = 'small.en'
    fast_stt_model: str = 'tiny.en'
    stt_device: Literal['auto','cpu','cuda'] = 'auto'
    stt_threads: int = Field(default=2, ge=1, le=32)
    language: str = 'en'
    tts_enabled: bool = True
    tts_engine: Literal['piper'] = 'piper'
    default_voice: str = 'en_US-lessac-medium'
    vad_enabled: bool = True
    interrupt_enabled: bool = True
    silence_ms: int = Field(default=450, ge=160, le=2000)
    min_speech_ms: int = Field(default=96, ge=32, le=500)
    threshold: float = Field(default=.5, ge=.1, le=.95)
    max_utterance_seconds: float = Field(default=15, ge=2, le=30)
    preview_ms: int = Field(default=480, ge=256, le=1000)
    model_dir: str = str(ROOT/'data/voice')
    interrupt_phrases: list[str] = ['stop', 'pause', 'stop drawing', 'wait', 'cancel']

    @classmethod
    def from_env(cls):
        mapping={'VOICE_ENABLED':'enabled','STT_MODEL':'stt_model','FAST_STT_MODEL':'fast_stt_model',
                 'STT_DEVICE':'stt_device','STT_THREADS':'stt_threads','STT_LANGUAGE':'language',
                 'TTS_ENABLED':'tts_enabled','TTS_ENGINE':'tts_engine','PIPER_VOICE':'default_voice',
                 'VAD_ENABLED':'vad_enabled','VOICE_INTERRUPT_ENABLED':'interrupt_enabled',
                 'VAD_SILENCE_MS':'silence_ms','VAD_MIN_SPEECH_MS':'min_speech_ms','VAD_THRESHOLD':'threshold',
                 'VOICE_MAX_UTTERANCE_SECONDS':'max_utterance_seconds','VOICE_PREVIEW_MS':'preview_ms',
                 'VOICE_MODEL_DIR':'model_dir'}
        values={field:os.environ[key] for key,field in mapping.items() if key in os.environ}
        if 'VOICE_INTERRUPT_PHRASES' in os.environ:
            values['interrupt_phrases']=[v.strip().lower() for v in os.environ['VOICE_INTERRUPT_PHRASES'].split(',') if v.strip()]
        return cls(**values)


class VoiceOptions(Model):
    type: Literal['start'] = 'start'
    voice: str = Field(default='en_US-lessac-medium', pattern=r'^[A-Za-z0-9_-]{1,100}$')
    tts_enabled: bool = True
    silence_ms: int = Field(default=450, ge=160, le=2000)
    threshold: float = Field(default=.5, ge=.1, le=.95)
