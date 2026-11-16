"""Upgrade only single-match profiles in user-owned saved match reports."""
import hashlib
import json
from threading import Lock
from fastapi import HTTPException
from sqlalchemy import text
from .style_profiles import VERSION, upgrade_match_profiles
from .dynamic_topic_language import VERSION as TOPIC_LANGUAGE_VERSION, upgrade_dynamic_topic_language

_LOCKS = [Lock() for _ in range(32)]


def upgrade_saved_match_profiles(db,report_id,user_id,content):
    if content.get('report_type')=='pre_match':
        return content
    if (content.get('match_profiles') or {}).get('version')==VERSION and (not content.get('team_deep_analyses') or content.get('dynamic_topic_language_version')==TOPIC_LANGUAGE_VERSION):
        return content
    db.rollback()
    index=int(hashlib.sha256(str(report_id).encode()).hexdigest()[:8],16)%len(_LOCKS)
    with _LOCKS[index]:
        latest=db.execute(text("SELECT report_content FROM enterprise_favorite_matches WHERE id=:id AND user_id=:uid AND report_status='ready'"),{'id':str(report_id),'uid':user_id}).scalar_one_or_none()
        if latest is None:
            raise HTTPException(404,'Saved match report not found')
        db.rollback()
        upgraded=upgrade_dynamic_topic_language(upgrade_match_profiles(latest))
        if upgraded is not latest:
            sections={key:upgraded[key] for key in ('match_profiles','team_deep_analyses','dynamic_topic_language_version') if key in upgraded and upgraded[key] is not latest.get(key)}
            db.execute(text("UPDATE enterprise_favorite_matches SET report_content=report_content || CAST(:sections AS jsonb) WHERE id=:id AND user_id=:uid"),{'id':str(report_id),'uid':user_id,'sections':json.dumps(sections,ensure_ascii=False)})
            db.commit()
        return upgraded
