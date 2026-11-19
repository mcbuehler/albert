"""Discovery logic and provider reads, without external services."""

import hashlib
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import requests

from albert import archive, config, vapi

NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


def provider_call(**fields: object) -> dict:
    """Build a provider response fixture with optional field overrides."""
    return {"id": "provider-call", "createdAt": NOW.isoformat(), "updatedAt": NOW.isoformat(),
            "type": "inboundPhoneCall", "status": "ended", **fields}


class CallDataTests(unittest.TestCase):
    def test_all_call_types_and_unknown_numbers(self) -> None:
        """Verify all call types and unknown numbers."""
        for kind, direction in (("inboundPhoneCall", "inbound"), ("outboundPhoneCall", "outbound"),
                                ("webCall", "web"), ("vapi.websocketCall", "web"), ("future", "unknown")):
            row = archive.call_data(provider_call(type=kind, customer={"number": "anonymous"}))
            self.assertEqual(row["direction"], direction)
            self.assertIsNone(row["remote_phone"])

    def test_normalized_number_and_optional_metadata(self) -> None:
        """Verify normalized number and optional metadata."""
        row = archive.call_data(provider_call(customer={"number": "0041 (0)79 123 45 67", "name": " Anna "},
                                              startedAt=NOW.isoformat(), endedAt=(NOW + timedelta(seconds=15)).isoformat(), cost=0.2))
        self.assertEqual(row["remote_phone"], "+41791234567")
        self.assertEqual((row["name_snapshot"], row["duration_seconds"], row["cost"], row["currency"]), ("Anna", 15, 0.2, "USD"))

    def test_message_allowlist_excludes_memory_and_tools(self) -> None:
        """Verify message allowlist excludes memory and tools."""
        row = archive.call_data(provider_call(artifact={"messages": [
            {"role": "system", "message": "PRIVATE MEMORY"}, {"role": "tool", "message": "PRIVATE RESULT"},
            {"role": "assistant", "toolCalls": [{"arguments": "PRIVATE ARGUMENTS"}]},
            {"role": "bot", "message": "Hello", "secondsFromStart": 1, "toolCalls": ["PRIVATE"], "metadata": "PRIVATE"},
            {"role": "user", "message": "Cooking", "time": 2, "originalMessage": "PRIVATE"},
        ]}, assistantOverrides={"variableValues": {"previous_messages": "PRIVATE MEMORY"}}))
        self.assertEqual(row["messages"], [{"role": "assistant", "message": "Hello", "secondsFromStart": 1},
                                           {"role": "user", "message": "Cooking", "time": 2}])
        self.assertNotIn("PRIVATE", str(row))
        self.assertEqual(row["transcript"], "Assistant: Hello\nCustomer: Cooking")

    def test_transcript_fallback_without_raw_message_history(self) -> None:
        """Verify transcript fallback without raw message history."""
        self.assertEqual(archive.call_data(provider_call(transcript="Customer: Hi"))["transcript"], "Customer: Hi")
        row = archive.call_data(provider_call(messages=[{"role": "system", "message": "private"}], transcript="private"))
        self.assertIsNone(row["transcript"])

    def test_explicit_disabled_artifacts(self) -> None:
        """Verify explicit disabled artifacts."""
        row = archive.call_data(provider_call(assistant={"artifactPlan": {"recordingEnabled": False,
                                                                         "transcriptPlan": {"enabled": False}}}))
        self.assertEqual((row["audio_state"], row["transcript_state"]), ("disabled", "disabled"))

    def test_invalid_provider_metadata_fails_closed(self) -> None:
        """Verify invalid provider metadata fails closed."""
        for fields in ({"id": None}, {"createdAt": None}, {"createdAt": "bad"}, {"createdAt": "2026-11-02T12:00:00"},
                       {"startedAt": NOW.isoformat(), "endedAt": (NOW - timedelta(seconds=1)).isoformat()}):
            with self.subTest(fields=fields), self.assertRaises(archive.InvalidCall):
                archive.call_data(provider_call(**fields))

    def test_nonfinite_and_boolean_numbers_are_not_stored(self) -> None:
        """Verify nonfinite and boolean numbers are not stored."""
        for cost in (float("nan"), float("inf"), -1, True, "1"):
            self.assertIsNone(archive.call_data(provider_call(cost=cost))["cost"])


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        """Prepare isolated archive mocks and a leased scan window."""
        patcher = patch("albert.archive.db")
        self.db = patcher.start()
        self.addCleanup(patcher.stop)
        self.window = [(NOW - timedelta(minutes=2)).isoformat(), NOW.isoformat()]
        self.db.claim_archive_scan.return_value = {"claim_token": "token", "pending_windows": [self.window]}
        self.db.save_archive_scan_progress.return_value = True
        self.db.finish_archive_scan.return_value = True
        self.db.archive_calls_to_refresh.return_value = []
        self.db.claim_archive_audio.return_value = None
        self.db.claim_audio_cleanup.return_value = None
        self.db.save_archive_calls.side_effect = len
        patcher = patch("albert.archive.vapi.list_archive_calls", return_value=[])
        self.list_calls = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("albert.archive.vapi.get_archive_call")
        self.get_call = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("albert.archive._reconcile")  # covered by ReconcileTests
        self.reconcile = patcher.start()
        self.addCleanup(patcher.stop)

    def test_busy_scan_does_no_provider_work(self) -> None:
        """Verify busy scan does no provider work."""
        self.db.claim_archive_scan.return_value = None
        self.assertTrue(archive.discover_calls()["busy"])
        self.list_calls.assert_not_called()

    def test_saves_before_advancing_window(self) -> None:
        """Verify saves before advancing window."""
        self.list_calls.return_value = [provider_call()]
        self.assertEqual(archive.discover_calls()["saved"], 1)
        self.db.save_archive_scan_progress.assert_called_once_with("token", [])
        self.db.finish_archive_scan.assert_called_once_with("token", None)

    def test_database_failure_keeps_window(self) -> None:
        """Verify database failure keeps window."""
        self.list_calls.return_value = [provider_call()]
        self.db.save_archive_calls.side_effect = RuntimeError("PRIVATE SQL ROW")
        with self.assertLogs("albert.archive", "WARNING") as logs:
            self.assertFalse(archive.discover_calls()["ok"])
        self.assertNotIn("PRIVATE", str(logs.output))
        self.db.save_archive_scan_progress.assert_not_called()

    def test_full_window_splits_and_resumes_with_budget(self) -> None:
        """Verify full window splits and resumes with budget."""
        self.list_calls.return_value = [provider_call()] * config.ARCHIVE_LIST_LIMIT
        with patch.object(config, "ARCHIVE_MAX_LIST_REQUESTS", 1):
            result = archive.discover_calls()
        self.assertTrue(result["ok"])
        self.db.save_archive_calls.assert_not_called()
        windows = self.db.save_archive_scan_progress.call_args.args[1]
        self.assertEqual(len(windows), 2)
        self.assertEqual((windows[0][0], windows[1][1]), tuple(self.window))
        self.assertEqual(windows[0][1], windows[1][0])

    def test_saturated_minimum_window_fails_without_progress(self) -> None:
        """Verify saturated minimum window fails without progress."""
        self.db.claim_archive_scan.return_value["pending_windows"] = [[NOW.isoformat(), (NOW + timedelta(milliseconds=1)).isoformat()]]
        self.list_calls.return_value = [provider_call()] * config.ARCHIVE_LIST_LIMIT
        with self.assertLogs("albert.archive", "WARNING"):
            result = archive.discover_calls()
        self.assertEqual(result["error"], "saturated_window")
        self.db.save_archive_scan_progress.assert_not_called()

    def test_bad_call_or_out_of_window_call_does_not_advance(self) -> None:
        """Verify bad call or out of window call does not advance."""
        for call in (provider_call(id=None), provider_call(createdAt=(NOW + timedelta(days=1)).isoformat())):
            self.db.reset_mock()
            self.list_calls.return_value = [call]
            with self.assertLogs("albert.archive", "WARNING"):
                self.assertFalse(archive.discover_calls()["ok"])
            self.db.save_archive_calls.assert_not_called()
            self.db.save_archive_scan_progress.assert_not_called()

    def test_refresh_runs_even_after_discovery_failure(self) -> None:
        """Verify refresh runs even after discovery failure."""
        self.list_calls.side_effect = vapi.VapiUnavailable("PRIVATE PROVIDER BODY")
        self.db.archive_calls_to_refresh.return_value = ["first", "second"]
        self.get_call.side_effect = [vapi.VapiUnavailable("failed"), provider_call(id="second")]
        with self.assertLogs("albert.archive", "WARNING") as logs:
            result = archive.discover_calls()
        self.assertEqual(result["refreshed"], 1)
        self.db.mark_archive_poll_failed.assert_called_once_with("first")
        self.assertNotIn("PRIVATE", str(logs.output))

    def test_lost_scan_lease_is_reported(self) -> None:
        """Verify lost scan lease is reported."""
        self.db.save_archive_scan_progress.return_value = False
        self.db.finish_archive_scan.return_value = False
        with self.assertLogs("albert.archive", "WARNING"):
            self.assertEqual(archive.discover_calls()["error"], "lease_lost")

    def test_budget_stops_requests_and_keeps_pending_work(self) -> None:
        """Verify budget stops requests and keeps pending work."""
        with patch("albert.archive.time.monotonic", side_effect=[0, config.ARCHIVE_RUN_SECONDS + 1]):
            self.assertTrue(archive.discover_calls()["ok"])
        self.list_calls.assert_not_called()
        self.db.save_archive_scan_progress.assert_not_called()


