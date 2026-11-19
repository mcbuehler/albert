"""Notes from Albert to the team: stored in `team_notifications` and emailed to env TEAM_EMAIL."""

import smtplib
import ssl
from email.message import EmailMessage

from flask import current_app

from albert import config, db
from albert.validation import normalize_phone


def _text(value, max_length: int) -> str | None:
    """A trimmed string of at most max_length characters, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:max_length]


def _one_line(text: str) -> str:
    """For email headers: no line breaks (header injection) and no runs of whitespace."""
    return " ".join(text.split())


def send_email(subject: str, body: str) -> bool:
    """Send a plain-text email to the team. False (logged) if not configured or sending failed."""
    user, password, to = config.env("SMTP_USER"), config.env("SMTP_PASSWORD"), config.env("TEAM_EMAIL")
    if not (user and password and to):
        current_app.logger.warning("Team email not sent: SMTP_USER, SMTP_PASSWORD or TEAM_EMAIL is not configured")
        return False
    try:
        message = EmailMessage()
        message["Subject"] = _one_line(subject)
        message["From"] = user
        message["To"] = to
        message.set_content(body)
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT_SECONDS) as smtp:
            smtp.starttls(context=ssl.create_default_context())  # verify the certificate and host name
            smtp.login(user, password)
            smtp.send_message(message)
    except Exception as e:  # also e.g. UnicodeEncodeError from odd text; never break the caller
        current_app.logger.warning("Team email not sent: %s", type(e).__name__)
        return False
    return True


def _email(notification: dict, saved: bool = True) -> tuple[str, str]:
    """Subject and body of the email for a team notification (a team_notifications row)."""
    label = config.NOTIFICATION_REASONS.get(notification["reason"], notification["reason"])
    who = " / ".join(part for part in (notification["contact_name"], notification["organization"]) if part)
    subject = f"[Albert] {label}" + (f": {who}" if who else "")
    body = "\n".join((
        f"Reason: {label}",
        f"Caller number: {notification['caller_phone'] or 'unknown'}",
        f"Contact name: {notification['contact_name'] or '-'}",
        f"Organization: {notification['organization'] or '-'}",
        f"Vapi call id: {notification['vapi_call_id'] or '-'}",
        "",
        notification["summary"] or "(no summary)",
        "",
        "" if saved else "Note: this notification could not be saved in the database.",
    )).rstrip() + "\n"
    return subject, body


def notify_team(reason: str, caller_phone: str | None, contact_name: str | None = None,
                organization: str | None = None, summary: str | None = None,
                vapi_call_id: str | None = None) -> bool:
    """Store a team notification; the dispatcher cron emails it (send_pending_emails), so a call never waits
    for the mail server. Only if it can't be stored is it emailed right away. True if either worked.

    Texts are trimmed to the table's limits and a number that isn't E.164 is dropped, so the insert can't fail on them.
    """
    caller_phone = normalize_phone(caller_phone)
    contact_name = _text(contact_name, config.MAX_NOTIFICATION_NAME_LENGTH)
    organization = _text(organization, config.MAX_NOTIFICATION_NAME_LENGTH)
    summary = _text(summary, config.MAX_NOTIFICATION_SUMMARY_LENGTH)
    try:
        db.insert_team_notification(reason, caller_phone, contact_name, organization, summary, vapi_call_id)
        current_app.logger.info("Team notification %s stored (call %s)", reason, vapi_call_id)
        return True
    except Exception as e:  # type only: database errors can contain the row (phone number, names)
        current_app.logger.error("Failed to store team notification (%s): %s; emailing it now",
                                 reason, type(e).__name__)
    notification = {"reason": reason, "caller_phone": caller_phone, "contact_name": contact_name,
                    "organization": organization, "summary": summary, "vapi_call_id": vapi_call_id}
    return send_email(*_email(notification, saved=False))


def send_pending_emails() -> int:
    """Email stored team notifications that haven't been emailed yet (run by the dispatcher cron).

    Each one gets a temporary lease before sending. Successful sends are then marked emailed; failed sends
    are released immediately, and leases left by a crashed run expire. Returns the number sent.
    """
    sent = 0
    for notification in db.claim_unsent_notifications(config.MAX_NOTIFICATION_EMAILS_PER_RUN,
                                                      config.NOTIFICATION_EMAIL_MAX_AGE_HOURS,
                                                      config.NOTIFICATION_EMAIL_CLAIM_MINUTES):
        if send_email(*_email(notification)):
            sent += 1
            try:
                marked = db.mark_notification_emailed(notification["id"], notification["email_claimed_at"])
                if not marked:
                    current_app.logger.warning("Team notification %s was sent but its claim expired before recording it",
                                               notification["id"])
            except Exception as e:
                # A later run retries after the lease expires. It may send a duplicate, but the
                # notification is not silently lost.
                current_app.logger.error("Failed to mark team notification %s emailed: %s",
                                         notification["id"], type(e).__name__)
            continue
        try:
            db.release_notification(notification["id"], notification["email_claimed_at"])
        except Exception as e:
            current_app.logger.error("Failed to release team notification %s: %s", notification["id"], type(e).__name__)
    if sent:
        current_app.logger.info("Emailed %s team notification(s)", sent)
    return sent


def notify_team_tool(arguments: dict, phone_number: str | None, vapi_call_id: str | None) -> str:
    """notify_team tool: returns a sentence for the assistant to act on."""
    reason = arguments.get("reason")
    if not isinstance(reason, str) or reason not in config.NOTIFICATION_REASONS:
        reason = "other"
    ok = notify_team(
        reason, phone_number,
        contact_name=arguments.get("contact_name"),
        organization=arguments.get("organization"),
        summary=arguments.get("summary"),
        vapi_call_id=vapi_call_id,
    )
    if ok:
        return "The team has been notified."
    return ("Could not notify the team right now. Don't promise that the team will get back to the caller; "
            "apologise and suggest they call again later.")
