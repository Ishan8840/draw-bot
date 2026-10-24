"""Continuous capture, utterance STT, lexical interruption, and generation-safe TTS."""
import asyncio
import base64
from collections import deque
import json
import struct
import time
from uuid import uuid4
from fastapi import WebSocketDisconnect
from ..astra_agent import AgentUnavailable, StaleTurn
from .commands import classify_control, likely_echo, normalize, spoken_text
from .config import VoiceOptions
from .diagnostics import Diagnostics
from .segmentation import FRAME_BYTES, Segmenter, UtteranceState
from .services import LocalVoiceServices, VoiceUnavailable


class VoiceConnection:
    def __init__(self, hub, session_id, options, vad):
        self.hub,self.agent,self.session_id=hub,hub.agent,session_id
        self.options,self.vad=options,vad
        self.id=str(uuid4())
        settings=hub.settings.model_copy(update={'silence_ms':options.silence_ms,'threshold':options.threshold})
        self.segmenter=Segmenter(settings)
        self.out=asyncio.Queue(maxsize=64)
        self.audio=asyncio.Queue(maxsize=48)
        self.finals=asyncio.Queue(maxsize=4)
        self.previews=asyncio.Queue(maxsize=1)
        self.speech=asyncio.Queue(maxsize=8)
        self.stop=asyncio.Event()
        self.speech_idle=asyncio.Event(); self.speech_idle.set()
        self.generation=0
        self.suppress_speech=False
        self.muted=False
        self.last_sequence=-1
        self.latest_utterance=0
        self.barrier=0
        self.owned_epoch=None
        self.last_admitted=0
        self.recent_text=None
        self.recent_text_at=0
        self.last_spoken=''
        self.last_playback_at=0
        self.turns={}
        self.control_context=None
        self.playback_ids={}
        self.seen_final=set()
        self.seen_order=deque(maxlen=128)
        self.tasks=[]
        self.admission=asyncio.Lock()

    def emit(self, type, **data):
        if self.stop.is_set(): return
        event={'type':type,'stream_id':self.id,**data}
        try: self.out.put_nowait(event)
        except asyncio.QueueFull: self.stop.set()  # Disconnect a stalled consumer.

    @staticmethod
    def drain(queue):
        while not queue.empty(): queue.get_nowait()

    def invalidate_playback(self, reason, suppress=True):
        self.generation+=1
        self.suppress_speech=suppress
        self.drain(self.speech)
        self.playback_ids.clear()
        self.emit('playback_stop',generation=self.generation,reason=reason)

    def on_event(self, event):
        if event['session_id']!=self.session_id or self.stop.is_set(): return
        if event['kind'] in ('user','interruption'):
            self.invalidate_playback('new_instruction' if event['kind']=='user' else 'interruption',suppress=event['kind']!='user')
        if event['kind']=='assistant':
            uid=self.control_context if self.control_context is not None else self.turns.get(event['turn_id'])
            stage='local_control_response' if self.control_context is not None else 'astra_response'
            self.hub.diagnostics.record(stage,self.id,uid,event_wall_ms=event['created_at']*1000)
            if not self.suppress_speech:
                text=spoken_text(event['content'])
                if text and self.options.tts_enabled and self.hub.settings.tts_enabled and not self.muted:
                    try: self.speech.put_nowait((self.generation,str(event['id']),text,uid))
                    except asyncio.QueueFull:
                        self.invalidate_playback('speech_queue_overflow')
                        self.emit('error',message='Speech output fell behind. Obsolete audio was discarded.')
        elif event['kind']=='error':
            self.invalidate_playback('agent_error')
            self.emit('error',message=event['content'])

    def start(self):
        self.agent.store.listeners.add(self.on_event)
        self.agent.instruction_listeners.add(self.on_instruction)
        self.tasks=[asyncio.create_task(fn(),name=f'voice-{name}') for name,fn in
                    [('vad',self.audio_loop),('stt',self.final_loop),('fast-stt',self.fast_loop),('tts',self.tts_loop)]]
        self.emit('ready',generation=self.generation,sample_rate=16000,frame_samples=512,
                  stt_device=getattr(self.hub.services.stt,'device','mock'),fast_stt_device=getattr(self.hub.services.fast,'device','mock'))
        self.emit('status',state='listening')

    def on_instruction(self):
        self.invalidate_playback('instruction_epoch_changed')

    def accept_audio(self, packet):
        if len(packet)!=FRAME_BYTES+4:
            self.emit('error',message='Invalid audio frame: expected sequence number plus 512 PCM16 samples.')
            return
        sequence=struct.unpack_from('<I',packet)[0]
        if sequence<=self.last_sequence:
            return  # Retransmitted frames cannot create another utterance.
        gap=self.last_sequence>=0 and sequence!=self.last_sequence+1
        self.last_sequence=sequence
        try: self.audio.put_nowait((packet[4:],gap))
        except asyncio.QueueFull:
            # Never silently concatenate across missing audio.
            self.drain(self.audio)
            self.audio.put_nowait((packet[4:],True))
            self.emit('error',message='Audio processing fell behind. Please repeat the current phrase.')

    async def audio_loop(self):
        while True:
            pcm,gap=await self.audio.get()
            try:
                if gap:
                    if self.segmenter.current: self.segmenter.current.finalized=True
                    self.drain(self.previews)
                    self.segmenter.reset()
                    self.speech_idle.set()
                    await asyncio.to_thread(self.vad.reset)
                    self.emit('status',state='listening')
                probability=await asyncio.to_thread(self.vad.probability,pcm)
                for event,utterance in self.segmenter.feed(pcm,probability):
                    if event=='start':
                        self.latest_utterance=utterance.id
                        utterance.epoch=self.agent.instruction_epoch
                        self.speech_idle.clear()
                        self.invalidate_playback('speech_onset')
                        self.hub.diagnostics.record('speech_start',self.id,utterance.id,estimated_speech_wall_ms=utterance.speech_start_ms)
                        self.emit('status',state='speaking',utterance_id=utterance.id)
                    elif event=='preview' and self.hub.settings.interrupt_enabled:
                        self.drain(self.previews)
                        self.previews.put_nowait((utterance,bytes(utterance.pcm)))
                    elif event=='end':
                        self.speech_idle.set()
                        self.hub.diagnostics.record('speech_end',self.id,utterance.id,estimated_speech_wall_ms=utterance.speech_end_ms)
                        if utterance.truncated:
                            utterance.finalized=True
                            self.emit('error',message='That utterance exceeded the audio limit. Please use a shorter instruction.')
                        else:
                            try: self.finals.put_nowait(utterance)
                            except asyncio.QueueFull:
                                utterance.finalized=True
                                self.emit('error',message='Transcription queue is full. This utterance was not submitted; please repeat it.')
                        self.emit('status',state='listening')
            except asyncio.CancelledError: raise
            except Exception as exc:
                self.hub.diagnostics.record('vad_error',self.id,error_type=type(exc).__name__)
                self.segmenter.reset(); self.speech_idle.set()
                self.emit('error',message='Voice detection failed. Disable and re-enable the microphone to retry.')

    def expected_epoch(self, utterance):
        current=self.agent.instruction_epoch
        if utterance.id<self.barrier:
            raise StaleTurn('A later voice control superseded this utterance')
        if utterance.epoch==current:
            return current
        if self.owned_epoch==current and utterance.id>=self.last_admitted:
            return current  # Ordered utterances from this stream share their history.
        raise StaleTurn('Text input or another board command superseded this utterance')

    async def apply_control(self, utterance, text, action):
        async with self.admission:
            return await self._apply_control(utterance,text,action)

    async def _apply_control(self, utterance, text, action):
        if utterance.fast_handled or self.stop.is_set(): return
        epoch=self.expected_epoch(utterance)
        self.barrier=max(self.barrier,utterance.id)
        def applied():
            self.hub.diagnostics.record('drawing_pause' if action=='pause' else 'drawing_cancel',self.id,utterance.id,
                                       speech_to_control_ms=round(time.time()*1000-utterance.speech_start_ms,3))
        self.control_context=utterance.id
        try:
            result=await self.agent.handle_voice_control(self.session_id,text,
                        f'voice-{self.id}-{utterance.id}-control',action,expected_epoch=epoch,on_applied=applied)
        finally:
            self.control_context=None
        self.owned_epoch=self.agent.instruction_epoch
        self.last_admitted=utterance.id
        utterance.fast_handled=True
        self.turns[result.turn_id]=utterance.id
        self.emit('control',action=action,utterance_id=utterance.id)

    async def fast_loop(self):
        while True:
            utterance,pcm=await self.previews.get()
            if utterance.finalized or utterance.fast_handled or utterance.id!=self.latest_utterance:
                continue
            try:
                started=time.perf_counter()
                text=await self.hub.services.fast.transcribe(pcm)
                self.hub.diagnostics.record('fast_transcription_complete',self.id,utterance.id,duration_ms=round((time.perf_counter()-started)*1000,3))
                if self.stop.is_set() or utterance.finalized or utterance.id!=self.latest_utterance:
                    continue
                action,_=classify_control(text,self.hub.settings.interrupt_phrases)
                if action:
                    await self.apply_control(utterance,text,action)
            except StaleTurn:
                pass
            except asyncio.CancelledError: raise
            except Exception as exc:
                self.hub.diagnostics.record('fast_stt_error',self.id,utterance.id,error_type=type(exc).__name__)
                self.emit('error',message='Fast speech recognition failed; full-utterance recognition is still available. Use Stop if needed.')

    async def final_loop(self):
        while True:
            utterance=await self.finals.get()
            if utterance.id in self.seen_final: continue
            if len(self.seen_order)==self.seen_order.maxlen:
                self.seen_final.discard(self.seen_order[0])
            self.seen_order.append(utterance.id); self.seen_final.add(utterance.id)
            self.emit('transcription',state='transcribing',utterance_id=utterance.id)
            started=time.perf_counter()
            try:
                text=await self.hub.services.stt.transcribe(utterance.pcm)
                self.hub.diagnostics.record('transcription_complete',self.id,utterance.id,
                    duration_ms=round((time.perf_counter()-started)*1000,3),
                    speech_end_to_transcript_ms=round(time.time()*1000-(utterance.speech_end_ms or time.time()*1000),3))
                if self.stop.is_set(): continue
                text=text.strip()[:8000]
                self.emit('transcript',text=text,utterance_id=utterance.id)
                if not text:
                    self.emit('transcription',state='empty',utterance_id=utterance.id)
                    continue
                await self.submit_transcript(utterance,text)
            except StaleTurn:
                utterance.state=UtteranceState.SUPERSEDED
                self.emit('transcription',state='superseded',utterance_id=utterance.id)
            except asyncio.CancelledError: raise
            except Exception as exc:
                utterance.state=UtteranceState.ERROR
                self.hub.diagnostics.record('stt_or_submission_error',self.id,utterance.id,error_type=type(exc).__name__)
                self.emit('error',message=str(exc) if isinstance(exc,AgentUnavailable) else 'Speech processing failed. Please repeat the instruction or use text chat.')
            finally:
                utterance.finalized=True

    async def submit_transcript(self, utterance, text):
        async with self.admission:
            self.expected_epoch(utterance)
            action,only_control=classify_control(text,self.hub.settings.interrupt_phrases)
            now=time.monotonic()
            if not action and now-self.last_playback_at<10 and likely_echo(text,self.last_spoken):
                self.emit('transcription',state='echo_suppressed',utterance_id=utterance.id)
                return
            key=normalize(text)
            if not action and key==self.recent_text and now-self.recent_text_at<1.5:
                self.emit('transcription',state='duplicate',utterance_id=utterance.id)
                return
            self.recent_text,self.recent_text_at=key,now
            if action and self.hub.settings.interrupt_enabled:
                await self._apply_control(utterance,text,action)
                if only_control:
                    utterance.state=UtteranceState.COMPLETE
                    return
            utterance.state=UtteranceState.SUBMITTING
            self.emit('transcription',state='submitting',utterance_id=utterance.id)
            result=await self.agent.handle_user_instruction(self.session_id,text,'voice',
                   f'voice-{self.id}-{utterance.id}-final',expected_epoch=self.expected_epoch(utterance))
            self.owned_epoch=self.agent.instruction_epoch; self.last_admitted=utterance.id
            self.turns[result.turn_id]=utterance.id
            if len(self.turns)>128: self.turns.pop(next(iter(self.turns)))
            utterance.state=UtteranceState.COMPLETE
            self.hub.diagnostics.record('instruction_submitted',self.id,utterance.id)
            self.emit('transcription',state='submitted',utterance_id=utterance.id)

    async def tts_loop(self):
        while True:
            generation,audio_id,text,uid=await self.speech.get()
            await self.speech_idle.wait()
            if generation!=self.generation or self.suppress_speech or self.muted: continue
            started=time.perf_counter()
            self.emit('tts_status',state='generating',generation=generation)
            try:
                wav=await self.hub.services.tts.synthesize(text,self.options.voice)
                self.hub.diagnostics.record('tts_generated',self.id,uid,duration_ms=round((time.perf_counter()-started)*1000,3),audio_bytes=len(wav))
                if self.stop.is_set() or generation!=self.generation or self.suppress_speech or self.muted:
                    continue
                if len(wav)>4_000_000:
                    raise ValueError('TTS output exceeded the response limit')
                self.last_spoken=text
                self.playback_ids[audio_id]=(generation,uid)
                if len(self.playback_ids)>16: self.playback_ids.pop(next(iter(self.playback_ids)))
                self.emit('tts_audio',generation=generation,audio_id=audio_id,utterance_id=uid,
                          audio=base64.b64encode(wav).decode('ascii'),mime='audio/wav')
            except asyncio.CancelledError: raise
            except Exception as exc:
                self.hub.diagnostics.record('tts_error',self.id,uid,error_type=type(exc).__name__)
                self.emit('error',message='Local speech synthesis failed. Text replies and drawing remain available.')

    def client_event(self, data):
        kind=data.get('type')
        if kind=='barge_in':
            self.invalidate_playback('browser_barge_in')
            self.hub.diagnostics.record('client_barge_in',self.id)
        elif kind=='mute':
            if not isinstance(data.get('muted'),bool): raise ValueError('muted must be boolean')
            self.muted=data['muted']
            self.invalidate_playback('mute_changed',suppress=False)
        elif kind=='responses':
            if not isinstance(data.get('enabled'),bool): raise ValueError('enabled must be boolean')
            self.options.tts_enabled=data['enabled']
            self.invalidate_playback('voice_responses_changed',suppress=False)
        elif kind=='playback_started':
            entry=self.playback_ids.get(data.get('audio_id'))
            if entry and entry[0]==self.generation and data.get('generation')==self.generation:
                self.last_playback_at=time.monotonic()
                self.hub.diagnostics.record('playback_started',self.id,entry[1],generation=self.generation)
        elif kind=='disable':
            self.stop.set()
        else:
            raise ValueError('Unknown voice control message')

    async def close(self):
        self.stop.set()
        self.agent.store.listeners.discard(self.on_event)
        self.agent.instruction_listeners.discard(self.on_instruction)
        for task in self.tasks: task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        self.drain(self.audio); self.drain(self.finals); self.drain(self.previews); self.drain(self.speech)
        self.playback_ids.clear()


