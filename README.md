# Astra Real-Time Robot Artist — Agent + Local Voice + MuJoCo

A local Python + SVG drawing studio with an asynchronous, interruptible drawing engine and a conversational GPT-6 Astra agent. Draw a house, interrupt it, then revise the plan or add a tree while preserving existing ink. Local conversational voice and an OpenArm v2 drawing arm in MuJoCo are included. Physical hardware integration remains future work.

**Phase 2 is implemented.** See [Astra setup, tool calling, interruption behavior, and the acceptance walkthrough](docs/PHASE2.md). Set `OPENAI_API_KEY` and optionally `OPENAI_MODEL` (default `gpt-6-astra`) on the server. The JSON whiteboard also works without an API key.

## Run OpenArm in MuJoCo

```sh
source .venv/bin/activate
pip install -r requirements.txt -r requirements-mujoco.txt
python -m scripts.run_mujoco
```

Open **http://127.0.0.1:8000** for the arm view, board, chat and voice controls. Export your existing OpenAI environment variables for Astra; JSON examples work without a key. The official seven-joint OpenArm v2 right arm uses a fixed pen and an 18 cm board. The simulator uses a separate `data/openarm-scene.sqlite3` database by default. See [MuJoCo setup, controls, architecture and verification](docs/MUJOCO.md). A reproducible offline demo is available with `python -m scripts.smoke_mujoco --gif`.

## Local voice

See [the Phase 3 voice guide](docs/VOICE.md) for installation, microphone controls, interruption behavior and latency diagnostics. Install optional speech dependencies with `pip install -r requirements-voice.txt`, cache models with `python -m scripts.setup_voice`, then enable the microphone in the chat panel. Text chat remains available.

## Run

Requires Python 3.11 or later. From this directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000**. Interactive API documentation is at **/docs**.

Use **one server process / worker** for one scene database. The engine lock and WebSocket subscriptions are process-local. Do not run multiple instances against the same database.

The scene is saved to `data/scene.sqlite3`. Set `ASTRA_SCENE_DB=/path/to/scene.sqlite3` to change its location. Completed and partial ink survives application restarts. Restarted unfinished strokes are cancelled and the generation advances; the application never silently resumes an interrupted drawing. Clear board intentionally removes stored objects and ink.

## Try the main workflow

For reactive interaction, enable **Draw by hand** and leave **Astra responds to my drawings** checked. After about 0.8 seconds without another submitted stroke, Astra receives a general board observation in the same conversation. It sees recent user ink and can retrieve exact geometry, interpret your intent, draw, speak, ask for clarification or wait. This works through the same agent for games and illustrations: there is no game parser, move validator or special ID convention. The pause is a batching heuristic, not proof that a symbol or move is complete. Astra makes that judgment. Its own strokes do not trigger another automatic turn. Stop, newer text/voice instructions and board controls supersede pending reactions. Uncheck the option to draw without requesting a response.

Manual API clients opt in with `POST /commands/manual?session_id=<conversation-id>`; omitting the session preserves the previous record-only behavior. Reactive observations are stored in that conversation with source `board`. Model availability and response time still depend on the configured Astra API.

1. Select **Simple house**, then **Execute drawing**.
2. While the roof is drawing, choose **Pause**. Ink stops advancing.
3. Choose **Resume** to continue, or **Cancel drawing** to preserve the current partial stroke and discard pending strokes.
4. Select **X** and execute it. The cancelled house stays visible and the X draws on the same board.
5. Reload the page. The board reconstructs from server-confirmed executed points.
6. Enable **Draw by hand**, then drag on the board. On pointer release, that stroke is saved as a user-created object.

The speed slider controls board units per second. Examples receive fresh semantic IDs each time they are loaded. Re-executing an unchanged JSON command returns an ID conflict; edit the IDs or load an example again. Multiple objects can overlap or coexist without clearing.

## Universal commands

Every example uses `POST /commands/draw`:

```json
{
  "schema_version": 1,
  "object_id": "house-roof",
  "expected_generation": 0,
  "paths": [
    {
      "stroke_id": "house-roof-outline",
      "geometry": {
        "type": "polyline",
        "points": [
          {"x": 0.2, "y": 0.45},
          {"x": 0.5, "y": 0.18},
          {"x": 0.8, "y": 0.45}
        ]
      }
    }
  ]
}
```

