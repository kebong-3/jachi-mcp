"""One-shot, locally persisted version snapshots. Scheduling is an operator action.
Failures never erase or advance a prior successful snapshot. No background worker is started.
"""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from datetime import date,datetime
from zoneinfo import ZoneInfo
from .models import Document,DocumentRef,digest,utcnow
from .client import LawClient,UpstreamError
from .analysis import diff_documents,amendment_impact

class SnapshotStore:
    def __init__(self,path:str):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path,timeout=15)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS snapshots (
            entity_key TEXT NOT NULL, content_hash TEXT NOT NULL, version TEXT NOT NULL,
            collected_at TEXT NOT NULL, document_json TEXT NOT NULL,
            PRIMARY KEY(entity_key,content_hash,version))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS heads (
            entity_key TEXT PRIMARY KEY,content_hash TEXT NOT NULL,version TEXT NOT NULL)''')
        self.db.commit()
    def __enter__(self):return self
    def __exit__(self,*a):self.db.close()
    def latest(self,key:str)->Document|None:
        row=self.db.execute('''SELECT s.document_json FROM heads h JOIN snapshots s ON
              s.entity_key=h.entity_key AND s.content_hash=h.content_hash AND s.version=h.version
              WHERE h.entity_key=?''',(key,)).fetchone()
        return Document.model_validate_json(row[0]) if row else None
    def record(self,key:str,document:Document)->dict:
        if document.source_state not in {'live','cache'}:
            raise ValueError('공식 조회 성공 자료만 감시 기준으로 저장합니다. 오래된 캐시·제공문서 제외')
        # Immediate transaction prevents concurrent writers from comparing against an obsolete head.
        try:
            self.db.execute('BEGIN IMMEDIATE')
            previous=self.latest(key)
            unchanged=previous and previous.content_hash==document.content_hash and previous.version==document.version
            if unchanged:
                self.db.commit()
                return {'status':'unchanged','entity_key':key,'version':document.version}
            delta=diff_documents(previous,document) if previous else None
            self.db.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?)',
               (key,document.content_hash,document.version,utcnow(),document.model_dump_json()))
            self.db.execute('INSERT INTO heads VALUES (?,?,?) ON CONFLICT(entity_key) DO UPDATE SET content_hash=excluded.content_hash,version=excluded.version',
               (key,document.content_hash,document.version))
            self.db.commit()
            return {'status':'changed' if previous else 'baseline_created','entity_key':key,'version':document.version,
                    'diff':delta,'previous_document':previous.model_dump() if previous else None,
                    'warning':'최초 실행은 기준 저장이며 과거 개정 전체를 감지했다는 뜻이 아닙니다.'}
        except Exception:
            self.db.rollback();raise


def watch_key(ref:DocumentRef)->str:
    if ref.mst or not ref.document_id:
        raise ValueError('변경 감시는 현재값을 조회할 document_id만 지정합니다. 고정 MST는 감시에 사용할 수 없습니다.')
    return ref.kind+':'+ref.document_id+(':promulgated' if ref.law_view=='promulgated' else '')

async def run_watch(references:list[DocumentRef],client:LawClient,store:SnapshotStore,
                    ordinance_refs:list[DocumentRef]|None=None)->dict:
    if len(references)>20 or len(ordinance_refs or [])>12:
        raise ValueError('감시 대상은 최대 20개, 영향 분석 조례는 최대 12개입니다.')
    results=[];ordinances=[];errors=[]
    for ref in ordinance_refs or []:
        if ref.kind!='ordinance':raise ValueError('영향 분석 대상은 ordinance여야 합니다.')
        try:ordinances.append(await client.get_document(ref))
        except (UpstreamError,ValueError):errors.append({'status':'ordinance_unavailable','reference':ref.model_dump()})
    as_of=datetime.now(ZoneInfo('Asia/Seoul')).date()
    for ref in references:
        key=watch_key(ref)
        try:
            doc=await client.get_document(ref) # stale fallback intentionally disabled
            event=store.record(key,doc)
            if event.get('previous_document') and doc.kind=='law':
                old=Document.model_validate(event.pop('previous_document'))
                event['impact']=amendment_impact(old,doc,ordinances,as_of)
            else:event.pop('previous_document',None)
            results.append(event)
        except (UpstreamError,ValueError) as e:
            results.append({'status':'unavailable','entity_key':key,'previous_snapshot_preserved':True,
                            'code':getattr(e,'code','validation_error'),'message':str(e)[:400]})
    return {'checked_at':utcnow(),'results':results,'errors':errors,
            'scheduler_installed':False,'notifications_sent':False,
            'coverage':'지정한 ID·law_view(effective/promulgated)의 조회값과 저장된 직전 성공본 비교. 시행예정은 promulgated로 별도 추적하며 과거 모든 개정의 전수 발견은 아님',
            'next_action':'운영자가 cron/작업 스케줄러를 설정하고 changed/unavailable 결과에 대한 내부 알림을 연결하세요.'}
