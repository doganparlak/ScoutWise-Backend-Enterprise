"""Deliver deterministic report data before generating narrative text."""
import json
import logging
from threading import Lock

from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)
NARRATIVE_LOCKS = [Lock() for _ in range(32)]


def progressive_response(first, complete):
    def events():
        yield json.dumps({'stage': 'data', 'data': first}) + '\n'
        try:
            yield json.dumps({'stage': 'complete', 'data': complete()}) + '\n'
        except Exception:
            logger.exception('Coach report narrative generation failed')
            # Already-delivered data remains useful; only prose needs retrying.
            yield json.dumps({'stage': 'aiError', 'detail': 'Narrative unavailable. Retry this section.'}) + '\n'

    return StreamingResponse(events(), media_type='application/x-ndjson', headers={
        'Cache-Control': 'no-store, no-transform', 'X-Accel-Buffering': 'no',
    })
