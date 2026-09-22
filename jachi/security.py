"""Local/HTTP boundary controls, independent of MCP SDK for testability.
Static bearer != OAuth2.1. Use an OAuth gateway where required by the client.
"""
from __future__ import annotations
import hmac
import json
import re
import time
from collections import deque
from urllib.parse import parse_qs
from .config import Settings


def allowed(value:str, patterns:tuple[str,...]) -> bool:
    value=value.lower()
    for p in patterns:
        p=p.lower()
        if p.endswith(':*'):
            if re.fullmatch(re.escape(p[:-2])+r'(?::\d{1,5})?',value):return True
        elif value==p:return True
    return False


class HTTPGuard:
    def __init__(self,app,settings:Settings,max_body:int=1800000,max_requests_per_minute:int=60):
        settings.check_http()
        self.app=app;self.settings=settings;self.max_body=max_body
        self.limit=max_requests_per_minute;self.requests=deque()

    async def __call__(self,scope,receive,send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        headers={k.lower():v for k,v in scope.get('headers',[])}
        async def reject(code,msg):
            data=json.dumps({'error':msg},ensure_ascii=False).encode()
            await send({'type':'http.response.start','status':code,
                        'headers':[(b'content-type',b'application/json; charset=utf-8'),(b'cache-control',b'no-store')]})
            await send({'type':'http.response.body','body':data})
        if not allowed(headers.get(b'host',b'').decode('latin-1'),self.settings.allowed_hosts):
            return await reject(403,'허용되지 않은 Host')
        origin=headers.get(b'origin')
        if origin and not allowed(origin.decode('latin-1'),self.settings.allowed_origins):
            return await reject(403,'허용되지 않은 Origin')
        query=parse_qs(scope.get('query_string',b'').decode('latin-1'),keep_blank_values=True)
        if any(k.lower() in {'oc','api_key','apikey','token','access_token','authorization'} for k in query):
            return await reject(400,'인증정보를 URL에 넣지 마세요. 서버 환경변수·Authorization 헤더를 사용하세요.')
        if scope.get('path')=='/health' and scope.get('method')=='GET':
            return await self.app(scope,receive,send)
        if self.settings.api_token:
            received=headers.get(b'authorization',b'')
            expected=('Bearer '+self.settings.api_token).encode('utf-8')
            if not hmac.compare_digest(received,expected):return await reject(401,'인증 필요')
        now=time.monotonic()
        while self.requests and self.requests[0]<=now-60:self.requests.popleft()
        if len(self.requests)>=self.limit:return await reject(429,'서버 요청 한도 초과')
        self.requests.append(now)
        try:
            length=int(headers.get(b'content-length',b'0'))
        except ValueError:return await reject(400,'잘못된 Content-Length')
        if length<0 or length>self.max_body:return await reject(413,'요청 크기 한도 초과')
        messages=[];size=0
        while True:
            event=await receive()
            if event['type']=='http.disconnect':return
            messages.append(event);size+=len(event.get('body',b''))
            if size>self.max_body:return await reject(413,'요청 크기 한도 초과')
            if not event.get('more_body',False):break
        i=0
        async def replay():
            nonlocal i
            if i<len(messages):
                event=messages[i];i+=1;return event
            return await receive()
        return await self.app(scope,replay,send)
