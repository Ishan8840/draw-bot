"""Optional pytest launcher for sandboxes that block asyncio socketpair wakeups.

Run: python -m scripts.test_sandbox
Adds timer wakeups only; does not mock application code, persistence, or tests.
Ordinary local environments should use python -m pytest -q instead.
"""
import asyncio
import pytest


class PollingPolicy(asyncio.DefaultEventLoopPolicy):
    def new_event_loop(self):
        loop = super().new_event_loop()

        def heartbeat():
            loop.call_later(0.01, heartbeat)

        loop.call_soon(heartbeat)
        return loop


if __name__ == '__main__':
    asyncio.set_event_loop_policy(PollingPolicy())
    raise SystemExit(pytest.main(['-q', '-o', 'faulthandler_timeout=60']))
