"""Local inference adapters. Heavy imports and inference happen only on workers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
from pathlib import Path
import wave


class VoiceUnavailable(RuntimeError):
    pass


class SileroVAD:
    """Official Silero ONNX recurrent model; isolated state per microphone stream."""
    def __init__(self, model_path):
        import numpy as np
        import onnxruntime as ort
        options=ort.SessionOptions()
        options.intra_op_num_threads=1
        options.inter_op_num_threads=1
        self.session=ort.InferenceSession(str(model_path),sess_options=options,providers=['CPUExecutionProvider'])
        self.np=np
        self.reset()

    def reset(self):
        self.state=self.np.zeros((2,1,128),dtype=self.np.float32)
        self.context=self.np.zeros((1,64),dtype=self.np.float32)

    def probability(self, pcm):
        np=self.np
        audio=np.frombuffer(pcm,dtype='<i2').astype(np.float32).reshape(1,-1)/32768
        if audio.shape!=(1,512):
            raise ValueError('Silero requires 512 samples at 16 kHz')
        combined=np.concatenate((self.context,audio),axis=1)
        output,self.state=self.session.run(None,{'input':combined,'state':self.state,'sr':np.array(16000,dtype=np.int64)})
        self.context=combined[:,-64:]
        return float(output[0,0])


class WhisperRecognizer:
    def __init__(self, model, settings):
        self.name=model
        self.settings=settings
        self.model=None
        self.device=None
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='whisper-'+model.replace('/','-'))

    def _load(self):
        if self.model is not None:
            return
        import ctranslate2
        from faster_whisper import WhisperModel
        auto=self.settings.stt_device=='auto'
        device=self.settings.stt_device
        if auto:
            try: device='cuda' if ctranslate2.get_cuda_device_count()>0 else 'cpu'
            except Exception: device='cpu'
        kwargs={'download_root':str(Path(self.settings.model_dir)/'whisper'), 'local_files_only':True,
                'cpu_threads':self.settings.stt_threads,'num_workers':1}
        try:
            self.model=WhisperModel(self.name,device=device,compute_type='float16' if device=='cuda' else 'int8',**kwargs)
            self.device=device
            # CUDA libraries may fail on first inference rather than construction.
            import numpy as np
            segments,_=self.model.transcribe(np.zeros(1600,dtype=np.float32),language=self.settings.language,beam_size=1)
            list(segments)
        except Exception:
            if not auto or device!='cuda':
                self.model=None
                raise
            self.model=WhisperModel(self.name,device='cpu',compute_type='int8',**kwargs)
            self.device='cpu'

    async def warmup(self):
        await asyncio.get_running_loop().run_in_executor(self.pool,self._load)

    def _transcribe(self, pcm):
        self._load()
        import numpy as np
        audio=np.frombuffer(pcm,dtype='<i2').astype(np.float32)/32768
        segments,_=self.model.transcribe(audio,language=self.settings.language,beam_size=1,
                                        condition_on_previous_text=False,vad_filter=False,temperature=0,
                                        no_speech_threshold=.6,log_prob_threshold=-1)
        # faster-whisper returns a generator: consume it INSIDE the worker.
        return ' '.join(s.text.strip() for s in segments if s.no_speech_prob<.6 and s.avg_logprob>-1).strip()

    async def transcribe(self, pcm):
        return await asyncio.get_running_loop().run_in_executor(self.pool,self._transcribe,pcm)

    def close(self):
        self.pool.shutdown(wait=False,cancel_futures=True)


class PiperSynthesizer:
    def __init__(self, settings):
        self.settings=settings
        self.voices={}
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='piper')

    def _load(self, name):
        from piper import PiperVoice
        if name not in self.voices:
            model=Path(self.settings.model_dir)/'piper'/f'{name}.onnx'
            if not model.is_file() or not Path(str(model)+'.json').is_file():
                raise VoiceUnavailable('Piper voice assets are missing; run scripts.setup_voice')
            # CPU TTS avoids competing with STT for GPU memory.
            self.voices[name]=PiperVoice.load(str(model),use_cuda=False)
        return self.voices[name]

    async def warmup(self, name):
        await asyncio.get_running_loop().run_in_executor(self.pool,self._load,name)

    def _synthesize(self, text, name):
        output=io.BytesIO()
        with wave.open(output,'wb') as wav:
            self._load(name).synthesize_wav(text,wav)
        return output.getvalue()

    async def synthesize(self, text, name):
        return await asyncio.get_running_loop().run_in_executor(self.pool,self._synthesize,text,name)

    def close(self):
        self.pool.shutdown(wait=False,cancel_futures=True)


class LocalVoiceServices:
    def __init__(self, settings):
        self.settings=settings
        self.stt=WhisperRecognizer(settings.stt_model,settings)
        self.fast=WhisperRecognizer(settings.fast_stt_model,settings)
        self.tts=PiperSynthesizer(settings)

    def voices(self):
        directory=Path(self.settings.model_dir)/'piper'
        return sorted(p.stem for p in directory.glob('*.onnx') if Path(str(p)+'.json').is_file())

    def availability(self):
        modules=['numpy','onnxruntime','faster_whisper']+(['piper'] if self.settings.tts_enabled else [])
        missing=[name for name in modules if importlib.util.find_spec(name) is None]
        return {'missing_packages':missing,'vad_model_present':(Path(self.settings.model_dir)/'silero_vad.onnx').is_file(),
                'voices':self.voices()}

    async def prepare(self, options):
        if not self.settings.vad_enabled:
            raise VoiceUnavailable('Continuous voice requires VAD_ENABLED=true')
        availability=self.availability()
        missing=[name for name in availability['missing_packages'] if name!='piper' or options.tts_enabled]
        if missing or not availability['vad_model_present']:
            raise VoiceUnavailable('Install requirements-voice.txt and run python -m scripts.setup_voice before enabling the microphone')
        if options.tts_enabled and self.settings.tts_enabled and options.voice not in self.voices():
            raise VoiceUnavailable('Selected Piper voice is not installed')
        try:
            await self.stt.warmup()
            if self.settings.interrupt_enabled:
                await self.fast.warmup()
            if options.tts_enabled and self.settings.tts_enabled:
                await self.tts.warmup(options.voice)
            return await asyncio.to_thread(SileroVAD,Path(self.settings.model_dir)/'silero_vad.onnx')
        except VoiceUnavailable:
            raise
        except Exception as exc:
            raise VoiceUnavailable('Local speech model initialization failed. Check cached models, device settings and the voice installation guide.') from exc

    def close(self):
        self.stt.close(); self.fast.close(); self.tts.close()
