import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from backend.agent_models import ChatMessage
from backend.agent_tools import tool_definitions
from backend.astra_agent import AstraAgent, StaleTurn, Turn
from backend.conversation_store import ConversationStore
from backend.drawing_engine import DrawingEngine
from backend.executor import VirtualDrawingExecutor, CommandConflict
from backend.models import DrawCommand, ModifyCommand
from backend.scene_state import SceneStateManager
from backend.scene_summary import summary
from backend.websocket_manager import WebSocketManager

ROOT=Path(__file__).resolve().parents[1]


def paths(name, object_id=None):
    data=json.loads((ROOT/'examples'/f'{name}.json').read_text())
    return {'object_id':object_id or data['object_id'],'paths':data['paths']}


class Item(dict):
    def model_dump(self, **kwargs):
        return dict(self)


def response(*calls, text='', extra=None, status='completed'):
    items=[Item(type='function_call', call_id=call_id,name=name,arguments=json.dumps(args)) for call_id,name,args in calls]
    if extra: items.insert(0,Item(extra))
    return SimpleNamespace(id='resp_mock',output=items,output_text=text,status=status)


def read(call_id='read', object_id=None, detail=False):
    return response((call_id,'get_scene_state',{'object_id':object_id,'include_geometry':detail,'offset':0,'limit':10}))


class MockClient:
    def __init__(self, *steps):
        self.steps=list(steps)
        self.requests=[]
        self.responses=self

    async def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        assert self.steps, 'Unexpected extra model request'
        step=self.steps.pop(0)
        if isinstance(step,Exception): raise step
        if callable(step): return await step()
        return step


@asynccontextmanager
async def setup(tmp_path, client, **kwargs):
    state=SceneStateManager(str(tmp_path/'scene.sqlite3'))
    executor=VirtualDrawingExecutor(state,WebSocketManager())
    store=ConversationStore(str(tmp_path/'chat.sqlite3'))
    agent=AstraAgent(DrawingEngine(executor),store,client=client,**kwargs)
    session=await store.create()
    try:
        yield agent,executor,session.session_id
    finally:
        await agent.close()
        await executor.close()


def async_test(fn):
    def run(tmp_path):
        asyncio.run(fn(tmp_path))
    return run


async def send(agent,sid,text,request_id='request-1'):
    await agent.submit(sid,ChatMessage(text=text,request_id=request_id))
    await asyncio.gather(*list(agent.tasks))
    return await agent.store.get(sid)


@async_test
async def test_routes_valid_strokes_and_continues_with_call_ids(tmp_path):
    reason={'type':'reasoning','id':'rs_1','summary':[],'encrypted_content':'opaque'}
    first=read(); first.output.insert(0,Item(reason))
    client=MockClient(first,response(('draw','draw_strokes',paths('house'))),response(text='The house is queued.'))
    async with setup(tmp_path,client) as (agent,ex,sid):
        session=await send(agent,sid,'Draw a house.')
        scene=await ex.get_scene_state()
        assert scene.objects[0].object_id=='house'
        assert all(s.status=='pending' for s in scene.objects[0].strokes)
        assert session.status=='idle'
        assert client.requests[0]['model']=='gpt-6-astra'
        assert client.requests[0]['store'] is False
        continued=client.requests[1]['input']
        assert reason in continued
        assert any(i.get('type')=='function_call_output' and i['call_id']=='read' for i in continued)
        assert any(e.kind=='tool_result' and json.loads(e.content)['ok'] for e in session.events)


