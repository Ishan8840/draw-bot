"""Chat HTTP contracts and bounded model tool arguments."""
from typing import Annotated, Literal
from pydantic import Field, model_validator
from .models import DrawCommand, Identifier, Model, StrokeSpec


class ChatMessage(Model):
    text: str = Field(min_length=1, max_length=8000)
    request_id: Identifier
    source: Literal['text', 'voice', 'board', 'feedback'] = 'text'

    @model_validator(mode='after')
    def meaningful(self):
        if not self.text.strip():
            raise ValueError('Message cannot be blank')
        return self


class ToolPaths(Model):
    object_id: Identifier
    paths: list[StrokeSpec] = Field(min_length=1, max_length=32)

    @model_validator(mode='after')
    def bounded(self):
        DrawCommand(object_id=self.object_id, paths=self.paths)
        count = sum(len(getattr(p.geometry, 'points', [])) or 1 for p in self.paths)
        if count > 2048:
            raise ValueError('Agent batch exceeds 2048 input points; use smaller objects')
        return self


class SceneQuery(Model):
    object_id: Identifier | None = None
    include_geometry: bool = False
    offset: Annotated[int, Field(ge=0, strict=True)] = 0
    limit: Annotated[int, Field(ge=1, le=10, strict=True)] = 10


class NoArguments(Model):
    pass


class ChatEvent(Model):
    id: int
    turn_id: str | None
    kind: Literal['user', 'assistant', 'tool_call', 'tool_result', 'error', 'interruption', 'observation']
    content: str
    tool_name: str | None = None
    call_id: str | None = None
    created_at: float
    source: Literal['text', 'voice', 'board', 'feedback'] = 'text'


class ChatSession(Model):
    session_id: str
    status: Literal['idle', 'thinking', 'interrupted', 'error']
    turn_id: str | None
    events: list[ChatEvent]


class AgentConfiguration(Model):
    configured: bool
    model: str
    max_rounds: int
