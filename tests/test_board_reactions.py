import asyncio
import json
from backend.models import DrawCommand
from test_agent import setup, MockClient, response, read, mark, paths, async_test


def ink(index):
    points=[[(.45,.45),(.55,.55)],[(.55,.45),(.45,.55)]][index]
    return DrawCommand(object_id=f'user-{index}',paths=[{'stroke_id':f'pen-{index}','geometry':{'type':'polyline','points':[{'x':x,'y':y} for x,y in points]}}])


async def finish(agent):
    for _ in range(300):
        if not agent.tasks: return
        await asyncio.sleep(.01)
    raise AssertionError('Agent did not finish')


@async_test
async def test_settled_ink_triggers_one_model_response_through_shared_tools(tmp_path):
    client=MockClient(read(),response(('move','draw_strokes',mark(0,'O'))),read('again'),response(('resume','resume_drawing',{})),response(text='Your turn!'))
    async with setup(tmp_path,client) as (agent,ex,sid):
        agent.board_reaction_delay=.02
        await ex.execute(DrawCommand(**paths('tic_tac_toe','arbitrary-layout')))
        await ex.advance(100)
        await agent.external_command('manual',ink(0),reaction_session=sid)
        await agent.external_command('manual',ink(1),reaction_session=sid)
        await finish(agent)
        scene=await ex.get_scene_state()
        assert any(o.object_id==mark(0,'O')['object_id'] for o in scene.objects)
        assert scene.status=='drawing'
        assert len([o for o in scene.objects if o.source=='user'])==2
        session=await agent.store.get(sid)
        assert len([e for e in session.events if e.kind=='user' and e.source=='board'])==1
        assert 'recent_user_ink' in json.dumps(client.requests[0]['input'])
        await ex.advance(100)
        assert len(client.requests)==5  # Engine ink must not recursively trigger replies.


@async_test
async def test_rapid_strokes_debounce_and_stop_revokes_pending_reaction(tmp_path):
    client=MockClient(response(text='I see your drawing.'))
    async with setup(tmp_path,client) as (agent,ex,sid):
        agent.board_reaction_delay=.04
        await agent.external_command('manual',ink(0),reaction_session=sid)
        await agent.external_command('manual',ink(1),reaction_session=sid)
        await finish(agent)
        assert len(client.requests)==1
        cmd=ink(0).model_copy(update={'object_id':'new-ink','paths':[ink(0).paths[0].model_copy(update={'stroke_id':'new-stroke'})]})
        await agent.external_command('manual',cmd,reaction_session=sid)
        await agent.stop(sid)
        await finish(agent)
        assert len(client.requests)==1
        assert len((await ex.get_scene_state()).objects)==3


@async_test
async def test_manual_without_session_keeps_legacy_behavior(tmp_path):
    client=MockClient()
    async with setup(tmp_path,client) as (agent,ex,sid):
        await agent.external_command('manual',ink(0))
        assert not agent.tasks and not client.requests
        assert (await ex.get_scene_state()).objects[0].source=='user'


@async_test
async def test_model_can_observe_a_single_stroke_and_choose_to_wait(tmp_path):
    client=MockClient(response(text=''))
    async with setup(tmp_path,client) as (agent,ex,sid):
        agent.board_reaction_delay=.01
        await agent.external_command('manual',ink(0),reaction_session=sid)
        await finish(agent)
        assert len(client.requests)==1  # No coded symbol classifier gates observation.
        assert len((await ex.get_scene_state()).objects)==1  # No forced drawing action.
        assert (await agent.store.get(sid)).status=='idle'


def test_manual_http_reacts_in_selected_session_and_rejects_unknown_session(tmp_path):
    from fastapi.testclient import TestClient
    from backend.main import create_app
    import time
    mock=MockClient(response(text='I see your drawing.'))
    with TestClient(create_app(str(tmp_path/'board.sqlite3'),agent_client=mock)) as client:
        sid=client.post('/agent/sessions').json()['session_id']
        command=ink(0).model_dump(mode='json')
        assert client.post('/commands/manual?session_id=missing',json=command).status_code==404
        assert client.get('/scene').json()['objects']==[]
        assert client.post(f'/commands/manual?session_id={sid}',json=command).status_code==200
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            session=client.get(f'/agent/sessions/{sid}').json()
            if any(e['kind']=='assistant' for e in session['events']): break
            time.sleep(.02)
        else: raise AssertionError('No automatic board response')
        assert any(e['source']=='board' and e['kind']=='user' for e in session['events'])
        assert len(mock.requests)==1