@async_test
async def test_multi_turn_reads_existing_scene_and_adds_object(tmp_path):
    client=MockClient(read(),response(('d1','draw_strokes',paths('house'))),response(text='House queued.'),
                      read('r2'),response(('d2','draw_strokes',paths('bezier','tree-right'))),response(text='Tree added.'))
    async with setup(tmp_path,client) as (agent,ex,sid):
        await send(agent,sid,'Draw a house.')
        await ex.advance(100)
        original=(await ex.get_scene_state()).objects[0]
        await send(agent,sid,'Add a tree to the right.','request-2')
        scene=await ex.get_scene_state()
        assert scene.objects[0]==original
        assert scene.objects[1].object_id=='tree-right'
        context=json.dumps(client.requests[3]['input'])
        assert 'house' in context and 'Draw a house.' in context and 'Add a tree to the right.' in context
        assert 'executed_points' not in context


@async_test
async def test_mid_execution_new_message_pauses_and_rejects_stale_tools(tmp_path):
    waiting=asyncio.Event(); release=asyncio.Event()
    async def delayed():
        waiting.set()
        try: await release.wait()
        except asyncio.CancelledError: await release.wait()  # simulate late transport completion
        return response(('late','draw_strokes',paths('circle','stale-object')))
    client=MockClient(read(),response(('d','draw_strokes',paths('house','dragon-wing'))),delayed,
                      read('new-read'),response(text='Stopped; I will preserve the existing ink.'))
    async with setup(tmp_path,client) as (agent,ex,sid):
        await agent.submit(sid,ChatMessage(text='Draw a dragon.',request_id='first'))
        await asyncio.wait_for(waiting.wait(),2)
        old_turn=agent.active
        await ex.advance(.5)
        before=await ex.get_scene_state()
        await agent.submit(sid,ChatMessage(text='Stop! Give it bigger wings.',request_id='second'))
        paused=await ex.get_scene_state()
        assert paused.status=='paused'
        assert paused.generation>before.generation
        assert paused.objects[0].strokes[0].executed_points==before.objects[0].strokes[0].executed_points
        with pytest.raises(StaleTurn):
            await agent.execute_tool(old_turn,'stale','clear_board','{}')
        await ex.advance(100)
        assert (await ex.get_scene_state()).objects==paused.objects
        release.set()
        await asyncio.gather(*list(agent.tasks))
        assert len((await ex.get_scene_state()).objects)==1


@async_test
async def test_modify_preserves_ink_and_replaces_only_remaining_plan(tmp_path):
    replacement=paths('bezier','house')
    client=MockClient(read('inspect','house',True),response(('edit','modify_object',replacement)),
                      read('again'),response(('resume','resume_drawing',{})),response(text='Revised the remaining plan.'))
    async with setup(tmp_path,client) as (agent,ex,sid):
        await ex.execute(DrawCommand(**paths('house')))
        await ex.advance(.4)
        before=await ex.get_scene_state()
        await send(agent,sid,'Make the unfinished roof bigger, then continue.')
        after=await ex.get_scene_state()
        assert after.status=='drawing'
        old=after.objects[0].strokes[:5]
        assert all(s.status=='cancelled' for s in old)
        assert old[0].executed_points==before.objects[0].strokes[0].executed_points
        assert old[0].geometry==before.objects[0].strokes[0].geometry
        assert after.objects[0].strokes[-1].status=='pending'
        await ex.advance(100)
        assert (await ex.get_scene_state()).objects[0].strokes[-1].status=='completed'


@async_test
async def test_tool_failure_returned_then_model_recovers(tmp_path):
    bad=paths('circle'); bad['paths'][0]['geometry']['radius']=.8
    client=MockClient(read(),response(('invalid','draw_strokes',bad)),
                      response(('valid','draw_strokes',paths('circle'))),response(text='Circle queued.'))
    async with setup(tmp_path,client) as (agent,ex,sid):
        session=await send(agent,sid,'Draw a circle.')
        assert session.status=='idle'
        assert len((await ex.get_scene_state()).objects)==1
        outputs=[i for i in client.requests[2]['input'] if i.get('type')=='function_call_output']
        assert json.loads(outputs[-1]['output'])['ok'] is False


