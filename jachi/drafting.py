"""Working drafts, never an autonomous legislative decision or remote write."""
from __future__ import annotations
import re
from datetime import date
from .models import ChangeOperation, Document, Article, UserText
from .analysis import evidence
from .normalize import article_order, jo_label, parse_user_text, compact


def legislative_options(project:str, baseline:Document|None, topic:str='') -> dict:
    return {
        'recommended_review_order':['기존 법령·조례에 따른 집행','기존 조례 일부개정','별도 조례 제정','규칙·지침 정비','추진 유보 또는 사업 재설계'],
        'baseline_identified':baseline is not None,
        'provisional_path':'기존 조례의 적용범위·개정 필요성 우선 검토' if baseline else '기존 근거 확인 전 제정 필요성 판단 유보',
        'options':[
            {'path':'existing_authority','condition':'소관사무·대상·지원방식이 현행 근거에 포함되고 예산·절차 요건을 충족하는지 확인','benefit':'불필요한 중복 입법 방지'},
            {'path':'amend','condition':'기존 조례 목적에 부합하지만 대상·사업·절차·위임사항 정비가 필요한지 확인','benefit':'기존 집행체계와 연속성 유지'},
            {'path':'enact','condition':'별도 입법의 필요성·권한·독립적 규율대상·기존 조례와의 관계가 확인되는지 검토','benefit':'새로운 제도의 체계적 근거 마련'},
            {'path':'rule_or_guideline','condition':'법률·조례가 허용한 집행 세부사항인지 확인; 권리제한·본질적 사항을 지침으로 신설하지 않음','benefit':'입법형식과 규율 수준의 적정화'},
            {'path':'defer_or_redesign','condition':'권한·상위법·재원·협의 요건이 충족되지 않거나 효과가 불명확한 경우','benefit':'위법·중복·집행불능 위험 예방'}],
        'decision':'human_confirmation_required','topic':topic,
        'warning':'다른 지자체의 제정 사실이나 검색 0건만으로 우리 기관의 제정 필요성을 확정하지 않습니다.'}


def draft_outline(project:str, jurisdiction:str, baseline:Document|None, comparison:dict|None,
                  mode:str='review', topic:str='') -> dict:
    options=legislative_options(project,baseline,topic)
    name=f'{jurisdiction} {topic or "[사업명 확인 필요]"} 조례안'
    lines=[name,'※ 검토용 골격: 대괄호를 모두 해소하고 법무·사업·예산 검토를 거쳐야 합니다.',
        '제1조(목적) 이 조례는 [소관사무·정책목표]에 관한 사항을 정함으로써 [주민 편익·공익]에 이바지함을 목적으로 한다.',
        '제2조(정의) 이 조례에서 사용하는 용어의 뜻은 [상위법 정의와의 일치 및 별도 정의 필요성 확인]과 같다.',
        '제3조(적용범위) [적용대상·지역·다른 법령 또는 조례와의 관계를 확인하여 규정한다].',
        '제4조(추진주체 및 사업) [권한 있는 기관·사업범위·기존 사업과의 중복 여부를 확인하여 규정한다].',
        '제5조(신청 및 결정) [신청이 필요한 사업인 경우에만 대상·선정기준·처리절차·결과통지를 규정한다].',
        '제6조(재정지원) [지원근거가 확인된 경우에만 범위·재원·중복방지·정산 등 필요한 사항을 규정한다].',
        '제7조(사업관리) [사업 특성에 맞는 점검·성과평가·기록관리 및 공개범위를 규정한다].',
        '제8조(권익보호) [개인정보·불복·환수 등의 적용 법령을 확인하고 필요한 사항만 규정한다].',
        '부칙','제1조(시행일) 이 조례는 [상위법 시행일·공포·예산·시스템 준비를 반영한 날짜]부터 시행한다.',
        '제2조(경과조치) [종전 신청·지원·위탁·위원 등의 보호가 필요한 경우 구체적으로 규정한다].']
    gaps=[]
    for comp in (comparison or {}).get('comparisons',[]):
        for match in comp['alignments']:
            if match['category']=='possible_gap':
                gaps.append({'topic':match['peer'].get('article'), 'peer_title':match['peer']['document_title'],
                             'evidence_ids':[match['peer']['id']],
                             'action':'우리 조례·다른 조례·상위법의 대체 기능 여부와 도입 필요성 검토'})
    return {'status':'working_outline_not_submission_ready','legislative_options':options,
        'suggested_title':name,'enactment_outline':'\n'.join(lines),
        'amendment_candidates':gaps,
        'amendment_draft_status':'원문과 변경문구를 확정한 후 jachi_draft_amendment로 신구대비·일부개정문 생성' if baseline else '기준 조례 선택 전 개정문 생성 유보',
        'proposal_reason_template':{'problem':project,'why_existing_framework_insufficient':'[현행 규정으로 해결되지 않는 이유와 근거]',
             'expected_effect':'[검증 가능한 성과지표]','evidence':'[공식 조문·비교사례·업무자료]'},
        'submission_ready':False,'auto_published':False,
        'warning':'모든 예시 조항이 필수라는 뜻이 아닙니다. 불필요한 조항·위원회·위탁·지원 근거를 신설하지 마세요.'}


