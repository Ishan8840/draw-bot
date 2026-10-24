import asyncio
import json
from math import hypot, pi
from pathlib import Path
import pytest
from backend.drawing_engine import DrawingEngine
from backend.executor import CommandConflict, VirtualDrawingExecutor
from backend.geometry import flatten
from backend.models import DrawCommand, Point
from backend.scene_state import SceneStateManager
from backend.websocket_manager import WebSocketManager

ROOT = Path(__file__).resolve().parents[1]


def command(name):
    return DrawCommand.model_validate_json((ROOT / f'examples/{name}.json').read_text())


def run_test(function):
    def wrapped(tmp_path):
        async def run():
            state = SceneStateManager(str(tmp_path / 'scene.sqlite3'))
            executor = VirtualDrawingExecutor(state, WebSocketManager())
            try:
                await function(executor)
            finally:
                await executor.close()
        asyncio.run(run())
    return wrapped


@run_test
async def test_circle_geometry_and_execution(ex):
    await ex.execute(command('circle'))
    await ex.advance(.5)
    partial = (await ex.get_scene_state()).objects[0].strokes[0]
    assert 0 < partial.progress < 1
    assert len(partial.executed_points) > 2
    await ex.advance(100)
    stroke = (await ex.get_scene_state()).objects[0].strokes[0]
    assert stroke.status == 'completed'
    assert stroke.executed_points[0] == stroke.executed_points[-1]
    assert stroke.total_length == pytest.approx(2*pi*.3, rel=1e-5)
    for point in stroke.executed_points:
        assert hypot(point.x-.5, point.y-.5) == pytest.approx(.3)


@run_test
async def test_grid_and_circle_share_universal_engine(ex):
    engine = DrawingEngine(ex)
    for name in ('circle', 'tic_tac_toe'):
        cmd = command(name)
        await engine.draw_strokes(cmd.object_id, cmd.paths)
    await ex.advance(100)
    scene = await engine.get_scene_state()
    assert len(scene.objects) == 2
    assert len(scene.completed_stroke_ids) == 5
    assert scene.objects[1].strokes[0].executed_points == [Point(x=.38,y=.15), Point(x=.38,y=.85)]


@run_test
async def test_pause_resume_exact_position(ex):
    await ex.execute(command('house'))
    await ex.advance(.8)
    before = await ex.pause()
    await ex.advance(100)
    paused = await ex.get_scene_state()
    assert paused == before
    await ex.execute(command('circle'))
    assert (await ex.get_scene_state()).status == 'paused'
    await ex.resume()
    await ex.advance(.4)
    after = await ex.get_scene_state()
    first = before.objects[0].strokes[0]
    last = after.objects[0].strokes[0]
    assert last.executed_length == pytest.approx(first.executed_length + .1)
    assert last.executed_points[0] == first.executed_points[0]


@run_test
async def test_cancel_preserves_partial_house_and_new_command(ex):
    await ex.execute(command('house'))
    await ex.execute(command('circle'))
    await ex.advance(1)
    before = await ex.get_scene_state()
    cancelled = await ex.cancel()
    assert cancelled.generation == before.generation+1
    assert cancelled.objects[0].strokes[0].executed_points == before.objects[0].strokes[0].executed_points
    assert 0 < cancelled.objects[0].strokes[0].progress < 1
    assert all(s.status == 'cancelled' for o in cancelled.objects for s in o.strokes)
    assert all(not s.executed_points for s in cancelled.objects[0].strokes[1:])
    await ex.resume()
    await ex.advance(100)
    assert await ex.get_scene_state() == cancelled
    await ex.execute(command('x'))
    await ex.advance(100)
    after = await ex.get_scene_state()
    assert after.objects[:2] == cancelled.objects
    assert all(s.status == 'completed' for s in after.objects[2].strokes)


@run_test
async def test_clear_invalidates_queue_and_stale_commands(ex):
    await ex.execute(command('house'))
    await ex.advance(.4)
    cleared = await ex.clear_board()
    await ex.resume()
    await ex.advance(100)
    assert await ex.get_scene_state() == cleared
    assert not cleared.objects and cleared.current_stroke_id is None
    assert not cleared.pending_stroke_ids and not cleared.completed_stroke_ids
    stale = command('circle').model_copy(update={'expected_generation':0})
    with pytest.raises(CommandConflict):
        await ex.execute(stale)
    await ex.execute(command('house'))  # IDs can be reused only after clear.
    await ex.advance(100)
    assert (await ex.get_scene_state()).status == 'idle'