@async_test
async def test_duplicate_call_id_executes_at_most_once(tmp_path):
    call=response(('same','draw_strokes',paths('circle')))
    client=MockClient(read(),call,call,response(text='Queued once.'))
    async with setup(tmp_path,client) as (agent,ex,sid):
        session=await send(agent,sid,'Circle.')
        assert len((await ex.get_scene_state()).objects)==1
        assert len([e for e in session.events if e.kind=='tool_call' and e.call_id=='same'])==1
        outputs=[i for i in client.requests[-1]['input'] if i.get('type')=='function_call_output' and i['call_id']=='same']
        assert outputs[0]['output']==outputs[1]['output']


@async_test
async def test_duplicate_http_message_does_not_restart_or_redraw(tmp_path):
    client=MockClient(read(),response(('d','draw_strokes',paths('circle'))),response(text='Queued.'))
    async with setup(tmp_path,client) as (agent,ex,sid):
        first=await send(agent,sid,'Circle.')
        snapshot=await ex.get_scene_state()
        second=await agent.submit(sid,ChatMessage(text='Circle.',request_id='request-1'))
        assert first==second
        assert await ex.get_scene_state()==snapshot
        with pytest.raises(CommandConflict):
            await agent.submit(sid,ChatMessage(text='Different',request_id='request-1'))


@async_test
async def test_read_before_write_and_unknown_tool_fail_safely(tmp_path):
    client=MockClient(response(('missing-read','draw_strokes',paths('circle'))),
                      response(('unknown','robot_joint',{})),read(),response(('good','draw_strokes',paths('circle'))),response(text='Queued.'))
    async with setup(tmp_path,client) as (agent,ex,sid):
        session=await send(agent,sid,'Circle.')
        errors=[json.loads(e.content) for e in session.events if e.kind=='tool_result' and not json.loads(e.content)['ok']]
        assert len(errors)==2
        assert len((await ex.get_scene_state()).objects)==1


@async_test
async def test_model_failure_pauses_ink_and_does_not_leak_secrets(tmp_path):
    client=MockClient(read(),response(('d','draw_strokes',paths('house'))),RuntimeError('secret-key-do-not-log'))
    async with setup(tmp_path,client) as (agent,ex,sid):
        session=await send(agent,sid,'House.')
        assert session.status=='error'
        assert (await ex.get_scene_state()).status=='paused'
        assert 'secret-key' not in session.model_dump_json()


@async_test
async def test_timeout_and_stop_do_not_wait_for_api(tmp_path):
    waiting=asyncio.Event()
    async def slow():
        waiting.set()
        await asyncio.sleep(5)
    client=MockClient(slow)
    async with setup(tmp_path,client,timeout=.05) as (agent,ex,sid):
        result=await send(agent,sid,'House.')
        assert result.status=='error'
        assert 'timed out' in result.events[-1].content


@async_test
async def test_legacy_cancel_invalidates_inflight_model(tmp_path):
    waiting=asyncio.Event()
    async def slow():
        waiting.set()
        await asyncio.sleep(10)
    client=MockClient(slow)
    async with setup(tmp_path,client) as (agent,ex,sid):
        await agent.submit(sid,ChatMessage(text='Draw.',request_id='first'))
        await waiting.wait()
        turn=agent.active
        await asyncio.wait_for(agent.external_command('cancel'),.5)
        with pytest.raises(StaleTurn):
            await agent.execute_tool(turn,'late','draw_strokes',json.dumps(paths('circle')))
        assert not (await ex.get_scene_state()).objects


@async_test
async def test_conversation_survives_store_reopen(tmp_path):
    client=MockClient(response(text='Hello, I can draw.'))
    async with setup(tmp_path,client) as (agent,ex,sid):
        before=await send(agent,sid,'Hello')
    store=ConversationStore(str(tmp_path/'chat.sqlite3'))
    try: assert await store.get(sid)==before
    finally: await store.close()


