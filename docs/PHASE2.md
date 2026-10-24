# Phase 2: autonomous drawing agent

The browser now has chat beside the original SVG whiteboard. Astra chooses geometry and placement through seven universal tools. The same executor still draws houses, game grids, lettering, and illustrations. There is no LLM code in the executor and no simulator integration. Local voice was added in Phase 3; see [the voice guide](VOICE.md).

## Setup and launch

From the repository root, with Python 3.11+:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` locally:

```dotenv
OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-6-astra
OPENAI_TIMEOUT_SECONDS=60
```

Then load those variables into the server process and start it:

```sh
set -a
. ./.env
set +a
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000**. Leave the server terminal open. The application does not automatically source `.env`; the commands above do. Alternatively export the variables in your shell. `.env` is ignored by Git. The key is used only by the server's `AsyncOpenAI` client, never returned by `/agent/config`, and never placed in browser storage or frontend code.

A key with access to the configured model is required. The default is exactly `gpt-6-astra`; the application does not silently substitute a different model. Without a key, chat displays a configuration message and returns HTTP 503 on submission, while all Phase 1 JSON drawing tools remain available.

Keep one Uvicorn worker per board. All sessions and browser tabs share the same physical board, so a new instruction in one session supersedes any currently active agent turn in another session. This is a local application without account authentication, not a multi-tenant hosted service.

## Acceptance walkthrough

Watch the whiteboard and the tool entries in chat while sending these messages:

1. **“Draw a small house on the left half of the board.”**
2. **“Add a tree to the right of the house, preserving the house.”**
3. **“Draw a small tic-tac-toe grid in the empty space.”**
4. **“I'll take the center.”**
5. **“Draw a dragon in the remaining space. Use separate semantic objects for its wings.”**
6. While it is drawing: **“Stop! Give it bigger wings, then continue.”**

Every new chat message pauses currently executing strokes before model inference. Astra reads the latest scene, revises the remaining plan, and may explicitly resume. If told only to stop, it should leave the board paused. The **Stop** button always pauses and revokes the active model turn immediately; it does not wait for the OpenAI request. The original Cancel button instead discards pending strokes while keeping ink. Clear board removes virtual ink explicitly.

Artwork and moves come from the model, so this is a qualitative live acceptance demo, not a scripted shape lookup. If the board has insufficient free space, Astra should ask how to proceed rather than erase drawings. For a clearer dragon interruption demonstration, explicitly clear the board first or ask for a small dragon. Look in the scene inspector for unchanged `executed_points` after interruption and for new pending stroke IDs after edits.

Enable **Draw by hand** and **Astra responds to my drawings** to send general board observations automatically after a short idle interval. Astra interprets the raw ink and conversation itself; no special game recognizer or validator is used. These observations can initiate an Astra API request. You can also describe a move in text or speech.

## Architecture

```text
Browser chat / other client
         ↓ POST message (returns 202 promptly)
AstraAgent ───── local conversation + tool receipt journal
         ↓ AsyncOpenAI Responses API
get_scene_state / draw_strokes / modify_object / controls
         ↓ admission token + scene generation + argument validation
Existing DrawingEngine
         ↓
Existing VirtualDrawingExecutor + SceneStateManager
         ↓
SQLite scene + existing WebSocket stream → SVG board
```

New files:

- `astra_agent.py`: turn orchestration, API cancellation, tool dispatch, duplicate suppression, failure recovery.
- `agent_tools.py` / `agent_models.py`: strict function schemas and bounded arguments, derived from shared geometry models.
- `agent_prompt.py`: behavior and scene/ink rules; no drawing templates or fixed move sequences.
- `conversation_store.py`: durable sessions, messages, visible tool traces, request IDs, and tool receipts.
- `scene_summary.py`: compact progress and spatial context, with paginated exact geometry.
- `vector_art.py`: a `VectorArtGenerator` protocol for a future specialized planner. It returns validated `ToolPaths` and has no execution privileges. No such provider is required or instantiated in Phase 2.
- `frontend/chat.js`: persistent session identity, chat submission, tool visibility, Stop, and reconnection.

Changes to Phase 1 are additive: generation-advancing interruption, `modify_object`, guarded control methods, chat routes, and agent invalidation around direct HTTP board mutations. Existing drawing request shapes and endpoints remain accepted. Tick revisions are not used as planning tokens because progress changes them continuously.

