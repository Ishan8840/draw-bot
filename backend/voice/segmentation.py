"""Deterministic utterance state machine over 32 ms Silero probabilities.

The caller runs Silero for each PCM frame. This component is not a keyword detector.
"""
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import time

SAMPLE_RATE=16000
FRAME_SAMPLES=512
FRAME_BYTES=1024
FRAME_MS=32


class UtteranceState(str,Enum):
    LISTENING='listening'
    SPEAKING='speaking'
    TRANSCRIBING='transcribing'
    SUBMITTING='submitting'
    COMPLETE='complete'
    SUPERSEDED='superseded'
    ERROR='error'


@dataclass
class Utterance:
    id: int
    pcm: bytes
    speech_start_ms: float
    speech_end_ms: float | None = None
    state: UtteranceState = UtteranceState.SPEAKING
    truncated: bool = False
    epoch: int = 0
    fast_handled: bool = False
    finalized: bool = False


class Segmenter:
    def __init__(self, settings):
        self.settings=settings
        self.counter=0
        self.reset()

    def reset(self):
        self.state=UtteranceState.LISTENING
        self.pre_roll=deque(maxlen=5)
        self.frames=[]
        self.voiced_ms=0
        self.silence=0
        self.elapsed=0
        self.last_preview=0
        self.current=None

    def feed(self, pcm, probability, now_ms=None):
        if len(pcm)!=FRAME_BYTES:
            raise ValueError('Expected exactly 512 PCM16 samples')
        now_ms=time.time()*1000 if now_ms is None else now_ms
        events=[]
        voiced=probability>=self.settings.threshold
        if self.state==UtteranceState.LISTENING:
            self.pre_roll.append(pcm)
            self.voiced_ms=self.voiced_ms+FRAME_MS if voiced else 0
            if self.voiced_ms<self.settings.min_speech_ms:
                return events
            self.counter+=1
            self.frames=list(self.pre_roll)
            self.elapsed=len(self.frames)*FRAME_MS
            self.current=Utterance(self.counter,b'',now_ms-self.voiced_ms)
            self.state=UtteranceState.SPEAKING
            events.append(('start',self.current))
        else:
            self.frames.append(pcm)
            self.elapsed+=FRAME_MS
        self.silence=0 if voiced else self.silence+FRAME_MS
        if self.elapsed>=self.settings.preview_ms and self.elapsed-self.last_preview>=320 and self.elapsed<=1600:
            self.current.pcm=b''.join(self.frames)
            self.last_preview=self.elapsed
            events.append(('preview',self.current))
        if self.silence>=self.settings.silence_ms or self.elapsed>=self.settings.max_utterance_seconds*1000:
            self.current.pcm=b''.join(self.frames)
            self.current.speech_end_ms=now_ms-self.silence
            self.current.truncated=self.elapsed>=self.settings.max_utterance_seconds*1000
            self.current.state=UtteranceState.TRANSCRIBING
            events.append(('end',self.current))
            self.reset()
        return events
