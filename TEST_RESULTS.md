# Latest verification — OpenArm v2 (2026-10-23)

```text
MUJOCO_GL=egl .venv/bin/python -m scripts.test_sandbox
112 passed, 1 warning in 62.32s
```

The simulator now uses the official OpenArm v2 right-arm geometry, inertias, axes, joint limits and actuator force limits from `enactic/openarm_mujoco` commit `a3c8a57ec15d385e3c1dbd3fd090ee486b7f258c`. The prior generic SCARA model is removed. A fixed pen and closed gripper form the drawing fixture; all seven actuated joints are the original rotary arm joints. The 18 cm board is inside the validated workspace.

Real-physics tests cover circles, rectangles, Bézier curves, all four board edges, contact recording, retraction, partial ink, cancellation/resume/new commands, stale generations, reset/restart, fault feedback, renderer isolation and API reconnect. New model tests check the seven joint names/types, original motor torque limits, position/orientation IK across 50 board poses, and rejection of unreachable targets. During tracing, tests assert actuator forces remain within the upstream limits and no external joint forces are applied. Feedforward goes through the force-limited actuators. All prior agent, generic-reactivity and voice tests pass.

The first OpenArm run exposed contact-induced endpoint bias; the delivered controller includes bounded endpoint feedback correction and passes the original completion/geometry checks. No test was skipped. The single warning is the existing Starlette/AnyIO deprecation.

## OpenArm acceptance demo

The current [animation](artifacts/mujoco/interruption-demo.gif), [arm view](artifacts/mujoco/overview.jpg), [top view](artifacts/mujoco/board-top.jpg), [scene](artifacts/mujoco/scene.json) and [metrics](artifacts/mujoco/metrics.json) were regenerated with OpenArm, not the former fixture.

`python -m scripts.smoke_mujoco --gif --sandbox-heartbeat` confirmed preservation of a cancelled partial house while completing a new circle. Actual measurements: 13.282 simulated seconds, 14.419 offline wall seconds including sampled rendering/export, 17.31 ms pause acknowledgment, maximum planar tracking error during drawing 1.393 mm, and 3,256 recorded contact samples. These are simulated-fixture measurements, not hardware stopping or guaranteed latency.

The default launcher now uses `data/openarm-scene.sqlite3`, leaving earlier scene files untouched. Native runtime assets are vendored with their Apache-2.0 license and provenance; no external checkout is needed. See [OpenArm setup](docs/MUJOCO.md) and [model provenance](models/openarm/SOURCE.md).

No live Astra request, microphone acceptance run or real-robot command was made. The sandbox still prevents serving a live TCP browser session; real MuJoCo renders and in-process API tests are verified. Historical results below refer to earlier models/revisions.

---

# Latest verification — MuJoCo integration (2026-10-23)

```text
MUJOCO_GL=egl .venv/bin/python -m scripts.test_sandbox
110 passed, 1 warning in 54.12s
```

All tests ran without skips. Runtime: Python 3.11.15, MuJoCo 3.17.0, NumPy 2.3.5, Pillow 13.0.0. The one warning is Starlette's deprecated AnyIO alias. `pip check` reports no broken requirements. Python compilation, simulator JavaScript syntax, the launcher help command, and the existing browser audio unit checks passed.

New coverage uses **actual MuJoCo dynamics and headless EGL rendering**, not fake physics: workspace/IK agreement, circles/rectangles/Bézier tracing, pen contact and retraction, measured partial ink, pause/resume, cancellation followed by new work, clearing and stale generations, reused IDs after clear, restart state, manual observations and mocked Astra tools through the simulator, current image observations, real JPEG rendering, HTTP/scene WebSocket reconnects, renderer isolation from Stop, and faulted-batch ink preservation. Existing virtual, agent, generic-reactivity and voice tests remain passing.

## Executed acceptance demo

`MUJOCO_GL=egl .venv/bin/python -m scripts.smoke_mujoco --gif --sandbox-heartbeat`

The demo drew part of a house, paused, cancelled, and completed a circle on the same board. It asserted that every cancelled house stroke and its measured ink remained identical after the circle completed.

- Simulated motion: **29.33 s**.
- Offline wall time, including sampled rendering and GIF export: **18.92 s**.
- Pause acknowledgment in this run: **17.91 ms**.
- Maximum measured planar tracking error during drawing: **1.70 mm**.
- Preserved/executed ink samples: **3,839**.

These are measurements of this simulated fixture and test environment, not guaranteed latency, live-browser throughput, hardware braking or physical robot accuracy. Simulation pause freezes its clock. The GIF is accelerated and sampled.

