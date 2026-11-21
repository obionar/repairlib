"""One helper checks a form's CSRF token against the signed-in session and the form's purpose."""

import unittest

from fastapi import HTTPException
from starlette.requests import Request

from repairlib.adapters.web.security import SESSION_COOKIE, FormExpired, check_form, csrf_token, form_token

KEY = bytearray(b"k" * 32)


def request(session="s" * 43, origin=None):
    headers = [(b"host", b"127.0.0.1")]
    if session:
        headers.append((b"cookie", f"{SESSION_COOKIE}={session}".encode()))
    if origin:
        headers.append((b"origin", origin.encode()))
    return Request({"type": "http", "method": "POST", "path": "/", "query_string": b"", "headers": headers,
                    "scheme": "http", "server": ("127.0.0.1", 80)})


class FormCheckTests(unittest.TestCase):
    def test_token_must_match_session_and_purpose(self):
        token = form_token(request(), KEY, "customer.create")
        self.assertEqual(token, csrf_token(KEY, "customer.create", "s" * 43))
        check_form(request(), {"csrf": token}, KEY, "customer.create")
        for form, purpose, session in (({"csrf": token}, "customer.update:1", "s" * 43),
                                       ({"csrf": token}, "customer.create", "t" * 43),
                                       ({}, "customer.create", "s" * 43),
                                       ({"csrf": ""}, "customer.create", "")):
            with self.assertRaises(FormExpired, msg=(purpose, session[:1])):
                check_form(request(session), form, KEY, purpose)

    def test_a_foreign_origin_is_refused_before_the_token(self):
        token = form_token(request(), KEY, "customer.create")
        with self.assertRaises(HTTPException) as refused:
            check_form(request(origin="http://evil.example"), {"csrf": token}, KEY, "customer.create")
        self.assertEqual(refused.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
