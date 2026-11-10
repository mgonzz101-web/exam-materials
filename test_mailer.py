"""Exercises every real email back-end (SMTP against a local fake server; Brevo/Resend payloads
against a fake HTTP layer) so we know the code paths work before deploying."""
import os
import socketserver
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mailer  # noqa: E402

RECEIVED = []


class FakeSMTP(socketserver.StreamRequestHandler):
    def reply(self, s):
        self.wfile.write((s + "\r\n").encode())

    def handle(self):
        self.reply("220 fake ESMTP")
        data_mode, buf, mail = False, [], {"rcpt": []}
        while True:
            line = self.rfile.readline()
            if not line:
                return
            t = line.decode().rstrip("\r\n")
            if data_mode:
                if t == ".":
                    mail["data"] = "\n".join(buf)
                    RECEIVED.append(mail)
                    data_mode = False
                    self.reply("250 queued")
                else:
                    buf.append(t)
                continue
            u = t.upper()
            if u.startswith("EHLO") or u.startswith("HELO"):
                self.reply("250-fake")
                self.reply("250 AUTH PLAIN LOGIN")
            elif u.startswith("AUTH"):
                self.reply("235 ok")
            elif u.startswith("MAIL FROM"):
                mail["from"] = t
                self.reply("250 ok")
            elif u.startswith("RCPT TO"):
                mail["rcpt"].append(t)
                self.reply("250 ok")
            elif u == "DATA":
                data_mode = True
                self.reply("354 go")
            elif u == "QUIT":
                self.reply("221 bye")
                return
            else:
                self.reply("250 ok")


class Mail(unittest.TestCase):
    def test_smtp_backend_delivers(self):
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), FakeSMTP)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        env = {"SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(srv.server_address[1]), "SMTP_STARTTLS": "0",
               "MAIL_FROM": "alerts@example.com", "MAIL_BACKEND": "smtp"}
        with mock.patch.dict(os.environ, env):
            self.assertTrue(mailer.configured())
            ok, err = mailer.send(["a@example.com", "b@example.com"], "Low stock: WISC-V", "Only 3 left")
        srv.shutdown()
        self.assertEqual((ok, err), (True, ""))
        self.assertEqual(len(RECEIVED[-1]["rcpt"]), 2)
        self.assertIn("Subject: Low stock: WISC-V", RECEIVED[-1]["data"])
        self.assertIn("Only 3 left", RECEIVED[-1]["data"])

    def test_smtp_connection_failure_is_reported_not_raised(self):
        env = {"SMTP_HOST": "127.0.0.1", "SMTP_PORT": "1", "SMTP_STARTTLS": "0", "MAIL_FROM": "a@example.com", "MAIL_BACKEND": "smtp"}
        with mock.patch.dict(os.environ, env):
            ok, err = mailer.send(["a@example.com"], "s", "t")
        self.assertFalse(ok)
        self.assertTrue(err)

    def test_brevo_request_shape(self):
        env = {"BREVO_API_KEY": "key123", "MAIL_FROM": "me@example.com", "MAIL_BACKEND": ""}
        with mock.patch.dict(os.environ, env), mock.patch("mailer.requests.post") as post:
            post.return_value = mock.Mock(ok=True, status_code=201, text="")
            ok, _ = mailer.send(["x@example.com"], "Subj", "Body")
        self.assertTrue(ok)
        args, kw = post.call_args
        self.assertEqual(args[0], "https://api.brevo.com/v3/smtp/email")
        self.assertEqual(kw["headers"]["api-key"], "key123")
        self.assertEqual(kw["json"]["sender"]["email"], "me@example.com")
        self.assertEqual(kw["json"]["to"], [{"email": "x@example.com"}])
        self.assertEqual(kw["json"]["textContent"], "Body")

    def test_resend_request_shape_and_error(self):
        env = {"RESEND_API_KEY": "rk", "MAIL_FROM": "me@example.com", "MAIL_BACKEND": "", "BREVO_API_KEY": ""}
        with mock.patch.dict(os.environ, env), mock.patch("mailer.requests.post") as post:
            post.return_value = mock.Mock(ok=False, status_code=403, text="domain not verified")
            ok, err = mailer.send(["x@example.com"], "Subj", "Body")
        self.assertFalse(ok)
        self.assertIn("403", err)
        args, kw = post.call_args
        self.assertEqual(args[0], "https://api.resend.com/emails")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer rk")
        self.assertEqual(kw["json"]["to"], ["x@example.com"])

    def test_not_configured_without_sender(self):
        with mock.patch.dict(os.environ, {"BREVO_API_KEY": "k", "MAIL_FROM": "", "MAIL_BACKEND": ""}):
            self.assertFalse(mailer.configured())


if __name__ == "__main__":
    unittest.main()
