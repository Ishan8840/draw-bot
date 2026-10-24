"""Benchmark real local adapters with a mono PCM16/16kHz WAV; no model API."""
import argparse
import asyncio
import json
import platform
import time
import wave
from pathlib import Path
from backend.voice.config import VoiceOptions, VoiceSettings
from backend.voice.services import LocalVoiceServices
from backend.voice.segmentation import Segmenter, FRAME_BYTES
from backend.voice.commands import classify_control


async def benchmark(args):
    settings=VoiceSettings.from_env()
    services=LocalVoiceServices(settings)
    records=[]
    def record(stage, **values):
        records.append(dict(stage=stage,monotonic_ms=time.perf_counter()*1000,**values))
    try:
        started=time.perf_counter()
        vad=await services.prepare(VoiceOptions(voice=settings.default_voice,tts_enabled=settings.tts_enabled))
        record('models_ready',duration_ms=(time.perf_counter()-started)*1000,device=services.stt.device)
        with wave.open(args.wav,'rb') as wav:
            if (wav.getframerate(),wav.getnchannels(),wav.getsampwidth())!=(16000,1,2):
                raise ValueError('Input must be mono 16-bit PCM WAV at 16000 Hz')
            pcm=wav.readframes(wav.getnframes())+bytes(32000)
        segmenter=Segmenter(settings)
        jobs=[]
        async def recognize(utterance, data, fast=False):
            start=time.perf_counter()
            text=await (services.fast if fast else services.stt).transcribe(data)
            action,_=classify_control(text,settings.interrupt_phrases)
            record('fast_transcription_complete' if fast else 'transcription_complete',
                   utterance_id=utterance.id,duration_ms=(time.perf_counter()-start)*1000,
                   recognized_control=action,text=text)
            if not fast and settings.tts_enabled:
                start=time.perf_counter()
                audio=await services.tts.synthesize("Okay, I've paused." if action else 'I heard you.',settings.default_voice)
                record('tts_generated',utterance_id=utterance.id,duration_ms=(time.perf_counter()-start)*1000,bytes=len(audio))
        for offset in range(0,len(pcm),FRAME_BYTES):
            tick=time.perf_counter()
            frame=pcm[offset:offset+FRAME_BYTES].ljust(FRAME_BYTES,b'\0')
            probability=await asyncio.to_thread(vad.probability,frame)
            for kind,utterance in segmenter.feed(frame,probability):
                if kind in ('start','end'):
                    record('speech_'+kind,utterance_id=utterance.id)
                if kind=='preview' and settings.interrupt_enabled and not any(not task.done() for task in jobs):
                    jobs.append(asyncio.create_task(recognize(utterance,bytes(utterance.pcm),True)))
                if kind=='end':
                    jobs.append(asyncio.create_task(recognize(utterance,utterance.pcm)))
            await asyncio.sleep(max(0,.032-(time.perf_counter()-tick)))
        await asyncio.gather(*jobs)
        result=dict(mode='real local inference; prerecorded audio paced in real time; no model API, drawing or browser playback',
                    python=platform.python_version(),platform=platform.platform(),records=records)
        Path(args.output).write_text(json.dumps(result,indent=2))
        print(f'Measurements saved to {args.output}. For drawing/API/playback use /voice/diagnostics in a live browser session.')
    finally:
        services.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wav',required=True)
    parser.add_argument('--output',default='voice-latency.json')
    asyncio.run(benchmark(parser.parse_args()))
