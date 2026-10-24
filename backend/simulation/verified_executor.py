"""Universal MuJoCo executor with interruptible, camera-only per-stroke checks."""
import asyncio
import logging
import os
from dataclasses import dataclass, field
from uuid import uuid4
from .executor import MuJoCoDrawingExecutor
from ..geometry import flatten
from ..mark_verification import verify_mark, BoardCameraCalibration
from ..models import DrawingFeedback, MarkVerification, StrokeState

log=logging.getLogger(__name__)


@dataclass
class Check:
    stroke: StrokeState
    phase: str = 'parking_before'
    before: bytes | None = None
    strokes: list[StrokeState] = field(default_factory=list)
    cleared_view: bool = False


class VerifiedMuJoCoExecutor(MuJoCoDrawingExecutor):
    """One state machine for every path; no activity-specific drawing behavior."""
    def __init__(self, state, events, tick_seconds=.02):
        super().__init__(state,events,tick_seconds)
        feedback=state.scene.drawing_feedback
        state.scene.drawing_feedback=DrawingFeedback(
            depth_offset_m=feedback.depth_offset_m if feedback else 0.,
            latest=feedback.latest if feedback else None)
        if feedback and feedback.phase in ('drawing','parking_after','checking'):
            unchecked=[s for s in state.strokes() if s.status=='completed' and s.mark_verification is None]
            for stroke in unchecked:
                stroke.mark_verification=MarkVerification(outcome='inconclusive',decision='inspection_required',
                    note='Server restarted before camera verification; the baseline image is unavailable.')
            if unchecked:
                state.scene.drawing_feedback.latest=unchecked[0].mark_verification
                state.scene.drawing_feedback.stroke_id=unchecked[0].stroke_id
                state.scene.drawing_feedback.phase='inspection_required'
                state.scene.status='paused';state.scene.generation+=1
        self.check=None
        self.baseline=None
        self.advance_lock=asyncio.Lock()
        self.feedback_listeners=set()
        self.operation_epoch=0

    async def initialize(self):
        first=self.robot is None
        await super().initialize()
        if first:
            # Optional environment faults, never provided to the camera policy/Astra.
            await self._call(self.robot.configure_chalk,
                float(os.getenv('SIM_CHALK_LOSS_M','0')),float(os.getenv('SIM_CHALK_WEAR_RATE','0')))
            await self._call(self.robot.set_depth_offset,self.state.scene.drawing_feedback.depth_offset_m)
            async with self.lock:
                await self._commit(snapshot=False)

    def _current(self, check, generation, epoch):
        return (self.check is check and self.operation_epoch==epoch and
                self.state.scene.generation==generation and self.state.scene.status=='drawing' and
                any(s is check.stroke for s in self.state.strokes()) and
                check.stroke.status!='cancelled')

    async def advance(self, seconds):
        if seconds<=0: return
        await self.initialize()
        async with self.advance_lock:
            async with self.lock:
                scene=self.state.scene
                if scene.status!='drawing': return
                if self.check and (self.check.stroke.status=='cancelled' or not any(s is self.check.stroke for s in self.state.strokes())):
                    self.check=None
                if self.check is None:
                    stroke=next((s for s in self.state.strokes() if s.status in ('pending','executing')),None)
                    if stroke is None:
                        scene.status='idle';scene.drawing_feedback.phase='idle'
                        await self._commit(snapshot=False);return
                    self.check=Check(stroke,phase='drawing' if self.baseline else 'parking_before',before=self.baseline)
                check=self.check
                generation,epoch=scene.generation,self.operation_epoch
                scene.drawing_feedback.stroke_id=check.stroke.stroke_id
                scene.drawing_feedback.phase=check.phase
                try:
                    if check.phase in ('parking_before','parking_after'):
                        done=await self._call(self.robot.observation_batch,min(seconds,.05))
                        self.telemetry=await self._call(self.robot.telemetry)
                        if done:
                            check.cleared_view=check.phase=='parking_after'
                            check.phase='capturing_before' if check.phase=='parking_before' else 'checking'
                        scene.drawing_feedback.phase=check.phase
                        await self._commit(snapshot=False)
                        return
                    if check.phase=='drawing':
                        active=next((s for s in self.state.strokes() if s.status in ('pending','executing')),None)
                        if active is not None and not any(s is active for s in check.strokes):
                            check.strokes.append(active)
                        await self._advance_motion_locked(seconds,commit=False)
                        if scene.generation!=generation:  # Motion fault already revoked the plan.
                            self.check=None
                        elif check.stroke.status=='completed':
                            scene.status='drawing'  # Do not announce idle before checking the marks.
                            check.phase='checking'
                            scene.drawing_feedback.phase=check.phase
                        await self._commit(snapshot=False)
                        return
                except Exception:
                    await self._finish_locked(check,{'outcome':'inconclusive','note':'Could not obtain a settled camera observation.'})
                    return
            # Slow rendering and pixel processing never hold the control/scene lock.
            try:
                frame=await self.frame('top',fresh=True)
                if check.phase=='checking':
                    if check.before is None: raise ValueError('No baseline image')
                    strokes=check.strokes or [check.stroke]
                    paths=[[(p.x,p.y) for p in flatten(s.geometry).points] for s in strokes]
                    evidence=[]
                    for stroke,points in zip(strokes,paths):
                        result=await asyncio.to_thread(verify_mark,check.before,frame,points,BoardCameraCalibration(),paths)
                        evidence.append((stroke,result))
                else:
                    evidence=None
            except Exception:
                evidence={'outcome':'inconclusive','note':'Camera check unavailable; drawing paused for inspection.'}
            async with self.lock:
                if not self._current(check,generation,epoch): return
                if evidence is None:
                    check.before=frame;check.phase='drawing'
                    scene.drawing_feedback.phase='drawing'
                    await self._commit(snapshot=False)
                else:
                    if isinstance(evidence,list):
                        if not check.cleared_view and any(e['outcome']!='visible' for _,e in evidence):
                            # An obstructed view is not evidence of missing ink.
                            # Clear the camera only when the normal pen-up view
                            # could not confirm the stroke, before any retry.
                            check.phase='parking_after'
                            scene.drawing_feedback.phase=check.phase
                            await self._commit(snapshot=False)
                            return
                        self.baseline=frame if all(e['outcome'] in ('visible','missing','weak_or_partial') for _,e in evidence) else None
                        await self._finish_batch_locked(check,evidence)
                    else:
                        await self._finish_locked(check,evidence)

    async def _finish_locked(self, check, evidence):
        await self._finish_batch_locked(check,[(check.stroke,evidence)])

    async def _finish_batch_locked(self, check, results):
        scene=self.state.scene; feedback=scene.drawing_feedback
        plans=[]
        for stroke,evidence in results:
            root=stroke.retry_of or stroke.stroke_id
            obj=next(o for o in scene.objects if any(s is stroke for s in o.strokes))
            retries=sum(s.retry_of==root for s in obj.strokes)
            geometries=[]
            if evidence['outcome']=='missing':
                geometries=[stroke.geometry.model_copy(deep=True)]
            elif evidence['outcome']=='weak_or_partial' and evidence.get('repair_paths'):
                from ..models import Polyline
                geometries=[Polyline(type='polyline',points=[{'x':min(1.,max(0.,float(x))),'y':min(1.,max(0.,float(y)))} for x,y in path])
                            for path in evidence['repair_paths']]
            eligible=bool(geometries) and retries+len(geometries)<=3
            plans.append((stroke,evidence,root,obj,geometries if eligible else []))
        # A single correction applies to the entire repair pass, not each stroke.
        budget=feedback.depth_offset_m+.00025<=.00075+1e-12
        uncertain=any(e['outcome']!='visible' and not geometries for _,e,_,_,geometries in plans)
        repair=budget and not uncertain and any(g for *_,g in plans)
        if repair:
            offset=round(feedback.depth_offset_m+.00025,8)
            await self._call(self.robot.set_depth_offset,offset)
            feedback.depth_offset_m=offset
        events=[]
        inspect=False
        for stroke,evidence,root,obj,geometries in plans:
            decision='accepted' if evidence['outcome']=='visible' else 'inspection_required'
            if repair and geometries:
                insertion=obj.strokes.index(stroke)+1
                for geometry in geometries:
                    trajectory=flatten(geometry)
                    retry=StrokeState(stroke_id=f'retry-{uuid4()}',geometry=geometry,
                                      total_length=trajectory.length,retry_of=root)
                    obj.strokes.insert(insertion,retry)
                    insertion+=1
                    self.trajectories[retry.stroke_id]=trajectory
                decision='retry'
            inspect |= decision=='inspection_required'
            report=MarkVerification(outcome=evidence['outcome'],decision=decision,
                path_coverage=evidence.get('path_coverage'),new_pixels=evidence.get('new_pixels'),
                note=evidence.get('note','Camera evidence is inconclusive.'))
            stroke.mark_verification=report;feedback.latest=report
            events.append({'stroke_id':stroke.stroke_id,'root_stroke_id':root,'object_id':obj.object_id,
                           'feedback':report.model_dump(mode='json'),'depth_offset_m':feedback.depth_offset_m})
        self.check=None
        if inspect:
            feedback.latest=next(stroke.mark_verification for stroke,*_ in plans
                                 if stroke.mark_verification.decision=='inspection_required')
            scene.status='paused';scene.generation+=1
            feedback.phase='inspection_required'
        else:
            scene.status='drawing' if any(s.status in ('pending','executing') for s in self.state.strokes()) else 'idle'
            feedback.phase='idle'
        await self._commit(snapshot=False)
        for event in events:
            event.update(generation=scene.generation,status=scene.status)
            log.info('camera_verification stroke=%s outcome=%s decision=%s',event['stroke_id'],
                     event['feedback']['outcome'],event['feedback']['decision'])
            for listener in tuple(self.feedback_listeners):
                try: listener(event)
                except Exception: log.exception('Camera feedback listener failed')

    async def pause(self, expected_generation=None):
        # Increment before awaiting the lock: even a quick pause/resume invalidates
        # an in-flight image result without changing legacy pause generations.
        self.operation_epoch+=1
        return await super().pause(expected_generation)

    async def interrupt(self):
        self.operation_epoch+=1
        return await super().interrupt()

    async def record_manual(self, command):
        self.baseline=None
        self.operation_epoch+=1
        return await super().record_manual(command)

    async def modify_object(self, command):
        self.operation_epoch+=1
        return await super().modify_object(command)

    async def _reset_execution(self):
        self.baseline=None
        self.operation_epoch+=1
        self.check=None
        self.state.scene.drawing_feedback.phase='idle'
        self.state.scene.drawing_feedback.stroke_id=None
        await super()._reset_execution()

    async def simulation_status(self):
        status=await super().simulation_status()
        async with self.lock:
            status['drawing_feedback']=self.state.scene.drawing_feedback.model_dump(mode='json')
        return status