@run_test
async def test_scene_progress_and_snapshot_isolation(ex):
    await ex.execute(command('x'))
    await ex.advance(.8)
    scene = await ex.get_scene_state()
    assert scene.current_stroke_id == 'x-stroke-1'
    assert scene.pending_stroke_ids == ['x-stroke-2']
    stroke = scene.objects[0].strokes[0]
    assert stroke.executed_length == pytest.approx(.2)
    assert stroke.progress == pytest.approx(.2 / stroke.total_length)
    assert sum(hypot(b.x-a.x,b.y-a.y) for a,b in zip(stroke.executed_points,stroke.executed_points[1:])) == pytest.approx(.2)
    scene.objects.clear()
    assert len((await ex.get_scene_state()).objects) == 1


@run_test
async def test_duplicate_command_is_atomic(ex):
    await ex.execute(command('circle'))
    before = await ex.get_scene_state()
    with pytest.raises(CommandConflict):
        await ex.execute(command('circle'))
    conflict = command('circle').model_copy(update={'object_id':'other'})
    with pytest.raises(CommandConflict):
        await ex.execute(conflict)
    assert await ex.get_scene_state() == before


@run_test
async def test_manual_strokes_are_observed_ink(ex):
    cmd = DrawCommand(object_id='user-mark', paths=[{'stroke_id':'user-stroke','geometry':{'type':'polyline','points':[{'x':.1,'y':.2},{'x':.2,'y':.4},{'x':.4,'y':.3}]}}])
    await ex.execute(command('circle'))
    await ex.pause()
    scene = await ex.record_manual(cmd)
    obj = scene.objects[1]
    assert obj.source == 'user'
    assert obj.strokes[0].executed_points == cmd.paths[0].geometry.points
    assert obj.strokes[0].status == 'completed'
    assert obj.strokes[0].progress == 1
    assert scene.status == 'paused'
    assert scene.pending_stroke_ids == ['circle-stroke-1']


@run_test
async def test_concurrent_cancel_and_ticks_never_resurrect(ex):
    await ex.execute(command('house'))
    await asyncio.gather(ex.advance(.2), ex.pause(), ex.resume(), ex.cancel(), ex.advance(100))
    snapshot = await ex.get_scene_state()
    await asyncio.gather(ex.resume(), ex.advance(100), ex.pause())
    assert await ex.get_scene_state() == snapshot
    assert snapshot.status == 'cancelled'


@run_test
async def test_speed_controls_distance(ex):
    await ex.execute(command('circle'))
    await ex.set_speed(.1)
    await ex.advance(1)
    assert (await ex.get_scene_state()).objects[0].strokes[0].executed_length == pytest.approx(.1)
    await ex.set_speed(.5)
    await ex.advance(1)
    assert (await ex.get_scene_state()).objects[0].strokes[0].executed_length == pytest.approx(.6)


@run_test
async def test_slow_websocket_client_only_retains_latest_scene(ex):
    queue = ex.events.subscribe()
    await ex.execute(command('circle'))
    for _ in range(10):
        await ex.advance(.1)
    assert queue.qsize() == 1
    assert queue.get_nowait()['scene'] == (await ex.get_scene_state()).model_dump(mode='json')
    ex.events.unsubscribe(queue)


def test_restart_recovers_durable_partial_scene(tmp_path):
    path = str(tmp_path / 'persistent.sqlite3')
    async def run():
        ex = VirtualDrawingExecutor(SceneStateManager(path), WebSocketManager())
        await ex.execute(command('house'))
        await ex.advance(.6)
        before = await ex.get_scene_state()
        # Simulate process death: close storage without orderly cancellation.
        await ex.state.close()
        recovered = VirtualDrawingExecutor(SceneStateManager(path), WebSocketManager())
        after = await recovered.get_scene_state()
        assert after.generation == before.generation + 1
        assert after.status == 'cancelled'
        assert after.objects[0].strokes[0].executed_points == before.objects[0].strokes[0].executed_points
        await recovered.advance(100)
        assert await recovered.get_scene_state() == after
        await recovered.close()
    asyncio.run(run())


def test_bezier_endpoints_and_polyline_prefix():
    trajectory = flatten(command('bezier').paths[0].geometry)
    assert trajectory.points[0] == Point(x=.1,y=.5)
    assert trajectory.points[-1] == Point(x=.9,y=.5)
    assert all(0 <= p.x <= 1 and 0 <= p.y <= 1 for p in trajectory.points)
    assert trajectory.prefix(trajectory.length) == trajectory.points
    assert trajectory.prefix(0) == []


@run_test
async def test_zero_length_stroke_does_not_stall(ex):
    cmd = DrawCommand(object_id='dot', paths=[{'stroke_id':'dot-stroke','geometry':{'type':'line','start':{'x':.5,'y':.5},'end':{'x':.5,'y':.5}}}])
    await ex.execute(cmd)
    await ex.advance(.02)
    scene = await ex.get_scene_state()
    assert scene.status == 'idle'
    assert scene.objects[0].strokes[0].progress == 1
