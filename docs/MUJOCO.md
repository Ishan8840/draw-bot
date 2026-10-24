# OpenArm drawing in MuJoCo

The existing agent, conversation, voice controls, manual observations, tool schemas and scene persistence now work with an OpenArm MuJoCo executor. Astra chooses all activities, interpretations, geometry and placement. The simulator contains no game logic, symbol recognizer or second reasoning model.

## Install and run

From the repository root, with Python 3.11+:

```sh
source .venv/bin/activate
pip install -r requirements.txt -r requirements-mujoco.txt
# Export your existing OpenAI settings; the key stays on the server.
# If you maintain a shell-compatible .env:
set -a
source .env
set +a
python -m scripts.run_mujoco
```

If you do not have `.env`, copy `.env.example` once, then set `OPENAI_API_KEY` and retain your `OPENAI_MODEL` (default `gpt-6-astra`). You can run the structured JSON examples without a key. Do not overwrite an existing environment file. The launcher selects MuJoCo even if `.env` says `ASTRA_EXECUTOR=virtual`.

Open **http://127.0.0.1:8000**. The live arm view appears above the editable board. Select Arm view or Top view. Drawing commands, chat, manual drawing, pause, resume, cancel and clear use the same interfaces as before. The lower SVG board shows encoder-estimated robot traces and manual input, not verified ink. The camera shows simulated physical marks independently; use it to check whether drawing actually occurred.

The launcher uses a separate `data/openarm-scene.sqlite3` and matching conversation database, preserving the existing virtual whiteboard. To intentionally use another scene:

```sh
python -m scripts.run_mujoco --database data/my-simulation.sqlite3 --port 8000
```

Alternatively, `ASTRA_EXECUTOR=mujoco python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000` uses the normal database selection. Use one worker/server per scene. Stop the previous server before starting a new one on port 8000.

For speech, install `requirements-voice.txt` and run `python -m scripts.setup_voice` as described in [the voice guide](VOICE.md). Voice and text use the same agent and MuJoCo drawing tools; the simulator does not require speech models. This implementation environment has not performed a live microphone or neural speech test.

## Try the interaction

1. Open Structured commands & examples and execute a house, or ask Astra to draw one.
2. Watch the pen lift, move to a stroke, lower, trace, and retract. Pen-up movement produces no connecting ink.
3. Pause halfway through a stroke. Both simulation time and ink stop.
4. Cancel, then submit a circle. The interrupted house stays on the board while the arm draws the circle.
5. Ask Astra for an activity, enable Draw by hand, and leave Astra responds to my drawings checked. Your manually drawn paths become observations in that conversation; Astra decides what to do next through the same tools.
6. With local speech configured, use the microphone and say “Stop” during execution. Local recognition invokes the same pause/cancel path.

Automatic board reactions use a short idle interval between submitted pen strokes. That interval is not proof that a drawing or move is complete; Astra decides whether to act or wait. There are no coded game rules.

## What is simulated

`models/openarm/drawing.xml` is derived from the **official OpenArm v2 right arm**, using Enactic's original joint frames, seven hinge axes, joint limits, inertias, actuator force limits, and visual/collision meshes. The source is [enactic/openarm_mujoco](https://github.com/enactic/openarm_mujoco), pinned at commit `a3c8a57ec15d385e3c1dbd3fd090ee486b7f258c`. The vendored assets and Apache-2.0 license are included; see [model provenance](../models/openarm/SOURCE.md).

This fixture uses one seven-DOF OpenArm, with its fingers rigidly closed around a transverse marker. A fixed pen attachment adds no robot joints. The arm itself positions and lifts the pen. The second arm is omitted. A static mount, board, lights and cameras complete the scene. The former generic SCARA model has been removed.

Damped-Jacobian inverse kinematics solves both pen position and wrist orientation on a scratch MuJoCo state, enforcing the original joint limits. The resulting targets drive position actuators through bounded Cartesian/joint setpoints. Gravity/Coriolis feedforward is applied through bounded actuator commands (not external forces), so upstream motor force limits still bound total actuator torque. Simulation-specific servo gains support contact tracing. Actual dynamics are integrated; drawing does not teleport between IK configurations. A bounded planar endpoint correction removes contact-induced servo bias before the pen retracts.