class ArchiveProviderTests(unittest.TestCase):
    def test_list_has_no_outbound_only_filter(self) -> None:
        """Verify list has no outbound only filter."""
        response = Mock(ok=True)
        response.json.return_value = []
        with patch("albert.vapi.config.env", return_value="key"), patch("albert.vapi.requests.get", return_value=response) as get:
            self.assertEqual(vapi.list_archive_calls(NOW, NOW), [])
        params = get.call_args.kwargs["params"]
        self.assertEqual(set(params), {"createdAtGe", "createdAtLe", "limit"})

    def test_malformed_or_failed_reads_are_not_empty_results(self) -> None:
        """Verify malformed or failed reads are not empty results."""
        response = Mock()
        with patch("albert.vapi.config.env", return_value="key"), patch("albert.vapi.requests.get", return_value=response):
            for payload in ({}, [None], [{}, "bad"]):
                response.json.return_value = payload
                with self.assertRaises(vapi.VapiUnavailable):
                    vapi.list_archive_calls(NOW, NOW)
            response.raise_for_status.side_effect = requests.HTTPError("PRIVATE BODY")
            with self.assertRaisesRegex(vapi.VapiUnavailable, "provider_read_failed"):
                vapi.get_archive_call("id")

    def test_missing_key_and_detail_shape(self) -> None:
        """Verify missing key and detail shape."""
        with patch("albert.vapi.config.env", return_value=None), self.assertRaises(vapi.VapiUnavailable):
            vapi.get_archive_call("id")
        response = Mock()
        response.json.return_value = []
        with patch("albert.vapi.config.env", return_value="key"), patch("albert.vapi.requests.get", return_value=response):
            with self.assertRaises(vapi.VapiUnavailable):
                vapi.get_archive_call("id")


