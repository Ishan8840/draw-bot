"""Download public speech model assets once; runtime inference is offline.

Run after installing requirements-voice.txt: python -m scripts.setup_voice
"""
import argparse
from pathlib import Path
import urllib.request
from backend.voice.config import VoiceSettings

SILERO_REVISION='1e261b036686cd0017d500ee96acd1c4ba572a9d'
SILERO_URL=f'https://raw.githubusercontent.com/snakers4/silero-vad/{SILERO_REVISION}/src/silero_vad/data/silero_vad.onnx'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--voice',action='append',help='Piper voice ID; may be repeated')
    args=parser.parse_args()
    settings=VoiceSettings.from_env()
    root=Path(settings.model_dir)
    root.mkdir(parents=True,exist_ok=True)
    vad=root/'silero_vad.onnx'
    if not vad.exists():
        temporary=vad.with_suffix('.download')
        urllib.request.urlretrieve(SILERO_URL,temporary)
        temporary.replace(vad)
    from faster_whisper.utils import download_model
    for name in dict.fromkeys([settings.stt_model,settings.fast_stt_model] if settings.interrupt_enabled else [settings.stt_model]):
        if Path(name).is_dir():
            print(f'Using local STT model: {name}')
        else:
            print(f'Caching STT model: {name}')
            download_model(name,cache_dir=str(root/'whisper'))
    if settings.tts_enabled:
        from piper.download_voices import download_voice
        destination=root/'piper';destination.mkdir(exist_ok=True)
        for name in args.voice or [settings.default_voice]:
            download_voice(name,destination)
    print(f'Voice assets cached at {root.resolve()}')
    print('Runtime inference does not download audio models or send audio to an API.')


if __name__=='__main__': main()
