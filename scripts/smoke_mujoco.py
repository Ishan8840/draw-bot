"""Real MuJoCo acceptance demo, no API key: python -m scripts.smoke_mujoco --gif.

Uses an isolated temporary board, accelerated simulation, and real measured ink.
"""
import argparse
import asyncio
import io
import json
from pathlib import Path
import tempfile
import time
from backend.models import DrawCommand
from backend.scene_state import SceneStateManager
from backend.websocket_manager import WebSocketManager
from backend.simulation.executor import MuJoCoDrawingExecutor

ROOT=Path(__file__).resolve().parents[1]


async def demo(args):
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='astra-mujoco-') as temporary:
        ex=MuJoCoDrawingExecutor(SceneStateManager(str(Path(temporary)/'scene.sqlite3')),WebSocketManager())
        frames=[];steps=0;maximum_error=0.;started=time.perf_counter()
        await ex.initialize()
        async def frame():
            from PIL import Image
            data=await ex.frame('overview')
            frames.append(Image.open(io.BytesIO(data)).resize((640,480)).copy())
        async def advance_until(predicate,limit=2500):
            nonlocal steps,maximum_error
            for _ in range(limit):
                await ex.advance(.05);steps+=1
                if ex.telemetry.get('fault'): raise RuntimeError(ex.telemetry['fault'])
                if ex.telemetry['phase']=='draw': maximum_error=max(maximum_error,ex.telemetry['tracking_error_m'])
                if args.gif and steps%20==0: await frame()
                if predicate(ex.state.scene): return
            raise RuntimeError('Demo exceeded simulation step limit')
        try:
            house=DrawCommand.model_validate_json((ROOT/'examples/house.json').read_text())
            await ex.execute(house)
            await advance_until(lambda s:s.objects[0].strokes[0].progress>.4)
            started_pause=time.perf_counter();paused=await ex.pause();pause_ms=(time.perf_counter()-started_pause)*1000
            clock=ex.telemetry['simulation_time']
            for _ in range(10): await ex.advance(.05)
            assert await ex.get_scene_state()==paused and ex.telemetry['simulation_time']==clock
            await frame()
            cancelled=await ex.cancel()
            circle=DrawCommand.model_validate_json((ROOT/'examples/circle.json').read_text())
            await ex.execute(circle)
            await advance_until(lambda s:s.status=='idle')
            final=await ex.get_scene_state()
            assert final.objects[0]==cancelled.objects[0]
            assert final.objects[1].strokes[0].status=='completed'
            (output/'scene.json').write_text(final.model_dump_json(indent=2))
            (output/'overview.jpg').write_bytes(await ex.frame('overview'))
            (output/'board-top.jpg').write_bytes(await ex.frame('top'))
            if args.gif:
                await frame()
                frames[0].save(output/'interruption-demo.gif',save_all=True,append_images=frames[1:],duration=120,loop=0)
            metrics={'physics':'actual MuJoCo OpenArm v2 dynamics; no mocked robot or model call',
                     'wall_seconds':time.perf_counter()-started,'simulation_seconds':ex.telemetry['simulation_time'],
                     'pause_ack_ms':pause_ms,'maximum_tracking_error_during_draw_m':maximum_error,
                     'house_partial_ink_preserved':True,'new_circle_completed':True,
                     'executed_points':sum(len(s.executed_points) for o in final.objects for s in o.strokes),
                     'simulation_pause':'clock frozen; not a real robot braking measurement',
                     'playback':'GIF is accelerated and sampled; not real-time performance'}
            (output/'metrics.json').write_text(json.dumps(metrics,indent=2))
            print(json.dumps(metrics,indent=2));print(f'Artifacts: {output.resolve()}')
        finally: await ex.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='artifacts/mujoco')
    parser.add_argument('--gif',action='store_true')
    parser.add_argument('--sandbox-heartbeat',action='store_true',help='Only for restricted test sandboxes that block asyncio wakeup sockets')
    args=parser.parse_args()
    if args.sandbox_heartbeat:
        from scripts.test_sandbox import PollingPolicy
        asyncio.set_event_loop_policy(PollingPolicy())
    asyncio.run(demo(args))
