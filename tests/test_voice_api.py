import shutil
import struct
import subprocess
import time
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from backend.main import create_app
from backend.voice.config import VoiceSettings
from test_voice import FakeServices, SPEECH, SILENCE
from test_agent import MockClient, response


def receive(ws, kind):
    for _ in range(80):
        event=ws.receive_json()
        if event['type']==kind: return event
    raise AssertionError(f'No {kind} event')


def test_voice_websocket_controls_and_reconnect(tmp_path):
    services=FakeServices(texts=['Stop'])
    app=create_app(str(tmp_path/'board.db'),agent_client=MockClient(response(text='Hello.')),
                   voice_settings=VoiceSettings(),voice_services=services)
    with TestClient(app) as client:
        sid=client.post('/agent/sessions').json()['session_id']
        assert client.get('/voice/config').json()['enabled']
        with client.websocket_connect(f'/voice/ws/{sid}') as ws:
            ws.send_json({'type':'start'})
            first=receive(ws,'ready')['stream_id']
            ws.send_bytes(b'invalid')
            assert 'Invalid audio frame' in receive(ws,'error')['message']
            for sequence,pcm in enumerate([SPEECH]*18+[SILENCE]*16):
                packet=struct.pack('<I',sequence)+pcm
                ws.send_bytes(packet)
                ws.send_bytes(packet)  # Network replay must not duplicate speech.
                time.sleep(.004)
            received={}
            while not {'control','tts_audio'}<=received.keys():
                message=ws.receive_json()
                received[message['type']]=message
            assert received['control']['action']=='pause'
            audio=received['tts_audio']
            ws.send_json({'type':'playback_started','audio_id':audio['audio_id'],'generation':audio['generation']})
            ws.send_json({'type':'disable'})
        history=client.get(f'/agent/sessions/{sid}').json()
        users=[e for e in history['events'] if e['kind']=='user']
        assert len(users)==1 and users[0]['source']=='voice'
        with client.websocket_connect(f'/voice/ws/{sid}') as ws:
            ws.send_json({'type':'start'})
            assert receive(ws,'ready')['stream_id']!=first
            assert client.get(f'/agent/sessions/{sid}').json()==history
            ws.send_json({'type':'mute','muted':True})
            ws.send_json({'type':'disable'})
        assert client.get('/voice/diagnostics').status_code==200


def test_disabled_voice_does_not_break_whiteboard(tmp_path):
    with TestClient(create_app(str(tmp_path/'board.db'),voice_settings=VoiceSettings(enabled=False),voice_services=FakeServices())) as client:
        sid=client.post('/agent/sessions').json()['session_id']
        with client.websocket_connect(f'/voice/ws/{sid}') as ws:
            ws.send_json({'type':'start'})
            assert ws.receive_json()['type']=='error'
        assert client.get('/scene').status_code==200


def test_browser_audio_unit_contract():
    if not shutil.which('node'): pytest.skip('Node is optional for browser unit checks')
    subprocess.run(['node','tests/voice_browser_unit.cjs'],cwd=Path(__file__).resolve().parents[1],check=True,timeout=15)
