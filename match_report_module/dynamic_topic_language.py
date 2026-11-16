"""Football-specific editorial review of dynamic match-topic headings."""
from copy import deepcopy
import json
import os

VERSION = 1


def upgrade_dynamic_topic_language(content):
    topics = content.get('team_deep_analyses') or {}
    if content.get('report_type')=='pre_match' or not topics or content.get('dynamic_topic_language_version')==VERSION:
        return content
    if not os.getenv('OPENAI_API_KEY'):
        return content
    from report_module.profile_generation import generate_profile_json
    from report_module.terminology import FOOTBALL_TERMINOLOGY_GUIDANCE
    rows=[{'rowId':f'{team_id}:{index}','header':row.get('header'),'interpretation':row.get('text')}
          for team_id,team in topics.items() for index,row in enumerate(team)]
    try:
        draft=generate_profile_json([
            ('system',"You are a football report editor writing for professional coaches and scouts. Return JSON with themes: one {rowId, header} object for every supplied row. Rewrite ONLY the dynamic headings, using natural football terminology in the requested language. Each heading has 2-4 words and identifies the specific finding established by its paragraph. Prefer precise noun phrases, not slogans, newspaper wordplay, dramatic metaphors or incomplete subject/predicate sentences. Turkish examples of awkward constructions to avoid include 'israfa döndü', 'şut hacmi', 'yaratıcı ikili besledi', 'az topla çok şut' and 'yedek bitiriciliği geldi'. These are not a substitution list: independently derive the best football heading from each paragraph. Distinguish shot accuracy from finishing efficiency and chance creation from goals; never introduce a stronger or different conclusion. Keep every rowId, do not merge, reorder or invent topics. Do not return paragraph text, numbers, recommendations or markdown."+FOOTBALL_TERMINOLOGY_GUIDANCE),
            ('human',json.dumps({'language':content.get('language') or 'en','topics':rows},ensure_ascii=False))])
        edited={row.get('rowId'):str(row.get('header') or '').strip() for row in draft.get('themes') or []}
        if set(edited)!={row['rowId'] for row in rows} or any(not 2<=len(header.split())<=4 or '\n' in header for header in edited.values()):
            raise ValueError('Incomplete or invalid dynamic heading review')
        result={**content,'team_deep_analyses':deepcopy(topics),'dynamic_topic_language_version':VERSION}
        for team_id,team in result['team_deep_analyses'].items():
            for index,row in enumerate(team):row['header']=edited[f'{team_id}:{index}']
        return result
    except Exception as exc:
        print(f'[enterprise_match_report] event=dynamic_topic_language_deferred error_type={type(exc).__name__}')
        return content
