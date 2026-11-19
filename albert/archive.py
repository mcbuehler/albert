"""Cron-only call discovery, refresh and audio transfer. Production activation is a later chunk."""

import hashlib
import math
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import BinaryIO
from uuid import UUID

import requests

from albert import config, db, storage, vapi
from albert.logging_utils import get_logger
from albert.validation import normalize_phone

AUDIO_EXTENSIONS = {"audio/wav": "wav", "audio/mpeg": "mp3"}
ERROR_CODE_RE = re.compile(r"[a-z_]{1,100}")


class InvalidCall(ValueError):
    """Malformed provider metadata; never advance a scan over this response."""


@dataclass
class AudioFile:
    """A downloaded recording, spooled in memory or on temporary disk; the caller closes `body`."""

    body: BinaryIO
    media_type: str
    size: int
    sha256: str


def moment(value: object) -> datetime | None:
    """Parse an optional provider timestamp, rejecting invalid or unzoned values."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidCall("invalid_timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidCall("invalid_timestamp") from None
    if result.tzinfo is None:
        raise InvalidCall("invalid_timestamp")
    return result.astimezone(timezone.utc)


def text(value: object) -> str | None:
    """Trim optional provider text without coercing other JSON types."""
    return (value.strip() or None) if isinstance(value, str) else None


def number(value: object) -> int | float | None:
    """Accept finite nonnegative provider numbers, excluding booleans."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return value
    return None


def _spoken_message(value: object) -> dict | None:
    """Keep only a spoken role, text and supported timing fields."""
    if not isinstance(value, dict) or value.get("role") not in ("user", "bot", "assistant"):
        return None
    spoken = text(value.get("message"))
    if not spoken:
        return None
    message = {"role": "user" if value["role"] == "user" else "assistant", "message": spoken}
    for key in ("secondsFromStart", "duration", "endTime", "time"):
        timing = number(value.get(key))
        if timing is not None:
            message[key] = timing
    return message


def _artifact_plan(call: dict) -> dict:
    """Read artifact settings without assuming optional provider objects exist."""
    assistant = call.get("assistant") if isinstance(call.get("assistant"), dict) else {}
    return assistant.get("artifactPlan") if isinstance(assistant.get("artifactPlan"), dict) else {}


def _conversation(call: dict) -> dict:
    """Build conversation fields without storing system prompts or tool payloads.

    Args:
        call: Provider call JSON, including optional artifacts and assistant settings.

    Returns:
        Allowlisted messages, transcript and independent transcript/audio states.
        Provider transcript text is a fallback only when message history is absent.
    """
    artifact = call.get("artifact") if isinstance(call.get("artifact"), dict) else {}
    raw_messages = artifact.get("messages", call.get("messages"))
    messages = [safe for value in raw_messages if (safe := _spoken_message(value))] if isinstance(raw_messages, list) else []
    transcript = "\n".join(f"{'Customer' if m['role'] == 'user' else 'Assistant'}: {m['message']}" for m in messages)
    if not transcript and raw_messages is None:
        transcript = text(artifact.get("transcript", call.get("transcript")))
    plan = _artifact_plan(call)
    transcript_plan = plan.get("transcriptPlan") if isinstance(plan.get("transcriptPlan"), dict) else {}
    state = "ready" if transcript else "pending"
    if not transcript and transcript_plan.get("enabled") is False:
        state = "disabled"
    return {"transcript": transcript or None, "messages": messages, "transcript_state": state,
            "audio_state": "disabled" if plan.get("recordingEnabled") is False else "pending"}


def _call_times(call: dict) -> dict:
    """Parse provider call times and reject a negative duration."""
    started, ended = moment(call.get("startedAt")), moment(call.get("endedAt"))
    if started and ended and ended < started:
        raise InvalidCall("invalid_call_times")
    return {"provider_created_at": moment(call["createdAt"]), "provider_updated_at": moment(call.get("updatedAt")),
            "started_at": started, "ended_at": ended,
            "duration_seconds": (ended - started).total_seconds() if ended and started else None}


