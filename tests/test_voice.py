import asyncio
import base64
from contextlib import asynccontextmanager
import io
import json
from pathlib import Path
import struct
import time
import wave
import pytest
from backend.agent_models import ChatMessage
from backend.astra_agent import StaleTurn
from backend.models import DrawCommand
from backend.voice.commands import classify_control, spoken_text
from backend.voice.config import VoiceOptions, VoiceSettings
from backend.voice.controller import VoiceHub
from backend.voice.segmentation import Segmenter, Utterance, UtteranceState
from backend.voice.services import VoiceUnavailable
from test_agent import setup, MockClient, response, paths, async_test

SPEECH=struct.pack('<512h',*([5000]*512))
SILENCE=b'\0'*1024


class FakeVAD:
    def __init__(self): self.fail=False
    def probability(self,pcm):
        if self.fail:
            self.fail=False
            raise RuntimeError('test VAD failure')
        return .95 if pcm==SPEECH else .01
    def reset(self): pass


class FakeRecognizer:
    device='mock-cpu'
    def __init__(self,texts): self.texts=list(texts); self.calls=0; self.entered=asyncio.Event(); self.release=None
    async def transcribe(self,pcm):
        self.calls+=1;self.entered.set()
        if self.release: await self.release.wait()
        await asyncio.sleep(.005)
        result=self.texts.pop(0) if self.texts else ''
        if isinstance(result,Exception): raise result
        return result


class FakeTTS:
    def __init__(self): self.calls=[];self.entered=asyncio.Event();self.release=None;self.fail=False
    async def synthesize(self,text,voice):
        self.calls.append((text,voice)); self.entered.set()
        if self.release: await self.release.wait()
        await asyncio.sleep(.005)
        if self.fail: raise RuntimeError('test TTS failure')
        out=io.BytesIO()
        with wave.open(out,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000);wav.writeframes(SPEECH)
        return out.getvalue()


class FakeServices:
    def __init__(self,texts=(),fast=()):
        self.stt=FakeRecognizer(texts);self.fast=FakeRecognizer(fast);self.tts=FakeTTS();self.vad=FakeVAD();self.closed=False
    def availability(self): return {'missing_packages':[],'vad_model_present':True,'voices':['en_US-lessac-medium']}
    async def prepare(self,options): return self.vad
    def close(self): self.closed=True


@asynccontextmanager
async def voice_setup(tmp_path,texts=(),fast=(),client=None,**settings):
    async with setup(tmp_path,client or MockClient(response(text='Sure, I can help.'))) as (agent,ex,sid):
        services=FakeServices(texts,fast)
        hub=VoiceHub(agent,VoiceSettings(**settings),services)
        conn=await hub.connect(sid,VoiceOptions())
        try: yield hub,conn,agent,ex,sid,services
        finally: await hub.close()


async def event(conn,kind,timeout=3):
    async def wait():
        while True:
            item=await conn.out.get()
            if item['type']==kind: return item
    return await asyncio.wait_for(wait(),timeout)


async def until(predicate,timeout=3):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate(): return
        await asyncio.sleep(.005)
    raise AssertionError('Timed out')


async def feed(conn,frames,sequence=0):
    for pcm in frames:
        conn.accept_audio(struct.pack('<I',sequence)+pcm);sequence+=1
        # Yield enough for VAD while keeping tests faster than real-time capture.
        await asyncio.sleep(.002)
    return sequence


def utterance(conn,index=1):
    return Utterance(index,SPEECH,time.time()*1000-1000,time.time()*1000,UtteranceState.TRANSCRIBING,epoch=conn.agent.instruction_epoch)


def test_vad_segmentation_short_noise_and_silence():
    segmenter=Segmenter(VoiceSettings(silence_ms=160,min_speech_ms=96))
    assert not segmenter.feed(SPEECH,.95)
    assert not segmenter.feed(SPEECH,.95)
    assert not segmenter.feed(SILENCE,.01)
    events=[]
    for i in range(18): events+=segmenter.feed(SPEECH,.95,now_ms=i*32)
    for i in range(5): events+=segmenter.feed(SILENCE,.01,now_ms=(i+18)*32)
    assert [e for e,_ in events].count('start')==1
    assert [e for e,_ in events].count('end')==1
    result=events[-1][1]
    assert result.state==UtteranceState.TRANSCRIBING
    assert len(result.pcm)>len(SPEECH)
    assert not result.truncated
    assert segmenter.state==UtteranceState.LISTENING


