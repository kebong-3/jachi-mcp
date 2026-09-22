import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from jachi.models import Document,Article

@pytest.fixture
def base():
    return Document(kind='ordinance',document_id='100',version='1001',title='가상도 가상시 돌봄 조례',
        jurisdiction='가상도 가상시',source_state='fixture',version_scope='unknown',effective_date='20250101',
        articles=[Article(key='main:제1조',label='제1조',title='목적',text='제1조(목적) 이 조례는 돌봄 지원에 관한 사항을 정한다.'),
                  Article(key='main:제2조',label='제2조',title='지원',text='제2조(지원) 「가상 돌봄법」 제5조의2에 따라 시장은 10만원을 지원할 수 있다.')],
        supplementary=['이 조례는 공포한 날부터 시행한다.'])

@pytest.fixture
def parent():
    return Document(kind='law',document_id='200',version='2001',title='가상 돌봄법',source_state='fixture',effective_date='20250101',
        articles=[Article(key='main:제5조의2',label='제5조의2',title='지원',text='제5조의2(지원) 지방자치단체는 10만원을 지원할 수 있다.')],
        supplementary=['시연용 법령이다.'])

@pytest.fixture
def ordinance_payload():
    return {'자치법규':{'기본정보':{'자치법규ID':'100','자치법규일련번호':'1001','자치법규명':'가상도 가상시 돌봄 조례',
        '지자체기관명':'가상도 가상시','시행일자':'20250101'},'조문':{'조문단위':[
            {'조문여부':'N','조문번호':'1','조문내용':'제1장 총칙'},
            {'조문여부':'Y','조문번호':'1','조문제목':'목적','조문내용':'제1조(목적) 주민의 돌봄을 지원한다.'},
            {'조문여부':'Y','조문번호':'2','조문가지번호':'2','조문제목':'지원','조문내용':'제2조의2(지원)',
             '항':{'항내용':'① 시장은 지원할 수 있다.','호':[{'호내용':'1. 돌봄','목':{'목내용':'가. 방문 돌봄'}},{'호내용':'2. 식사'}]}}]},
            '부칙':{'부칙단위':{'부칙내용':['이 조례는 공포한 날부터 시행한다.','종전의 지원은 유지한다.']}},
            '별표':{'별표단위':{'별표제목':'별표1 지원 기준','별표내용':'가상 내용','별표번호':'1'}}}}

@pytest.fixture
def law_payload():
    return {'법령':{'기본정보':{'법령ID':'200','법령일련번호':'2001','법령명_한글':'가상 돌봄법','시행일자':'20250101'},
            '조문':{'조문단위':{'조문번호':'5','조문가지번호':'2','조문제목':'지원','조문내용':'제5조의2(지원) 지원할 수 있다.'}},
            '부칙':{'부칙단위':{'부칙내용':'시연용'}}}}