WAV = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 100


def download_response(*chunks: bytes, length: str = "") -> Mock:
    """Build a streamed download response usable as a context manager."""
    response = Mock(headers={"Content-Length": length})
    response.iter_content.return_value = list(chunks)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


class RecordingUrlTests(unittest.TestCase):
    def resolve(self, status: int, location: str = "") -> str | None:
        """Resolve a recording URL against one mocked provider response."""
        response = Mock(status_code=status, headers={"Location": location} if location else {})
        with patch("albert.vapi.config.env", return_value="key"), patch("albert.vapi.requests.get", return_value=response) as get:
            result = vapi.recording_url("call/1")
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertTrue(get.call_args.args[0].endswith("/call%2F1/stereo-recording"))
        return result

    def test_redirect_target_is_returned_without_following_it(self) -> None:
        """Verify the signed URL is returned, so the API key never reaches the storage host."""
        self.assertEqual(self.resolve(302, "https://signed.example/a.wav"), "https://signed.example/a.wav")

    def test_missing_recording_is_none(self) -> None:
        """Verify a 404 means no recording yet rather than an error."""
        self.assertIsNone(self.resolve(404))

    def test_unusable_answers_raise(self) -> None:
        """Verify errors, missing or non-HTTPS redirects are failures, not empty results."""
        for status, location in ((500, ""), (200, ""), (302, ""), (302, "http://signed.example/a.wav")):
            with self.assertRaisesRegex(vapi.VapiUnavailable, "invalid_recording_redirect"):
                self.resolve(status, location)


