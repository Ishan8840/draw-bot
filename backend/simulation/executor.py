"""MuJoCo feedback executor sharing queue/state admission with the virtual executor."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
from math import hypot
from ..executor import SceneDrawingExecutor
from ..models import Point

log=logging.getLogger(__name__)


class MuJoCoDrawingExecutor(SceneDrawingExecutor):
    def __init__(self,state,events,tick_seconds=.02):
        super().__init__(state,events,tick_seconds)
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='mujoco')
        self.render_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='mujoco-view')
        self.render_lock=asyncio.Lock()
        self.render_robot=None
        self.robot=None
        self.telemetry={'backend':'mujoco','phase':'initializing'}
        self.frame_cache={}
        self.render_error=None

    async def _call(self,fn,*args):
        return await asyncio.get_running_loop().run_in_executor(self.pool,fn,*args)

    async def initialize(self):
        if self.robot is None:
            from .robot import RobotSimulation
            self.robot=await self._call(RobotSimulation)
            self.telemetry=await self._call(self.robot.telemetry)

    async def start(self):
        await self.initialize()
        await super().start()

    async def advance(self,seconds):
        if seconds<=0: return
        await self.initialize()
        async with self.lock:
            scene=self.state.scene
            if scene.status!='drawing': return  # Freeze dynamics and ink on pause/cancel.
            stroke=next((s for s in self.state.strokes() if s.status in ('pending','executing')),None)
            if stroke is None:
                scene.status='idle';await self._commit(snapshot=False);return
            stroke.status='executing'
            try:
                # A short, single-owner batch is atomic with pause/cancel/clear. Those
                # operations wait for its feedback to be committed, never for an API.
                result=await self._call(self.robot.batch,stroke.stroke_id,self.trajectories[stroke.stroke_id],scene.speed,min(seconds,.05))
                self.telemetry=result['telemetry']
                for x,y in result['samples']:
                    point=Point(x=x,y=y)
                    if stroke.executed_points:
                        last=stroke.executed_points[-1]
                        stroke.executed_length+=hypot(point.x-last.x,point.y-last.y)
                    stroke.executed_points.append(point)
                if result.get('fault'):
                    raise RuntimeError(result['fault'])
                if result['done']:
                    stroke.status='completed';stroke.progress=1
                    self.trajectories.pop(stroke.stroke_id,None)
                elif stroke.executed_points:
                    stroke.progress=max(stroke.progress,min(.999,result['reached']/stroke.total_length if stroke.total_length else .999))
                if all(s.status not in ('pending','executing') for s in self.state.strokes()): scene.status='idle'
            except Exception as exc:
                # Fail closed. Keep all prior confirmed ink and revoke old plan tokens.
                scene.generation+=1;scene.status='paused'
                self.telemetry['fault']=f'{type(exc).__name__}: {exc}'
                log.exception('MuJoCo execution paused after simulator fault')
            await self._commit(snapshot=False)

    async def simulation_status(self):
        async with self.lock:
            return {**self.telemetry,'execution_status':self.state.scene.status,
                    'revision':self.state.scene.revision,'generation':self.state.scene.generation,
                    'render_error':self.render_error}

    async def _reset_execution(self):
        if self.robot is not None:
            self.telemetry=await self._call(self.robot.reset_execution)
        self.frame_cache.clear()

    def _render_snapshot(self,ink,joints,camera):
        # A separate model/data owns the GL context. Rendering never holds the
        # physics worker or scene lock, so a slow GPU cannot delay Stop.
        if self.render_robot is None:
            from .robot import RobotSimulation
            self.render_robot=RobotSimulation()
        self.render_robot.data.qpos[:]=joints
        self.render_robot.mj.mj_forward(self.render_robot.model,self.render_robot.data)
        return self.render_robot.render(ink,camera)

    async def frame(self,camera='overview'):
        await self.initialize()
        async with self.render_lock:
            now=asyncio.get_running_loop().time()
            cached=self.frame_cache.get(camera)
            if cached and now-cached[0]<.2 and cached[1]==self.state.scene.generation: return cached[2]
            async with self.lock:
                generation=self.state.scene.generation
                joints=list(self.telemetry['joint_positions'])
                ink=[(obj.source=='user',[(p.x,p.y) for p in s.executed_points])
                     for obj in self.state.scene.objects for s in obj.strokes if s.executed_points]
            try:
                frame=await asyncio.get_running_loop().run_in_executor(self.render_pool,self._render_snapshot,ink,joints,camera)
                self.render_error=None
            except Exception as exc:
                self.render_error=f'{type(exc).__name__}: {exc}'
                raise RuntimeError('MuJoCo rendering unavailable; check MUJOCO_GL and graphics libraries') from exc
            self.frame_cache[camera]=(asyncio.get_running_loop().time(),generation,frame)
            return frame

    async def close(self):
        try:
            await super().close()
        finally:
            if self.robot is not None: await self._call(self.robot.close)
            async with self.render_lock:
                if self.render_robot is not None:
                    await asyncio.get_running_loop().run_in_executor(self.render_pool,self.render_robot.close)
            self.render_pool.shutdown(wait=True,cancel_futures=True)
            self.pool.shutdown(wait=True,cancel_futures=True)
