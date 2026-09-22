"""Real SDK HTTP integration; never replace missing SDK with a fake."""
import pytest
pytest.importorskip('mcp',reason='Actual MCP SDK required')
from jachi.config import Settings
from jachi.server import build_server
from jachi.security import HTTPGuard


async def test_public_full_registers_all_tools():
    server=build_server(Settings(auth_mode='none',public_read_only=False),http=True)
    names={t.name for t in await server.list_tools()}
    assert len(names)==16
    assert {'jachi_review_project','jachi_draft_amendment','jachi_review_provided_text'}<=names


async def test_real_http_initialize_list_call_without_token():
    import httpx
    settings=Settings(host='0.0.0.0',auth_mode='none',api_token='obsolete-token',
                      allowed_hosts=('testserver',),allowed_origins=('https://trusted.example',))
    server=build_server(settings,http=True)
    app=server.streamable_http_app()
    guard=HTTPGuard(app,settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guard),base_url='http://testserver',
                                     headers={'Accept':'application/json, text/event-stream'}) as c:
            health=await c.get('/health')
            assert health.status_code==200 and health.json()['authentication']=='none'
            initialized=await c.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'initialize',
                'params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'public-test','version':'1'}}})
            assert initialized.status_code==200,initialized.text
            c.headers['MCP-Protocol-Version']=initialized.json()['result']['protocolVersion']
            listed=await c.post('/mcp',json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}})
            assert listed.status_code==200,listed.text
            assert len(listed.json()['result']['tools'])==16
            called=await c.post('/mcp',json={'jsonrpc':'2.0','id':3,'method':'tools/call',
                    'params':{'name':'jachi_health','arguments':{}}})
            assert called.status_code==200,called.text
            assert not called.json()['result'].get('isError',False)
            assert 'obsolete-token' not in called.text
