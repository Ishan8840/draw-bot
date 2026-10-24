import asyncio
import json
from contextlib import asynccontextmanager
import pytest
pytest.importorskip('mujoco')
from backend.models import DrawCommand, StrokeState
from backend.scene_state import SceneStateManager
from backend.websocket_manager import WebSocketManager
from backend.simulation.verified_executor import VerifiedMuJoCoExecutor, Check
from backend.astra_agent import AstraAgent, Turn
from backend.drawing_engine import DrawingEngine
from backend.conversation_store import ConversationStore
from test_agent import async_test, MockClient, read, response
from test_mark_verification import frame, PATH


def line():
    return DrawCommand(object_id='line',paths=[{'stroke_id':'line-1','geometry':{
        'type':'line','start':{'x':.25,'y':.5},'end':{'x':.75,'y':.5}}}])


@asynccontextmanager
async def executor(tmp_path):
    ex=VerifiedMuJoCoExecutor(SceneStateManager(str(tmp_path/'live.sqlite3')),WebSocketManager())
    await ex.initialize()
    try: yield ex
    finally: await ex.close()


async def finish(ex):
    for _ in range(4000):
        await ex.advance(.05)
        if ex.state.scene.status in ('idle','paused'): return ex.state.scene.model_copy(deep=True)
    pytest.fail(f'Verification did not finish: {await ex.simulation_status()}')


@async_test
async def test_live_executor_checks_fresh_mark_and_preserves_on_reconnect(tmp_path):
    async with executor(tmp_path) as ex:
        await ex.execute(line())
        scene=await finish(ex)
        assert scene.status=='idle'
        assert scene.objects[0].strokes[0].mark_verification.outcome=='visible'
        assert scene.drawing_feedback.depth_offset_m==0
    restored=SceneStateManager(str(tmp_path/'live.sqlite3'))
    try:
        assert restored.scene.objects[0].strokes[0].mark_verification.outcome=='visible'
    finally: await restored.close()


@async_test
async def test_live_shortened_chalk_retries_through_shared_scene(tmp_path):
    async with executor(tmp_path) as ex:
        await ex._call(ex.robot.configure_chalk,.0006)
        await ex.execute(line())
        scene=await finish(ex)
        assert scene.status=='idle'
        strokes=scene.objects[0].strokes
        assert len(strokes)==3 and len({s.stroke_id for s in strokes})==3
        assert [s.mark_verification.outcome for s in strokes]==['missing','missing','visible']
        assert all(s.retry_of=='line-1' for s in strokes[1:])
        assert all(s.executed_points for s in strokes)
        assert scene.drawing_feedback.depth_offset_m==pytest.approx(.0005)
        await ex.clear_board()
        assert ex.state.scene.drawing_feedback.depth_offset_m==pytest.approx(.0005)
    async with executor(tmp_path) as ex:
        assert ex.robot.depth_offset_m==pytest.approx(.0005)


@async_test
async def test_astra_gets_failed_camera_result_and_read_only_followup(tmp_path):
    async with executor(tmp_path) as ex:
        client=MockClient(read(),response(('draw','draw_strokes',line().model_dump(include={'object_id','paths'},mode='json'))),
                          read('r2'),response(('resume','resume_drawing',{})),response(text='Drawing now.'),
                          read('failure-read'),response(('bad-resume','resume_drawing',{})),
                          response(text='The camera check failed. Drawing is paused.'))
        agent=AstraAgent(DrawingEngine(ex),ConversationStore(':memory:'),client=client)
        sid=(await agent.store.create()).session_id
        try:
            await agent.handle_user_instruction(sid,'Draw a line.','text','live-feedback')
            while agent.tasks: await asyncio.sleep(.01)
            # Real physics, controlled inconclusive camera detector outcome.
            import backend.simulation.verified_executor as module
            original=module.verify_mark
            module.verify_mark=lambda *args:{'outcome':'weak_or_partial','path_coverage':.5,'new_pixels':30}
            try: scene=await finish(ex)
            finally: module.verify_mark=original
            assert scene.status=='paused' and scene.drawing_feedback.depth_offset_m==0
            while agent.tasks: await asyncio.sleep(.01)
            history=await agent.store.get(sid)
            assert any(e.kind=='observation' and 'weak_or_partial' in e.content for e in history.events)
            assert history.events[-1].content=='The camera check failed. Drawing is paused.'
            assert ex.state.scene.status=='paused'
            assert 'read-only' in json.dumps(client.requests[-1]['input'])
        finally: await agent.close()


