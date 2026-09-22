from datetime import date,datetime
from zoneinfo import ZoneInfo
import pytest
from jachi.models import Article,ChangeOperation,UserText
from jachi.analysis import (temporal_status,compare_documents,semantic_flags,diff_documents,amendment_impact,
                           evidence_index,verify_references,review_rules)
from jachi.normalize import extract_references
from jachi.audit import verify_claims,quality_gate
from jachi.drafting import draft_amendment,legislative_options


def test_future_not_current(parent):
    parent.effective_date='20261015';parent.source_state='live';parent.version_scope='current'
    assert temporal_status(parent,date(2026,9,22))['state']=='future'
    assert not temporal_status(parent,date(2026,9,22))['version_current_verified']

def test_retrospective_not_current(parent):
    parent.source_state='live';parent.version_scope='current'
    assert not temporal_status(parent,date(2025,1,2))['version_current_verified']

def test_past_effective_not_proof_of_current(parent):
    assert not temporal_status(parent,datetime.now(ZoneInfo('Asia/Seoul')).date())['version_current_verified']

@pytest.mark.parametrize('a,b,label',[
 ('10만원 지원','20만원 지원','수치'),('지원할 수 있다','지원하여야 한다','의무'),('대상에 포함','대상에서 제외','범위')])
def test_semantic_risks(a,b,label):assert any(label in x for x in semantic_flags(a,b))

def test_untitled_duplicate_titles_not_lost(base):
    other=base.model_copy(deep=True);other.document_id='101'
    other.articles.append(Article(key='main:제3조',label='제3조',title='',text='제3조 정한다.'))
    other.articles.append(Article(key='main:제4조',label='제4조',title='지원',text='제4조 지원한다.'))
    c=compare_documents(base,[other],date(2026,9,22))
    assert len(c['comparisons'][0]['alignments'])==4 and len(c['dimension_matrix'])==12

def test_diff_flags_and_impacts(base,parent):
    new=parent.model_copy(deep=True);new.version='2002';new.articles[0].text='제5조의2(지원) 지방자치단체는 20만원을 지원하여야 한다.'
    r=amendment_impact(parent,new,[base],date(2026,9,22))
    assert len(r['impacts'])==1 and r['impacts'][0]['link_type']=='explicit_article_reference'
    assert len(r['parent_diff']['changes'][0]['flags'])>=2

def test_diff_sees_supplement_annex(parent):
    new=parent.model_copy(deep=True);new.supplementary=['변경 부칙'];new.annexes=[{'title':'별표','text':'수정'}]
    r=diff_documents(parent,new)
    assert r['supplementary_changed'] and r['annexes_changed']

def test_distinct_official_ids_not_version_pair(parent):
    old=parent.model_copy(deep=True);old.source_state='live'
    new=old.model_copy(deep=True);new.document_id='999'
    with pytest.raises(ValueError):diff_documents(old,new)

def test_reference_missing_and_branch(parent):
    checks=verify_references(extract_references('「가상 돌봄법」 제5조의2제1항'),[parent],date(2026,9,22))
    assert checks[0]['versions'][0]['status']=='article_present'
    assert checks[0]['versions'][0]['subdivision_status']=='not_verified'
    assert verify_references(extract_references('「없는법」 제1조'),[parent],date(2026,9,22))[0]['status']=='source_missing'

def test_quote_registry_verification(base):
    registry=evidence_index([base]);e=next(iter(registry.values()))
    claims=[{'text':'목적 조항 존재','category':'observation','references':[{'evidence_id':e['id'],'quote':'돌봄 지원'}]},
            {'text':'가짜','category':'legal_candidate','references':[{'evidence_id':'E-fake','quote':'가짜'}]},
            {'text':'틀린 인용','category':'observation','references':[{'evidence_id':e['id'],'quote':'벌금을 부과한다'}]}]
    r=verify_claims(claims,registry,date(2026,9,22))
    assert len(r['accepted'])==1 and len(r['rejected'])==2
    assert not r['accepted'][0]['verification']['semantic_entailment_verified']

def test_unfounded_legal_claim_rejected():
    r=verify_claims([{'text':'적법 확정','category':'legal_candidate','references':[]}],{},date(2026,9,22))
    assert len(r['rejected'])==1

def test_gate_never_legal_approval(base,parent):
    r=quality_gate([base,parent],[],date(2026,9,22),base,[parent],[])
    assert r['status']=='human_review_required' and r['legal_approval'] is False

def test_rules_flag_privacy_and_rights(base):
    r=review_rules([base],'주민등록번호를 수집하고 출산 지원금을 지급하며 과태료를 부과한다.')
    assert {'R02','R04','R07'}<={x['id'] for x in r['findings']}

def test_no_baseline_does_not_force_enactment():
    r=legislative_options('새 사업',None)
    assert r['decision']=='human_confirmation_required' and '유보' in r['provisional_path']


def op(base,**kw):
    args={'operation':'replace_article','article':'제2조','expected_text':base.articles[1].text,
          'new_text':'제2조(지원) 법적 근거를 확인하여 지원한다.','reason':'정비 검토'};args.update(kw)
    return ChangeOperation(**args)

def test_safe_mechanical_patch(base):
    r=draft_amendment(base,[op(base)],base.content_hash)
    assert r['comparison_rows'][0]['before']==base.articles[1].text
    assert r['preview']['kind']=='draft' and r['submission_ready'] is False
    assert '10만원' in base.articles[1].text # original was not mutated

@pytest.mark.parametrize('mutation',['hash','expected','duplicate','wrong_label','missing_new','unknown_evidence'])
def test_patch_preconditions(base,mutation):
    ops=[op(base)];h=base.content_hash
    if mutation=='hash':h='fake'
    elif mutation=='expected':ops=[op(base,expected_text='다른 버전')]
    elif mutation=='duplicate':ops*=2
    elif mutation=='wrong_label':ops=[op(base,new_text='제3조(지원) 다른 조문이다.')]
    elif mutation=='missing_new':ops=[op(base,new_text='')]
    elif mutation=='unknown_evidence':ops=[op(base,evidence_ids=['E-fake'])]
    with pytest.raises(ValueError):draft_amendment(base,ops,h)

def test_delete_keeps_number(base):
    r=draft_amendment(base,[op(base,operation='delete_article',new_text='')],base.content_hash)
    assert r['preview']['articles'][1]['deleted'] and r['preview']['articles'][1]['text']=='제2조 삭제'

def test_insert_branch_without_renumber(base):
    r=draft_amendment(base,[ChangeOperation(operation='insert_article',article='제1조의2',new_text='제1조의2(정의) 용어를 정한다.',reason='정의 보완')],base.content_hash)
    assert [a['label'] for a in r['preview']['articles']]==['제1조','제1조의2','제2조']

def test_comparison_budget_marks_unprocessed(base):
    other=base.model_copy(deep=True);other.document_id='201'
    r=compare_documents(base,[other],date(2026,9,22),max_pairs=1)
    assert not r['coverage']['comparison_complete'] and r['coverage']['unprocessed_articles']==2
    assert all(a['category']=='not_processed_budget' for a in r['comparisons'][0]['alignments'])
