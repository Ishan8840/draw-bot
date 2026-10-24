"""Actual MuJoCo dynamics; these tests never fake physics or measured pen feedback."""
import asyncio
from contextlib import asynccontextmanager
import json
import math
import os
from pathlib import Path
import pytest
os.environ.setdefault('MUJOCO_GL','egl')
pytest.importorskip('mujoco')
from backend.simulation.robot import RobotSimulation, to_board, to_world, PEN_UP, PEN_DOWN
from backend.simulation.executor import MuJoCoDrawingExecutor
from backend.geometry import flatten
from backend.models import DrawCommand, Point
from backend.executor import CommandConflict
from backend.scene_state import SceneStateManager
from backend.websocket_manager import WebSocketManager
from test_agent import async_test, MockClient, read, response

ROOT=Path(__file__).resolve().parents[1]


def example(name): return DrawCommand.model_validate_json((ROOT/f'examples/{name}.json').read_text())


def test_workspace_mapping_and_robot_forward_kinematics():
    robot=RobotSimulation()
    try:
        for x,y in [(0,0),(0,1),(1,0),(1,1),(.5,.5)]:
            target=to_world(x,y)
            assert to_board(target)==pytest.approx((x,y))
            robot.data.qpos[:]=robot.ik.solve(target)
            robot.mj.mj_forward(robot.model,robot.data)
            assert robot.tip==pytest.approx(target,abs=1.5e-5)
    finally: robot.close()


@pytest.mark.parametrize('name',['circle','square','bezier'])
def test_real_physics_traces_geometry_and_lifts_pen(name):
    robot=RobotSimulation(); samples=[]
    try:
        command=example(name)
        for stroke in command.paths:
            trajectory=flatten(stroke.geometry)
            for _ in range(2500):
                result=robot.batch(stroke.stroke_id,trajectory,.25,.05)
                assert not result['fault']
                assert all(abs(robot.data.actuator_force)<=robot.model.actuator_forcerange[:,1]+1e-8)
                assert not any(robot.data.qfrc_applied)
                samples.extend(result['samples'])
                if result['done']: break
            else: pytest.fail('Physics did not complete stroke')
        assert len(samples)>20
        assert not robot.in_contact() and robot.tip[2]>PEN_UP-.001
        assert all(0<=x<=1 and 0<=y<=1 for x,y in samples)
        if name=='circle':
            g=command.paths[0].geometry
            errors=[abs(math.hypot(x-g.center.x,y-g.center.y)-g.radius) for x,y in samples]
            assert max(errors)<.005  # <0.9 mm radius error in the .18 m workspace.
            assert math.dist(samples[0],samples[-1])<.002
    finally: robot.close()


@asynccontextmanager
async def executor(tmp_path):
    ex=MuJoCoDrawingExecutor(SceneStateManager(str(tmp_path/'physics.sqlite3')),WebSocketManager())
    await ex.initialize()
    try: yield ex
    finally: await ex.close()


async def until(ex,predicate,steps=1600):
    for _ in range(steps):
        await ex.advance(.05)
        scene=await ex.get_scene_state()
        if predicate(scene): return scene
    raise AssertionError(f'Physics did not reach expected state: {ex.telemetry}')


@async_test
async def test_partial_physics_ink_pause_cancel_new_command_clear_and_restart(tmp_path):
    async with executor(tmp_path) as ex:
        await ex.execute(example('house'))
        partial=await until(ex,lambda s: bool(s.objects[0].strokes[0].executed_points) and s.objects[0].strokes[0].progress>.1)
        assert partial.objects[0].strokes[0].progress<1
        paused=await ex.pause()
        telemetry=await ex.simulation_status()
        await ex.advance(.05)
        assert await ex.get_scene_state()==paused
        assert (await ex.simulation_status())['simulation_time']==telemetry['simulation_time']
        await ex.resume()
        resumed=await until(ex,lambda s:s.objects[0].strokes[0].progress>paused.objects[0].strokes[0].progress)
        assert resumed.objects[0].strokes[0].executed_points[:len(paused.objects[0].strokes[0].executed_points)]==paused.objects[0].strokes[0].executed_points
        cancelled=await ex.cancel()
        await ex.execute(example('o'))
        completed=await until(ex,lambda s:s.status=='idle')
        assert completed.objects[0]==cancelled.objects[0]
        assert completed.objects[1].strokes[0].status=='completed'
        assert completed.objects[1].strokes[0].executed_length>0
        await ex.execute(example('x'))
        await ex.advance(.05)
        cleared=await ex.clear_board()
        with pytest.raises(CommandConflict): await ex.resume(expected_generation=cancelled.generation)
        await ex.advance(.05)
        assert await ex.get_scene_state()==cleared
        assert not cleared.objects
    restored=SceneStateManager(str(tmp_path/'physics.sqlite3'))
    try: assert restored.scene.objects==[]
    finally: await restored.close()


