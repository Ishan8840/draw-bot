"""Run from repository root: python -m scripts.export_schemas."""
import json
from pathlib import Path
from backend.main import create_app
from backend.models import ControlCommand, DrawCommand, ModifyCommand, Scene, SceneEvent, SpeedCommand
from backend.agent_models import ChatMessage, ChatSession
from backend.agent_tools import tool_definitions


def schemas():
    result = {model.__name__: model.model_json_schema() for model in (DrawCommand, ModifyCommand, ControlCommand, SpeedCommand, Scene, SceneEvent, ChatMessage, ChatSession)}
    result['AstraTools'] = tool_definitions()
    result['openapi'] = create_app().openapi()
    return result


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1] / 'schemas'
    root.mkdir(exist_ok=True)
    for name, schema in schemas().items():
        (root / f'{name}.json').write_text(json.dumps(schema, indent=2) + '\n')
