import unittest
from datetime import date
from email.message import EmailMessage
from unittest import mock

import main


def make_newsletter(subject, sender, body, link):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["Date"] = "Sat, 20 Jun 2026 10:00:00 -0400"
    message.set_content(f"{body}\n{link}")
    message.add_alternative(
        f'<html><body><h1>{body}</h1><a href="{link}">{subject}</a></body></html>',
        subtype="html",
    )
    return message.as_bytes()


class FakeImap:
    def __init__(self, messages):
        self.messages = messages
        self.selected = []
        self.fetch_calls = []

    def login(self, _user, _password):
        return "OK", []

    def list(self):
        return "OK", [b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"']

    def select(self, mailbox, readonly=False):
        self.selected.append((mailbox, readonly))
        return "OK", [b"2"]

    def search(self, _charset, _query):
        return "OK", [b"1 2"]

    def fetch(self, number, query):
        self.fetch_calls.append((number, query))
        raw = self.messages[number]
        if "HEADER.FIELDS" in query:
            header = raw.split(b"\n\n", 1)[0] + b"\n\n"
            metadata = b'1 (INTERNALDATE "21-Jun-2026 08:15:00 +0900" BODY[HEADER.FIELDS] {100}'
            return "OK", [(metadata, header), b")"]
        return "OK", [(b"1 (BODY[] {100}", raw), b")"]

    def logout(self):
        return "BYE", []


class NewsletterCollectionTests(unittest.TestCase):
    def test_authentication_failure_skips_newsletter_sources(self):
        failed_mail = mock.Mock()
        failed_mail.login.side_effect = main.imaplib.IMAP4.error(
            b"[ALERT] Application-specific password required"
        )

        with mock.patch.object(main.imaplib, "IMAP4_SSL", return_value=failed_mail):
            items = main.fetch_newsletter_emails(
                "user@example.com", "expired-password", date(2026, 6, 21), set(), []
            )
        self.assertEqual(items, [])

    def test_uses_all_mail_and_received_date_for_two_bloomberg_newsletters(self):
        fake_mail = FakeImap({
            b"1": make_newsletter(
                "Climate adaptation finance",
                "Bloomberg Newsletters <green@newsletters.bloomberg.com>",
                "Bloomberg Green daily briefing",
                "https://links.message.bloomberg.com/one",
            ),
            b"2": make_newsletter(
                "A warming world",
                "Bloomberg Newsletters <green@newsletters.bloomberg.com>",
                "Bloomberg Green weekend briefing",
                "https://links.message.bloomberg.com/two",
            ),
        })

        with mock.patch.object(main.imaplib, "IMAP4_SSL", return_value=fake_mail):
            items = main.fetch_newsletter_emails(
                "user@example.com", "password", date(2026, 6, 21), set(), []
            )

        self.assertEqual(len(items), 2)
        self.assertEqual({item["source"] for item in items}, {"Bloomberg Green"})
        self.assertTrue(all(item["date"] == "2026.06.21" for item in items))
        self.assertEqual(fake_mail.selected[0], ("[Gmail]/All Mail", True))
        self.assertTrue(all("BODY.PEEK" in query for _number, query in fake_mail.fetch_calls))

    def test_ctvc_can_be_identified_by_sightline_sender(self):
        self.assertEqual(
            main.newsletter_source_from_message(
                "The IPO boom is back", "newsletter@sightlineclimate.com"
            ),
            "CTVC",
        )

    def test_internaldate_is_converted_to_kst(self):
        parsed = main.parse_imap_internaldate(
            b'7 (INTERNALDATE "18-Jun-2026 16:30:00 -0700" RFC822.SIZE 1234)'
        )
        self.assertEqual(parsed.strftime("%Y.%m.%d %H:%M"), "2026.06.19 08:30")


if __name__ == "__main__":
    unittest.main()