@async_test
async def test_manual_observation_and_model_tools_share_physics_scene(tmp_path):
    from backend.astra_agent import AstraAgent
    from backend.conversation_store import ConversationStore
    from backend.drawing_engine import DrawingEngine
    async with executor(tmp_path) as ex:
        manual=example('x')
        await ex.record_manual(manual)
        client=MockClient(read(),response(('draw','draw_strokes',example('o').model_dump(include={'object_id','paths'},mode='json'))),read('r2'),response(('resume','resume_drawing',{})),response(text='Added.'))
        agent=AstraAgent(DrawingEngine(ex),ConversationStore(':memory:'),client=client)
        sid=(await agent.store.create()).session_id
        try:
            await agent.handle_user_instruction(sid,'Respond to my sketch.','text','sim-turn')
            while agent.tasks: await asyncio.sleep(.01)
            scene=await ex.get_scene_state()
            assert len(scene.objects)==2 and scene.objects[0].source=='user'
            assert client.requests[0]['input'][-1]['content'][1]['type']=='input_image'
            assert scene.objects[1].strokes[0].executed_points==[]
            drawn=await until(ex,lambda s:bool(s.objects[1].strokes[0].executed_points))
            assert drawn.objects[0]==scene.objects[0]
        finally: await agent.close()


@async_test
async def test_real_headless_renderer_and_authoritative_manual_ink(tmp_path):
    async with executor(tmp_path) as ex:
        await ex.record_manual(example('house'))
        image=await ex.frame('overview')
        assert image[:2]==b'\xff\xd8' and len(image)>5000
        top=await ex.frame('top')
        assert top!=image
        assert len((await ex.get_scene_state()).objects)==1
        Path('/tmp/astra-mujoco-render-test.jpg').write_bytes(image)


@async_test
async def test_clearing_resets_physics_cursor_even_when_ids_are_reused(tmp_path):
    async with executor(tmp_path) as ex:
        await ex.execute(example('circle'))
        await until(ex,lambda s:bool(s.objects[0].strokes[0].executed_points))
        await ex.clear_board()
        await ex.execute(example('circle'))
        assert ex.robot.stroke_id is None
        scene=await until(ex,lambda s:bool(s.objects[0].strokes[0].executed_points))
        assert scene.objects[0].strokes[0].progress<.1


def test_simulator_http_frames_scene_reconnect_and_controls(tmp_path):
    from fastapi.testclient import TestClient
    from backend.main import create_app
    with TestClient(create_app(str(tmp_path/'http.sqlite3'),executor_factory=MuJoCoDrawingExecutor)) as client:
        status=client.get('/simulation/status').json()
        assert status['enabled'] and status['backend']=='mujoco'
        assert client.get('/simulation/frame?camera=invalid').status_code==422
        result=client.get('/simulation/frame?camera=top')
        assert result.status_code==200 and result.headers['content-type']=='image/jpeg'
        assert client.post('/commands/manual',json=example('x').model_dump(mode='json')).status_code==200
        client.post('/commands/draw',json=example('house').model_dump(mode='json'))
        paused=client.post('/commands/pause').json()
        with client.websocket_connect('/ws') as ws:
            assert ws.receive_json()['scene']==paused
        cancelled=client.post('/commands/cancel').json()
        assert cancelled['objects'][0]==paused['objects'][0]
        assert client.get('/static/simulation.js').status_code==200