def mark(cell,letter):
    row,col=divmod(cell,3)
    x=.15+(col+.5)*(.7/3); y=.15+(row+.5)*(.7/3)
    geometry={'type':'circle','center':{'x':x,'y':y},'radius':.05} if letter=='O' else {'type':'line','start':{'x':x-.05,'y':y-.05},'end':{'x':x+.05,'y':y+.05}}
    return {'object_id':f'ttt.g.r{row}c{col}.{letter}', 'paths':[{'stroke_id':f'move-{cell}-{letter}','geometry':geometry}]}


@async_test
async def test_game_geometry_uses_only_universal_tools(tmp_path):
    grid=paths('tic_tac_toe','board-layout')
    client=MockClient(read(),response(('grid','draw_strokes',grid)),read('r2'),response(('x','draw_strokes',mark(4,'X'))),
                      read('r3'),response(('o','draw_strokes',mark(0,'O'))),response(text='Your turn.'))
    async with setup(tmp_path,client) as (agent,ex,sid):
        await send(agent,sid,"Let's play. I'll take the center.")
        scene=await ex.get_scene_state()
        assert len(scene.objects)==3
        assert {o.object_id for o in scene.objects}=={'board-layout',mark(4,'X')['object_id'],mark(0,'O')['object_id']}
        assert set(call['name'] for req in client.requests for call in req['tools'])==set(t['name'] for t in tool_definitions())
        assert 'games' not in summary(scene)


@async_test
async def test_semantic_ids_do_not_activate_hidden_game_rules(tmp_path):
    # The model may use any ID for any valid geometry, without a prerequisite grid,
    # reserved turn order or backend interpretation of X/O naming conventions.
    client=MockClient(read(),response(('free','draw_strokes',mark(4,'O'))),response(text='Done.'))
    async with setup(tmp_path,client) as (agent,ex,sid):
        await send(agent,sid,'Draw a circle with my chosen identifier.')
        assert len((await ex.get_scene_state()).objects)==1


@async_test
async def test_manual_ink_is_available_as_geometry_without_classification(tmp_path):
    from backend.scene_summary import recent_user_ink
    async with setup(tmp_path,MockClient()) as (agent,ex,sid):
        for i,points in enumerate([[(.45,.45),(.55,.55)],[(.55,.45),(.45,.55)]]):
            await ex.record_manual(DrawCommand(object_id=f'user-{i}',paths=[{'stroke_id':f'user-stroke-{i}','geometry':{'type':'polyline','points':[{'x':x,'y':y} for x,y in points]}}]))
        observed=recent_user_ink(await ex.get_scene_state())
        assert len(observed['objects'])==2
        assert observed['objects'][0]['strokes'][0]['executed_points']==[{'x':.45,'y':.45},{'x':.55,'y':.55}]
        assert 'mark' not in observed and 'cells' not in observed


def test_all_tool_schemas_are_strict_and_geometry_bounded():
    def check(node):
        if isinstance(node,list):
            for item in node: check(item)
        elif isinstance(node,dict):
            assert 'oneOf' not in node and 'discriminator' not in node
            if node.get('type')=='object':
                assert node['additionalProperties'] is False
                assert set(node['required'])==set(node.get('properties',{}))
            for item in node.values(): check(item)
    for tool in tool_definitions():
        assert tool['strict'] is True
        check(tool['parameters'])
    draw=next(t for t in tool_definitions() if t['name']=='draw_strokes')
    assert draw['parameters']['properties']['paths']['maxItems']==32


@async_test
async def test_summary_is_compact_and_detailed_geometry_is_paged(tmp_path):
    client=MockClient(read('detail','circle',True),response(text='Inspected.'))
    async with setup(tmp_path,client) as (agent,ex,sid):
        await ex.execute(DrawCommand(**paths('circle')))
        await ex.advance(100)
        scene=await ex.get_scene_state()
        assert len(json.dumps(summary(scene)))<len(scene.model_dump_json())/3
        await send(agent,sid,'Inspect circle.')
        output=next(i for i in client.requests[-1]['input'] if i.get('type')=='function_call_output')
        assert len(json.loads(output['output'])['object']['strokes'][0]['executed_points'])>500


