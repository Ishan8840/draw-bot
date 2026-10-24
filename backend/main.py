import math
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
import anyio
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from .drawing_engine import DrawingEngine
from .executor import CommandConflict, VirtualDrawingExecutor
from .models import ControlCommand, DrawCommand, ExecutionStatus, Scene, SceneEvent, SpeedCommand
from .models import ModifyCommand
from .agent_models import AgentConfiguration, ChatMessage, ChatSession
from .astra_agent import AgentUnavailable, AstraAgent
from .conversation_store import ConversationStore
from .scene_state import SceneStateManager
from .websocket_manager import WebSocketManager
from .voice.config import VoiceSettings
from .voice.controller import VoiceHub

ROOT = Path(__file__).resolve().parent.parent


def create_app(database: str | None = None, tick_seconds=0.02, executor_factory=None,
               agent_client=None, conversation_database=None, voice_settings=None, voice_services=None):
    @asynccontextmanager
    async def lifespan(app):
        agent_log = logging.getLogger('astra.agent')
        if not agent_log.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
            agent_log.addHandler(handler)
        agent_log.setLevel(logging.INFO)
        voice_log = logging.getLogger('astra.voice')
        if not voice_log.handlers:
            voice_log.addHandler(logging.StreamHandler())
        voice_log.setLevel(logging.INFO)
        events = WebSocketManager()
        scene_database = database or os.getenv("ASTRA_SCENE_DB", str(ROOT / "data/scene.sqlite3"))
        state = SceneStateManager(scene_database)
        factory = executor_factory
        if factory is None:
            mode = os.getenv('ASTRA_EXECUTOR', 'virtual')
            if mode == 'mujoco':
                if os.getenv('SIM_VERIFY_MARKS','true').lower()=='true':
                    from .simulation.verified_executor import VerifiedMuJoCoExecutor
                    factory = VerifiedMuJoCoExecutor
                else:
                    from .simulation.executor import MuJoCoDrawingExecutor
                    factory = MuJoCoDrawingExecutor
            elif mode == 'virtual':
                factory = VirtualDrawingExecutor
            else:
                raise ValueError('ASTRA_EXECUTOR must be virtual or mujoco')
        executor = factory(state, events, tick_seconds)
        app.state.engine = DrawingEngine(executor)
        app.state.events = events
        chat_database = conversation_database or os.getenv('ASTRA_CHAT_DB') or (':memory:' if scene_database==':memory:' else scene_database+'.chat.sqlite3')
        app.state.agent = AstraAgent(app.state.engine, ConversationStore(chat_database), client=agent_client,
                                     timeout=float(os.getenv('OPENAI_TIMEOUT_SECONDS','60')))
        app.state.voice = VoiceHub(app.state.agent, voice_settings or VoiceSettings.from_env(), services=voice_services)
        try:
            await executor.start()
            yield
        finally:
            await app.state.voice.close()
            await app.state.agent.close()
            await executor.close()

    app = FastAPI(title="Astra Robot Artist", version="1.0.0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request, exc):
        # Python's JSON decoder accepts NaN/Infinity. Reject them with a valid
        # JSON 422 response instead of echoing non-finite values into JSONResponse.
        def safe(value):
            if isinstance(value, float) and not math.isfinite(value):
                return str(value)
            if isinstance(value, dict):
                return {key: safe(item) for key, item in value.items()}
            if isinstance(value, list):
                return [safe(item) for item in value]
            return value
        return JSONResponse(status_code=422, content={"detail": safe(jsonable_encoder(exc.errors()))})

    @app.exception_handler(CommandConflict)
    async def conflict_handler(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(AgentUnavailable)
    async def unavailable_handler(request, exc):
        return JSONResponse(status_code=503, content={'detail': str(exc)})

    @app.get('/agent/config', response_model=AgentConfiguration)
    async def agent_config():
        return app.state.agent.configuration()

    @app.post('/agent/sessions', response_model=ChatSession)
    async def create_session():
        return await app.state.agent.store.create()

    async def require_session(session_id):
        from fastapi import HTTPException
        try:
            return await app.state.agent.store.get(session_id)
        except KeyError:
            raise HTTPException(404, 'Session not found') from None

    @app.get('/agent/sessions/{session_id}', response_model=ChatSession)
    async def get_session(session_id: str):
        return await require_session(session_id)

    @app.post('/agent/sessions/{session_id}/messages', response_model=ChatSession, status_code=202)
    async def chat(session_id: str, message: ChatMessage):
        await require_session(session_id)
        return await app.state.agent.handle_user_instruction(session_id,message.text,message.source,message.request_id)

    @app.get('/voice/config')
    async def voice_config():
        return app.state.voice.configuration()

    @app.get('/voice/diagnostics')
    async def voice_diagnostics():
        return {'clock':'server monotonic/wall time; playback is browser acknowledgement receipt',
                'measurements':app.state.voice.diagnostics.snapshot()}

    @app.websocket('/voice/ws/{session_id}')
    async def voice_socket(ws: WebSocket, session_id: str):
        await app.state.voice.websocket(ws,session_id)

    @app.post('/agent/sessions/{session_id}/stop', response_model=ChatSession)
    async def stop_agent(session_id: str):
        await require_session(session_id)
        return await app.state.agent.stop(session_id)

    @app.post("/commands/draw", response_model=Scene)
    async def draw(command: DrawCommand):
        return await app.state.agent.external_command('draw', command)

    @app.post("/commands/manual", response_model=Scene)
    async def manual(command: DrawCommand, session_id: str | None = None):
        """Record user ink already observed on the virtual board, without replay."""
        try:
            return await app.state.agent.external_command('manual', command, reaction_session=session_id)
        except KeyError:
            raise HTTPException(status_code=404, detail='Session not found')

    @app.post('/commands/modify', response_model=Scene)
    async def modify(command: ModifyCommand):
        return await app.state.agent.external_command('modify', command)

    @app.post("/commands/pause", response_model=Scene)
    async def pause(command: ControlCommand = ControlCommand()):
        return await app.state.agent.external_command('pause')

    @app.post("/commands/resume", response_model=Scene)
    async def resume(command: ControlCommand = ControlCommand()):
        return await app.state.agent.external_command('resume')

    @app.post("/commands/cancel", response_model=Scene)
    async def cancel(command: ControlCommand = ControlCommand()):
        return await app.state.agent.external_command('cancel')

    @app.post("/commands/clear", response_model=Scene)
    async def clear(command: ControlCommand = ControlCommand()):
        return await app.state.agent.external_command('clear_board')

    @app.post("/commands/speed", response_model=Scene)
    async def speed(command: SpeedCommand):
        return await app.state.engine.executor.set_speed(command.speed)

    @app.get("/scene", response_model=Scene)
    async def scene():
        return await app.state.engine.get_scene_state()

    @app.get("/status", response_model=ExecutionStatus)
    async def status():
        return await app.state.engine.executor.get_status()

    @app.get('/simulation/status')
    async def simulation_status():
        executor = app.state.engine.executor
        if not hasattr(executor, 'simulation_status'):
            return {'backend':'virtual','enabled':False}
        return {'enabled':True, **await executor.simulation_status()}

    @app.get('/simulation/frame')
    async def simulation_frame(camera: Literal['overview','top'] = 'overview'):
        executor = app.state.engine.executor
        if not hasattr(executor, 'frame'):
            raise HTTPException(409, 'Start with ASTRA_EXECUTOR=mujoco to view the simulator')
        try:
            frame = await executor.frame(camera)
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        return Response(frame,media_type='image/jpeg',headers={'Cache-Control':'no-store'})

    @app.websocket("/ws")
    async def websocket(ws: WebSocket):
        await ws.accept()
        queue = app.state.events.subscribe()
        async def send():
            # Subscribe before snapshot; revision filtering avoids duplicates and races.
            initial = await app.state.engine.get_scene_state()
            await ws.send_json(SceneEvent(scene=initial).model_dump(mode="json"))
            revision = initial.revision
            while True:
                event = await queue.get()
                if event["scene"]["revision"] > revision:
                    await ws.send_json(event)
                    revision = event["scene"]["revision"]

        async def receive():
            while True:
                await ws.receive_text()

        try:
            async with anyio.create_task_group() as group:
                async def send_until_disconnect():
                    try:
                        await send()
                    except WebSocketDisconnect:
                        pass
                    finally:
                        group.cancel_scope.cancel()

                group.start_soon(send_until_disconnect)
                try:
                    await receive()
                except WebSocketDisconnect:
                    pass
                finally:
                    group.cancel_scope.cancel()
        finally:
            app.state.events.unsubscribe(queue)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(ROOT / "frontend/index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "frontend"), name="static")
    app.mount("/examples", StaticFiles(directory=ROOT / "examples"), name="examples")
    app.mount("/schemas", StaticFiles(directory=ROOT / "schemas"), name="schemas")
    return app


app = create_app()
