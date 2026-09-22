"""Public access regression tests; boundary tests are not protocol handshakes."""
from dataclasses import replace
import httpx
import pytest
from jachi.config import Settings
from jachi.security import HTTPGuard


def config(**changes):
    return replace(Settings(host='0.0.0.0',auth_mode='none',allowed_hosts=('testserver',),
                            allowed_origins=('https://trusted.example',)),**changes)


async def downstream(scope,receive,send):
    await receive()
    await send({'type':'http.response.start','status':200,'headers':[]})
    await send({'type':'http.response.body','body':b'accepted'})


@pytest.mark.parametrize('token',['','old-short-token','x'*40])
@pytest.mark.parametrize('authorization',['','Bearer stale-client-token'])
async def test_no_auth_ignores_legacy_tokens(token,authorization):
    guard=HTTPGuard(downstream,config(api_token=token))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=guard),base_url='http://testserver') as c:
        headers={'Authorization':authorization} if authorization else {}
        r=await c.post('/mcp',json={},headers=headers)
        assert r.status_code==200
        assert 'www-authenticate' not in r.headers


@pytest.mark.parametrize('headers,path,status',[
    ({'Host':'evil.example'},'/mcp',403),
    ({'Origin':'https://evil.example'},'/mcp',403),
    ({'Origin':'https://trusted.example'},'/mcp',200),
    ({},'/mcp?token=fixture',400),
    ({},'/mcp?oc=fixture',400)])
async def test_public_boundaries(headers,path,status):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=HTTPGuard(downstream,config())),base_url='http://testserver') as c:
        assert (await c.post(path,json={},headers=headers)).status_code==status


async def test_public_size_limit():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=HTTPGuard(downstream,config(),max_body=5)),base_url='http://testserver') as c:
        assert (await c.post('/mcp',content=b'123456')).status_code==413


async def test_public_rate_limit_and_health():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=HTTPGuard(downstream,config(),max_requests_per_minute=1)),base_url='http://testserver') as c:
        assert (await c.post('/mcp',json={})).status_code==200
        assert (await c.post('/mcp',json={})).status_code==429
        assert (await c.get('/health')).status_code==200


def test_render_defaults(monkeypatch):
    for name in ('MCP_AUTH_MODE','MCP_API_TOKEN','MCP_PUBLIC_READ_ONLY','ALLOW_EXTERNAL_LLM',
                 'ALLOW_PUBLIC_LLM','MCP_HOST','ALLOWED_HOSTS','ALLOWED_ORIGINS'):
        monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv('RENDER_EXTERNAL_HOSTNAME','fixture.onrender.com')
    monkeypatch.setenv('LAW_OC','fixture-upstream')
    settings=Settings.from_env()
    settings.check_http()
    assert settings.authentication=='none' and settings.tool_profile=='full'
    assert settings.law_oc=='fixture-upstream' and not settings.allow_llm


@pytest.mark.parametrize('mode',['invalid','','oauth'])
def test_invalid_auth_fails(mode):
    with pytest.raises(ValueError):config(auth_mode=mode).check_http()


@pytest.mark.parametrize('token',['','tiny'])
def test_bearer_requires_token(token):
    with pytest.raises(ValueError):config(auth_mode='bearer',api_token=token).check_http()


def test_public_llm_operator_permission():
    with pytest.raises(ValueError):config(allow_llm=True).check_http()
    config(allow_llm=True,allow_public_llm=True).check_http()


def test_profile_independent_of_auth():
    settings=config(public_read_only=True)
    settings.check_http()
    assert settings.authentication=='none' and settings.tool_profile=='read_only'
