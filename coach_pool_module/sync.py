"""Refresh the coach directory explicitly: python -m coach_pool_module.sync.

No refresh job runs on web requests. A failed refresh retains existing rows;
rerunning safely upserts by Sportmonks coach ID, including missing-image records.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import time

import requests
from sqlalchemy import text
from api_module.database import engine
from match_analysis_module.current_leagues import fold

INCLUDE = 'teams.team;country;nationality'


def fetch_page(page):
    base = os.getenv('SPORTMONKS_BASE_URL', 'https://api.sportmonks.com/v3/football').rstrip('/')
    for attempt in range(3):
        try:
            response = requests.get(f'{base}/coaches', headers={'Authorization': os.environ['SPORTMONKS_API_KEY']},
                                    params={'include': INCLUDE, 'per_page': 50, 'page': page}, timeout=30)
            if response.status_code == 429:
                raise RuntimeError('Coach refresh stopped: Sportmonks rate limit reached.')
            if response.status_code >= 500 and attempt < 2:
                time.sleep(attempt + 1)
                continue
            if not response.ok:
                raise RuntimeError(f'Coach refresh failed (HTTP {response.status_code}, page {page}).')
            return response.json()
        except (requests.RequestException, ValueError):
            if attempt == 2:
                raise RuntimeError(f'Coach refresh request failed on page {page}.') from None
            time.sleep(attempt + 1)


def save_page(coaches):
    values = []
    for coach in coaches:
        image = coach.get('image_path') or ''
        image = image if image and 'placeholder' not in image.lower() else None
        name = coach.get('display_name') or coach.get('name') or coach.get('common_name') or ''
        values.append({'id': coach['id'], 'name': name, 'image': image,
                       'status': 'available' if image else 'missing',
                       'search': fold(' '.join(str(coach.get(k) or '') for k in ('name', 'display_name', 'common_name', 'firstname', 'lastname'))),
                       'profile': coach})
    if not values:
        return
    with engine.begin() as db:
        db.execute(text('''INSERT INTO public.enterprise_coach_images
            (coach_id, coach_name, image_url, image_status, search_text, profile_data, last_fetched_at, fetch_attempt_count)
            SELECT id, name, image, status, search, profile, now(), 1
            FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS x(
                id bigint, name text, image text, status text, search text, profile jsonb)
            ON CONFLICT (coach_id) DO UPDATE SET coach_name=EXCLUDED.coach_name,
                image_url=EXCLUDED.image_url, image_status=EXCLUDED.image_status,
                search_text=EXCLUDED.search_text, profile_data=EXCLUDED.profile_data,
                last_fetched_at=now(), updated_at=now(), last_fetch_error=NULL,
                last_verified_at=CASE WHEN enterprise_coach_images.image_url IS NOT DISTINCT FROM EXCLUDED.image_url
                    THEN enterprise_coach_images.last_verified_at ELSE NULL END,
                fetch_attempt_count=enterprise_coach_images.fetch_attempt_count+1'''), {'rows': json.dumps(values)})


def main():
    total = 0
    with ThreadPoolExecutor(max_workers=6) as executor:
        for first in range(1, 2001, 6):
            pages = list(executor.map(fetch_page, range(first, first + 6)))
            done = False
            for payload in pages:
                data = payload.get('data') or []
                save_page(data)
                total += len(data)
                if not (payload.get('pagination') or {}).get('has_more'):
                    done = True
                    break
            print(f'Coach directory: {total} records refreshed', flush=True)
            if done:
                print('Coach directory refresh complete.', flush=True)
                return
    raise RuntimeError('Coach refresh exceeded page limit; directory refresh is incomplete.')


if __name__ == '__main__':
    main()
