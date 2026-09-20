"""Body-size guards shared by every route that reads a request body itself
rather than letting FastAPI's own pydantic parsing cap it.

Two unrelated caps live here because both need the same Content-Length
trick and both are plain, Request-shaped helpers with no other home:
:data:`MAX_OPEN_BODY_BYTES` guards the handful of routes that read a form or
a JSON body before any credential exists to check (``/setup``, ``/login``,
the device telemetry POST); :data:`MAX_RESTORE_BYTES`'s half (imported from
``app.backup``, which owns the number itself) guards the one restore upload,
which is large enough to need its own cap and its own byte-counted stream to
disk rather than a single ``request.body()`` read.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.backup import MAX_RESTORE_BYTES

#: /setup and /login are the only POST routes with no bearer token: /setup
#: because that is how the hub's first credential is minted, /login because
#: it exchanges a credential for a session rather than requiring one
#: already. POST /setup is further restricted to a private/loopback caller
#: (see is_private_client_host in hub_config.py) while the hub is
#: unconfigured; every other route, including /api/device/telemetry, is 503
#: until then. Cap what either open POST route will buffer in memory before
#: validation ever runs.
MAX_OPEN_BODY_BYTES = 64 * 1024

#: How much of a restore upload is copied into DATA_DIR per hop through the
#: thread pool. Big enough that a 64 MiB file is a few hundred writes, small
#: enough that the byte counter refuses an oversized upload long before it is
#: all on disk.
UPLOAD_CHUNK_BYTES = 256 * 1024


async def _read_capped_body(request: Request) -> bytes:
    """Read ``request``'s body in chunks, rejecting it once it passes
    ``MAX_OPEN_BODY_BYTES`` instead of buffering an arbitrarily large one.

    Sets ``request._body`` on the way out (the same attribute
    ``Request.body()`` caches), so a route that goes on to call
    ``request.form()`` or ``request.json()`` reuses this read instead of
    trying to consume the already-drained stream again.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_OPEN_BODY_BYTES:
            raise HTTPException(status_code=413, detail="body too large")
        chunks.append(chunk)
    body = b"".join(chunks)
    request._body = body  # noqa: SLF001 - see docstring
    return body


async def _cap_form_body(request: Request) -> None:
    """The Content-Length guard shared by POST /setup and POST /login:
    ``request.form()`` reads ``request.stream()`` itself, so it cannot be
    handed ``_read_capped_body``'s bytes directly. When Content-Length is
    present, reject an oversized body before ``form()`` ever touches the
    stream; when it is absent (e.g. chunked), read the capped body first so
    it is cached on ``request._body``, which ``stream()`` (and so ``form()``)
    reuses.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > MAX_OPEN_BODY_BYTES:
            raise HTTPException(status_code=413, detail="body too large")
    else:
        await _read_capped_body(request)


def _cap_restore_length(request: Request) -> None:
    """The declared-size half of the restore cap, checked before the
    multipart parser reads a byte.

    Content-Length is a claim, not a fact, so it is only ever a fast refusal:
    :func:`_stream_upload_to`'s counter is what actually decides. A request
    with no Content-Length (chunked, most often) is refused outright here
    instead: ``request.form()`` no longer carries ``max_part_size=
    MAX_RESTORE_BYTES`` (that only ever raised the *text*-field cap; a
    starlette 1.6 file part is spooled to disk with no size check of its
    own), so without a declared length there is nothing to stop the whole
    body being read into a spooled temp file before the byte counter in
    :func:`_stream_upload_to` ever sees it.
    """
    content_length = request.headers.get("content-length")
    if content_length is None:
        raise HTTPException(status_code=411, detail="restore needs a Content-Length")
    try:
        declared_length = int(content_length)
    except ValueError:
        return
    if declared_length > MAX_RESTORE_BYTES:
        raise HTTPException(status_code=413, detail="the uploaded file is too large")


async def _stream_upload_to(upload: UploadFile, target: Path) -> int:
    """Copy ``upload`` into ``target`` a chunk at a time, returning the byte
    count and raising 413 the moment it passes :data:`MAX_RESTORE_BYTES`.

    The count comes from the bytes actually written, never from
    ``UploadFile.size`` or a Content-Length: both are the client's word for
    it. Each write goes through the thread pool, the same rule every other
    synchronous file write in this module follows.
    """
    written = 0
    with target.open("wb") as handle:
        while True:
            chunk = await upload.read(UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_RESTORE_BYTES:
                raise HTTPException(status_code=413, detail="the uploaded file is too large")
            await run_in_threadpool(handle.write, chunk)
    return written


__all__ = [
    "MAX_OPEN_BODY_BYTES",
    "UPLOAD_CHUNK_BYTES",
    "_cap_form_body",
    "_cap_restore_length",
    "_read_capped_body",
    "_stream_upload_to",
]