@async_test
async def test_slow_render_cannot_block_cancellation(tmp_path):
    import threading
    async with executor(tmp_path) as ex:
        await ex.execute(example('circle'))
        entered=threading.Event();release=threading.Event()
        original=ex._render_snapshot
        def delayed(*args):
            entered.set()
            assert release.wait(5)
            return original(*args)
        ex._render_snapshot=delayed
        rendering=asyncio.create_task(ex.frame())
        try:
            assert await asyncio.to_thread(entered.wait,2)
            scene=await asyncio.wait_for(ex.cancel(),1)
            assert scene.status=='cancelled' and not release.is_set()
        finally:
            release.set()
            await rendering


@async_test
async def test_simulator_fault_commits_measured_ink_and_revokes_old_generation(tmp_path):
    async with executor(tmp_path) as ex:
        await ex.execute(example('circle'))
        before=await until(ex,lambda s:s.objects[0].strokes[0].progress>.05)
        original=ex.robot.batch
        def fault_after_feedback(*args):
            result=original(*args)
            result['fault']='Injected test fault after real physics feedback'
            return result
        ex.robot.batch=fault_after_feedback
        await ex.advance(.05)
        after=await ex.get_scene_state()
        assert after.status=='paused' and after.generation>before.generation
        assert after.objects[0].strokes[0].executed_points[:len(before.objects[0].strokes[0].executed_points)]==before.objects[0].strokes[0].executed_points
        assert len(after.objects[0].strokes[0].executed_points)>len(before.objects[0].strokes[0].executed_points)
        with pytest.raises(CommandConflict): await ex.resume(expected_generation=before.generation)
        await ex.cancel()
        assert (await ex.simulation_status())['fault'] is None


def test_openarm_official_joint_model_and_workspace_orientation():
    import numpy as np
    from backend.simulation.openarm_ik import PEN_ROTATION, HOME_SEED
    robot=RobotSimulation()
    try:
        assert robot.model.nq==robot.model.nv==robot.model.nu==7
        assert [robot.model.joint(i).name for i in range(7)]==[f'openarm_right_joint{i}' for i in range(1,8)]
        assert all(robot.model.jnt_type==robot.mj.mjtJoint.mjJNT_HINGE)
        assert robot.model.actuator_forcerange[:,1]==pytest.approx([40,40,27,27,7,7,7])
        for z in (PEN_UP,PEN_DOWN):
            for x in np.linspace(0,1,5):
                for y in np.linspace(0,1,5):
                    target=to_world(x,y,z)
                    robot.data.qpos[:]=robot.ik.solve(target,seed=HOME_SEED)
                    robot.mj.mj_forward(robot.model,robot.data)
                    assert robot.tip==pytest.approx(target,abs=1.5e-5)
                    rotation=robot.data.site_xmat[robot.tip_id].reshape(3,3)
                    assert rotation==pytest.approx(PEN_ROTATION,abs=1e-4)
                    assert np.all(robot.data.qpos>=robot.model.jnt_range[:,0])
                    assert np.all(robot.data.qpos<=robot.model.jnt_range[:,1])
        with pytest.raises(ValueError): robot.ik.solve([4,4,4])
    finally: robot.close()


def test_openarm_can_draw_all_four_board_edges():
    from backend.models import Line
    robot=RobotSimulation()
    try:
        corners=[(0.,0.),(1.,0.),(1.,1.),(0.,1.),(0.,0.)]
        for index,(a,b) in enumerate(zip(corners,corners[1:])):
            trajectory=flatten(Line(type='line',start={'x':a[0],'y':a[1]},end={'x':b[0],'y':b[1]}))
            samples=[]
            for _ in range(600):
                result=robot.batch(f'edge-{index}',trajectory,.25,.05)
                assert result['fault'] is None
                samples.extend(result['samples'])
                if result['done']: break
            assert result['done'] and samples
            assert all(0<=x<=1 and 0<=y<=1 for x,y in samples)
    finally: robot.close()