@async_test
async def test_valid_turn_can_cancel_then_draw_with_new_generation(tmp_path):
    client=MockClient(read(),response(('cancel','cancel_drawing',{})),read('new-generation'),
                      response(('new','draw_strokes',paths('circle'))),response(text='New drawing queued.'))
    async with setup(tmp_path,client) as (agent,ex,sid):
        await ex.execute(DrawCommand(**paths('house')))
        await ex.advance(.4)
        ink=(await ex.get_scene_state()).objects[0].strokes[0].executed_points
        session=await send(agent,sid,'Cancel the house and draw a circle.')
        scene=await ex.get_scene_state()
        assert session.status=='idle'
        assert scene.objects[0].strokes[0].executed_points==ink
        assert scene.objects[0].strokes[0].status=='cancelled'
        assert scene.objects[1].object_id=='circle'
        assert scene.objects[1].generation==scene.generation


@async_test
async def test_direct_generation_change_finishes_stale_turn(tmp_path):
    waiting=asyncio.Event(); release=asyncio.Event()
    async def delayed():
        waiting.set()
        await release.wait()
        return read()
    client=MockClient(delayed)
    async with setup(tmp_path,client) as (agent,ex,sid):
        await agent.submit(sid,ChatMessage(text='Draw a circle.',request_id='one'))
        await waiting.wait()
        await ex.cancel()
        release.set()
        await asyncio.gather(*list(agent.tasks))
        assert (await agent.store.get(sid)).status=='interrupted'
        assert agent.active is None
        assert not (await ex.get_scene_state()).objects


@async_test
async def test_uncertain_receipt_never_replays_mutation(tmp_path):
    import hashlib
    async with setup(tmp_path,MockClient()) as (agent,ex,sid):
        scene=await ex.get_scene_state()
        turn=Turn('turn-test',sid,scene.generation,scene_read=True)
        agent.active=turn
        arguments=json.dumps(paths('circle'))
        signature=hashlib.sha256(('draw_strokes\n'+arguments).encode()).hexdigest()
        await agent.store.reserve(turn.id,'uncertain',signature)
        result=await agent.execute_tool(turn,'uncertain','draw_strokes',arguments)
        assert not result['ok'] and 'uncertain' in result['error']
        assert not (await ex.get_scene_state()).objects


@async_test
async def test_bounds_and_modify_validation_are_atomic(tmp_path):
    from pydantic import ValidationError
    from backend.agent_models import ToolPaths
    cmd=paths('circle')
    with pytest.raises(ValidationError): ToolPaths(**dict(cmd,paths=cmd['paths']*33))
    async with setup(tmp_path,MockClient()) as (agent,ex,sid):
        await ex.execute(DrawCommand(**paths('house')))
        await ex.advance(.4)
        before=await ex.get_scene_state()
        with pytest.raises(CommandConflict):
            await ex.modify_object(ModifyCommand(**paths('house')))
        assert await ex.get_scene_state()==before
        await ex.interrupt()
        with pytest.raises(CommandConflict):
            await ex.resume(expected_generation=before.generation)
        assert (await ex.get_scene_state()).status=='paused'


@async_test
async def test_unrecognized_manual_ink_does_not_block_model_geometry(tmp_path):
    async with setup(tmp_path,MockClient(read(),response(('draw','draw_strokes',mark(4,'X'))),response(text='Added.'))) as (agent,ex,sid):
        await ex.record_manual(DrawCommand(object_id='user-cross',paths=[{'stroke_id':'cross','geometry':{'type':'line','start':{'x':.2,'y':.2},'end':{'x':.8,'y':.8}}}]))
        await send(agent,sid,'Respond to my sketch.')
        assert len((await ex.get_scene_state()).objects)==2