@pytest.mark.parametrize('text,action,only',[
    ('Stop','pause',True),('Wait, stop!','pause',True),('Astra, stop drawing.','pause',True),
    ('Cancel','cancel',True),('Please pause now','pause',True),
    ('Stop and make its wings bigger','pause',False),("Don't stop",None,False),
    ('Draw a sign that says stop',None,False),('Astra draw a dinosaur',None,False)])
def test_local_command_classification(text,action,only):
    assert classify_control(text,VoiceSettings().interrupt_phrases)==(action,only)


@async_test
async def test_transcription_submission_shares_text_history(tmp_path):
    client=MockClient(response(text='Hello from text.'),response(text='I will add to the drawing.'))
    async with voice_setup(tmp_path,texts=['Add a tree.'],client=client) as (hub,conn,agent,ex,sid,services):
        await agent.handle_user_instruction(sid,'Draw a house.','text','typed')
        await asyncio.gather(*list(agent.tasks))
        item=utterance(conn);conn.finals.put_nowait(item)
        await until(lambda:item.finalized)
        await asyncio.gather(*list(agent.tasks))
        session=await agent.store.get(sid)
        users=[e for e in session.events if e.kind=='user']
        assert [e.content for e in users]==['Draw a house.','Add a tree.']
        assert [e.source for e in users]==['text','voice']
        assert 'Draw a house.' in json.dumps(client.requests[-1]['input'])


@async_test
async def test_continuous_audio_reaches_transcript_and_pause(tmp_path):
    async with voice_setup(tmp_path,texts=['Wait, stop!'],fast=['']) as (hub,conn,agent,ex,sid,services):
        await ex.execute(DrawCommand(**paths('house')));await ex.advance(.4)
        before=await ex.get_scene_state()
        await feed(conn,[SPEECH]*18+[SILENCE]*16)
        control=await event(conn,'control')
        assert control['action']=='pause'
        after=await ex.get_scene_state()
        assert after.status=='paused'
        assert after.objects[0].strokes[0].executed_points==before.objects[0].strokes[0].executed_points
        assert services.stt.calls==1
        assert not agent.client.requests  # local control made no model request


@async_test
async def test_fast_recognition_cancels_before_full_transcription(tmp_path):
    async with voice_setup(tmp_path,texts=['Cancel'],fast=['Cancel']) as (hub,conn,agent,ex,sid,services):
        services.stt.release=asyncio.Event()
        await ex.execute(DrawCommand(**paths('house')));await ex.advance(.4)
        before=await ex.get_scene_state()
        await feed(conn,[SPEECH]*24)
        assert (await event(conn,'control'))['action']=='cancel'
        assert services.fast.calls>0 and services.stt.calls==0
        after=await ex.get_scene_state()
        assert after.status=='cancelled'
        assert after.objects[0].strokes[0].executed_points==before.objects[0].strokes[0].executed_points
        services.stt.release.set()


@async_test
async def test_control_revokes_inflight_astra_and_preserves_ink(tmp_path):
    blocked=asyncio.Event()
    async def slow(): blocked.set();await asyncio.sleep(10)
    client=MockClient(slow)
    async with voice_setup(tmp_path,texts=['stop'],client=client) as (hub,conn,agent,ex,sid,services):
        await agent.handle_user_instruction(sid,'Draw a dragon.','text','one')
        await blocked.wait();old=agent.active
        item=utterance(conn);conn.finals.put_nowait(item)
        await until(lambda:item.finalized)
        with pytest.raises(StaleTurn): await agent.execute_tool(old,'late','draw_strokes',json.dumps(paths('circle')))
        assert (await agent.store.get(sid)).status=='interrupted'


@async_test
async def test_duplicate_utterance_ids_and_transcripts_not_submitted_twice(tmp_path):
    async with voice_setup(tmp_path,texts=['Draw a house.','Draw a house.']) as (hub,conn,agent,ex,sid,services):
        first=utterance(conn);conn.finals.put_nowait(first);conn.finals.put_nowait(first)
        await until(lambda:first.finalized)
        second=utterance(conn,2);conn.finals.put_nowait(second)
        await until(lambda:second.finalized)
        assert services.stt.calls==2
        assert len([e for e in (await agent.store.get(sid)).events if e.kind=='user'])==1


@async_test
async def test_late_transcript_cannot_override_newer_text(tmp_path):
    async with voice_setup(tmp_path,texts=['Draw old drawing.']) as (hub,conn,agent,ex,sid,services):
        services.stt.release=asyncio.Event()
        item=utterance(conn);conn.finals.put_nowait(item)
        await services.stt.entered.wait()
        await agent.handle_user_instruction(sid,'Newer typed instruction.','text','new')
        services.stt.release.set();await until(lambda:item.finalized)
        assert item.state==UtteranceState.SUPERSEDED
        users=[e.content for e in (await agent.store.get(sid)).events if e.kind=='user']
        assert users==['Newer typed instruction.']


