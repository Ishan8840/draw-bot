"""Executor contract and single-writer, asynchronous virtual implementation."""
import asyncio
from abc import ABC, abstractmethod
from .geometry import flatten
from .models import DrawCommand, DrawingObject, ExecutionStatus, ModifyCommand, Scene, StrokeState
from .scene_state import SceneStateManager
from .websocket_manager import WebSocketManager


class CommandConflict(ValueError):
    pass


class DrawingExecutor(ABC):
    async def interrupt(self) -> Scene:
        raise NotImplementedError("Executor must support generation-advancing pause")

    async def modify_object(self, command: ModifyCommand) -> Scene:
        raise NotImplementedError("Executor must support planned-geometry edits")

    @abstractmethod
    async def execute(self, command: DrawCommand) -> Scene: ...
    @abstractmethod
    async def pause(self, expected_generation=None) -> Scene: ...
    @abstractmethod
    async def resume(self, expected_generation=None) -> Scene: ...
    @abstractmethod
    async def cancel(self, expected_generation=None) -> Scene: ...
    @abstractmethod
    async def clear_board(self, expected_generation=None) -> Scene: ...
    @abstractmethod
    async def get_scene_state(self) -> Scene: ...
    @abstractmethod
    async def get_status(self) -> ExecutionStatus: ...
    @abstractmethod
    async def set_speed(self, speed: float) -> Scene: ...
    @abstractmethod
    async def record_manual(self, command: DrawCommand) -> Scene: ...
    @abstractmethod
    async def start(self) -> None: ...
    @abstractmethod
    async def close(self) -> None: ...


