# Local voice — Phase 3

Voice extends the existing Astra session and universal drawing engine. Speech inference runs locally; transcribed instructions and scene context still go to the existing Astra API. No hosted speech service is used.

## Installation

Python 3.11 or 3.12 is recommended for native-wheel compatibility. Preserve your existing environment and OpenAI settings.

```sh
source .venv/bin/activate
pip install -r requirements.txt -r requirements-voice.txt
# Create .env from .env.example only if you do not already have one.
# Edit .env, preserving OPENAI_API_KEY and OPENAI_MODEL.
set -a
source .env
set +a
python -m scripts.setup_voice
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000, enable the microphone, grant permission and wait for Listening. Use port **8000**, not privileged port 800. Environment files are not loaded automatically: export them before setup/start as above. The initial model downloads need internet; speech inference uses cached assets only.

The maintained Piper package is `piper-tts` from [OHF-Voice/piper1-gpl](https://github.com/OHF-Voice/piper1-gpl). This integration uses its [Python API](https://github.com/OHF-Voice/piper1-gpl/blob/main/docs/API_PYTHON.md), `PiperVoice.load()` and `synthesize_wav()`. [Piper 1.8.0](https://pypi.org/project/piper-tts/1.8.0/) was the current release when checked; requirements allow compatible 1.x versions from 1.4.2. Piper is GPL-3.0; individual voices have their own model-card licenses.

Setup caches a pinned official Silero ONNX model, Whisper models and Piper `.onnx`/`.onnx.json` files under `data/voice`. Install another voice with `python -m scripts.setup_voice --voice en_US-amy-medium`, refresh the page and select it before enabling capture. No PyTorch is required for the direct Silero ONNX adapter.

`STT_DEVICE=auto` attempts GPU inference, then falls back to CPU int8 if CUDA initialization fails. Explicit `cuda` reports failure instead. Recent faster-whisper builds require compatible CUDA 12 cuBLAS/cuDNN 9; see [upstream GPU requirements](https://github.com/SYSTRAN/faster-whisper). Piper and VAD use CPU. Two Whisper instances need enough RAM/VRAM.

## Try it

1. Say “Astra, draw a house.”
2. While drawing, say “Wait, stop!”
3. Say “Add a chimney.”
4. Say “Now draw a tic-tac-toe board next to it.”
5. Say “I'll take the center.”

Text and voice share the same session, journal, model tools and scene. Astra still decides geometry and game moves. Local pause/cancel commands return deterministic acknowledgments without waiting for Astra. No new drawing pipeline was introduced.

Mute controls speakers, not capture. Disable Voice responses for input-only operation. Disable microphone releases capture and queued audio without cancelling an already accepted drawing. Change voice, microphone device, silence duration and sensitivity before enabling/re-enabling capture. Disconnects require clicking Enable again; scene and conversation survive.

## Implementation and interruption ordering

AudioWorklet resamples continuous microphone audio to mono 16 kHz PCM16, 512 samples per frame. Each WebSocket binary packet has a little-endian uint32 sequence followed by 1024 PCM bytes. Official [Silero recurrent ONNX inference](https://github.com/snakers4/silero-vad/blob/master/src/silero_vad/utils_vad.py) runs outside the event loop with per-connection state. The utterance state machine includes listening, speaking, transcribing, submitting, complete, superseded and error states. It uses pre-roll, onset debounce, configurable endpoint silence and bounded queues.

The full recognizer defaults to `small.en`; a separate `tiny.en` worker transcribes short snapshots starting at 480 ms and continuing through 1.6 seconds. A configurable lexical classifier handles stop/pause/wait/cancel without a model call. VAD detects speech, not words. Negated commands and incidental phrases such as “draw a sign that says stop” do not match. Short commands may still need final-utterance transcription. This is not token-level streaming.

Speech onset immediately invalidates speaker playback, but does not alone pause drawing. Recognized control commands pause/cancel through the existing engine. Other completed instructions use the shared `handle_user_instruction` entrypoint, pausing and invalidating the previous plan before Astra runs. Partial ink remains. Instruction epochs, utterance IDs and drawing generations reject stale transcription and model tools. Repeated frames/utterances are suppressed; fresh stop commands remain admissible.

Whisper and Piper use independent worker pools; lazy transcription generators are consumed in their workers. Drawing never waits for synthesis. Assistant prose is filtered to short speech without JSON or tool arguments. Astra is prompted to acknowledge alongside its first tool call, allowing speech before drawing finishes.

Playback carries stream, generation and audio IDs. Speech onset, new text, Stop, mute and disconnect invalidate queued and in-flight speech. The browser checks generation after decoding and before playback. Browser energy detection anticipates barge-in, while server Silero provides speech detection. Echo cancellation, noise suppression and transcript similarity checks reduce feedback; headphones help. There is no wake-word gate or guaranteed echo rejection.

## Configuration

Preserve OpenAI settings. `.env.example` lists every voice option.

| Variable | Default / purpose |
| --- | --- |
| `VOICE_ENABLED` | `true`; models load lazily on microphone enable |
| `STT_MODEL`, `FAST_STT_MODEL` | `small.en`, `tiny.en`; configurable names or local directories |
| `STT_DEVICE`, `STT_THREADS`, `STT_LANGUAGE` | `auto`, `2` per recognizer, `en` |
| `TTS_ENABLED`, `TTS_ENGINE`, `PIPER_VOICE` | `true`, `piper`, `en_US-lessac-medium` |
| `VAD_ENABLED` | `true`; required for continuous capture |
| `VAD_SILENCE_MS`, `VAD_MIN_SPEECH_MS`, `VAD_THRESHOLD` | `450`, `96`, `0.5` |
| `VOICE_INTERRUPT_ENABLED` | `true` |
| `VOICE_INTERRUPT_PHRASES` | `"stop,pause,stop drawing,wait,cancel"` |
| `VOICE_PREVIEW_MS`, `VOICE_MAX_UTTERANCE_SECONDS` | `480`, `15` |
| `VOICE_MODEL_DIR` | `data/voice` |

UI silence/threshold settings override defaults for that connection. One microphone session is active per server. A reconnect replaces that session's stream; another active session receives an error. Raw microphone audio is not persisted. Overload or missing frames discard affected audio visibly rather than silently combining incomplete phrases.

## Diagnostics and verification

`GET /voice/config` reports installed voices and missing packages. `WS /voice/ws/{session_id}` accepts a JSON start message, then audio frames and controls. See `frontend/voice.js` for the protocol. `GET /voice/diagnostics` returns the last 200 timing events, also logged under `astra.voice` without transcripts, audio or secrets.

Timings include speech onset/end estimates, fast/full transcription, instruction submission, Astra response, local control response, executor pause/cancel acknowledgment, TTS generation and browser playback-start acknowledgment received by the server. Correlate stream/utterance IDs and monotonic timestamps. Playback acknowledgment includes network delay and is not a measurement of audible speaker latency.

Run `python -m scripts.voice_benchmark --wav stop.wav --output voice-latency.json` with a mono 16-bit 16 kHz recording to measure real local VAD/STT/Piper without a paid model request. For full drawing/API/playback timing, perform the browser walkthrough and save `/voice/diagnostics`.

Run `python -m pytest -q` and `node tests/voice_browser_unit.cjs`. Tests use synthetic input and mocked inference, plus actual scene persistence and agent interruption code. Browser unit tests exercise capture/resampling/playback with mocked browser APIs; they are not real microphone tests.

This implementation environment could not install speech dependencies or host a browser server. Neural inference, microphone quality, GPU compatibility and audible barge-in remain unverified here. No guaranteed latency is claimed. See `TEST_RESULTS.md` for actual results. CPU contention, model size, endpoint silence and speaker arrangement affect responsiveness.

Browser capture requires localhost or HTTPS, microphone permission, AudioWorklet and Web Audio support. This is a trusted-local-machine application, not an authenticated public server.
