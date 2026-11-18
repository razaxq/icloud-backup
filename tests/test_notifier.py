"""Offline checks for how saved notification credentials are reused."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
import notifier


class NotificationConfigTests(unittest.TestCase):
    def save(self, channel, current=None, **fields):
        return notifier.merge(current, dict(channel=channel, events={"error": True}, **fields))

    def test_ntfy_token_stays_with_one_publishing_service(self):
        saved = self.save("ntfy", ntfy_url="https://example.test/push/photos", ntfy_token="fixture")
        same = self.save("ntfy", saved, ntfy_url="https://EXAMPLE.test:443/push/other")
        self.assertEqual(same["ntfy_token"], "fixture")
        for url in ("https://other.test/push/photos", "http://example.test/push/photos",
                    "https://example.test:8443/push/photos", "https://example.test/team/photos"):
            with self.subTest(url=url):
                self.assertEqual(self.save("ntfy", saved, ntfy_url=url)["ntfy_token"], "")
        changed = self.save("ntfy", saved, ntfy_url="https://other.test/photos", ntfy_token="new")
        self.assertEqual(changed["ntfy_token"], "new")
        self.assertEqual(self.save("ntfy", saved, clear=["ntfy_token"])["ntfy_token"], "")

    def test_smtp_credentials_require_the_same_login_and_transport(self):
        saved = self.save("smtp", smtp_host="mail.example.test", smtp_user="one@example.test",
                          smtp_password="fixture", smtp_to="two@example.test", smtp_from="one@example.test")
        self.assertEqual(self.save("smtp", saved, smtp_host="MAIL.example.test")["smtp_password"], "fixture")
        for changes in (dict(smtp_host="other.test"), dict(smtp_security="none"), dict(smtp_port=999),
                        dict(smtp_user="other@example.test")):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "当前邮件服务器"):
                self.save("smtp", saved, **changes)
        replaced = self.save("smtp", saved, smtp_host="other.test", smtp_password="new")
        self.assertEqual(replaced["smtp_password"], "new")
        anonymous = self.save("smtp", saved, smtp_user="", clear=["smtp_password"])
        self.assertEqual(anonymous["smtp_password"], "")

    def test_only_selected_channel_is_updated_and_validated(self):
        ntfy = self.save("ntfy", ntfy_url="https://example.test/photos", ntfy_token="fixture")
        hook = self.save("webhook", ntfy, webhook_url="https://other.test/hook",
                         telegram_token="unfinished", smtp_port=-1, ntfy_token="inactive edit")
        self.assertEqual(hook["ntfy_token"], "fixture")
        self.assertEqual(hook["telegram_token"], "")
        self.assertEqual(self.save("ntfy", hook)["ntfy_token"], "fixture")
        off = self.save("none", hook)
        self.assertFalse(any(off[key] for key in notifier.SECRETS))
