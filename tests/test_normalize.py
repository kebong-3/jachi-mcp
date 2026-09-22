import pytest
from pydantic import ValidationError
from jachi.models import UserText,DocumentRef,ReviewInput
from jachi.normalize import (jo_label,article_order,listing_rows,total_count,parse_document,
                 parse_user_text,extract_references,region_match,clean,public_url)
from jachi.client import parse_payload,UpstreamError

@pytest.mark.parametrize('raw,branch,expected',[('1','','제1조'),('000502','','제5조의2'),('0005021','','제5조의2'),
  ('5','2','제5조의2'),('제 5 조 의 2','','제5조의2'),('0','',''),('별표1','','')])
def test_article_ids(raw,branch,expected):assert jo_label(raw,branch)==expected

def test_singleton_search():
    data={'OrdinSearch':{'totalCnt':'1','ordin':{'자치법규명':'시험 조례','자치법규ID':'10','자치법규일련번호':'100'}}}
    assert len(listing_rows(data,'ordinance'))==1
    assert total_count(data)==1

def test_not_arbitrary_list():assert listing_rows({'error_details':[{'x':1}]},'ordinance')==[]
def test_unknown_total_not_zero():assert total_count({'message':'not ready'}) is None

def test_nested_article_and_supplements(ordinance_payload):
    doc=parse_document(ordinance_payload,'ordinance')
    assert [a.label for a in doc.articles]==['제1조','제2조의2']
    assert all(t in doc.articles[1].text for t in ['①','1. 돌봄','가. 방문 돌봄','2. 식사'])
    assert '종전의 지원' in doc.supplementary[0]
    assert len(doc.annexes)==1
    assert any('별표 API' in w for w in doc.warnings)

def test_parse_law(law_payload):
    d=parse_document(law_payload,'law')
    assert d.document_id=='200' and d.version=='2001' and d.title=='가상 돌봄법'

def test_duplicate_article_rejected(ordinance_payload):
    units=ordinance_payload['자치법규']['조문']['조문단위'];units.append(units[1])
    with pytest.raises(ValueError):parse_document(ordinance_payload,'ordinance')

def test_empty_detail_rejected():
    with pytest.raises(ValueError):parse_document({'자치법규명':'시험 조례'},'ordinance')

def test_user_text_citation_not_new_heading():
    d=parse_user_text(UserText(title='제공안',text='제1조(목적) 이 조례는 「가상법」 제2조에 따른다.\n제2조(지원) 지원한다.\n부칙\n제1조(시행일) 내일부터 시행한다.'))
    assert len(d.articles)==2 and d.source_state=='user_provided' and len(d.supplementary)==1

def test_user_unstructured_warning():
    d=parse_user_text(UserText(title='제공안',text='번호 없는 설명'))
    assert d.articles[0].key=='unstructured:1' and len(d.warnings)==2

def test_user_cannot_assert_trust():
    with pytest.raises(ValidationError):UserText(title='a',text='x',source_state='live')

def test_no_ambiguous_region_match():
    assert not region_match('광주광역시 서구','서구')
    assert not region_match('경기도 광주시','광주광역시')
    assert region_match('광주광역시 서구','광주광역시  서구')

def test_extract_precise_reference():
    r=extract_references('「가상법」 제5조의2제3항제1호 및 「다른법」 제7조')
    assert r[0]['article']=='제5조의2' and r[0]['paragraph']=='제3항' and r[0]['item']=='제1호'

def test_bare_reference_not_guessed():assert extract_references('법 제5조에 따른다.')==[]
def test_lists_preserved():assert clean(['가','나'])=='가\n나'
def test_public_urls_no_oc():assert 'OC' not in public_url('law','001656','2001','20250101')

@pytest.mark.parametrize('payload',[b'',b'<html>login</html>',b'{bad',b'{"error":"denied"}',b'{"resultCode":"401"}',
  b'<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><x>&a;</x>'])
def test_api_errors_fail_closed(payload):
    with pytest.raises(UpstreamError):parse_payload(payload)

def test_xml_fallback_singleton():
    p=parse_payload('<OrdinSearch><totalCnt>1</totalCnt><ordin><자치법규ID>10</자치법규ID><자치법규명>시험</자치법규명></ordin></OrdinSearch>'.encode())
    assert len(listing_rows(p,'ordinance'))==1

@pytest.mark.parametrize('value',[{'kind':'law','mst':'20'}, {'kind':'law','mst':'20','effective_date':'20260230'},
 {'document_id':'https://example.com'},{}])
def test_invalid_refs(value):
    with pytest.raises(ValidationError):DocumentRef.model_validate(value)

def test_external_model_needs_consent():
    with pytest.raises(ValidationError):ReviewInput(project='사업 설명',jurisdiction='가상시',reasoning='gemini')

@pytest.mark.parametrize('d',['202611','202601011','abcd0101'])
def test_date_has_exact_eight_digits(d):
    with pytest.raises(ValidationError):DocumentRef(kind='law',mst='1',effective_date=d)

def test_bad_utf8_is_api_error():
    with pytest.raises(UpstreamError):parse_payload(b'\xff')

def test_ordinance_not_promulgated_law_view():
    with pytest.raises(ValidationError):DocumentRef(document_id='1',law_view='promulgated')

def test_annex_files_without_text_are_incomplete(ordinance_payload):
    item=ordinance_payload['자치법규']['별표']['별표단위'];item.pop('별표내용')
    item['별표서식파일링크']='https://www.law.go.kr/example'
    d=parse_document(ordinance_payload,'ordinance')
    assert d.annexes[0]['file_url'] and any('본문 미수집' in w for w in d.warnings)