Fetch `/scene` for the current generation before supplying `expected_generation`. It is optional for simple clients; the browser supplies it automatically. A mismatch rejects delayed submissions from before a cancel or clear. Omitting it explicitly means enqueue against the current generation. Cancellation always invalidates all previously accepted pending strokes, regardless of whether commands supplied this field.

```sh
curl -X POST http://127.0.0.1:8000/commands/draw \
  -H 'Content-Type: application/json' --data-binary @examples/house.json
curl -X POST http://127.0.0.1:8000/commands/cancel
curl -X POST http://127.0.0.1:8000/commands/draw \
  -H 'Content-Type: application/json' --data-binary @examples/x.json
```

All coordinates and control points are numeric, finite values in `[0, 1]`. Each geometry is one stroke / continuous pen-down path:

| `type` | Fields |
| --- | --- |
| `line` | `start`, `end`: `{x, y}` |
| `polyline` | `points`: 2–4096 points |
| `cubic_bezier` | `start`, `control1`, `control2`, `end` |
| `circle` | `center`, positive `radius` (entire circle must fit) |
| `rectangle` | `origin` (top-left), positive `width`, `height` (entire rectangle must fit) |

This is typed SVG-style geometry, not an unvalidated SVG `d` string. Bézier control points must also be on the board, a conservative constraint that keeps the whole curve inside it. Unknown fields, invalid types, out-of-bounds geometry, malformed paths, and duplicate stroke IDs inside a command return HTTP 422. Existing object/stroke IDs and stale generations return 409. Validation failures do not mutate the scene. IDs contain letters, digits, `.`, `_`, `:`, or `-`, start with an alphanumeric character, and have at most 128 characters. Stroke IDs are unique across all objects, including cancelled strokes. IDs can be reused after clearing the scene. Commands are limited to 256 strokes and 8192 input points total.

Examples: [circle](examples/circle.json), [square](examples/square.json), [house](examples/house.json), [tic-tac-toe grid](examples/tic_tac_toe.json), [X](examples/x.json), [O](examples/o.json), [cubic Bézier](examples/bezier.json). They contain geometry only; there are no game-specific tools or execution paths.

## HTTP and WebSocket contract

| Endpoint | Body / result |
| --- | --- |
| `POST /commands/draw` | `DrawCommand`; atomically validates and appends an object, returns scene immediately |
| `POST /commands/pause` | No body or `{}`; holds current progress and queue |
| `POST /commands/resume` | No body or `{}`; resumes only paused work |
| `POST /commands/cancel` | No body or `{}`; cancels current and pending work, preserving ink |
| `POST /commands/clear` | No body or `{}`; advances generation, removes objects and ink |
| `POST /commands/speed` | `{"speed": 0.25}`; range 0.01–5 normalized units/second |
| `POST /commands/manual` | `DrawCommand`; records already-observed user ink as completed strokes |
| `GET /scene` | Full authoritative scene |
| `GET /status` | Revision, generation, execution status, speed, stroke counts and current stroke ID |
| `WS /ws` | Initial scene, then `{"type":"scene","scene":{...}}` snapshots |

Controls are global to the board, including objects queued by other tabs. A draw submitted during pause joins the queue and stays paused. Cancel and clear increment the generation; resume cannot resurrect cancelled strokes. Pause/resume are harmless no-ops when inapplicable. Execution is FIFO by object acceptance order, then path order. Simultaneous requests are serialized in lock-acquisition order.

Manual input goes through the same command validation and scene model. It is an observation of completed user ink, so it does not replay through the robot queue or change its pause status. The in-progress mouse preview is temporary; only the server response makes it permanent. Pointer cancellation discards that temporary preview. A generation change during a drag discards it; stale manual submissions return 409.

Each WebSocket client has a one-element mailbox holding the newest full snapshot. Slow clients can skip intermediate revisions, but cannot stall the drawing engine or lose final scene state. The server subscribes before fetching its initial snapshot, and sends monotonically increasing revisions. The browser also ignores stale HTTP/WebSocket snapshots and fetches `/scene` on reconnect. The browser is never authoritative.

