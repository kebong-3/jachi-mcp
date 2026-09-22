"""Run after installing requirements.txt. Real SDK stdio/HTTP handshake, no legal API call."""
import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

async def check_streams(reader,writer):
    from mcp import ClientSession
    async with ClientSession(reader,writer,read_timeout_seconds=timedelta(seconds=90)) as session:
        initialized=await session.initialize()
        tools=await session.list_tools()
        health=await session.call_tool('jachi_health',{})
        result={'protocol_version':initialized.protocolVersion,'server_name':initialized.serverInfo.name,
                'tools':[t.name for t in tools.tools],
                'health':health.model_dump(mode='json'),'network_law_calls':0}
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if health.isError:raise RuntimeError('Health tool reported an error')

async def main(args):
    if args.url:
        from mcp.client.streamable_http import streamablehttp_client
        headers={}
        if os.getenv('MCP_API_TOKEN'):headers['Authorization']='Bearer '+os.environ['MCP_API_TOKEN']
        async with streamablehttp_client(args.url,headers=headers) as (reader,writer,_):
            await check_streams(reader,writer)
    else:
        from mcp import StdioServerParameters
        from mcp.client.stdio import stdio_client
        root=Path(__file__).resolve().parents[1]
        params=StdioServerParameters(command=sys.executable,args=[str(root/'jachibeopgyu_mcp.py')],
                                    env=dict(os.environ),cwd=str(root))
        async with stdio_client(params) as (reader,writer):await check_streams(reader,writer)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--url',default='')
    args=parser.parse_args()
    asyncio.run(main(args))