class VoiceHub:
    def __init__(self,agent,settings,services=None):
        self.agent,self.settings=agent,settings
        self.services=services or LocalVoiceServices(settings)
        self.diagnostics=Diagnostics()
        self.connection=None
        self.lock=asyncio.Lock()

    def configuration(self):
        return {'enabled':self.settings.enabled, 'stt_model':self.settings.stt_model,
                'fast_stt_model':self.settings.fast_stt_model,'device':self.settings.stt_device,
                'tts_enabled':self.settings.tts_enabled,'default_voice':self.settings.default_voice,
                'silence_ms':self.settings.silence_ms,'threshold':self.settings.threshold,
                **self.services.availability()}

    async def connect(self,session_id,options):
        if not self.settings.enabled: raise VoiceUnavailable('Voice is disabled by VOICE_ENABLED=false')
        await self.agent.store.get(session_id)
        async with self.lock:
            if self.connection:
                if self.connection.session_id!=session_id and not self.connection.stop.is_set():
                    raise VoiceUnavailable('Another session is using the microphone pipeline. Disable it first.')
                await self.connection.close()
            vad=await self.services.prepare(options)
            connection=VoiceConnection(self,session_id,options,vad)
            self.connection=connection
            connection.start()
            return connection

    async def websocket(self,ws,session_id):
        await ws.accept()
        connection=None
        try:
            raw=await asyncio.wait_for(ws.receive_text(),timeout=10)
            if len(raw)>4096: raise ValueError('Voice handshake is too large')
            options=VoiceOptions.model_validate_json(raw)
            connection=await self.connect(session_id,options)
            async def sender():
                while not connection.stop.is_set():
                    event=await connection.out.get()
                    await asyncio.wait_for(ws.send_json(event),timeout=3)
            async def receiver():
                while not connection.stop.is_set():
                    message=await ws.receive()
                    if message['type']=='websocket.disconnect': return
                    if message.get('bytes') is not None:
                        connection.accept_audio(message['bytes'])
                    elif message.get('text') is not None:
                        if len(message['text'])>4096: raise ValueError('Voice control message too large')
                        data=json.loads(message['text'])
                        if not isinstance(data,dict): raise ValueError('Voice control must be an object')
                        connection.client_event(data)
            import anyio
            async with anyio.create_task_group() as group:
                async def run(fn):
                    try: await fn()
                    finally: group.cancel_scope.cancel()
                group.start_soon(run,sender)
                group.start_soon(run,receiver)
                group.start_soon(run,connection.stop.wait)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:
            # ExceptionGroups from receive loops and model errors stay local.
            try:
                await ws.send_json({'type':'error','message':'Voice connection failed. Check local speech dependencies and model files, then reconnect.'})
            except Exception: pass
        finally:
            if connection:
                await connection.close()
                if self.connection is connection: self.connection=None
            try: await ws.close()
            except Exception: pass

    async def close(self):
        if self.connection: await self.connection.close()
        self.services.close()