async def staged_check(ex):
    await ex.execute(line())
    stroke=ex.state.scene.objects[0].strokes[0]
    stroke.status='completed'
    ex.check=Check(stroke,phase='checking',before=frame())
    entered=asyncio.Event();release=asyncio.Event()
    async def delayed(*args,**kwargs):
        entered.set();await release.wait();return frame()
    ex.frame=delayed
    task=asyncio.create_task(ex.advance(.05))
    await entered.wait()
    return task,release


@async_test
async def test_cancel_discards_inflight_verification_and_retry(tmp_path):
    async with executor(tmp_path) as ex:
        task,release=await staged_check(ex)
        cancelled=await asyncio.wait_for(ex.cancel(),1)
        release.set();await task
        assert ex.state.scene==cancelled
        assert ex.robot.depth_offset_m==0 and len(cancelled.objects[0].strokes)==1


@async_test
async def test_pause_resume_discards_old_camera_result_without_generation_change(tmp_path):
    async with executor(tmp_path) as ex:
        task,release=await staged_check(ex)
        generation=ex.state.scene.generation
        await ex.pause();await ex.resume()
        release.set();await task
        assert ex.state.scene.generation==generation
        assert ex.robot.depth_offset_m==0
        assert ex.state.scene.objects[0].strokes[0].mark_verification is None


@async_test
async def test_limit_and_uncertainty_pause_and_revoke_old_generation(tmp_path):
    async with executor(tmp_path) as ex:
        await ex.execute(line())
        stroke=ex.state.scene.objects[0].strokes[0];stroke.status='completed'
        ex.state.scene.drawing_feedback.depth_offset_m=.00075
        generation=ex.state.scene.generation
        async with ex.lock:
            await ex._finish_locked(Check(stroke),{'outcome':'missing','path_coverage':0,'new_pixels':0})
        assert ex.state.scene.status=='paused' and ex.state.scene.generation>generation
        assert len(ex.state.scene.objects[0].strokes)==1
        assert stroke.mark_verification.decision=='inspection_required'


