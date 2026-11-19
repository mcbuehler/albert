import os
import smtplib
import ssl
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from albert import config, notify
from main import app

PHONE = "+41791234567"
SMTP_ENV = {"SMTP_USER": "albert@example.com", "SMTP_PASSWORD": "app-password", "TEAM_EMAIL": "team@example.com"}


class NotifyTestCase(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        patcher = patch.dict(os.environ, SMTP_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.smtp_class = self._start(patch("albert.notify.smtplib.SMTP"))
        self.smtp = self.smtp_class.return_value.__enter__.return_value
        self.db = self._start(patch("albert.notify.db"))
        self.db.insert_team_notification.return_value = {"id": "n-1", "created_at": None}

    def _start(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def sent_message(self):
        return self.smtp.send_message.call_args.args[0]


class SendEmailTests(NotifyTestCase):
    def test_sends_via_starttls_with_login(self):
        with self.assertNoLogs(app.logger, "WARNING"):
            self.assertTrue(notify.send_email("Subject", "Body"))
        self.smtp_class.assert_called_once_with(config.SMTP_HOST, config.SMTP_PORT,
                                                timeout=config.SMTP_TIMEOUT_SECONDS)
        self.smtp.starttls.assert_called_once()
        context = self.smtp.starttls.call_args.kwargs["context"]
        self.assertEqual((context.verify_mode, context.check_hostname), (ssl.CERT_REQUIRED, True))
        self.smtp.login.assert_called_once_with("albert@example.com", "app-password")
        message = self.sent_message()
        self.assertEqual((message["From"], message["To"], message["Subject"]),
                         ("albert@example.com", "team@example.com", "Subject"))
        self.assertEqual(message.get_content(), "Body\n")

    def test_subject_line_breaks_removed(self):
        notify.send_email("Lead:\r\nBcc: evil@example.com  x", "Body")
        self.assertEqual(self.sent_message()["Subject"], "Lead: Bcc: evil@example.com x")
        self.assertIsNone(self.sent_message()["Bcc"])

    def test_not_configured(self):
        for name in SMTP_ENV:
            with self.subTest(name), patch.dict(os.environ, {name: ""}):
                with self.assertLogs(app.logger, "WARNING") as logs:
                    self.assertFalse(notify.send_email("S", "B"))
                self.assertIn("SMTP_USER, SMTP_PASSWORD or TEAM_EMAIL is not configured", logs.output[0])
        self.smtp_class.assert_not_called()

    def test_smtp_errors_logged_without_details(self):
        for error in (smtplib.SMTPAuthenticationError(535, b"app-password wrong"), TimeoutError("timed out"),
                      OSError("unreachable")):
            with self.subTest(error=type(error).__name__):
                self.smtp.login.side_effect = error
                with self.assertLogs(app.logger, "WARNING") as logs:
                    self.assertFalse(notify.send_email("S", "B"))
                self.assertIn(f"Team email not sent: {type(error).__name__}", logs.output[0])
                self.assertNotIn("app-password", "\n".join(logs.output))


ROW = {"id": "n-1", "created_at": None, "reason": "institution_lead", "caller_phone": PHONE,
       "contact_name": "Maria Keller", "organization": "Sunshine Home", "summary": "Wants a demo for 20 residents.",
       "vapi_call_id": "call-1", "email_claimed_at": datetime(2026, 10, 23, 12, tzinfo=timezone.utc)}


class NotifyTeamTests(NotifyTestCase):
    def test_only_stores_no_email_during_the_call(self):
        self.assertTrue(notify.notify_team("institution_lead", PHONE, "Maria Keller", "Sunshine Home",
                                           "Wants a demo for 20 residents.", "call-1"))
        self.db.insert_team_notification.assert_called_once_with(
            "institution_lead", PHONE, "Maria Keller", "Sunshine Home", "Wants a demo for 20 residents.", "call-1")
        self.smtp_class.assert_not_called()

    def test_db_failure_emails_right_away(self):
        self.db.insert_team_notification.side_effect = RuntimeError(f"Failing row contains ({PHONE}, Wants a taxi)")
        with self.assertLogs(app.logger, "ERROR") as logs:
            self.assertTrue(notify.notify_team("request", PHONE, summary="Wants a taxi."))
        self.assertIn("Failed to store team notification (request): RuntimeError; emailing it now", logs.output[0])
        self.assertNotIn(PHONE, "\n".join(logs.output))
        body = self.sent_message().get_content()
        self.assertIn("Wants a taxi.", body)
        self.assertIn("Note: this notification could not be saved in the database.", body)

    def test_both_fail(self):
        self.db.insert_team_notification.side_effect = RuntimeError("db down")
        self.smtp.send_message.side_effect = smtplib.SMTPException("x")
        with self.assertLogs(app.logger, "WARNING"):
            self.assertFalse(notify.notify_team("request", PHONE))

    def test_texts_trimmed_and_limited(self):
        long_name = "Margaret " + "x" * 200  # e.g. first + last name from a sign-up (100 + 1 + 100 characters)
        notify.notify_team("inbound_signup", PHONE, long_name, " Org ", "s" * 2000, "call-1")
        self.db.insert_team_notification.assert_called_once_with(
            "inbound_signup", PHONE, long_name[:config.MAX_NOTIFICATION_NAME_LENGTH], "Org",
            "s" * config.MAX_NOTIFICATION_SUMMARY_LENGTH, "call-1")

    def test_empty_or_non_text_fields_stored_as_null(self):
        notify.notify_team("request", PHONE, "  ", 5, "", None)
        self.db.insert_team_notification.assert_called_once_with("request", PHONE, None, None, None, None)

    def test_unusable_number_stored_as_null(self):
        for number in ("{{customer.number}}", "anonymous", "x" * 500, None):
            with self.subTest(number=number):
                self.db.insert_team_notification.reset_mock()
                notify.notify_team("request", number)
                self.assertIsNone(self.db.insert_team_notification.call_args.args[1])

    def test_number_normalized(self):
        notify.notify_team("request", "0041 79 123 45 67")
        self.assertEqual(self.db.insert_team_notification.call_args.args[1], PHONE)

    def test_logs_never_contain_phone_or_text(self):
        with self.assertLogs(app.logger, "DEBUG") as logs:
            notify.notify_team("caregiver_lead", PHONE, "Maria Keller", "Org", "Her mother", "call-1")
        text = "\n".join(logs.output)
        for secret in (PHONE, "Maria Keller", "Her mother"):
            self.assertNotIn(secret, text)


class EmailTextTests(NotifyTestCase):
    def test_full_notification(self):
        subject, body = notify._email(ROW)
        self.assertEqual(subject, "[Albert] Institution lead: Maria Keller / Sunshine Home")
        for text in ("Reason: Institution lead", f"Caller number: {PHONE}", "Contact name: Maria Keller",
                     "Organization: Sunshine Home", "Vapi call id: call-1", "Wants a demo for 20 residents."):
            self.assertIn(text, body)
        self.assertNotIn("could not be saved", body)

    def test_minimal_notification(self):
        subject, body = notify._email({**ROW, "reason": "no_consent", "caller_phone": None, "contact_name": None,
                                       "organization": None, "summary": None, "vapi_call_id": None})
        self.assertEqual(subject, "[Albert] Caller doesn't want to be recorded")
        for text in ("Caller number: unknown", "Contact name: -", "Organization: -", "Vapi call id: -",
                     "(no summary)"):
            self.assertIn(text, body)

    def test_unknown_reason_shown_as_is(self):
        self.assertEqual(notify._email({**ROW, "reason": "legacy", "contact_name": None,
                                        "organization": None})[0], "[Albert] legacy")


class SendPendingEmailsTests(NotifyTestCase):
    def test_sends_claimed_notifications(self):
        self.db.claim_unsent_notifications.return_value = [ROW, {**ROW, "id": "n-2"}]
        with self.assertLogs(app.logger, "INFO") as logs:
            self.assertEqual(notify.send_pending_emails(), 2)
        self.db.claim_unsent_notifications.assert_called_once_with(config.MAX_NOTIFICATION_EMAILS_PER_RUN,
                                                                   config.NOTIFICATION_EMAIL_MAX_AGE_HOURS,
                                                                   config.NOTIFICATION_EMAIL_CLAIM_MINUTES)
        self.assertEqual(self.smtp.send_message.call_count, 2)
        self.assertEqual(self.sent_message()["Subject"], "[Albert] Institution lead: Maria Keller / Sunshine Home")
        self.assertEqual(self.db.mark_notification_emailed.call_args_list, [
            unittest.mock.call("n-1", ROW["email_claimed_at"]),
            unittest.mock.call("n-2", ROW["email_claimed_at"]),
        ])
        self.db.release_notification.assert_not_called()
        self.assertIn("Emailed 2 team notification(s)", "\n".join(logs.output))

    def test_nothing_pending(self):
        self.db.claim_unsent_notifications.return_value = []
        with self.assertNoLogs(app.logger, "INFO"):
            self.assertEqual(notify.send_pending_emails(), 0)
        self.smtp_class.assert_not_called()

    def test_failed_email_released_for_next_run(self):
        self.db.claim_unsent_notifications.return_value = [ROW, {**ROW, "id": "n-2"}]
        self.smtp.send_message.side_effect = [smtplib.SMTPException("x"), None]
        with self.assertLogs(app.logger, "WARNING"):
            self.assertEqual(notify.send_pending_emails(), 1)
        self.db.release_notification.assert_called_once_with("n-1", ROW["email_claimed_at"])
        self.db.mark_notification_emailed.assert_called_once_with("n-2", ROW["email_claimed_at"])

    def test_odd_text_doesnt_stop_the_others(self):
        self.db.claim_unsent_notifications.return_value = [{**ROW, "summary": "\ud800"}, {**ROW, "id": "n-2"}]
        self.smtp.send_message.side_effect = None
        with self.assertLogs(app.logger, "WARNING"):
            self.assertEqual(notify.send_pending_emails(), 1)
        self.db.release_notification.assert_called_once_with("n-1", ROW["email_claimed_at"])
        self.db.mark_notification_emailed.assert_called_once_with("n-2", ROW["email_claimed_at"])

    def test_release_failure_logged(self):
        self.db.claim_unsent_notifications.return_value = [ROW]
        self.smtp.send_message.side_effect = smtplib.SMTPException("x")
        self.db.release_notification.side_effect = RuntimeError("db down")
        with self.assertLogs(app.logger, "WARNING") as logs:
            self.assertEqual(notify.send_pending_emails(), 0)
        self.assertIn("Failed to release team notification n-1: RuntimeError", "\n".join(logs.output))

    def test_mark_failure_is_logged_and_left_for_lease_retry(self):
        self.db.claim_unsent_notifications.return_value = [ROW]
        self.db.mark_notification_emailed.side_effect = RuntimeError("db down")
        with self.assertLogs(app.logger, "ERROR") as logs:
            self.assertEqual(notify.send_pending_emails(), 1)
        self.assertIn("Failed to mark team notification n-1 emailed: RuntimeError", "\n".join(logs.output))
        self.db.release_notification.assert_not_called()

    def test_expired_claim_before_mark_is_logged(self):
        self.db.claim_unsent_notifications.return_value = [ROW]
        self.db.mark_notification_emailed.return_value = False
        with self.assertLogs(app.logger, "WARNING") as logs:
            self.assertEqual(notify.send_pending_emails(), 1)
        self.assertIn("claim expired before recording it", "\n".join(logs.output))


class NotifyTeamToolTests(NotifyTestCase):
    def setUp(self):
        super().setUp()
        self.notify_team = self._start(patch("albert.notify.notify_team", return_value=True))

    def test_passes_arguments(self):
        result = notify.notify_team_tool({"reason": "caregiver_lead", "contact_name": "Tom", "organization": "",
                                          "summary": "Testing for his father."}, PHONE, "call-1")
        self.assertEqual(result, "The team has been notified.")
        self.notify_team.assert_called_once_with("caregiver_lead", PHONE, contact_name="Tom", organization="",
                                                 summary="Testing for his father.", vapi_call_id="call-1")

    def test_unknown_or_missing_reason_is_other(self):
        for reason in (None, "", "spam", 3, ["no_consent"]):
            with self.subTest(reason=reason):
                notify.notify_team_tool({"reason": reason}, PHONE, None)
                self.assertEqual(self.notify_team.call_args.args[0], "other")

    def test_all_reasons_accepted(self):
        for reason in config.NOTIFICATION_REASONS:
            with self.subTest(reason):
                notify.notify_team_tool({"reason": reason}, PHONE, None)
                self.assertEqual(self.notify_team.call_args.args[0], reason)

    def test_failure_sentence(self):
        self.notify_team.return_value = False
        self.assertIn("Could not notify the team right now", notify.notify_team_tool({}, PHONE, None))


if __name__ == "__main__":
    unittest.main()
