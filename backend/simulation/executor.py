"""MuJoCo feedback executor sharing queue/state admission with the virtual executor."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
from math import hypot
from ..executor import SceneDrawingExecutor
from ..models import Point

log=logging.getLogger(__name__)


class MuJoCoDrawingExecutor(SceneDrawingExecutor):
    sensor_only = True
    def __init__(self,state,events,tick_seconds=.02):
        super().__init__(state,events,tick_seconds)
        self.state.scene.execution_observation="encoder_estimate_unverified"
        if self.state.scene.drawing_feedback:
            self.state.scene.drawing_feedback.enabled=False
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
            self.robot.world_ink=self.state.simulation_ink
            self.telemetry=await self._call(self.robot.telemetry)

    async def start(self):
        await self.initialize()
        await super().start()

    async def advance(self,seconds):
        if seconds<=0: return
        await self.initialize()
        async with self.lock:
            await self._advance_motion_locked(seconds)

    async def _advance_motion_locked(self, seconds, commit=True):
        scene=self.state.scene
        if scene.status!='drawing': return  # Freeze dynamics and ink on pause/cancel.
        stroke=next((s for s in self.state.strokes() if s.status in ('pending','executing')),None)
        if stroke is None:
            scene.status='idle'
            if commit: await self._commit(snapshot=False)
            return
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
            # Fail closed. Preserve trace estimates and world ink; revoke old plans.
            scene.generation+=1;scene.status='paused'
            self.telemetry['fault']=f'{type(exc).__name__}: {exc}'
            log.exception('MuJoCo execution paused after simulator fault')
        if commit: await self._commit(snapshot=False)

    async def prepare_observation(self):
        """Park only paused/idle motion. Each short batch remains interruptible."""
        await self.initialize()
        generation=self.state.scene.generation
        for _ in range(1200):
            async with self.lock:
                if self.state.scene.generation!=generation or self.state.scene.status not in ('idle','paused'):
                    return False
                try:
                    done=await self._call(self.robot.observation_batch,.05)
                    self.telemetry=await self._call(self.robot.telemetry)
                    self.frame_cache.clear()
                    if done:
                        await self._commit(snapshot=False)
                        return True
                except Exception:
                    self.state.scene.generation+=1
                    self.state.scene.status='paused'
                    await self._commit(snapshot=False)
                    raise
            await asyncio.sleep(0)
        raise RuntimeError('Observation pose timeout')

    async def simulation_status(self):
        async with self.lock:
            return {**self.telemetry,'execution_status':self.state.scene.status,
                    'revision':self.state.scene.revision,'generation':self.state.scene.generation,
                    'render_error':self.render_error}

    async def _reset_execution(self):
        if self.robot is not None:
            self.telemetry=await self._call(self.robot.reset_execution)
        if not self.state.scene.objects:
            self.state.simulation_ink.clear()
        self.frame_cache.clear()

    def _render_snapshot(self,ink,joints,camera,chalk_loss_m=0.):
        # A separate model/data owns the GL context. Rendering never holds the
        # physics worker or scene lock, so a slow GPU cannot delay Stop.
        if self.render_robot is None:
            from .robot import RobotSimulation
            self.render_robot=RobotSimulation()
        self.render_robot.configure_chalk(chalk_loss_m)
        self.render_robot.data.qpos[:]=joints
        self.render_robot.mj.mj_forward(self.render_robot.model,self.render_robot.data)
        return self.render_robot.render(ink,camera)

    async def frame(self,camera='overview',fresh=False):
        await self.initialize()
        async with self.render_lock:
            now=asyncio.get_running_loop().time()
            cached=self.frame_cache.get(camera)
            if not fresh and cached and now-cached[0]<.2 and cached[1]==self.state.scene.generation: return cached[2]
            async with self.lock:
                generation=self.state.scene.generation
                joints=list(self.telemetry['joint_positions'])
                chalk_loss_m=self.robot.chalk_loss_m  # Private environment geometry for rendering only.
                ink=[(False,list(points)) for points in self.state.simulation_ink.values()]
                # Mouse input is an environment injection, never an agent sensor.
                ink.extend((True,[(p.x,p.y) for p in s.executed_points])
                     for obj in self.state.scene.objects if obj.source=='user' for s in obj.strokes if s.executed_points)
            try:
                frame=await asyncio.get_running_loop().run_in_executor(self.render_pool,self._render_snapshot,ink,joints,camera,chalk_loss_m)
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