## Responses API tool loop

Each turn uses the official Python `AsyncOpenAI` SDK and `client.responses.create`. Function definitions explicitly set `strict: true`, require all object properties, and set `additionalProperties: false`. Optional fields have nullable schemas. Shared discriminated geometry unions are represented by nested `anyOf` schemas with literal type enums; backend Pydantic validation remains authoritative.

The service handles all returned `function_call` items, dispatches tools serially, and sends JSON results in `function_call_output` items using the exact returned **`call_id`**, not the output item's `id`. Both success and validation failures go back to Astra so it can inspect and recover. Each continuation includes all prior output items in that turn, including reasoning items. `store=False` and `include=["reasoning.encrypted_content"]` support this manual continuation strategy. Instructions and tool definitions are supplied on every request. There is no dependency on a remote conversation ID.

The default bounds are 12 model rounds, 48 tool calls, 10,000 output tokens per response, and a 60-second timeout per model request. `parallel_tool_calls=False` reduces conflicting simultaneous mutations, and the dispatcher still handles multiple calls defensively. A timeout, API failure, incomplete response, or exhausted planning budget pauses the board and creates a visible error. Authentication/quota failures get clear sanitized messages; raw SDK exception strings are not displayed or logged.

These mechanics follow the official [function-calling guide](https://developers.openai.com/api/docs/guides/function-calling) and [conversation-state guide](https://developers.openai.com/api/docs/guides/conversation-state). The configured model is described in the official [GPT-6 Astra model reference](https://developers.openai.com/api/docs/models/gpt-6-astra).

## Tools and scene awareness

| Tool | Behavior |
| --- | --- |
| `get_scene_state` | Current semantic objects, executed/incomplete progress, board bounds, conservative free cells, recent user ink; paginate object summaries or exact stroke geometry |
| `draw_strokes` | Append one object using the original engine; at most 32 strokes and 2048 input points per model batch |
| `pause_drawing` | Hold current progress and queued paths |
| `resume_drawing` | Explicitly authorize continuation of the latest paused plan |
| `cancel_drawing` | Cancel queued/current strokes while preserving executed points |
| `clear_board` | Explicitly clear virtual ink and invalidate pending work |
| `modify_object` | Cancel the selected object's old unfinished paths and append replacement future paths with fresh IDs; preserve all prior ink and historical intent |

A scene read is required before **each** model mutation. Editing also requires a detailed read of that object. Model arguments never choose the generation token: the server supplies the admitted turn's generation. Within a valid turn, cancel/clear update that token, and the model must read the scene again before doing more work.

A default context contains the last 24 conversational/interruption events and compact scene summaries for up to 50 objects, with bounds, sources, stroke types, progress, and statuses. Historical messages are clipped to 4000 characters; the current instruction retains its full 8000-character allowance. Large raw point histories are not automatically sent. The scene tool returns up to 10 object summaries or 10 exact strokes per page; geometry pages also have a 10,000-point soft cap, with at least one whole stroke returned. `next_offset` signals additional pages. A coarse 8×8 free-area grid excludes intended/observed bounding boxes; it is a conservative layout aid, not a collision or packing solver.

`modify_object` does not erase anything. Old completed strokes stay completed; old pending/executing strokes become cancelled with their original geometry and partial ink unchanged. New paths are appended as pending and use fresh stroke IDs. Paused execution remains paused until resumed. The new `POST /commands/modify` endpoint uses the existing draw-command shape (`object_id`, `paths`, optional `expected_generation`).

## Interruption and race handling

1. Message admission acquires a short board-operation lock, pauses the executor, and increments the scene generation. This happens before model I/O.
2. The previous turn's stop event is set and its authorization is revoked. Its HTTP request is cancelled separately; admission does not await that cancellation.
3. The new turn receives the latest scene, including its actual partial stroke, pending paths, and statuses.
4. Tool execution checks both turn identity and scene generation, then submits under the same board-operation lock. Drawing/control generation checks also run inside the executor's state lock.
5. Old model responses cannot draw, resume, modify, cancel, or clear a newer board state. Even a transport that delivers a response after cancellation cannot regain authority.

Interruption deliberately retains old pending plans in a paused state so Astra can inspect and revise them. They never resume automatically. Explicit latest-turn resume authorizes retained plans; cancel invalidates them instead. Direct JSON/manual drawing and legacy control endpoints also revoke in-flight model authorization, without changing the established Phase 1 endpoint behavior. A rejected direct command can still revoke the active agent turn; the board geometry remains unchanged.

The admission lock is never held across an OpenAI request. Executor SQLite commits remain serialized and asynchronous as in Phase 1. No robot behavior is implemented.

## Persistence, reconnection, and duplicate prevention

The conversation database defaults to `<scene-database>.chat.sqlite3`, configurable through `ASTRA_CHAT_DB`. SQLite stores sessions, user/assistant messages, interruptions, visible tool call/result events, request IDs, and tool execution receipts. The browser stores only a random session ID in `localStorage`; reloads restore the last 200 events. Earlier events remain in SQLite. Chat polls every 600 ms independently of the existing real-time board WebSocket.

HTTP message requests require `request_id`. Retrying the same ID and text returns the existing session, without starting another model turn. Reusing it with different text returns 409. A `(turn_id, call_id)` receipt is reserved before tool execution and completed afterward. Repeated tool IDs return the recorded result; changed arguments for the same ID fail. Unknown-outcome receipts are not replayed. Scene and conversation databases do not share a transaction, so this is at-most-once admission with conservative crash recovery, not an exactly-once distributed guarantee. On restart, unfinished turns are marked interrupted and never automatically replayed; the scene retains the Phase 1 restart behavior.

Model requests, allowed tool names, call IDs, success/failure outcomes, and interruptions are logged. Logs omit API keys, user message text, model arguments, raw SDK errors, and request headers. Conversation text and tool arguments are intentionally persisted locally for the UI; do not paste secrets into chat.

## Chat API

| Endpoint | Purpose |
| --- | --- |
| `GET /agent/config` | Configured boolean, model name, round limit; no credentials |
| `POST /agent/sessions` | Create a conversation |
| `GET /agent/sessions/{session_id}` | Status, current turn ID, last 200 persisted events |
| `POST /agent/sessions/{session_id}/messages` | `{"text":"Draw a house","request_id":"unique-id"}`; returns 202 after interruption/admission |
| `POST /agent/sessions/{session_id}/stop` | Immediate global board pause and turn revocation |

Session status is `idle`, `thinking`, `interrupted`, or `error`. It is separate from the board's execution status: the agent may be idle while queued strokes continue drawing. Tool calls/results are visible before the whole turn finishes. The message box remains available while thinking and drawing.

Versioned HTTP and tool schemas are exported in `schemas/`; refresh with `python -m scripts.export_schemas`.

## Activity-independent agency

Astra owns interpretation, activity rules, turn-taking, object IDs, placement and geometry. The backend does not identify X/O marks, track game cells, choose moves or enforce game-specific rules. Tic-tac-toe is one possible conversation, not a coded mode. The same observation-to-reasoning-to-tool loop serves arbitrary illustrations and games.

A settled manual ink update starts a turn in the selected conversation with source `board`. Compact context includes bounded previews of the latest user paths; `get_scene_state` provides exact intended/executed geometry with pagination. The agent may act or wait. Its own executed strokes do not recursively start turns. Geometry validation, read-before-write checks, generation fencing, ink preservation and explicit clearing remain backend responsibilities. Activity correctness depends on Astra's reasoning and is not guaranteed by a game validator.

## Verification and limitations

```sh
python -m pytest -q
node --check frontend/app.js
node --check frontend/chat.js
```

In the restricted development sandbox, use `python -m scripts.test_sandbox` to supply asyncio timer wakeups. See [actual test results](../TEST_RESULTS.md) for environments and the SDK transport test.

To watch the optional browser test on an unrestricted local machine, install Playwright separately, start a disposable server/database, and run:

```sh
HEADLESS=0 SLOW_MO=150 node tests/browser_smoke.cjs
```

It clears the board and verifies the original visible drawing workflow. Chat's model/tool loop is tested with mocks; no API key is needed for the automated tests.

Known limits: live Astra illustration quality, live game strategy, and the complete natural-language acceptance walkthrough need a configured API key and live testing. Browser interaction remains unverified in the restricted sandbox. Long conversations use bounded recent history rather than unlimited semantic memory. Free-area estimation is coarse; interpretation of manual ink depends on the model. There is no per-object physical erase, vision model, kinematics, MuJoCo, account authentication, or multi-worker coordination.