The square board is horizontal, **0.18 m per side**, placed inside the tested OpenArm workspace:

```text
world_x = 0.20 + 0.18 * board_x
world_y = -0.07 - 0.18 * board_y
surface_z = 0.14 m
```

The sphere nib radius is 3 mm. Its contact target is z=0.1427 m and its lifted target is z=0.175 m. The marker is rigidly mounted across the gripper so a fixed wrist orientation points it downwards. This is a drawing fixture using the official robot model, not a calibrated digital twin of your particular hardware. Upstream arm collision geometry remains active, as does pen/board contact. There is no general collision-avoiding planner or hardware safety controller.

Control uses joint positions/velocities and forward kinematics from the nominal robot model. Pen-down is estimated against the fixed calibrated plane; there is no contact sensor input. Gravity/Coriolis compensation is computed on a separate nominal model using those joint readings. Neither simulator site positions nor simulator bias forces are feedback inputs.

For API compatibility, `executed_points` and `executed_length` remain present but represent **estimated pen traces**, with scene `execution_observation="encoder_estimate_unverified"`. Completion means endpoint convergence and retraction, not verified ink. A dry marker can complete its motion without producing marks.

The hidden simulation environment uses physical contacts to deposit marks for rendering, just as contact determines marks in the real world. These marks are stored separately in the SQLite `simulation_environment` table. Their coordinates and contact states are never sent to Astra or used to control movement. The camera renderer uses these environment marks rather than estimated trajectories. Mouse strokes inject marks into the simulated environment; their geometry and IDs are hidden from Astra in this mode.

Existing databases from the previous contact-feedback implementation have no separate environment-ink record. Old robot traces remain in execution memory but are not invented as camera observations. Use a new simulation database for a clean demo. New environment marks persist across restart and cancellation; Clear erases them in the simulator. A real robot would need a separate erasing procedure.

Requested speed is capped at 0.25 normalized units/s (**0.045 m/s**), with additional slowing from servo tracking and travel. The controller does not guarantee wall-clock real-time operation: rendering, persistence and hardware performance can slow simulation.

## Execution and interruptions

`SceneDrawingExecutor` holds shared queue admission, validation, locking, persistence, modification and cancellation behavior. `VirtualDrawingExecutor` supplies ideal motion. `MuJoCoDrawingExecutor` supplies OpenArm dynamics feedback. The default `VerifiedMuJoCoExecutor` adds interruptible before/after camera checks and bounded missing-mark retries; see [live chalk feedback](CHALK_FEEDBACK.md). Astra and activity decisions do not enter either motion controller.

MuJoCo dynamics live on a dedicated worker thread. A separate renderer worker owns its own model/data and OpenGL context, using joint snapshots and private environment ink. Physics batches are short (20 ms nominal simulation time; 50 ms maximum per call). A scene lock serializes those batches and their measured feedback against pause/cancel/clear. Server I/O and model/speech inference remain asynchronous. A control waits for an in-flight batch and its persistence, not an Astra API response. Rendering does not hold the scene lock or physics worker, so a slow frame does not block Stop. The browser polls serially and the server caches recent frames.

Pause freezes simulation time at a committed feedback boundary. Cancel and clear discard motion cursors and advance drawing generations. Cancellation does not erase existing ink; new commands first lift and reposition. Clear removes the virtual ink and permits reuse of IDs, with a fresh controller cursor. Pending strokes are cancelled on restart; the robot starts lifted at home, and saved ink is restored. A simulator fault pauses execution and revokes older model generations; use Cancel and a new instruction to discard a faulted plan.

These are simulator stop semantics. Setting a simulation velocity to zero at a reset is not a real robot braking procedure. A future hardware adapter needs measured stop acknowledgment, braking/contact behavior, calibration, collision constraints and independent safety limits.

## Visual observations and APIs