def draft_amendment(baseline:Document, operations:list[ChangeOperation], expected_hash:str,
                    known_evidence:dict|None=None) -> dict:
    if not operations or len(operations)>30:
        raise ValueError('변경 작업은 1~30개로 지정하세요.')
    if expected_hash != baseline.content_hash:
        raise ValueError('기준 원문 해시가 다릅니다. 최신 원문을 다시 조회하고 변경사항을 재확인하세요.')
    if any(not a.key.startswith('main:') for a in baseline.articles):
        raise ValueError('조문 구조가 확인된 단일 버전의 본문이 필요합니다.')
    working={a.label:a.model_copy(deep=True) for a in baseline.articles}
    planned=set(); rows=[]; sentences=[]
    for op in operations:
        label=jo_label(op.article)
        if not label or label!=compact(op.article):
            raise ValueError('정확한 조번호(제5조 또는 제5조의2)를 지정하세요.')
        if label in planned:
            raise ValueError('같은 조문에 여러 작업을 동시에 적용할 수 없습니다.')
        planned.add(label)
        if op.evidence_ids and (known_evidence is None or any(i not in known_evidence for i in op.evidence_ids)):
            raise ValueError('등록되지 않은 근거 ID가 있습니다.')
        old=working.get(label)
        if op.operation=='insert_article':
            if old is not None or op.expected_text:
                raise ValueError('신설 조번호는 원문에 없어야 하며 expected_text는 비워야 합니다.')
        else:
            if old is None or not op.expected_text or op.expected_text!=old.text:
                raise ValueError('대상 조문 또는 expected_text가 기준 원문과 정확히 일치하지 않습니다.')
        if op.operation=='delete_article':
            if op.new_text:
                raise ValueError('삭제 작업의 new_text는 비워야 합니다.')
            new=Article(key='main:'+label,label=label,text=label+' 삭제',deleted=True)
            sentences.append(f'{label}를 삭제한다.')
        else:
            if not op.new_text:
                raise ValueError('신설·대체 작업에는 new_text가 필요합니다.')
            parsed=parse_user_text(UserText(title='변경 조문',text=op.new_text))
            if len(parsed.articles)!=1 or parsed.articles[0].label!=label or parsed.supplementary:
                raise ValueError('new_text는 대상 조번호로 시작하는 하나의 본칙 조문이어야 합니다. 부칙은 별도 검토하세요.')
            new=parsed.articles[0]
            if op.operation=='insert_article':
                sentences.append(f'{label}를 다음과 같이 신설한다.\n{op.new_text}')
            else:
                sentences.append(f'{label}를 다음과 같이 한다.\n{op.new_text}')
        working[label]=new
        rows.append({'article':label,'operation':op.operation,'before':old.text if old else '(신설)',
            'after':new.text,'reason':op.reason,'evidence_ids':op.evidence_ids,
            'baseline_evidence_id':evidence(baseline,old)['id'] if old else None,
            'legal_basis_review':'unreviewed'})
    preview=baseline.model_copy(deep=True)
    preview.articles=sorted(working.values(),key=lambda a:article_order(a.label))
    preview.kind='draft';preview.source_state='user_provided';preview.version_scope='unknown'
    preview.effective_date='';preview.version='working-draft';preview.source_url=''
    preview.warnings.append('기계적 변경 미리보기: 인용조문·조번호 연계·부칙·법적 내용은 사람 확인 필요')
    return {'status':'mechanical_working_draft','submission_ready':False,'expected_hash':expected_hash,
        'partial_amendment_text':baseline.title+' 일부개정조례안\n\n'+baseline.title+' 일부를 다음과 같이 개정한다.\n'+
          '\n\n'.join(sentences)+'\n\n부칙\n이 조례는 [시행일 확인 필요]부터 시행한다.\n[적용례·경과조치·다른 조례 개정 필요성 확인]',
        'comparison_rows':rows,'preview':preview.model_dump(),'preview_hash':preview.content_hash,
        'checks_pending':['위임·권한 및 개정 취지의 타당성','본문의 내·외부 인용 조번호 연쇄 변경',
                          '부칙·별표·시행규칙·신청서·시스템 연계','비용추계·사전협의·의회 제출절차'],
        'warning':'단어 끝 조사 등 입법문 형식도 최종 교정이 필요합니다. 자동 공포·의결·외부 저장은 하지 않습니다.'}