def test_app_enables_live_camera_feedback_and_reconnects(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from backend.main import create_app
    monkeypatch.setenv('ASTRA_EXECUTOR','mujoco')
    monkeypatch.setenv('SIM_VERIFY_MARKS','true')
    with TestClient(create_app(str(tmp_path/'app.sqlite3'))) as client:
        status=client.get('/simulation/status').json()
        assert status['drawing_feedback']['enabled'] is True
        scene=client.get('/scene').json()
        with client.websocket_connect('/ws') as ws:
            assert ws.receive_json()['scene']==scene


@async_test
async def test_clear_discards_camera_result_and_pending_work(tmp_path):
    async with executor(tmp_path) as ex:
        task,release=await staged_check(ex)
        cleared=await asyncio.wait_for(ex.clear_board(),1)
        release.set();await task
        assert ex.state.scene==cleared and not cleared.objects
        assert ex.robot.depth_offset_m==0


@async_test
async def test_restart_during_post_stroke_check_is_inconclusive(tmp_path):
    async with executor(tmp_path) as ex:
        await ex.execute(line())
        stroke=ex.state.scene.objects[0].strokes[0];stroke.status='completed'
        ex.state.scene.drawing_feedback.phase='checking'
        ex.state.scene.drawing_feedback.stroke_id=stroke.stroke_id
        await ex._commit(snapshot=False)
    async with executor(tmp_path) as ex:
        scene=await ex.get_scene_state()
        assert scene.status=='paused'
        assert scene.objects[0].strokes[0].mark_verification.outcome=='inconclusive'
        assert scene.drawing_feedback.phase=='inspection_required'


@async_test
async def test_each_stroke_is_checked_before_next_stroke_starts(tmp_path):
    async with executor(tmp_path) as ex:
        command=line()
        second=command.paths[0].model_copy(deep=True)
        second.stroke_id='line-2';second.geometry.start.y=.7;second.geometry.end.y=.7
        command.paths.append(second)
        await ex.execute(command)
        captures=[]
        original=ex.frame
        async def capture(*args,**kwargs):
            captures.append([s.status for s in ex.state.strokes()])
            return await original(*args,**kwargs)
        ex.frame=capture
        scene=await finish(ex)
        assert scene.status=='idle'
        assert captures[0]==['pending','pending']
        assert ['completed','pending'] in captures
        assert captures[-1]==['completed','completed']
        assert all(s.mark_verification.outcome=='visible' for s in scene.objects[0].strokes)
        assert ex.robot.depth_offset_m==0


@async_test
async def test_repairs_are_queued_after_pass_with_one_shared_correction(tmp_path):
    async with executor(tmp_path) as ex:
        command=line()
        second=command.paths[0].model_copy(deep=True);second.stroke_id='line-2'
        command.paths.append(second)
        await ex.execute(command)
        strokes=list(ex.state.strokes())
        for stroke in strokes: stroke.status='completed'
        partial={'outcome':'weak_or_partial','path_coverage':.5,'new_pixels':70,
                 'repair_paths':[[(.5,.5),(.75,.5)]]}
        async with ex.lock:
            await ex._finish_batch_locked(Check(strokes[0]),[(strokes[0],partial),(strokes[1],{'outcome':'missing'})])
        scene=await ex.get_scene_state()
        assert scene.status=='drawing' and scene.drawing_feedback.depth_offset_m==pytest.approx(.00025)
        paths=scene.objects[0].strokes
        assert paths[0].stroke_id=='line-1' and paths[2].stroke_id=='line-2'
        assert len(paths)==4 and paths[1].geometry.type=='polyline'
        assert paths[1].geometry.points[0].x==.5
        assert paths[3].retry_of=='line-2'


@async_test
async def test_physical_partial_stroke_is_repaired_after_drawing_finishes(tmp_path):
    async with executor(tmp_path) as ex:
        await ex.execute(line())
        for _ in range(1000):
            await ex.advance(.05)
            if ex.state.scene.objects[0].strokes[0].progress>.5:
                break
        else: pytest.fail('Did not reach middle of stroke')
        # Environment-only loss of contact midway through the intended line.
        await ex._call(ex.robot.configure_chalk,.0006)
        scene=await finish(ex)
        strokes=scene.objects[0].strokes
        assert strokes[0].mark_verification.outcome=='weak_or_partial'
        assert strokes[1].geometry.type=='polyline'
        assert strokes[1].geometry.points[0].x>.45  # Do not retrace the good half.
        assert strokes[-1].mark_verification.outcome=='visible'
        assert scene.status=='idle' and scene.drawing_feedback.depth_offset_m<=.00075


@async_test
async def test_missing_stroke_retry_has_priority_over_remaining_drawing(tmp_path):
    async with executor(tmp_path) as ex:
        command=line()
        second=command.paths[0].model_copy(deep=True)
        second.stroke_id='line-2';second.geometry.start.y=.7;second.geometry.end.y=.7
        command.paths.append(second)
        await ex.execute(command)
        stroke=ex.state.scene.objects[0].strokes[0]
        stroke.status='completed'
        async with ex.lock:
            await ex._finish_locked(Check(stroke),{'outcome':'missing','path_coverage':0,'new_pixels':0})
        pending=[s for s in ex.state.strokes() if s.status=='pending']
        assert pending[0].retry_of=='line-1'
        assert pending[1].stroke_id=='line-2'
        assert ex.state.scene.status=='drawing'


@async_test
async def test_obstructed_lift_view_does_not_lower_chalk_before_clear_view(tmp_path):
    async with executor(tmp_path) as ex:
        await ex.execute(line())
        stroke=ex.state.scene.objects[0].strokes[0];stroke.status='completed'
        ex.check=Check(stroke,phase='checking',before=frame())
        async def obscured(*args,**kwargs): return frame(obstruction=True)
        ex.frame=obscured
        await ex.advance(.05)
        assert ex.check.phase=='parking_after'
        assert ex.robot.depth_offset_m==0
        assert stroke.mark_verification is None


@async_test
async def test_clear_lift_view_accepts_without_parking(tmp_path):
    async with executor(tmp_path) as ex:
        await ex.execute(line())
        stroke=ex.state.scene.objects[0].strokes[0];stroke.status='completed'
        ex.check=Check(stroke,phase='checking',before=frame())
        async def clear_view(*args,**kwargs): return frame(PATH)
        ex.frame=clear_view
        def unexpected_park(*args): raise AssertionError('Unnecessary observation move')
        ex.robot.observation_batch=unexpected_park
        await ex.advance(.05)
        assert stroke.mark_verification.outcome=='visible'
        assert ex.state.scene.status=='idle' and ex.baseline is not None
