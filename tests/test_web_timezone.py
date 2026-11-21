"""Browser timestamp rendering when shop zone data is missing."""

from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfoNotFoundError

from test_repairs import API, DEVICE
from test_tokens import ACCOUNT, TokenFixture, issued_secret


class WebTimezoneTests(TokenFixture, unittest.TestCase):
    def test_missing_shop_zone_warns_once_and_preserves_timestamps(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE shop_settings SET timezone='Asia/Tokyo' WHERE shop_id=1")
        with self.client() as client:
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
            timestamps = []
            for _ in range(2):
                record = client.post(API, json=DEVICE, headers={
                    "Authorization": f"Bearer {token}", "Idempotency-Key": uuid4().hex,
                })
                self.assertEqual(record.status_code, 201, record.text)
                timestamps.append(record.json()["created_at"])

            with patch("repairlib.adapters.web.routes.ZoneInfo", side_effect=ZoneInfoNotFoundError):
                with self.assertLogs("repairlib", "WARNING") as logs:
                    page = client.get("/repairs")
            self.assertEqual(page.status_code, 200)
            for timestamp in timestamps:
                self.assertIn(f'<time datetime="{timestamp}">{timestamp}</time>', page.text)
            warning, = logs.output
            self.assertIn("Asia/Tokyo", warning)
            self.assertIn("tzdata", warning)
