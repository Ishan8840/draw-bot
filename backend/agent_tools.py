"""Strict Responses API function schemas, derived from shared geometry models."""
from copy import deepcopy
from .agent_models import NoArguments, SceneQuery, ToolPaths

TOOL_MODELS = {
    'get_scene_state': SceneQuery, 'draw_strokes': ToolPaths, 'modify_object': ToolPaths,
    'pause_drawing': NoArguments, 'resume_drawing': NoArguments,
    'cancel_drawing': NoArguments, 'clear_board': NoArguments,
}
DESCRIPTIONS = {
    'get_scene_state':'Read current semantic objects, actual ink, incomplete paths, free area and recent user ink. With object_id and include_geometry=true, offset/limit paginate exact strokes. Otherwise paginate object summaries.',
    'draw_strokes':'Append one semantic object with at most 32 strokes / 2048 input points. It queues asynchronously; success does not mean execution is complete. Use fresh object and globally unique stroke IDs.',
    'modify_object':'Replace the remaining plan of an existing object with fresh stroke IDs. Old pending/executing paths become cancelled; all old geometry and executed ink remain. Supply ONLY additional ink; never claim ink was erased. Paused execution stays paused.',
    'pause_drawing':'Pause the current execution without erasing ink or pending paths.',
    'resume_drawing':'Explicitly resume the currently paused valid plan.',
    'cancel_drawing':'Invalidate all current/pending strokes and preserve executed ink.',
    'clear_board':'Erase the virtual board and invalidate pending work. Only call when the user explicitly requests clearing.',
}


def strict_schema(model):
    schema=deepcopy(model.model_json_schema())
    def visit(node):
        if isinstance(node,list):
            return [visit(item) for item in node]
        if not isinstance(node,dict):
            return node
        node={key:visit(value) for key,value in node.items() if key not in ('title','default','discriminator')}
        if 'oneOf' in node:
            node['anyOf']=node.pop('oneOf')
        if 'const' in node:
            node['enum']=[node.pop('const')]
        if node.get('type')=='object':
            node['additionalProperties']=False
            node['required']=list(node.get('properties',{}))
        return node
    return visit(schema)


def tool_definitions():
    return [{'type':'function','name':name,'description':DESCRIPTIONS[name],
             'parameters':strict_schema(model),'strict':True} for name,model in TOOL_MODELS.items()]
