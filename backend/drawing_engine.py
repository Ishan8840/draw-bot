"""Universal tool boundary. No activity-specific drawing or AI dependencies."""
from .executor import DrawingExecutor
from .models import DrawCommand, ModifyCommand, StrokeSpec


class DrawingEngine:
    def __init__(self, executor: DrawingExecutor):
        self.executor = executor

    async def draw_strokes(self, object_id: str, paths: list[StrokeSpec], expected_generation=None):
        return await self.executor.execute(DrawCommand(object_id=object_id, paths=paths, expected_generation=expected_generation))

    async def pause(self, expected_generation=None):
        return await self.executor.pause(expected_generation=expected_generation)

    async def resume(self, expected_generation=None):
        return await self.executor.resume(expected_generation=expected_generation)

    async def cancel(self, expected_generation=None):
        return await self.executor.cancel(expected_generation=expected_generation)

    async def clear_board(self, expected_generation=None):
        return await self.executor.clear_board(expected_generation=expected_generation)

    async def interrupt(self):
        return await self.executor.interrupt()

    async def modify_object(self, command: ModifyCommand):
        return await self.executor.modify_object(command)

    async def get_scene_state(self):
        return await self.executor.get_scene_state()
