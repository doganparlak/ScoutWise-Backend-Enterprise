"""Request-scoped report streaming; no queue service or durable worker required."""
import asyncio
import json
import queue
import threading
from contextvars import ContextVar
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

_sink = ContextVar('report_progress_sink', default=None)
_publish = ContextVar('report_progress_publish', default=None)
_slots = threading.BoundedSemaphore(4)


def streaming_requested(request):
    return request is not None and 'application/x-ndjson' in request.headers.get('accept', '')


def is_streaming():
    return _sink.get() is not None


def configure_progress(formatter=lambda value: value, persist=None):
    """Collect cumulative snapshots so slow consumers may safely skip intermediate events."""
    if not is_streaming():
        return
    content = {}
    completed = []
    sink = _sink.get()
    def publish(patch, section):
        content.update(patch)
        if section and section not in completed:
            completed.append(section)
        snapshot = jsonable_encoder({**content, 'completed_sections': list(completed)})
        if persist:
            persist(snapshot)
        sink({'type': 'progress', 'data': formatter(snapshot)})
    _publish.set(publish)


def emit_progress(patch, section=None):
    callback = _publish.get()
    if callback:
        callback(patch, section)


def stream_report(work):
    if not _slots.acquire(blocking=False):
        raise HTTPException(503, 'Report capacity is busy. Please try again shortly.')
    events = queue.Queue(maxsize=2)
    def send(event):
        data = json.dumps(jsonable_encoder(event), ensure_ascii=False, allow_nan=False) + '\n'
        try:
            events.put_nowait(data)
        except queue.Full:
            # Every progress event is a complete snapshot, not an incremental delta.
            try:
                events.get_nowait()
            except queue.Empty:
                pass
            events.put_nowait(data)
    def run():
        from api_module.database import SessionLocal
        token = _sink.set(send)
        try:
            with SessionLocal() as db:
                result = work(db)
                send({'type': 'complete', 'data': result})
        except Exception as exc:
            send({'type': 'error', 'detail': exc.detail if isinstance(exc, HTTPException) else 'Report generation failed. Please try again.'})
        finally:
            _sink.reset(token)
            _publish.set(None)
            _slots.release()
    async def body():
        # Send headers immediately, even before data providers respond.
        yield '{"type":"started"}\n'
        while True:
            try:
                item = await asyncio.to_thread(events.get, True, 10)
            except queue.Empty:
                yield '{"type":"heartbeat"}\n'
                continue
            yield item
            if json.loads(item)['type'] in ('complete', 'error'):
                break
    threading.Thread(target=run, daemon=True, name='report-stream').start()
    return StreamingResponse(body(), media_type='application/x-ndjson', headers={
        'Cache-Control': 'no-store, no-transform', 'X-Accel-Buffering': 'no',
    })
