import json
import time
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from backend.main import create_app

ROOT = Path(__file__).resolve().parents[1]


def example(name='house'):
    return json.loads((ROOT / f'examples/{name}.json').read_text())


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(str(tmp_path / 'scene.sqlite3'), tick_seconds=.005)) as c:
        yield c


def wait_for(client, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        scene = client.get('/scene').json()
        if predicate(scene):
            return scene
        time.sleep(.01)
    raise AssertionError(f'Timed out; scene={scene}')


def test_live_interrupt_and_reconnect(client):
    with client.websocket_connect('/ws') as ws:
        assert ws.receive_json()['scene'] == client.get('/scene').json()
        assert client.post('/commands/draw',json=example()).status_code == 200
        event = ws.receive_json()
        assert event['type'] == 'scene' and event['scene']['objects']
        wait_for(client, lambda s: s['current_stroke_id'] is not None)
        paused = client.post('/commands/pause').json()
        time.sleep(.04)
        assert client.get('/scene').json() == paused
        assert 0 < paused['objects'][0]['strokes'][0]['progress'] < 1
        client.post('/commands/resume',json={})
        wait_for(client, lambda s: s['objects'][0]['strokes'][0]['progress'] > paused['objects'][0]['strokes'][0]['progress'])
        cancelled = client.post('/commands/cancel',json={}).json()
    with client.websocket_connect('/ws') as ws:
        assert ws.receive_json()['scene'] == cancelled == client.get('/scene').json()
    assert client.post('/commands/speed',json={'speed':5}).status_code == 200
    assert client.post('/commands/draw',json=example('x')).status_code == 200
    finished = wait_for(client, lambda s:s['status']=='idle')
    assert finished['objects'][0] == cancelled['objects'][0]
    assert len(finished['completed_stroke_ids']) == 2
    status = client.get('/status').json()
    assert status['completed'] == 2 and status['cancelled'] == 5
    assert client.post('/commands/clear',json={}).json()['objects'] == []
    time.sleep(.03)
    assert client.get('/scene').json()['objects'] == []


@pytest.mark.parametrize('geometry',[
    {'type':'line','start':{'x':-.1,'y':0},'end':{'x':1,'y':1}},
    {'type':'polyline','points':[{'x':.2,'y':.3}]},
    {'type':'circle','center':{'x':.9,'y':.5},'radius':.2},
    {'type':'circle','center':{'x':.5,'y':.5},'radius':0},
    {'type':'rectangle','origin':{'x':.5,'y':.5},'width':.6,'height':.2},
    {'type':'cubic_bezier','start':{'x':0,'y':0},'end':{'x':1,'y':1}},
    {'type':'line','start':{'x':'0.1','y':0},'end':{'x':1,'y':1}},
    {'type':'path','d':'M 0 0 L 1 1'},
    {'type':'line','start':{'x':True,'y':0},'end':{'x':1,'y':1}},
])
def test_invalid_geometry_fails_without_mutation(client, geometry):
    before = client.get('/scene').json()
    response = client.post('/commands/draw',json={'object_id':'invalid','paths':[{'stroke_id':'bad','geometry':geometry}]})
    assert response.status_code == 422
    assert client.get('/scene').json() == before


def test_invalid_ids_duplicates_and_stale_generation(client):
    cmd = example('circle')
    duplicate = dict(cmd, paths=cmd['paths']*2)
    assert client.post('/commands/draw',json=duplicate).status_code == 422
    assert client.post('/commands/draw',json=dict(cmd,object_id='bad id')).status_code == 422
    assert client.post('/commands/draw',json=dict(cmd,unknown=True)).status_code == 422
    assert client.post('/commands/draw',json=cmd).status_code == 200
    assert client.post('/commands/draw',json=cmd).status_code == 409
    assert client.post('/commands/draw',json=dict(cmd,object_id='different')).status_code == 409
    client.post('/commands/cancel',json={})
    stale = dict(example('x'),expected_generation=0)
    assert client.post('/commands/draw',json=stale).status_code == 409
    assert client.post('/commands/speed',json={'speed':-1}).status_code == 422
    assert client.post('/commands/pause',json={'unexpected':1}).status_code == 422


def test_manual_api_persists_and_reconnects(client):
    command = {'object_id':'user-1','paths':[{'stroke_id':'user-1-pen','geometry':{'type':'polyline','points':[{'x':.2,'y':.2},{'x':.3,'y':.4},{'x':.4,'y':.2}]}}]}
    result = client.post('/commands/manual',json=command)
    assert result.status_code == 200
    scene = result.json()
    obj = scene['objects'][0]
    assert obj['source'] == 'user'
    assert obj['strokes'][0]['executed_points'] == command['paths'][0]['geometry']['points']
    assert obj['strokes'][0]['progress'] == 1
    with client.websocket_connect('/ws') as ws:
        assert ws.receive_json()['scene'] == scene


def test_static_ui_examples_and_schemas(client):
    assert client.get('/').status_code == 200
    assert 'Drawing studio' in client.get('/').text
    assert client.get('/static/app.js').status_code == 200
    assert client.get('/examples/house.json').json() == example()
    schema = client.get('/openapi.json').json()
    assert 'DrawCommand' in schema['components']['schemas']
    assert 'discriminator' in schema['components']['schemas']['StrokeSpec']['properties']['geometry']


def test_completed_board_survives_restart(tmp_path):
    path = str(tmp_path / 'restart.sqlite3')
    with TestClient(create_app(path)) as client:
        client.post('/commands/manual',json=example('square'))
        snapshot = client.get('/scene').json()
    with TestClient(create_app(path)) as client:
        assert client.get('/scene').json() == snapshot


def test_exported_schemas_match_contract():
    from scripts.export_schemas import schemas
    for name, schema in schemas().items():
        assert json.loads((ROOT / 'schemas' / f'{name}.json').read_text()) == schema


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity'])
def test_nonfinite_inputs_return_safe_validation_error(client, value):
    before = client.get('/scene').json()
    body = '{"object_id":"bad","paths":[{"stroke_id":"bad-stroke","geometry":{"type":"circle","center":{"x":' + value + ',"y":0.5},"radius":0.1}}]}'
    response = client.post('/commands/draw',content=body,headers={'Content-Type':'application/json'})
    assert response.status_code == 422
    assert 'detail' in response.json()
    assert client.get('/scene').json() == before


def test_malformed_json_returns_safe_validation_error(client):
    response = client.post('/commands/draw',content='{broken',headers={'Content-Type':'application/json'})
    assert response.status_code == 422
    assert client.get('/scene').json()['objects'] == []


def test_repeated_websocket_disconnects_release_subscriptions(client):
    for _ in range(25):
        with client.websocket_connect('/ws') as ws:
            assert ws.receive_json()['type'] == 'scene'
        assert not client.app.state.events.subscribers