Version 1 JSON Schemas live in [schemas/](schemas/), including the exported OpenAPI contract. Generate them with:

```sh
python -m scripts.export_schemas
```

## State, execution, and persistence

```text
Future Astra / JSON UI / other tools
                 ↓
        Pydantic DrawCommand
                 ↓
     Universal DrawingEngine
                 ↓
        DrawingExecutor contract
                 ↓
  VirtualDrawingExecutor (Phase 1)
      ↙                     ↘
SceneStateManager       WebSocketManager
SQLite snapshots        Browser SVG renderer
```

`backend/models.py` defines the shared contract. `geometry.py` deterministically flattens geometry to polylines and interpolates prefixes by arc length. `executor.py` implements the abstract contract and virtual execution. `drawing_engine.py` exposes the activity-independent operations. `scene_state.py` owns authoritative state and SQLite persistence. `main.py` wires lifecycle, HTTP, static assets, and WebSockets. `frontend/` needs no build step or runtime JavaScript dependencies.

Every stroke retains its original **intended geometry**, its **executed_points**, executed/total length, progress, and status (`pending`, `executing`, `completed`, or `cancelled`). Object source distinguishes `engine` from `user`. The scene contains current/pending/completed/cancelled stroke indexes, status (`idle`, `drawing`, `paused`, or `cancelled`), a monotonically increasing revision, and a cancellation generation. A cancelled stroke can have zero, partial, or previously drawn ink; changing its status never erases its executed points.

The virtual worker advances every 20 ms by a deterministic distance budget (`speed × tick duration`). A single asyncio lock serializes drawing, controls, manual observations, and snapshots. There are no detached per-command execution tasks that could revive cancelled work. Strokes crossing a tick boundary consume the remaining distance budget in FIFO order. Persistence time can slow wall-clock animation slightly; delayed ticks never cause a sudden catch-up jump. This is a virtual timing model, not a hard real-time controller.

Every mutation is committed as one SQLite transaction **before** it is published or acknowledged. SQLite writes run in a thread while the executor retains its lock, so the event loop remains available. Full snapshots and one worker keep this implementation simple; very large scenes would benefit from incremental storage and event deltas. Completed trajectories are removed from the executor cache. Curve samples use a maximum control-polygon interval of 0.002 board units (at least 16 Bézier segments and 32 circle segments). Original analytic geometry remains available for a future finer trajectory planner. Rendered ink is the exact confirmed polyline prefix, including an interpolated last point.

## Coordinates and future physical mapping

The board is square in both backend and frontend: SVG `viewBox="0 0 1000 1000"`, with a CSS aspect ratio of 1. Its normalized frame has `(0,0)` at the top-left, `(1,1)` at the bottom-right, X rightward, Y downward. Pointer coordinates account for the displayed board size. Circle proportions remain correct at every viewport size.

For a square physical drawing area of side length `L` meters, use board-local coordinates:

```text
p_board = [L*x, L*y, z_pen, 1]^T
p_robot_base = T_base_from_board @ p_board
```

`T_base_from_board` is a calibrated rigid transform: board origin and orthonormal right/down/normal axes expressed in the robot-base frame. `z_pen` is the measured contact offset; pen-up motion uses a clearance along the normal. Determine the normal sign and pen orientation during calibration. `L = 0.6` gives `(0.5,0.5) → (0.3,0.3)` meters in the board plane.

On a rectangular whiteboard, reserve a square drawing region of side `min(width,height)` and translate its origin to the desired margin. Scaling X and Y by different physical dimensions would turn circles into ellipses; supporting a rectangular logical board later requires changing the declared aspect ratio, distance metric, and viewport together. No kinematics or calibration are implemented in this phase.

## Phase 2: Astra integration

The `AstraAgent` service uses the official OpenAI Python SDK and Responses API. It exposes seven universal drawing/scene tools, maintains durable conversations, returns execution outcomes to the model, and rejects superseded responses. New chat messages and Stop pause drawing immediately; partial ink stays intact. `modify_object` replaces only future plans while preserving historical geometry and ink. Tic-tac-toe moves use exactly the same drawing engine as all other artwork.

