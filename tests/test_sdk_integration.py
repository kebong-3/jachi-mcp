"""This file is skipped only when the real MCP SDK is absent.
A fake SDK is intentionally not used to claim protocol compatibility.
"""
import pytest
pytest.importorskip('mcp',reason='실제 MCP SDK 미설치: SDK 통합시험은 설치 환경에서 재실행 필요')
from jachi.server import build_server
from jachi.config import Settings

async def test_real_sdk_lists_tools():
    server=build_server(Settings())
    tools=await server.list_tools()
    names={t.name for t in tools}
    assert {'jachi_review_project','jachi_search_ordinances','jachi_draft_amendment'}<=names
    assert len(names)==16

async def test_real_sdk_resource_and_call():
    server=build_server(Settings())
    resources=await server.list_resources()
    assert resources
    result=await server.call_tool('jachi_health',{})
    assert result

def test_real_sdk_streamable_app():
    server=build_server(Settings(),http=True)
    app=server.streamable_http_app()
    assert app is not None

async def test_public_profile_excludes_private_tools():
    server=build_server(Settings(public_read_only=True))
    names={t.name for t in await server.list_tools()}
    assert 'jachi_review_project' not in names and 'jachi_draft_amendment' not in names