@async_test
async def test_tts_audio_and_playback_latency_ack(tmp_path):
    async with voice_setup(tmp_path) as (hub,conn,agent,ex,sid,services):
        await agent.store.append(sid,'spoken-turn','assistant','Sure, I will draw a house.')
        audio=await event(conn,'tts_audio')
        with wave.open(io.BytesIO(base64.b64decode(audio['audio'])),'rb') as wav:
            assert wav.getframerate()==16000 and wav.getnframes()==512
        conn.client_event({'type':'playback_started','audio_id':audio['audio_id'],'generation':audio['generation']})
        assert any(r['stage']=='playback_started' for r in hub.diagnostics.snapshot())


@async_test
async def test_barge_in_discards_tts_inference_and_future_obsolete_text(tmp_path):
    async with voice_setup(tmp_path) as (hub,conn,agent,ex,sid,services):
        services.tts.release=asyncio.Event()
        await agent.store.append(sid,'old','assistant','Old response.')
        await services.tts.entered.wait()
        conn.client_event({'type':'barge_in'})
        generation=conn.generation
        await agent.store.append(sid,'old','assistant','Another obsolete sentence.')
        services.tts.release.set()
        await until(lambda:any(r['stage']=='tts_generated' for r in hub.diagnostics.snapshot()))
        assert not any(e['type']=='tts_audio' for e in list(conn.out._queue))
        assert conn.generation==generation
        assert len(services.tts.calls)==1
        assert (await ex.get_scene_state()).status=='idle'  # audio barge-in alone never changes drawing


@async_test
async def test_microphone_onset_interrupts_speech_not_drawing(tmp_path):
    async with voice_setup(tmp_path) as (hub,conn,agent,ex,sid,services):
        await ex.execute(DrawCommand(**paths('circle')))
        await agent.store.append(sid,None,'assistant','I am drawing now.')
        await event(conn,'tts_audio')
        before=conn.generation
        await feed(conn,[SPEECH]*4)
        await until(lambda:conn.generation>before)
        assert (await ex.get_scene_state()).status=='drawing'


@async_test
async def test_mute_and_disable_voice_responses(tmp_path):
    async with voice_setup(tmp_path) as (hub,conn,agent,ex,sid,services):
        conn.client_event({'type':'mute','muted':True})
        await agent.store.append(sid,None,'assistant','Muted speech.')
        await asyncio.sleep(.03)
        assert services.tts.calls==[]
        conn.client_event({'type':'mute','muted':False})
        conn.client_event({'type':'responses','enabled':False})
        await agent.store.append(sid,None,'assistant','Disabled speech.')
        await asyncio.sleep(.03)
        assert services.tts.calls==[]


@async_test
async def test_multiple_rapid_utterances_ordered_and_later_stop_wins(tmp_path):
    async with voice_setup(tmp_path,texts=['Draw a house.','Add a tree.','Stop'],client=MockClient(response(text='Okay.'),response(text='Tree next.'))) as (hub,conn,agent,ex,sid,services):
        items=[utterance(conn,i) for i in range(1,4)]
        for item in items: conn.finals.put_nowait(item)
        await until(lambda:all(i.finalized for i in items))
        users=[e.content for e in (await agent.store.get(sid)).events if e.kind=='user']
        assert users==['Draw a house.','Add a tree.','Stop']
        assert (await agent.store.get(sid)).status=='interrupted'


@async_test
async def test_late_normal_transcription_discarded_after_fast_stop(tmp_path):
    async with voice_setup(tmp_path,texts=['Old request']) as (hub,conn,agent,ex,sid,services):
        services.stt.release=asyncio.Event()
        old=utterance(conn,1);conn.finals.put_nowait(old);await services.stt.entered.wait()
        latest=utterance(conn,2)
        await conn.apply_control(latest,'Stop','pause')
        services.stt.release.set();await until(lambda:old.finalized)
        assert old.state==UtteranceState.SUPERSEDED
        assert [e.content for e in (await agent.store.get(sid)).events if e.kind=='user']==['Stop']