Artifacts: [animation](artifacts/mujoco/interruption-demo.gif), [arm view](artifacts/mujoco/overview.jpg), [top view](artifacts/mujoco/board-top.jpg), [metrics](artifacts/mujoco/metrics.json), [authoritative scene](artifacts/mujoco/scene.json).

The first renderer version was much slower; the delivered version uses lightweight ink display geometry, disables costly shadows/reflections, and owns a separate rendering model/worker. A regression test holds rendering blocked and confirms cancellation still completes. Only the latest demo metrics above describe the delivered version.

## Verification boundaries

No live OpenAI request was made: no API key was configured. Agent integration uses mocked Responses results; model interpretation, illustration quality and strategy require live testing. Voice still needs installed local speech dependencies/model assets, and no real microphone or neural speech acceptance test was performed in this turn.

The server completed initialization but sandbox policy prevented binding localhost port 8000. It is **not running here**. HTTP/WebSocket tests use FastAPI's in-process TestClient, while the render artifacts come from the actual MuJoCo engine. Launch locally using `.venv/bin/python -m scripts.run_mujoco`; see [setup and architecture](docs/MUJOCO.md).

Earlier results below describe prior revisions; in particular the historical game validator was removed at the user's request.

---

# Latest verification — general reactive agent

```text
.venv-sdk/bin/python -m scripts.test_sandbox
98 passed, 541 warnings in 25.49s
```

Manual ink now triggers a general scene observation in the shared conversation. Tests verify debouncing, generic model tool execution, preservation of manual ink, no recursive response to agent strokes, Stop revocation, optional session routing, unknown-session rejection, and the model choosing to wait. Recent geometry is available without backend symbol classification. Game-specific rule tests were replaced with tests confirming arbitrary semantic IDs and valid geometry are accepted without a special game state or rule guard. The production game parser/validator was removed at the user's request; earlier game-validator results below are historical, not current behavior.

Frontend JavaScript syntax and Python compilation passed. Model responses were mocked; live autonomous gameplay and drawing quality were not tested. Existing voice hardware and sandbox limitations below still apply.

---

# Verification — Phase 3 local voice

Executed on 2026-10-09.

```text
.venv-sdk/bin/python -m scripts.test_sandbox
93 passed, 523 warnings in 22.48s
```

All 61 existing Phase 1/2 tests pass; 32 additional voice outcomes pass. No tests skipped. The environment uses Python 3.14.4 with the official SDK; warnings are dependency asyncio/AnyIO deprecations. The sandbox launcher adds timer wakeups because socketpair notifications are restricted; it does not mock the drawing engine or persistence. On a normal machine run `python -m pytest -q`.

New coverage: synthetic VAD segmentation; local STT submission; shared text/voice journal; duplicate audio/utterance suppression; fast lexical pause/cancel; partial ink; stale Astra and transcription rejection; rapid commands and repeated stop; synthesis on a worker; stale TTS rejection and playback acknowledgment; mute/disable; live in-process audio WebSocket reconnect; malformed frames; audio-processing recovery; disabled voice leaving the board available.

Actual JavaScript unit execution also passed: 16/44.1/48 kHz worklet resampling, stale decode/playback, barge-in, continued capture, mute, device release and permission denial. These execute the frontend code with mocked browser APIs, not a physical microphone. Python compilation and voice JavaScript syntax checks passed. Benchmark CLI help and missing-package diagnostics were checked.

## Measured diagnostics and boundaries

[Recorded test diagnostics](docs/voice-mock-diagnostics.json) contain measured software-stage timings from mocked inference and simulated playback acknowledgment. They are explicitly **not hardware speech latency**. Tests feed audio faster than real time, so estimated speech-boundary deltas in that artifact are not valid real-time latency measurements. Inference mocks intentionally sleep about 5 ms; those durations say nothing about Whisper or Piper performance. Live `/voice/diagnostics` instrumentation measures actual executor pause acknowledgment, model responses and audio stages when run with real components.

The speech installation attempt failed because the package index was unreachable. Availability reports missing numpy, onnxruntime, faster_whisper and piper, no cached VAD and no Piper voices. No real neural audio inference, GPU run, microphone capture or speaker playback was performed. The server was not started here because local TCP binding is restricted. No live paid Astra request was made. The full spoken acceptance demo and hardware latency remain unverified.

Use [the voice setup guide](docs/VOICE.md), then `python -m scripts.voice_benchmark --wav stop.wav --output voice-latency.json` for real offline adapter measurements. For the complete interaction, run the browser walkthrough and collect `/voice/diagnostics`. No guaranteed latency is claimed.

---

# Verification — Phases 1 and 2

Executed on 2026-10-09.

## Latest full-suite result

```text
.venv-sdk/bin/python -m scripts.test_sandbox
61 passed, 433 warnings in 17.29s
```

