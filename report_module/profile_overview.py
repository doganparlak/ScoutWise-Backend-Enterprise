"""Compact overview text bound to the report's selected profile labels."""
import json
import os
import re

FIELDS = {'attack':'attackProfile', 'defense':'defenseProfile', 'strengths':'strengths', 'weaknesses':'weaknesses'}
VERSION = 1
CONTRACT = """For attack, defense, strengths and weaknesses, selected theme titles are fixed labels.
Use every selected theme once, in source order: normally two attacking labels, two defensive
labels, three strengths and three weaknesses. subBullets must contain one object per selected
theme; copy its exact title as label, without renaming or shortening it. The generic zero-to-two
bullet and 2-4-word label limits do not apply to these four sections. Each text is an 8-16-word
team-specific interpretation of that theme's evidence and analysis, not its generic description.
summary remains 20-32 words and synthesizes the selected themes' combined football identity,
advantage or vulnerability; do not merely list labels. Keep subset findings scoped to those
matches. Preserve distinctions between style, actual strength and vulnerability. Do not copy
full analysis paragraphs, introduce new topics, invent tactics, recommend changes or repeat
metric values. In Turkish use dripling rather than çalım. No markdown."""


def bind_row(row, profile):
    themes = profile.get('themes') or []
    bullets = row.get('subBullets') or []
    if len(bullets) != len(themes):
        raise ValueError('Overview must cover every selected label')
    summary = str(row.get('summary') or '').strip()
    if not summary or len(summary.split()) > 32:
        raise ValueError('Overview summary exceeds compact format')
    bound = []
    for theme, bullet in zip(themes, bullets):
        if bullet.get('label') != theme['title']:
            raise ValueError('Overview labels must match selected theme order')
        text = str(bullet.get('text') or '').strip()
        if not text or len(text.split()) > 16 or '\n' in text:
            raise ValueError('Overview supporting text exceeds compact format')
        bound.append({'label':theme['title'], 'text':text})
    return {**row, 'summary':summary, 'subBullets':bound, 'profileLabelVersion':VERSION}


def upgrade_profile_overview(content, lang, *, editable_sections=None):
    rows = content.get('overview') or []
    profiles = {key:content.get(field) or {} for key,field in FIELDS.items()}
    pending = [row for row in rows if row.get('key') in FIELDS and (editable_sections is None or row['key'] in editable_sections) and (
        row.get('profileLabelVersion') != VERSION or
        len(str(row.get('summary') or '').split()) > 32 or
        any(len(str(b.get('text') or '').split()) > 16 for b in row.get('subBullets') or []) or
        [b.get('label') for b in row.get('subBullets') or []] !=
        [t.get('title') for t in profiles[row['key']].get('themes') or []])]
    if not pending or not os.getenv('OPENAI_API_KEY'):
        return content
    try:
        from langchain_openai import ChatOpenAI
        model = ChatOpenAI(model=os.getenv('OPENAI_MATCH_REPORT_MODEL',os.getenv('OPENAI_REPORT_MODEL','gpt-5.6-luna')),
                           api_key=os.environ['OPENAI_API_KEY'],temperature=.2,
                           model_kwargs={'response_format':{'type':'json_object'}})
        keys = [row['key'] for row in pending]
        messages = [('system', 'You are ScoutWise Enterprise’s football overview editor. Return one JSON object keyed by the supplied section keys, each with summary and subBullets. '+CONTRACT),
                    ('human',json.dumps({'language':lang,'sections':{key:profiles[key] for key in keys}},ensure_ascii=False,default=str))]
        for attempt in range(2):
            raw = str(model.invoke(messages).content or '').strip()
            try:
                parsed = json.loads(re.sub(r'^```(?:json)?\s*|\s*```$','',raw,flags=re.I))
                replacements = {row['key']:bind_row({**row,**{field:parsed[row['key']][field] for field in ('summary','subBullets')}},profiles[row['key']]) for row in pending}
                return {**content,'overview':[replacements.get(row['key'],row) for row in rows]}
            except (ValueError,KeyError,TypeError,AttributeError):
                if attempt: raise
                messages += [('assistant',raw),('human','Repair the format and length only. Include every selected label in order, one 8-16-word text each, and one 20-32-word summary per section.')]
    except Exception as exc:
        print(f'[enterprise_team_report] event=profile_overview_upgrade_deferred error_type={type(exc).__name__}')
    return content
