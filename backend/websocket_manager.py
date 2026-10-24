"""Bounded per-client mailboxes: slow clients cannot block execution."""
import asyncio
from .models import Scene, SceneEvent


class WebSocketManager:
    def __init__(self):
        self.subscribers: set[asyncio.Queue] = set()

    def subscribe(self):
        queue = asyncio.Queue(maxsize=1)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self.subscribers.discard(queue)

    def publish(self, scene: Scene):
        event = SceneEvent(scene=scene).model_dump(mode="json")
        for queue in tuple(self.subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)