All 34 Phase 1 tests remain passing. There are 27 additional agent, session/API, game-validation, interruption, and SDK transport tests. **No tests were skipped in this full run.**

SDK-enabled verification environment: Python 3.14.4, OpenAI Python SDK 2.54.0, FastAPI 0.115.12, Starlette 0.46.2, Pydantic 2.13.5, pytest 9.1.1, HTTPX 0.28.1, AnyIO 4.15.1. The warnings concern older FastAPI/Starlette asyncio and AnyIO aliases deprecated by newer dependencies. The sandbox launcher itself also reports asyncio policy deprecations on Python 3.14. They are not test failures.

An earlier Python 3.11.15 compatibility run passed 55 tests and skipped the SDK-specific test because that interpreter did not have an available cached jiter binary. Five final additional regression tests were subsequently added and passed in the complete SDK-enabled run. The original `.venv` was retained; `.venv-sdk` was created separately to validate the official SDK without modifying the existing interpreter. Ordinary installations should run `pip install -r requirements.txt` in their selected environment.

Other actual checks:

- `node --check frontend/app.js`: passed.
- `node --check frontend/chat.js`: passed.
- `node --check tests/browser_smoke.cjs`: passed.
- Python compilation checks: passed.
- SDK environment `pip check`: no broken installed requirements.
- Exported JSON/OpenAPI/tool schemas match live models: passed in pytest.

## Phase 2 coverage

| Requirement | Verified behavior |
| --- | --- |
| Tool routing | Mocked Responses calls dispatch all drawing activity through shared engine methods |
| Valid drawing requests | Model tool arguments create validated pending geometry |
| Multi-turn scene awareness | Later requests include prior conversation and existing semantic objects |
| Add without clearing | A second object coexists with the original unchanged drawing |
| Immediate interruption | New messages/Stop pause before awaiting model output |
| Stale calls rejected | Late responses and direct stale tools cannot draw or clear newer state |
| Partial ink preserved | Interrupted and modified strokes retain identical executed points |
| Sequential tools | Results return to the model, followed by further reads, edits, draws, and resume |
| Legal game moves | Occupied cells, wrong turns, finished games, out-of-cell geometry, and ambiguous manual marks are rejected |
| Tool failure recovery | Validation errors are returned as tool results; corrected calls succeed |
| Conversation reconnect | HTTP/session and WebSocket reconnects reconstruct state; SQLite sessions survive process recreation |
| Official SDK protocol | Real AsyncOpenAI + mocked HTTP verifies `/v1/responses`, strict schemas, `call_id` pairing, reasoning-item continuation, and drawing admission |
| Duplicate protection | Duplicate HTTP request IDs and tool call IDs do not repeat mutations; unresolved receipts are not replayed |
| Model failure handling | Timeouts and errors pause the board and return sanitized messages |
| Bounded scene/tool context | Stroke/point bounds, compact summaries, paginated detailed geometry, read-before-write enforcement |
| Plan revisions | Guarded `modify_object` preserves ink, cancels superseded paths, and appends fresh pending strokes |

Game tests use generated fixture geometry to verify routing and legality, not a production scripted game pipeline. The production validator does not generate geometry or choose moves.

## Phase 1 coverage retained

Circle radius/closure/arc length; universal circle/grid execution; multiple objects; exact pause/resume position; cancel preserving partial ink; new commands after cancel; clear invalidation; accurate progress/indexes; invalid geometry/IDs/JSON/non-finite inputs; scene reconstruction; manual strokes; concurrency; speed changes; snapshot isolation; zero-length strokes; bounded WebSocket queues; repeated disconnect cleanup; SQLite restart recovery.

## Verification boundaries

**No live OpenAI request was made.** No API key was configured. The official SDK test uses a fake key and HTTPX MockTransport; it verifies request/continuation mechanics and engine effects without network access or paid inference. Recognizable live illustrations, live game strategy, and the complete natural-language acceptance walkthrough remain to be assessed with a real key.

**Browser testing was not run.** The execution sandbox denies local TCP sockets and prevents reliable asyncio socketpair thread wakeups. The included sandbox launcher supplies timer wakeups so the real in-process FastAPI/HTTP/WebSocket/SQLite tests can run; it does not mock application state or execution. On a normal local machine use `python -m pytest -q`.

`tests/browser_smoke.cjs` remains available for a disposable local server. It now supports `HEADLESS=0 SLOW_MO=150` for visible testing and opens the collapsible structured-command panel. It clears the board. Browser-rendered layout, mouse interaction, chat rendering, and SVG appearance are not claimed verified here.

Package-index DNS was unavailable. Cached wheels and unmodified upstream framework source releases were used to create the local test environments. No temporary checkout is a runtime application dependency. No local preview server was started for Phase 2.
