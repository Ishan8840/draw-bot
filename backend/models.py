"""Version 1 tool and scene contracts. Coordinates are in a square [0, 1] frame."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Coordinate = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Point(Model):
    x: Coordinate
    y: Coordinate


class Line(Model):
    type: Literal["line"]
    start: Point
    end: Point


class Polyline(Model):
    type: Literal["polyline"]
    points: list[Point] = Field(min_length=2, max_length=4096)


class CubicBezier(Model):
    type: Literal["cubic_bezier"]
    start: Point
    control1: Point
    control2: Point
    end: Point


class Circle(Model):
    type: Literal["circle"]
    center: Point
    radius: Annotated[float, Field(gt=0, le=0.5, allow_inf_nan=False, strict=True)]

    @model_validator(mode="after")
    def bounds(self):
        if any(v - self.radius < 0 or v + self.radius > 1 for v in (self.center.x, self.center.y)):
            raise ValueError("Circle must fit within the board")
        return self


class Rectangle(Model):
    type: Literal["rectangle"]
    origin: Point
    width: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False, strict=True)]
    height: Annotated[float, Field(gt=0, le=1, allow_inf_nan=False, strict=True)]

    @model_validator(mode="after")
    def bounds(self):
        if self.origin.x + self.width > 1 or self.origin.y + self.height > 1:
            raise ValueError("Rectangle must fit within the board")
        return self


Geometry = Annotated[Line | Polyline | CubicBezier | Circle | Rectangle, Field(discriminator="type")]


class StrokeSpec(Model):
    stroke_id: Identifier
    geometry: Geometry


class DrawCommand(Model):
    schema_version: Literal[1] = 1
    object_id: Identifier
    paths: list[StrokeSpec] = Field(min_length=1, max_length=256)
    expected_generation: Annotated[int, Field(ge=0, strict=True)] | None = None

    @model_validator(mode="after")
    def unique_and_bounded(self):
        ids = [p.stroke_id for p in self.paths]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate stroke IDs in command")
        if sum(len(p.geometry.points) if isinstance(p.geometry, Polyline) else 1 for p in self.paths) > 8192:
            raise ValueError("Command exceeds 8192 input points")
        return self


class ControlCommand(Model):
    schema_version: Literal[1] = 1


class ModifyCommand(DrawCommand):
    """Replace future ink; old strokes and their executed portions remain historical."""



class SpeedCommand(Model):
    speed: Annotated[float, Field(ge=0.01, le=5, allow_inf_nan=False, strict=True)]


class MarkVerification(Model):
    outcome: Literal['visible', 'missing', 'weak_or_partial', 'inconclusive']
    decision: Literal['accepted', 'retry', 'inspection_required']
    path_coverage: float | None = Field(default=None, ge=0, le=1)
    new_pixels: int | None = Field(default=None, ge=0)
    note: str
    source: Literal['camera_frame_difference'] = 'camera_frame_difference'


class DrawingFeedback(Model):
    enabled: bool = True
    phase: Literal['idle', 'parking_before', 'capturing_before', 'drawing', 'parking_after', 'checking', 'inspection_required'] = 'idle'
    stroke_id: str | None = None
    depth_offset_m: float = Field(default=0., ge=0, le=.00075)
    latest: MarkVerification | None = None


class StrokeState(StrokeSpec):
    mark_verification: MarkVerification | None = None
    retry_of: str | None = None
    status: Literal["pending", "executing", "completed", "cancelled"] = "pending"
    executed_points: list[Point] = Field(default_factory=list)
    executed_length: float = 0
    total_length: float
    progress: float = 0


class DrawingObject(Model):
    object_id: Identifier
    source: Literal["engine", "user"]
    generation: int
    strokes: list[StrokeState]


class Scene(Model):
    drawing_feedback: DrawingFeedback | None = None
    execution_observation: Literal["virtual_ink", "encoder_estimate_unverified"] = "virtual_ink"
    schema_version: Literal[1] = 1
    revision: int = 0
    generation: int = 0
    status: Literal["idle", "drawing", "paused", "cancelled"] = "idle"
    speed: float = 0.25
    board_aspect_ratio: Literal[1.0] = 1.0
    objects: list[DrawingObject] = Field(default_factory=list)
    current_stroke_id: str | None = None
    pending_stroke_ids: list[str] = Field(default_factory=list)
    completed_stroke_ids: list[str] = Field(default_factory=list)
    cancelled_stroke_ids: list[str] = Field(default_factory=list)


class ExecutionStatus(Model):
    revision: int
    generation: int
    status: Literal["idle", "drawing", "paused", "cancelled"]
    speed: float
    current_stroke_id: str | None
    pending: int
    completed: int
    cancelled: int


class SceneEvent(Model):
    type: Literal["scene"] = "scene"
    scene: Scene