@async_test
async def test_processing_errors_recover_and_tts_error_does_not_stop_board(tmp_path):
    async with voice_setup(tmp_path,texts=[RuntimeError('bad audio'),'Stop']) as (hub,conn,agent,ex,sid,services):
        first=utterance(conn,1);conn.finals.put_nowait(first);await until(lambda:first.finalized)
        assert first.state==UtteranceState.ERROR
        second=utterance(conn,2);conn.finals.put_nowait(second);await until(lambda:second.finalized)
        assert second.state==UtteranceState.COMPLETE
        services.tts.fail=True
        await ex.execute(DrawCommand(**paths('circle')))
        await agent.store.append(sid,None,'assistant','Another sentence.')
        await until(lambda:any(r['stage']=='tts_error' for r in hub.diagnostics.snapshot()))
        assert (await ex.get_scene_state()).status=='drawing'


@async_test
async def test_disconnect_discards_pending_transcript_and_reconnect_preserves_session(tmp_path):
    async with voice_setup(tmp_path,texts=['Old microphone request']) as (hub,conn,agent,ex,sid,services):
        services.stt.release=asyncio.Event();old=utterance(conn);conn.finals.put_nowait(old)
        await services.stt.entered.wait();await conn.close()
        services.stt.release.set()
        newer=await hub.connect(sid,VoiceOptions())
        assert newer.id!=conn.id and newer.session_id==sid
        assert conn.on_event not in agent.store.listeners
        assert not (await agent.store.get(sid)).events
        await newer.close()


@async_test
async def test_voice_enabled_switch(tmp_path):
    async with setup(tmp_path,MockClient()) as (agent,ex,sid):
        hub=VoiceHub(agent,VoiceSettings(enabled=False),FakeServices())
        with pytest.raises(VoiceUnavailable): await hub.connect(sid,VoiceOptions())
        await hub.close()


def test_spoken_text_does_not_read_tool_arguments():
    assert spoken_text('Sure, I will draw it.\n```json\n{"object_id":"house"}\n```')=='Sure, I will draw it.'
    assert 'stroke_id' not in spoken_text('stroke_id: bad\nYour turn!')


@async_test
async def test_fresh_repeated_stop_is_not_suppressed(tmp_path):
    async with voice_setup(tmp_path) as (hub,conn,agent,ex,sid,services):
        first=Utterance(1,b'',time.time()*1000,epoch=agent.instruction_epoch)
        await conn.submit_transcript(first,'Stop')
        await agent.external_command('resume')
        second=Utterance(2,b'',time.time()*1000,epoch=agent.instruction_epoch)
        epoch=agent.instruction_epoch
        await conn.submit_transcript(second,'Stop')
        assert agent.instruction_epoch==epoch+1
        assert second.fast_handled


def test_piper_adapter_runs_on_worker_and_produces_wav(tmp_path,monkeypatch):
    import sys
    import threading
    from types import SimpleNamespace
    from backend.voice.services import PiperSynthesizer
    directory=tmp_path/'piper';directory.mkdir()
    (directory/'test.onnx').write_bytes(b'mock')
    (directory/'test.onnx.json').write_text('{}')
    main_thread=threading.get_ident()
    class Voice:
        @staticmethod
        def load(path,use_cuda):
            assert threading.get_ident()!=main_thread and not use_cuda
            return Voice()
        def synthesize_wav(self,text,wav):
            assert threading.get_ident()!=main_thread
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000)
            wav.writeframes(SPEECH)
    monkeypatch.setitem(sys.modules,'piper',SimpleNamespace(PiperVoice=Voice))
    async def check():
        service=PiperSynthesizer(VoiceSettings(model_dir=str(tmp_path)))
        try:
            audio=await service.synthesize('Hello','test')
            with wave.open(io.BytesIO(audio),'rb') as wav:
                assert wav.getnframes()==512 and wav.getframerate()==16000
        finally: service.close()
    asyncio.run(check())


@async_test
async def test_diagnostics_synthetic_report(tmp_path):
    async with voice_setup(tmp_path,texts=['Stop'],fast=['']) as (hub,conn,agent,ex,sid,services):
        await ex.execute(DrawCommand(**paths('house')));await ex.advance(.4)
        await feed(conn,[SPEECH]*18+[SILENCE]*16)
        audio=await event(conn,'tts_audio')
        conn.client_event({'type':'playback_started','audio_id':audio['audio_id'],'generation':audio['generation']})
        records=hub.diagnostics.snapshot()
        stages={r['stage'] for r in records}
        assert {'speech_start','speech_end','transcription_complete','drawing_pause','local_control_response','tts_generated','playback_started'}<=stages
        # Development artifact records real measured software timings with MOCK
        # audio inference, not real microphones, neural inference, or playback.
        Path('/tmp/astra-voice-mock-diagnostics.json').write_text(json.dumps({'mode':'mocked audio inference and simulated playback acknowledgement; not hardware latency','records':records},indent=2))