Read [the Phase 2 guide](docs/PHASE2.md) for API configuration, strict tool schemas, session endpoints, error handling, game validation, the live acceptance demo, and known limitations. The existing engine details below still apply.

## MuJoCo adapter and future hardware

The MuJoCo adapter is implemented in `backend/simulation/`; see [the simulator guide](docs/MUJOCO.md). For a future hardware adapter, implement `DrawingExecutor` with the same public methods and `Scene` results, and supply it through `create_app(executor_factory=...)`. The factory receives `(SceneStateManager, WebSocketManager, tick_seconds)`. The HTTP routes, DrawingEngine, geometry schemas, examples, persistence representation, and renderer remain unchanged.

Reuse deterministic geometry flattening to produce board-space targets, add pen lifts between strokes, apply the calibrated transform, then send targets to the simulator's controller. Unlike the virtual executor's time-based progress, record **measured pen-down motion** as executed points. Keep original intended geometry separately; do not mark a stroke complete merely because its targets were submitted.

The simulator/hardware adapter owns actuator-specific stepping and stop acknowledgement. Reuse the same serialized state/control ordering and generation invalidation semantics. Slow hardware I/O should run outside the state lock; tag requests/feedback with their generation and reject old completion callbacks. On cancel, stop/acknowledge the actuator and reconcile any final measured ink before admitting new motion. The scene store's invalidation and indexing helpers can be reused, but physical stop semantics require an adapter rather than inheriting the virtual timing loop. Faults and interrupted restarts must preserve observations and require explicit new commands. Real robot safety limits, kinematics, contact/force control, and calibration are future work.

## Tests and verification

```sh
python -m pytest -q
node --check frontend/app.js
```

The pytest suite exercises all twelve requested acceptance cases, plus duplicate/stale commands, concurrency ordering, speed changes, snapshot isolation, zero-length paths, bounded WebSocket mailboxes, and SQLite restart recovery. Unit tests advance a deterministic clock; integration tests run FastAPI lifespan, real worker ticks, SQLite, HTTP validation, and in-process WebSocket reconnects.

An optional browser test is included in `tests/browser_smoke.cjs`. Run it only against a disposable scene: **it clears the board**. With Playwright installed separately and a test server running:

```sh
ASTRA_SCENE_DB=/tmp/astra-browser.sqlite3 python -m uvicorn backend.main:app
# In another terminal (requires npm-installed playwright and a Chromium browser):
node tests/browser_smoke.cjs
```

Optional environment variables: `HEADLESS=0` (visible browser), `SLOW_MO=150` (milliseconds), `ASTRA_URL`, `PLAYWRIGHT_MODULE` (path to an existing installation), `CHROME_PATH`, and `ASTRA_SCREENSHOT`. It checks the interrupted-house workflow, DOM reconstruction after reload, mouse drawing, circle bounds, mobile overflow/aspect ratio, and browser errors.

See [TEST_RESULTS.md](TEST_RESULTS.md) for actual execution results and environment limitations.

### OpenArm sensor boundary

MuJoCo mode now gives Astra camera images, joint readings and fixed board calibration, plus its own drawing-plan memory. It does not expose contact state, exact human stroke geometry or ground-truth robot ink. The SVG execution trace is an encoder estimate; the camera is the observation of actual simulated marks. See [sensor semantics and remaining hardware work](docs/MUJOCO.md). Launch with `python -m scripts.run_mujoco` and open http://127.0.0.1:8000.

### Test camera feedback with chalk wear

Run `python -m scripts.test_chalk_feedback` to try four isolated MuJoCo cases: fresh chalk, shortened chalk with bounded retries, excessive shortening, and wear during a stroke. Before/after camera images and results are saved to `artifacts/chalk-feedback/`. No API key is needed. See [the experiment and its limits](docs/CHALK_FEEDBACK.md); MuJoCo now enables these checks in the normal app as well (`SIM_VERIFY_MARKS=true` by default), with a persistent 0.75 mm correction limit. Failed checks pause drawing and notify Astra; see the guide for live wear settings.

MuJoCo checks each finished stroke before moving on. Confirmed missing strokes are retried immediately, ahead of the remaining drawing. It tries the normal pen-up view first and moves aside only when that view cannot confirm the stroke. Corrections remain bounded.