With MuJoCo, each Astra request receives a rendered 640×480 JPEG from the fixed overhead camera, joint positions/velocities, and the fixed board calibration. The camera includes the arm and possible occlusion during drawing. Before a paused/idle agent observation, the executor lifts the pen to 0.19 m, then parks it beside the board at world (0.074, -0.16, 0.19) using bounded actuator motion. The initial pose is also parked. The interrupted arc cursor and partial trace are retained; resume lifts/travels back to that cursor before continuing. Parking runs in short batches and aborts on a changed generation or resumed drawing. Fresh agent frames bypass the display cache. Active drawing is not stopped merely to obtain an image. Own planned geometry and estimated motion progress remain accessible as action memory; human geometry, exact ink coordinates, contact state, and computed ground-truth free-space cells are excluded from all agent scene/tool responses. Astra interprets marks and available space visually. The same universal drawing tools remain in use.

Camera observations are mandatory in this mode (even if `ASTRA_BOARD_IMAGES=false`). A rendering failure fails the agent request rather than falling back to privileged vector geometry. Virtual-only mode retains its previous optional ideal raster for compatibility. Frames may be cached for up to 0.2 seconds and image/joint snapshots are not a synchronized hardware sensor packet. No real-camera acquisition, calibration estimator, visual ink verification algorithm, or continuous camera-change trigger is implemented yet: mouse input still supplies the generic board-change event. Real deployment needs these adapters, plus calibrated pen/tool geometry and validation of the clear-view travel path on hardware.

The existing model is unchanged. Images use the documented Responses `input_image` data-URL format; see [official OpenAI image input documentation](https://developers.openai.com/api/docs/guides/images-vision?api-mode=responses). No image-generation service or additional reasoning model is used.

Additional read-only endpoints:

| Endpoint | Result |
| --- | --- |
| `GET /simulation/status` | Executor type, phase, simulation time, joint positions, estimated pen position (no contact reading), tracking error, board transform, faults and scene revision/generation |
| `GET /simulation/frame?camera=overview` | JPEG from the MuJoCo offscreen renderer |
| `GET /simulation/frame?camera=top` | Top camera JPEG |

The UI polls the frame serially, never queues stale frame requests, and releases image URLs. Rendered trajectories can be subsampled in very large scenes, without altering stored ink. The usual `/scene`, `/status`, `/commands/*`, `/ws`, agent and voice routes remain available.

## Graphics setup

Linux defaults to `MUJOCO_GL=egl` for offscreen rendering, including software Mesa when supported. A working EGL/OpenGL driver is required for images, but not for physics. Typical Ubuntu libraries are `libegl1` and `libgl1-mesa-dri`. You can try `MUJOCO_GL=osmesa` with `libosmesa6`, or `MUJOCO_GL=glfw` with a usable desktop display. On macOS/Windows the default is GLFW. Set the variable before Python imports MuJoCo. A rendering error is reported in the UI and `/simulation/status`; drawing controls remain usable.

MuJoCo's maintained Python runtime uses `mj_step` for dynamics and `Renderer` for offscreen images. See [MuJoCo Python documentation](https://mujoco.readthedocs.io/en/stable/python.html) and [simulation documentation](https://mujoco.readthedocs.io/en/stable/programming/simulation.html). Tested here with MuJoCo 3.17.0, NumPy 2.3.5 and Pillow 13.0.0 on Python 3.11.15.

## Verification and reproducible demo

```sh
python -m pytest -q
python -m scripts.smoke_mujoco --gif
```

The smoke demo uses an isolated temporary scene and real OpenArm MuJoCo physics. It pauses/cancels a partially drawn house, then draws a circle while verifying preserved ink. It writes `artifacts/mujoco/overview.jpg`, `board-top.jpg`, `interruption-demo.gif`, `scene.json` and `metrics.json`. The GIF is accelerated/sampled, not a real-time performance claim. The JSON records simulation/wall time, measured tracking error and pause acknowledgment time.

In restricted sandboxes only, `python -m scripts.test_sandbox` adds timer wakeups, and the smoke command accepts `--sandbox-heartbeat`. Ordinary local installations do not need these options.

The tests use real dynamics and real headless rendering. Agent responses are mocked in integration tests; no live API key was available here. TCP binding was denied by the execution sandbox, so the live browser/server acceptance walkthrough remains to be run locally. In-process FastAPI/WebSocket tests and standalone MuJoCo renders are executed here. See [actual test results](../TEST_RESULTS.md).