def _call_identity(call: dict) -> dict:
    """Select provider identity and normalize the remote phone with shared validation."""
    customer = call.get("customer") if isinstance(call.get("customer"), dict) else {}
    kind = text(call.get("type"))
    direction = {"inboundPhoneCall": "inbound", "outboundPhoneCall": "outbound",
                 "webCall": "web", "vapi.websocketCall": "web"}.get(kind, "unknown")
    return {"vapi_call_id": text(call["id"]), "remote_phone": normalize_phone(customer.get("number")),
            "name_snapshot": text(customer.get("name")), "direction": direction, "provider_type": kind,
            "vapi_assistant_id": text(call.get("assistantId")), "vapi_phone_number_id": text(call.get("phoneNumberId"))}


def call_data(call: object) -> dict:
    """Select validated metadata and spoken conversation from a provider call.

    Args:
        call: Untrusted JSON returned by Vapi's list or detail endpoint.

    Returns:
        Fields accepted by the archive database writer, without a raw payload.

    Raises:
        InvalidCall: The call lacks identity or contains invalid timestamps.
    """
    if not isinstance(call, dict) or not text(call.get("id")) or not call.get("createdAt"):
        raise InvalidCall("invalid_call")
    cost = number(call.get("cost"))
    return {**_call_identity(call), **_call_times(call), **_conversation(call),
            "provider_status": text(call.get("status")), "ended_reason": text(call.get("endedReason")),
            "cost": cost, "currency": "USD" if cost is not None else None}


def _split_window(start: datetime, end: datetime) -> list[list[str]]:
    """Split inclusive millisecond bounds, failing rather than skipping a full timestamp."""
    if end - start <= timedelta(milliseconds=1):
        raise InvalidCall("saturated_window")
    middle = start + timedelta(milliseconds=int((end - start).total_seconds() * 500))
    return [[start.isoformat(), middle.isoformat()], [middle.isoformat(), end.isoformat()]]


def _save_window(calls: list[dict], start: datetime, end: datetime, result: dict) -> None:
    """Validate the entire response before saving calls and counting accepted records."""
    rows = [call_data(call) for call in calls]
    if any(not start <= row["provider_created_at"] <= end for row in rows):
        raise InvalidCall("outside_scan_window")
    result["saved"] += db.save_archive_calls(rows)


def _discover_windows(claim: dict, deadline: float, result: dict, max_requests: int) -> None:
    """Resume scan subdivisions and advance their queue only after durable writes.

    Args:
        claim: Leased root window with its token and pending subdivisions.
        deadline: Monotonic invocation deadline; reserve twelve seconds for refresh.
        result: Mutable invocation counters, retaining partial counts on failure.
        max_requests: Provider list requests allowed for this stream in this run.

    Raises:
        InvalidCall: A window is saturated, a response is invalid, or ownership is lost.
    """
    windows = claim["pending_windows"]
    for _ in range(max_requests):
        if not windows or time.monotonic() >= deadline - 12:
            break
        start, end = (moment(value) for value in windows[0])
        calls = vapi.list_archive_calls(start, end)
        if len(calls) >= config.ARCHIVE_LIST_LIMIT:
            windows = [*_split_window(start, end), *windows[1:]]
        else:
            _save_window(calls, start, end, result)
            windows = windows[1:]
        if not db.save_archive_scan_progress(claim["claim_token"], windows):
            raise InvalidCall("lease_lost")


def _error_code(exc: Exception) -> str:
    """Name a failure by its generic archive code, or by its type when the message could hold private text."""
    archive_error = isinstance(exc, (InvalidCall, vapi.VapiUnavailable, storage.StorageUnavailable))
    return str(exc) if archive_error and ERROR_CODE_RE.fullmatch(str(exc)) else type(exc).__name__


def _reconcile(deadline: float, result: dict) -> None:
    """Sweep the provider's retention period oldest first, importing missed calls and refreshing known ones.

    A few requests per run; its first full sweep is the backfill of calls made before archiving
    started. Failures are kept in its own scan row and `reconcile_error`, never failing the run.

    Args:
        deadline: Monotonic invocation deadline shared with the other steps.
        result: Mutable invocation counters.
    """
    error = None
    try:
        claim = db.claim_archive_scan(datetime.now(timezone.utc), "reconcile")
        if claim is None:
            return
        try:
            _discover_windows(claim, deadline, result, config.ARCHIVE_MAX_RECONCILE_REQUESTS)
        except Exception as exc:
            error = _error_code(exc)
        if not db.finish_archive_scan(claim["claim_token"], error):
            error = "lease_lost"
    except Exception as exc:
        error = _error_code(exc)
    if error:
        get_logger(__name__).warning("Call reconciliation failed (%s)", error)
        result["reconcile_error"] = error


