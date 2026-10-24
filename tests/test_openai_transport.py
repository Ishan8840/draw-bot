"""Exercise the actual SDK against mocked HTTP, without API keys or network calls."""
import asyncio
import json
import httpx
import pytest
from backend.astra_agent import AstraAgent
from backend.agent_models import ChatMessage
from backend.conversation_store import ConversationStore
from backend.drawing_engine import DrawingEngine
from backend.executor import VirtualDrawingExecutor
from backend.scene_state import SceneStateManager
from backend.websocket_manager import WebSocketManager


def test_official_sdk_responses_wire_format(tmp_path):
    openai=pytest.importorskip('openai',reason='Official SDK must be installed for wire-format test')
    async def run():
        requests=[]
        def handler(request):
            payload=json.loads(request.content)
            requests.append(payload)
            assert request.url.path=='/v1/responses'
            if len(requests)==1:
                output=[{'type':'reasoning','id':'rs_1','summary':[],'encrypted_content':'opaque-reasoning'},
                        {'type':'function_call','id':'fc_1','call_id':'call_scene','name':'get_scene_state','arguments':json.dumps({'object_id':None,'include_geometry':False,'offset':0,'limit':10})}]
            elif len(requests)==2:
                output=[{'type':'function_call','id':'fc_2','call_id':'call_circle','name':'draw_strokes','arguments':json.dumps({'object_id':'circle','paths':[{'stroke_id':'circle-1','geometry':{'type':'circle','center':{'x':.5,'y':.5},'radius':.2}}]})}]
            else:
                output=[{'type':'message','id':'msg_1','role':'assistant','status':'completed','content':[{'type':'output_text','text':'Circle queued.','annotations':[]}]}]
            return httpx.Response(200,json={'id':f'resp_{len(requests)}','object':'response','created_at':0,'status':'completed','model':'gpt-6-astra','output':output,'parallel_tool_calls':False,'tool_choice':'auto','tools':[]})
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sdk=openai.AsyncOpenAI(api_key='test-key-not-real',http_client=http,max_retries=0)
        executor=VirtualDrawingExecutor(SceneStateManager(str(tmp_path/'scene.sqlite3')),WebSocketManager())
        store=ConversationStore(str(tmp_path/'chat.sqlite3'))
        agent=AstraAgent(DrawingEngine(executor),store,client=sdk)
        try:
            sid=(await store.create()).session_id
            await agent.submit(sid,ChatMessage(text='Draw a circle',request_id='one'))
            await asyncio.gather(*list(agent.tasks))
            assert (await store.get(sid)).status=='idle'
            assert len((await executor.get_scene_state()).objects)==1
            assert len(requests)==3
            assert requests[0]['store'] is False
            assert requests[0]['parallel_tool_calls'] is False
            assert requests[0]['include']==['reasoning.encrypted_content']
            assert all(tool['strict'] for tool in requests[0]['tools'])
            continued=requests[1]['input']
            assert any(i.get('type')=='reasoning' and i['encrypted_content']=='opaque-reasoning' for i in continued)
            assert any(i.get('type')=='function_call_output' and i['call_id']=='call_scene' for i in continued)
            assert any(i.get('type')=='function_call_output' and i['call_id']=='call_circle' for i in requests[2]['input'])
        finally:
            await agent.close()
            await executor.close()
            await sdk.close()
    asyncio.run(run())
