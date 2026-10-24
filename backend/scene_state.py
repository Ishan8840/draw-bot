"""Durable authoritative scene. Executor owns synchronization of all mutations."""
import asyncio
import sqlite3
from pathlib import Path
from .models import Scene


class SceneStateManager:
    def __init__(self, database: str):
        if database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(database, check_same_thread=False)
        self.connection.execute("CREATE TABLE IF NOT EXISTS scene (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)")
        row = self.connection.execute("SELECT data FROM scene WHERE id=1").fetchone()
        self.scene = Scene.model_validate_json(row[0]) if row else Scene()
        # Restart is an interruption, never an instruction to resume old work.
        if self.scene.pending_stroke_ids or self.scene.current_stroke_id:
            self.invalidate()
            self.scene.revision += 1
        self._write(self.scene.model_dump_json())

    def strokes(self):
        return (s for obj in self.scene.objects for s in obj.strokes)

    def reindex(self):
        scene = self.scene
        strokes = list(self.strokes())
        for state in ("pending", "completed", "cancelled"):
            setattr(scene, f"{state}_stroke_ids", [s.stroke_id for s in strokes if s.status == state])
        scene.current_stroke_id = next((s.stroke_id for s in strokes if s.status == "executing"), None)

    def invalidate(self):
        self.scene.generation += 1
        for stroke in self.strokes():
            if stroke.status in ("pending", "executing"):
                stroke.status = "cancelled"
        self.scene.status = "cancelled"
        self.reindex()

    def _write(self, payload):
        with self.connection:
            self.connection.execute("INSERT INTO scene VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (payload,))

    async def commit(self):
        self.reindex()
        self.scene.revision += 1
        await asyncio.to_thread(self._write, self.scene.model_dump_json())

    async def close(self):
        await asyncio.to_thread(self.connection.close)