def _set_failure(result: dict, error: str) -> None:
    """Record a generic failure while preserving the first failure category."""
    result["ok"] = False
    result.setdefault("error", error)


def _refresh_call(call_id: str) -> None:
    """Refresh one known call without accepting an unrelated provider identity."""
    row = call_data(vapi.get_archive_call(call_id))
    if row["vapi_call_id"] != call_id:
        raise InvalidCall("call_id_mismatch")
    db.save_archive_call(row)


def _refresh_calls(deadline: float, result: dict) -> None:
    """Rotate known calls fairly and continue after individual provider failures.

    Args:
        deadline: Monotonic invocation deadline shared with discovery.
        result: Mutable counters and first failure, preserved across refresh errors.
    """
    try:
        for call_id in db.archive_calls_to_refresh(config.ARCHIVE_MAX_REFRESH_CALLS):
            if time.monotonic() >= deadline:
                break
            try:
                _refresh_call(call_id)
                result["refreshed"] += 1
            except Exception:
                db.mark_archive_poll_failed(call_id)
                _set_failure(result, "provider_refresh_failed")
    except Exception:
        _set_failure(result, "refresh_failed")


def _audio_media_type(head: bytes) -> str:
    """Identify WAV or MP3 audio from its first bytes instead of trusting the download's headers."""
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    raise InvalidCall("unsupported_audio")


def _spool_audio(response: requests.Response, deadline: float) -> AudioFile:
    """Stream a recording into a temporary file, enforcing the size cap and time budget."""
    body = tempfile.SpooledTemporaryFile(max_size=config.ARCHIVE_AUDIO_SPOOL_BYTES)
    digest, size = hashlib.sha256(), 0
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            size += len(chunk)
            if size > config.ARCHIVE_AUDIO_MAX_BYTES:
                raise InvalidCall("audio_too_large")
            if time.monotonic() >= deadline:
                raise InvalidCall("audio_deadline")
            digest.update(chunk)
            body.write(chunk)
        body.seek(0)
        media_type = _audio_media_type(body.read(12))
        body.seek(0)
    except BaseException:
        body.close()
        raise
    return AudioFile(body, media_type, size, digest.hexdigest())


def download_audio(url: str, deadline: float) -> AudioFile:
    """Download a signed recording URL without sending provider credentials.

    Args:
        url: Short-lived HTTPS URL from `vapi.recording_url`; never logged.
        deadline: Monotonic time after which the download is abandoned.

    Returns:
        The spooled recording with its detected media type, size and SHA-256.

    Raises:
        InvalidCall: The file is too large, too slow, empty or not WAV/MP3.
        vapi.VapiUnavailable: The download request fails.
    """
    try:
        with requests.get(url, stream=True, timeout=(3, 15)) as response:
            response.raise_for_status()
            declared = response.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > config.ARCHIVE_AUDIO_MAX_BYTES:
                raise InvalidCall("audio_too_large")
            return _spool_audio(response, deadline)
    except requests.RequestException:
        raise vapi.VapiUnavailable("recording_download_failed") from None


def transfer_audio(record_id: UUID | str, call_id: str, deadline: float) -> dict | None:
    """Copy a call's recording from Vapi into private storage under a deterministic key.

    Args:
        record_id: Durable call record UUID; names the object, so retries replace it.
        call_id: Provider call ID whose stereo recording is copied.
        deadline: Monotonic time after which the download is abandoned.

    Returns:
        Ready audio fields for `call_records`, or None while Vapi has no recording.

    Raises:
        InvalidCall: The recording is unusable (see `download_audio`).
        vapi.VapiUnavailable: Vapi can't be asked or the download fails.
        storage.StorageUnavailable: The upload isn't confirmed.
    """
    url = vapi.recording_url(call_id)
    if url is None:
        return None
    audio = download_audio(url, deadline)
    key = f"calls/{record_id}.{AUDIO_EXTENSIONS[audio.media_type]}"
    with audio.body:
        storage.upload_object(key, audio.body, audio.media_type, audio.size)
    return {"audio_state": "ready", "audio_object_key": key, "audio_media_type": audio.media_type,
            "audio_bytes": audio.size, "audio_sha256": audio.sha256}


