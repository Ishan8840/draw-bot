"""Deterministic geometry flattening shared by execution and future adapters.

Curves use bounded sampling (maximum control polygon interval 0.002 units).
Executed ink is the resulting polyline, not an SVG dash animation.
"""
from bisect import bisect_right
from dataclasses import dataclass
from math import ceil, cos, hypot, pi, sin
from .models import Circle, CubicBezier, Geometry, Line, Point, Polyline, Rectangle


def distance(a: Point, b: Point) -> float:
    return hypot(b.x - a.x, b.y - a.y)


@dataclass
class Trajectory:
    points: list[Point]
    lengths: list[float]

    @property
    def length(self):
        return self.lengths[-1]

    def prefix(self, length: float) -> list[Point]:
        if length >= self.length:
            return list(self.points)
        if length <= 0:
            return []
        index = bisect_right(self.lengths, length) - 1
        a, b = self.points[index:index + 2]
        t = (length - self.lengths[index]) / (self.lengths[index + 1] - self.lengths[index])
        return self.points[:index + 1] + [Point(x=a.x + t * (b.x - a.x), y=a.y + t * (b.y - a.y))]


def flatten(g: Geometry) -> Trajectory:
    if isinstance(g, Line):
        points = [g.start, g.end]
    elif isinstance(g, Polyline):
        points = g.points
    elif isinstance(g, Rectangle):
        x, y, w, h = g.origin.x, g.origin.y, g.width, g.height
        points = [Point(x=a, y=b) for a, b in [(x, y), (x+w, y), (x+w, y+h), (x, y+h), (x, y)]]
    elif isinstance(g, Circle):
        n = max(32, ceil(2 * pi * g.radius / 0.002))
        points = [Point(x=min(1, max(0, g.center.x + g.radius * cos(2*pi*i/n))),
                        y=min(1, max(0, g.center.y + g.radius * sin(2*pi*i/n)))) for i in range(n)]
        points.append(points[0])
    elif isinstance(g, CubicBezier):
        controls = [g.start, g.control1, g.control2, g.end]
        n = max(16, ceil(sum(distance(a, b) for a, b in zip(controls, controls[1:])) / 0.002))
        points = []
        for i in range(n + 1):
            t = i / n
            weights = [(1-t)**3, 3*(1-t)**2*t, 3*(1-t)*t*t, t**3]
            points.append(Point(**{axis: min(1, max(0, sum(w * getattr(p, axis) for w, p in zip(weights, controls)))) for axis in ("x", "y")}))
    else:
        raise TypeError("Unsupported geometry")
    # Remove duplicate adjacent points to avoid zero-length interpolation spans.
    unique = [points[0]]
    for point in points[1:]:
        if distance(unique[-1], point) > 0:
            unique.append(point)
    lengths = [0.0]
    for a, b in zip(unique, unique[1:]):
        lengths.append(lengths[-1] + distance(a, b))
    return Trajectory(unique, lengths)
