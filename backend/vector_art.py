"""Optional future planner boundary. No provider or extra drawing pipeline in Phase 2."""
from typing import Protocol
from .agent_models import ToolPaths


class VectorArtGenerator(Protocol):
    async def generate_vector_art(self, prompt: str, scene_summary: dict) -> list[ToolPaths]:
        """Return bounded semantic objects, never execute them.

        Future callers must validate each ToolPaths and submit through the same
        AstraAgent guarded draw_strokes route; never bypass its generation checks.
        """
        ...
