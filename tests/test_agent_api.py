import asyncio
import json
import time
from fastapi.testclient import TestClient
from backend.main import create_app
from test_agent import MockClient, read, response, paths


def finished(client,sid):
    for _ in range(300):
        session=client.get(f'/agent/sessions/{sid}').json()
        if session['status']!='thinking': return session
        time.sleep(.01)
    raise AssertionError('Agent did not finish')


def test_chat_http_and_reconnect_restore_conversation(tmp_path):
    mock=MockClient(read(),response(('draw','draw_strokes',paths('circle'))),response(text='Circle queued.'))
    database=str(tmp_path/'board.sqlite3')
    with TestClient(create_app(database,agent_client=mock)) as client:
        assert client.get('/agent/config').json()['model']=='gpt-6-astra'
        sid=client.post('/agent/sessions').json()['session_id']
        sent=client.post(f'/agent/sessions/{sid}/messages',json={'text':'Circle please','request_id':'message-1'})
        assert sent.status_code==202
        session=finished(client,sid)
        assert session['status']=='idle'
        assert any(e['kind']=='assistant' and 'queued' in e['content'] for e in session['events'])
        client.post('/commands/pause')
        scene=client.get('/scene').json()
        with client.websocket_connect('/ws') as ws:
            assert ws.receive_json()['scene']==scene
        assert client.get(f'/agent/sessions/{sid}').json()==session
        assert client.post(f'/agent/sessions/{sid}/messages',json={'text':'Circle please','request_id':'message-1'}).status_code==202
        assert len(client.get('/scene').json()['objects'])==1
    with TestClient(create_app(database,agent_client=MockClient())) as client:
        assert client.get(f'/agent/sessions/{sid}').json()==session
        assert 'Circle please' in json.dumps(session)


def test_missing_key_and_invalid_chat_fail_clearly(tmp_path,monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    with TestClient(create_app(str(tmp_path/'scene.sqlite3'))) as client:
        assert client.get('/agent/config').json()['configured'] is False
        sid=client.post('/agent/sessions').json()['session_id']
        assert client.post(f'/agent/sessions/{sid}/messages',json={'text':'Draw','request_id':'one'}).status_code==503
        assert client.post(f'/agent/sessions/{sid}/messages',json={'text':' ','request_id':'two'}).status_code==422
        assert client.get('/agent/sessions/missing').status_code==404
        assert client.post('/commands/draw',json=paths('circle')).status_code==200
        assert client.post(f'/agent/sessions/{sid}/stop').status_code==200
        assert client.get('/scene').json()['status']=='paused'


def test_stop_endpoint_is_immediate_during_model_request(tmp_path):
    async def blocked():
        await asyncio.sleep(10)
    mock=MockClient(blocked)
    with TestClient(create_app(str(tmp_path/'scene.sqlite3'),agent_client=mock)) as client:
        sid=client.post('/agent/sessions').json()['session_id']
        client.post(f'/agent/sessions/{sid}/messages',json={'text':'Dragon','request_id':'one'})
        start=time.monotonic()
        stopped=client.post(f'/agent/sessions/{sid}/stop')
        assert time.monotonic()-start<1
        assert stopped.json()['status']=='interrupted'
        assert client.get('/scene').json()['objects']==[]


def test_modify_endpoint_preserves_existing_ink(tmp_path):
    with TestClient(create_app(str(tmp_path/'scene.sqlite3'))) as client:
        client.post('/commands/manual',json=paths('circle'))
        before=client.get('/scene').json()['objects'][0]['strokes'][0]
        modification=paths('bezier','circle')
        result=client.post('/commands/modify',json=modification)
        assert result.status_code==200
        assert result.json()['objects'][0]['strokes'][0]==before
        assert len(result.json()['objects'][0]['strokes'])==2