class DownloadAudioTests(unittest.TestCase):
    def download(self, response: Mock, deadline: float = float("inf")) -> archive.AudioFile:
        """Download through a mocked signed URL."""
        with patch("albert.archive.requests.get", return_value=response) as get:
            audio = archive.download_audio("https://signed.example/a", deadline)
        self.assertNotIn("headers", get.call_args.kwargs)
        return audio

    def test_streams_and_detects_media_type(self) -> None:
        """Verify size, checksum and sniffed type for WAV and MP3."""
        audio = self.download(download_response(WAV[:10], WAV[10:]))
        with audio.body:
            self.assertEqual((audio.media_type, audio.size, audio.body.read()), ("audio/wav", len(WAV), WAV))
        self.assertEqual(audio.sha256, hashlib.sha256(WAV).hexdigest())
        for head in (b"ID3\x04", b"\xff\xfb\x90"):
            audio = self.download(download_response(head + b"\x00" * 20))
            audio.body.close()
            self.assertEqual(audio.media_type, "audio/mpeg")

    def test_rejects_unusable_files(self) -> None:
        """Verify empty, non-audio, oversized and late downloads fail."""
        cases = ((download_response(), "unsupported_audio"), (download_response(b"<html>error</html>"), "unsupported_audio"),
                 (download_response(WAV, length=str(config.ARCHIVE_AUDIO_MAX_BYTES + 1)), "audio_too_large"))
        for response, error in cases:
            with self.assertRaisesRegex(archive.InvalidCall, error):
                self.download(response)
        with patch.object(config, "ARCHIVE_AUDIO_MAX_BYTES", 50), self.assertRaisesRegex(archive.InvalidCall, "audio_too_large"):
            self.download(download_response(WAV))
        with self.assertRaisesRegex(archive.InvalidCall, "audio_deadline"):
            self.download(download_response(WAV), deadline=0)

    def test_request_failure_hides_signed_url(self) -> None:
        """Verify network errors become a generic provider failure."""
        response = download_response(WAV)
        response.raise_for_status.side_effect = requests.HTTPError("https://signed.example/secret")
        with self.assertRaises(vapi.VapiUnavailable) as caught:
            self.download(response)
        self.assertEqual(str(caught.exception), "recording_download_failed")


class TransferAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        """Patch Vapi and storage around the transfer."""
        self.url = patch("albert.archive.vapi.recording_url", return_value="https://signed.example/a").start()
        self.get = patch("albert.archive.requests.get", return_value=download_response(WAV)).start()
        self.upload = patch("albert.archive.storage.upload_object").start()
        self.addCleanup(patch.stopall)

    def test_uploads_under_deterministic_key(self) -> None:
        """Verify the record UUID names the object and the ready fields are returned."""
        fields = archive.transfer_audio("rec-1", "call-1", float("inf"))
        key, body, media_type, size = self.upload.call_args.args
        self.assertEqual((key, media_type, size), ("calls/rec-1.wav", "audio/wav", len(WAV)))
        self.assertTrue(body.closed)
        self.assertEqual(fields, {"audio_state": "ready", "audio_object_key": "calls/rec-1.wav", "audio_media_type": "audio/wav",
                                  "audio_bytes": len(WAV), "audio_sha256": hashlib.sha256(WAV).hexdigest()})

    def test_no_recording_yet(self) -> None:
        """Verify nothing is downloaded or uploaded without a recording."""
        self.url.return_value = None
        self.assertIsNone(archive.transfer_audio("rec-1", "call-1", float("inf")))
        self.get.assert_not_called()
        self.upload.assert_not_called()

    def test_upload_failure_propagates_and_closes_file(self) -> None:
        """Verify a failed upload raises and still releases the temporary file."""
        self.upload.side_effect = archive.storage.StorageUnavailable("storage_write_failed")
        with self.assertRaises(archive.storage.StorageUnavailable):
            archive.transfer_audio("rec-1", "call-1", float("inf"))
        self.assertTrue(self.upload.call_args.args[1].closed)