class SceneDrawingExecutor(DrawingExecutor):
    def __init__(self, state: SceneStateManager, events: WebSocketManager, tick_seconds=0.02):
        self.state, self.events = state, events
        self.tick_seconds = tick_seconds
        self.lock = asyncio.Lock()
        self.worker = None
        self.stopping = asyncio.Event()
        self.trajectories = {}

    async def start(self):
        if self.worker is None:
            self.worker = asyncio.create_task(self._run(), name="drawing-executor")

    async def close(self):
        self.stopping.set()
        try:
            if self.worker:
                await self.worker
        finally:
            async with self.lock:
                if self.state.scene.pending_stroke_ids or self.state.scene.current_stroke_id:
                    self.state.invalidate()
                    await self._commit()
                await self.state.close()

    async def _commit(self, snapshot=True):
        await self.state.commit()
        self.events.publish(self.state.scene)
        return self.state.scene.model_copy(deep=True) if snapshot else None

    async def _add(self, command, manual=False):
        async with self.lock:
            scene = self.state.scene
            if command.expected_generation is not None and command.expected_generation != scene.generation:
                raise CommandConflict("Stale command generation; fetch /scene before submitting")
            if any(obj.object_id == command.object_id for obj in scene.objects):
                raise CommandConflict("Object ID already exists")
            existing = {s.stroke_id for s in self.state.strokes()}
            if any(p.stroke_id in existing for p in command.paths):
                raise CommandConflict("Stroke ID already exists")
            strokes = []
            for spec in command.paths:
                trajectory = flatten(spec.geometry)
                stroke = StrokeState(**spec.model_dump(), total_length=trajectory.length)
                if manual:
                    stroke.status = "completed"
                    stroke.executed_points = trajectory.points
                    stroke.executed_length = trajectory.length
                    stroke.progress = 1
                else:
                    self.trajectories[stroke.stroke_id] = trajectory
                strokes.append(stroke)
            scene.objects.append(DrawingObject(object_id=command.object_id, source="user" if manual else "engine", generation=scene.generation, strokes=strokes))
            if not manual and scene.status != "paused":
                scene.status = "drawing"
            return await self._commit()

    async def execute(self, command):
        return await self._add(command)

    async def record_manual(self, command):
        return await self._add(command, manual=True)

    def _check_generation(self, expected_generation):
        if expected_generation is not None and expected_generation != self.state.scene.generation:
            raise CommandConflict("Stale command generation; fetch the latest scene")

    async def interrupt(self):
        """Freeze ink and revoke old model plans, retaining pending paths for editing."""
        async with self.lock:
            self.state.scene.generation += 1
            if self.state.scene.status == "drawing":
                self.state.scene.status = "paused"
            return await self._commit()

    async def modify_object(self, command):
        async with self.lock:
            self._check_generation(command.expected_generation)
            scene = self.state.scene
            obj = next((o for o in scene.objects if o.object_id == command.object_id), None)
            if obj is None:
                raise CommandConflict("Object does not exist")
            existing = {s.stroke_id for s in self.state.strokes()}
            if any(p.stroke_id in existing for p in command.paths):
                raise CommandConflict("Replacement paths need fresh stroke IDs")
            prepared = [(spec, flatten(spec.geometry)) for spec in command.paths]
            # Preserve every old stroke, including partial ink and superseded intent.
            for stroke in obj.strokes:
                if stroke.status in ("pending", "executing"):
                    stroke.status = "cancelled"
                    self.trajectories.pop(stroke.stroke_id, None)
            for spec, trajectory in prepared:
                obj.strokes.append(StrokeState(**spec.model_dump(), total_length=trajectory.length))
                self.trajectories[spec.stroke_id] = trajectory
            obj.generation = scene.generation
            if scene.status != "paused":
                scene.status = "drawing"
            return await self._commit()

    async def pause(self, expected_generation=None):
        async with self.lock:
            self._check_generation(expected_generation)
            if self.state.scene.status == "drawing":
                self.state.scene.status = "paused"
                return await self._commit()
            return self.state.scene.model_copy(deep=True)

    async def resume(self, expected_generation=None):
        async with self.lock:
            self._check_generation(expected_generation)
            if self.state.scene.status == "paused":
                self.state.scene.status = "drawing"
                return await self._commit()
            return self.state.scene.model_copy(deep=True)

    async def cancel(self, expected_generation=None):
        async with self.lock:
            self._check_generation(expected_generation)
            self.state.invalidate()
            self.trajectories.clear()
            await self._reset_execution()
            return await self._commit()

    async def clear_board(self, expected_generation=None):
        async with self.lock:
            self._check_generation(expected_generation)
            self.state.invalidate()
            self.state.scene.objects.clear()
            self.state.scene.status = "idle"
            self.trajectories.clear()
            await self._reset_execution()
            return await self._commit()

    async def _reset_execution(self):
        """Adapter hook under the scene lock; discard old physical plan cursors."""

    async def set_speed(self, speed):
        from .models import SpeedCommand
        speed = SpeedCommand(speed=speed).speed
        async with self.lock:
            self.state.scene.speed = speed
            return await self._commit()

    async def get_scene_state(self):
        async with self.lock:
            return self.state.scene.model_copy(deep=True)

    async def get_status(self):
        scene = await self.get_scene_state()
        return ExecutionStatus(revision=scene.revision, generation=scene.generation, status=scene.status, speed=scene.speed,
                               current_stroke_id=scene.current_stroke_id, pending=len(scene.pending_stroke_ids),
                               completed=len(scene.completed_stroke_ids), cancelled=len(scene.cancelled_stroke_ids))

    @abstractmethod
    async def advance(self, seconds: float): ...

    async def _run(self):
        while not self.stopping.is_set():
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=self.tick_seconds)
            except TimeoutError:
                await self.advance(self.tick_seconds)


class VirtualDrawingExecutor(SceneDrawingExecutor):
    """Ideal virtual motion; simulator adapters share admission, not this timing model."""
    async def advance(self, seconds: float):
        """One deterministic simulation step. All hardware feedback must share this lock."""
        if seconds <= 0:
            return
        async with self.lock:
            scene = self.state.scene
            if scene.status != "drawing":
                return
            budget = scene.speed * seconds
            for stroke in self.state.strokes():
                if stroke.status not in ("pending", "executing"):
                    continue
                if budget <= 0:
                    break
                stroke.status = "executing"
                trajectory = self.trajectories[stroke.stroke_id]
                advance = min(budget, stroke.total_length - stroke.executed_length)
                stroke.executed_length = min(stroke.total_length, stroke.executed_length + advance)
                budget -= advance
                stroke.executed_points = trajectory.prefix(stroke.executed_length)
                stroke.progress = stroke.executed_length / stroke.total_length if stroke.total_length else 1
                if stroke.progress >= 1:
                    stroke.status = "completed"
                    self.trajectories.pop(stroke.stroke_id, None)
                else:
                    break
            if all(s.status not in ("pending", "executing") for s in self.state.strokes()):
                scene.status = "idle"
            await self._commit()