def _transfer_job(job: dict, deadline: float) -> str | None:
    """Copy one claimed recording; returns None once saved, otherwise the retry reason."""
    try:
        fields = transfer_audio(job["id"], job["vapi_call_id"], deadline)
    except Exception as exc:
        return _error_code(exc)
    if fields is None:
        return "recording_not_ready"
    return None if db.finish_archive_audio(job["id"], job["archive_claim_token"], fields) else "lease_lost"


def _cleanup_audio(deadline: float, result: dict) -> None:
    """Delete queued recordings of deleted or merged calls; failures are retried with backoff.

    Args:
        deadline: Monotonic invocation deadline shared with the other steps.
        result: Mutable invocation counters (`cleaned`, `cleanup_failed`).
    """
    try:
        for _ in range(config.ARCHIVE_MAX_CLEANUP_PER_RUN):
            job = db.claim_audio_cleanup() if time.monotonic() < deadline else None
            if job is None:
                break
            try:
                storage.delete_object(job["object_key"])
            except Exception as exc:
                db.retry_audio_cleanup(job["object_key"], job["claim_token"], _error_code(exc))
                get_logger(__name__).warning("Call recording cleanup failed (%s)", _error_code(exc))
                result["cleanup_failed"] += 1
                continue
            db.finish_audio_cleanup(job["object_key"], job["claim_token"])
            result["cleaned"] += 1
    except Exception as exc:
        get_logger(__name__).warning("Call recording cleanup jobs failed (%s)", _error_code(exc))
        result["cleanup_failed"] += 1


def _archive_audio(deadline: float, result: dict) -> None:
    """Finalize stale transcripts and copy due recordings with the run's remaining budget.

    Audio failures are retried later with backoff and counted in `audio_failed`; they never
    mark the scan as failed, so recording problems can't hold back call discovery.

    Args:
        deadline: Monotonic invocation deadline shared with discovery and refresh.
        result: Mutable invocation counters.
    """
    try:
        db.finalize_archive_transcripts()
        for _ in range(config.ARCHIVE_MAX_AUDIO_PER_RUN):
            job = db.claim_archive_audio() if time.monotonic() < deadline - config.ARCHIVE_AUDIO_MIN_SECONDS else None
            if job is None:
                break
            error = _transfer_job(job, deadline)
            if error is None:
                result["audio"] += 1
                continue
            if error != "lease_lost":
                db.retry_archive_audio(job["id"], job["archive_claim_token"], error, final=error == "audio_too_large")
            if error != "recording_not_ready":
                get_logger(__name__).warning("Call recording transfer failed (%s)", error)
                result["audio_failed"] += 1
    except Exception as exc:
        get_logger(__name__).warning("Call recording jobs failed (%s)", _error_code(exc))
        result["audio_failed"] += 1


def discover_calls() -> dict:
    """Run bounded discovery, reconciliation, refresh, recording cleanup and audio transfer, always releasing the scan lease.

    Returns:
        Saved/refreshed/audio/cleanup counts, success and busy flags, and a generic error on failure.
        Logs omit provider bodies and SQL exception messages containing private data.

    Raises:
        Exception: Scan acquisition or finalization fails at the database boundary.
    """
    result = {"saved": 0, "refreshed": 0, "audio": 0, "audio_failed": 0, "cleaned": 0, "cleanup_failed": 0,
              "ok": True, "busy": False}
    deadline = time.monotonic() + config.ARCHIVE_RUN_SECONDS
    claim = db.claim_archive_scan(datetime.now(timezone.utc))
    if claim is None:
        return {**result, "busy": True}
    try:
        _discover_windows(claim, deadline, result, config.ARCHIVE_MAX_LIST_REQUESTS)
    except Exception as exc:
        error = _error_code(exc)
        get_logger(__name__).warning("Call discovery failed (%s)", error)
        _set_failure(result, error)
    finally:
        _reconcile(deadline, result)
        _refresh_calls(deadline, result)
        _cleanup_audio(deadline, result)
        _archive_audio(deadline, result)
        if not db.finish_archive_scan(claim["claim_token"], result.get("error")):
            result.update(ok=False, error="lease_lost")
    return result