class AudioJobTests(unittest.TestCase):
    def setUp(self) -> None:
        """Patch the database and transfer around the audio job loop."""
        self.db = patch("albert.archive.db").start()
        self.transfer = patch("albert.archive.transfer_audio", return_value={"audio_object_key": "calls/rec-1.wav"}).start()
        self.addCleanup(patch.stopall)
        self.job = {"id": "rec-1", "vapi_call_id": "call-1", "archive_claim_token": "token"}
        self.db.claim_archive_audio.side_effect = [self.job, None]
        self.db.finish_archive_audio.return_value = True
        self.result = {"audio": 0, "audio_failed": 0}

    def run_jobs(self, deadline: float = float("inf")) -> dict:
        """Run the audio step once and return its counters."""
        archive._archive_audio(deadline, self.result)
        return self.result

    def test_saved_recording_is_counted(self) -> None:
        """Verify a transferred recording is finished and the transcript finalization runs."""
        self.assertEqual(self.run_jobs(), {"audio": 1, "audio_failed": 0})
        self.db.finish_archive_audio.assert_called_once_with("rec-1", "token", {"audio_object_key": "calls/rec-1.wav"})
        self.db.finalize_archive_transcripts.assert_called_once()
        self.db.retry_archive_audio.assert_not_called()

    def test_not_ready_retries_without_counting_a_failure(self) -> None:
        """Verify a missing recording is retried quietly."""
        self.transfer.return_value = None
        self.assertEqual(self.run_jobs(), {"audio": 0, "audio_failed": 0})
        self.db.retry_archive_audio.assert_called_once_with("rec-1", "token", "recording_not_ready", final=False)

    def test_failures_retry_and_too_large_is_final(self) -> None:
        """Verify failures are retried with their code, and oversized recordings are given up."""
        for exc, final in ((vapi.VapiUnavailable("recording_download_failed"), False),
                           (archive.InvalidCall("audio_too_large"), True), (RuntimeError("private detail"), False)):
            self.db.reset_mock()
            self.db.claim_archive_audio.side_effect = [self.job, None]
            self.transfer.side_effect = exc
            self.run_jobs()
            code = "RuntimeError" if isinstance(exc, RuntimeError) else str(exc)
            self.db.retry_archive_audio.assert_called_once_with("rec-1", "token", code, final=final)
        self.assertEqual(self.result["audio_failed"], 3)

    def test_lost_claim_is_not_retried(self) -> None:
        """Verify a lost claim leaves the job to its new owner."""
        self.db.finish_archive_audio.return_value = False
        self.assertEqual(self.run_jobs(), {"audio": 0, "audio_failed": 1})
        self.db.retry_archive_audio.assert_not_called()

    def test_budget_limits_jobs(self) -> None:
        """Verify no job starts without enough budget and at most the configured number run."""
        self.run_jobs(deadline=time.monotonic() + config.ARCHIVE_AUDIO_MIN_SECONDS - 1)
        self.db.claim_archive_audio.assert_not_called()
        self.db.claim_archive_audio.side_effect = None
        self.db.claim_archive_audio.return_value = self.job
        self.assertEqual(self.run_jobs()["audio"], config.ARCHIVE_MAX_AUDIO_PER_RUN)

    def test_database_failure_is_contained(self) -> None:
        """Verify a database error in the audio step doesn't escape."""
        self.db.finalize_archive_transcripts.side_effect = RuntimeError("private detail")
        self.assertEqual(self.run_jobs(), {"audio": 0, "audio_failed": 1})

    def test_discovery_runs_audio_without_failing_the_scan(self) -> None:
        """Verify discover_calls runs the audio step and audio failures keep the run ok."""
        self.db.claim_audio_cleanup.return_value = None
        self.db.claim_archive_scan.side_effect = [{"claim_token": "scan", "pending_windows": []}, None]
        self.db.archive_calls_to_refresh.return_value = []
        self.transfer.side_effect = vapi.VapiUnavailable("recording_download_failed")
        result = archive.discover_calls()
        self.assertEqual((result["ok"], result["audio_failed"]), (True, 1))
        self.db.finish_archive_scan.assert_called_once_with("scan", None)


class CleanupAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        """Patch the database and storage around the cleanup loop."""
        self.db = patch("albert.archive.db").start()
        self.delete = patch("albert.archive.storage.delete_object").start()
        self.addCleanup(patch.stopall)
        self.db.claim_audio_cleanup.side_effect = [{"object_key": "calls/a.wav", "claim_token": "token"}, None]
        self.result = {"cleaned": 0, "cleanup_failed": 0}

    def test_deleted_object_finishes_job(self) -> None:
        """Verify a deleted object removes its job."""
        archive._cleanup_audio(float("inf"), self.result)
        self.delete.assert_called_once_with("calls/a.wav")
        self.db.finish_audio_cleanup.assert_called_once_with("calls/a.wav", "token")
        self.assertEqual(self.result, {"cleaned": 1, "cleanup_failed": 0})

    def test_failed_deletion_is_retried(self) -> None:
        """Verify a storage failure keeps the job with backoff."""
        self.delete.side_effect = archive.storage.StorageUnavailable("storage_delete_failed")
        archive._cleanup_audio(float("inf"), self.result)
        self.db.retry_audio_cleanup.assert_called_once_with("calls/a.wav", "token", "storage_delete_failed")
        self.db.finish_audio_cleanup.assert_not_called()
        self.assertEqual(self.result, {"cleaned": 0, "cleanup_failed": 1})

    def test_budget_and_database_errors(self) -> None:
        """Verify nothing starts after the deadline and database errors are contained."""
        archive._cleanup_audio(0, self.result)
        self.db.claim_audio_cleanup.assert_not_called()
        self.db.claim_audio_cleanup.side_effect = RuntimeError("private detail")
        archive._cleanup_audio(float("inf"), self.result)
        self.assertEqual(self.result["cleanup_failed"], 1)


class ReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        """Patch the database and provider around one reconcile step."""
        self.db = patch("albert.archive.db").start()
        self.list_calls = patch("albert.archive.vapi.list_archive_calls", return_value=[provider_call()]).start()
        self.addCleanup(patch.stopall)
        window = [(NOW - timedelta(hours=6)).isoformat(), NOW.isoformat()]
        self.db.claim_archive_scan.return_value = {"claim_token": "sweep", "pending_windows": [window, window, window]}
        self.db.save_archive_scan_progress.return_value = True
        self.db.finish_archive_scan.return_value = True
        self.db.save_archive_calls.side_effect = len
        self.result = {"saved": 0}

    def test_bounded_requests_on_its_own_stream(self) -> None:
        """Verify the sweep claims the reconcile stream and stops after its request budget."""
        archive._reconcile(float("inf"), self.result)
        self.assertEqual(self.db.claim_archive_scan.call_args.args[1], "reconcile")
        self.assertEqual(self.list_calls.call_count, config.ARCHIVE_MAX_RECONCILE_REQUESTS)
        self.assertEqual(self.result["saved"], config.ARCHIVE_MAX_RECONCILE_REQUESTS)
        self.db.finish_archive_scan.assert_called_once_with("sweep", None)

    def test_failures_are_recorded_without_failing_the_run(self) -> None:
        """Verify provider errors, lost leases and database errors only set reconcile_error."""
        self.list_calls.side_effect = vapi.VapiUnavailable("provider_read_failed")
        archive._reconcile(float("inf"), self.result)
        self.db.finish_archive_scan.assert_called_once_with("sweep", "provider_read_failed")
        self.list_calls.side_effect = None
        self.db.finish_archive_scan.return_value = False
        archive._reconcile(float("inf"), self.result)
        self.assertEqual(self.result["reconcile_error"], "lease_lost")
        self.db.claim_archive_scan.side_effect = RuntimeError("private detail")
        archive._reconcile(float("inf"), self.result)
        self.assertEqual(self.result["reconcile_error"], "RuntimeError")
        self.assertNotIn("ok", self.result)

    def test_busy_sweep_is_skipped(self) -> None:
        """Verify nothing is listed while another run owns the sweep."""
        self.db.claim_archive_scan.return_value = None
        archive._reconcile(float("inf"), self.result)
        self.list_calls.assert_not_called()
        self.assertNotIn("reconcile_error", self.result)

    def test_discovery_runs_the_sweep(self) -> None:
        """Verify discover_calls runs reconciliation even when recent discovery fails."""
        self.db.claim_archive_scan.return_value = {"claim_token": "recent", "pending_windows": [[NOW.isoformat(), NOW.isoformat()]]}
        self.db.archive_calls_to_refresh.return_value = []
        self.db.claim_audio_cleanup.return_value = None
        self.db.claim_archive_audio.return_value = None
        with patch("albert.archive._reconcile") as reconcile:
            self.list_calls.side_effect = vapi.VapiUnavailable("provider_read_failed")
            self.assertFalse(archive.discover_calls()["ok"])
        reconcile.assert_called_once()
