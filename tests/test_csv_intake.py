"""S5 customer CSV gates through the admin API and HTTP CLI."""

from contextlib import closing
from hashlib import sha256
import json
import sqlite3
import subprocess
import unittest
from unittest.mock import patch
from uuid import uuid4

from repairlib.storage import initialization
from test_baseline import python_env
from test_cli_identity import COMMAND, workspace_client
from test_tokens import ACCOUNT, TokenFixture, issued_secret

CSV = "/api/v1/csv/customers"
HEADER = b"name,phone,email,address,notes\r\n"
FIRST = b'Alpha,+357 555 0100,alpha@example.test,"1 Example, Apt 2","line one\nline two"\r\n'
SECOND = b"Beta,,beta@example.test,,\r\n"
BATCH = HEADER + FIRST + SECOND


class CustomerCsvTests(TokenFixture, unittest.TestCase):
    def authorization(self, api, scope="write"):
        if not api.cookies:
            self.sign_in(api)
        response = api.post(ACCOUNT, data=self.issue_form(api, scope=scope, name=f"CSV {scope}"))
        return {"Authorization": f"Bearer {issued_secret(response)}"}

    def preview(self, api, auth, body=BATCH):
        response = api.post(CSV + "/preview", content=body,
                            headers={**auth, "Content-Type": "text/csv"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def apply(self, api, auth, review, body=BATCH, key="customer-batch"):
        return api.post(CSV + "/apply", content=body,
                        params={field: review[field] for field in ("preview_id", "sha256")},
                        headers={**auth, "Content-Type": "text/csv", "Idempotency-Key": key})

    def test_template_cli_transaction_and_replay_after_restart(self):
        path = self.workspace.parent / "customers.csv"

        def cli(*args, status=0):
            result = subprocess.run([*COMMAND, "csv", "customers", *args, "--workspace", str(self.workspace)],
                                    env=python_env(), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, status, result.stderr)
            return result.stdout

        with workspace_client(self.workspace) as (api, _):
            auth = self.authorization(api)
            self.assertEqual(cli("template"), HEADER.decode().replace("\r\n", "\n"))
            path.write_bytes(BATCH)
            review = json.loads(cli("preview", str(path), "--json"))
            self.assertEqual(review["sha256"], sha256(BATCH).hexdigest())
            self.assertEqual(review["errors"], [])
            self.assertEqual([(r["row"], r["action"]) for r in review["actions"]],
                             [(2, "create"), (4, "create")])
            options = ("apply", str(path), "--preview-id", review["preview_id"],
                       "--sha256", review["sha256"], "--key", "customer-batch", "--json")
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("""CREATE TRIGGER reject_second BEFORE INSERT ON customers
                              WHEN NEW.name = 'Beta' BEGIN SELECT RAISE(ABORT, 'fixture'); END""")
            cli(*options, status=3)
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM customers"), [(0,)])
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM csv_requests"), [(0,)])
            self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE entity_type='customer'"), [(0,)])
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("DROP TRIGGER reject_second")
            result = json.loads(cli(*options))
            self.assertEqual(len(result["rows"]), 2)
        with workspace_client(self.workspace) as (api, _):
            self.assertEqual(json.loads(cli(*options)), result)
            self.assertEqual(self.apply(api, auth, review, BATCH.replace(b"Beta", b"Gamma")).status_code, 409)
            path.write_bytes(HEADER + b"Gamma,,,,\r\n")
            fresh = json.loads(cli("preview", str(path), "--json"))
            cli("apply", str(path), "--preview-id", fresh["preview_id"], "--sha256", fresh["sha256"],
                "--key", "customer-batch", status=7)
            path.write_bytes(HEADER + b",,,,\r\n")
            self.assertIsNone(json.loads(cli("preview", str(path), "--json", status=7))["preview_id"])
        self.assertEqual(self.snapshot("SELECT name,address,notes FROM customers ORDER BY id"),
                         [("Alpha", "1 Example, Apt 2", "line one\nline two"), ("Beta", "", "")])
        self.assertEqual(self.snapshot(
            "SELECT shop_id, actor_username, source, token_label, operation FROM audit_events "
            "WHERE entity_type='customer' ORDER BY id"
        ), [(1, "cli", "cli", None, "customer.create")] * 2)

    def test_excel_style_files_with_a_bom_or_trailing_blank_lines_preview_cleanly(self):
        with workspace_client(self.workspace) as (api, _):
            auth = self.authorization(api)
            plain = self.preview(api, auth)
            for label, body in (("bom", b"\xef\xbb\xbf" + BATCH), ("trailing crlf", BATCH + b"\r\n"),
                                ("trailing blanks", BATCH + b"\n\n"), ("bom and blank", b"\xef\xbb\xbf" + BATCH + b"\n")):
                with self.subTest(label):
                    review = self.preview(api, auth, body)
                    self.assertEqual(review["errors"], [])
                    self.assertIsNotNone(review["preview_id"])
                    self.assertEqual(review["actions"], plain["actions"])
            for label, body in (("header only", HEADER + b"\n"), ("bom header only", b"\xef\xbb\xbf" + HEADER),
                                ("blank before header", b"\n" + BATCH)):
                with self.subTest(label):
                    refused = self.preview(api, auth, body)
                    self.assertIsNone(refused["preview_id"])
                    self.assertTrue(refused["errors"])

    def test_preview_validation_duplicates_and_review_boundaries(self):
        with self.client() as api:
            auth = self.authorization(api)
            read = self.authorization(api, "read")
            self.assertEqual(api.get(CSV + "/template").status_code, 401)
            self.assertEqual(api.get(CSV + "/template", headers=read).content, HEADER)
            self.assertEqual(api.post(CSV + "/preview", headers=read, content=BATCH).status_code, 403)
            for payload, row in [
                (HEADER + b'Bad"quote,,,,\r\n', 2),
                (HEADER + b'"Unclosed,,,,\r\n', 2),
                (HEADER + b"bad\xff,,,,\r\n", 2),
                (HEADER + b"Bad,,,\r\n", 2),
                (HEADER + b",,,,\r\n", 2),
                (HEADER + b"Long,,,," + b"x" * 1001, 2),
                (HEADER + b"X,,,,\n" * 101, 102),
                (HEADER + FIRST + FIRST, 2),
            ]:
                with self.subTest(row=row, size=len(payload)):
                    invalid = self.preview(api, auth, payload)
                    self.assertIsNone(invalid["preview_id"])
                    self.assertIn(row, [issue["row"] for issue in invalid["errors"]])
            self.assertEqual(api.post(CSV + "/preview", headers=auth, content=b"x" * (256 * 1024 + 1))
                             .status_code, 413)

            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("INSERT INTO shops(id, name) VALUES (2, 'Outside')")
                db.execute("INSERT INTO customers(shop_id, name, email) VALUES (2, 'Alpha', 'alpha@example.test')")
                other = "rl_" + uuid4().hex + uuid4().hex[:11]
                actor = db.execute("INSERT INTO users(shop_id, username, role, password_hash) "
                                   "VALUES (2, 'outside', 'admin', 'synthetic')").lastrowid
                db.execute("INSERT INTO api_tokens(shop_id, user_id, user_revision, token_hash, name, scope, create_key) "
                           "VALUES (2, ?, 1, ?, 'outside', 'write', 'test')",
                           (actor, sha256(other.encode()).hexdigest()))
                db.execute("INSERT INTO customers(shop_id, name, phone, archived_at) "
                           "VALUES (1, 'Archived', '+357-555-0101', '2026-01-01')")
            review = self.preview(api, auth)
            self.assertEqual(review["errors"], [])
            self.assertEqual(self.apply(api, {"Authorization": f"Bearer {other}"}, review).status_code, 401)
            self.assertEqual(self.apply(api, read, review).status_code, 403)
            self.assertEqual(self.apply(api, auth, review, BATCH + b"\n").status_code, 409)
            self.assertEqual(self.apply(api, auth, {**review, "preview_id": uuid4().hex}).status_code, 409)
            existing = self.preview(api, auth, HEADER + b"New,+357 555 0101,,,\r\n")
            self.assertEqual(existing["errors"][0]["row"], 2)
            self.assertIsNone(existing["preview_id"])
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("UPDATE shop_settings SET revision = revision + 1 WHERE shop_id=1")
            self.assertEqual(self.apply(api, auth, review).status_code, 409)
            review = self.preview(api, auth)
            replacement = self.preview(api, auth)
            self.assertEqual(self.apply(api, auth, review).status_code, 409)
            review = replacement
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute("INSERT INTO customers(shop_id, name, email) VALUES (1, 'New', 'BETA@example.test')")
            stale = self.apply(api, auth, review)
            self.assertEqual(stale.status_code, 409)
            self.assertIn("row.4.email", stale.json()["error"]["fields"])
        self.assertEqual(self.snapshot("SELECT shop_id,name FROM customers ORDER BY id"),
                         [(2, "Alpha"), (1, "Archived"), (1, "New")])
        self.assertEqual(self.snapshot("SELECT COUNT(*) FROM audit_events WHERE entity_type='customer'"), [(0,)])

    def test_schema_22_migration_preserves_existing_customers_and_rolls_back(self):
        prior = self.workspace.parent / "prior.sqlite3"
        initialization.initialize_database(prior, "Prior shop", "UTC", "owner", "unused", target=22)
        with closing(sqlite3.connect(prior)) as db, db:
            db.execute("INSERT INTO customers(shop_id, name) VALUES (1, 'Prior customer')")
        apply_migration = initialization.apply_migration

        def fail(db, version):
            apply_migration(db, version)
            raise sqlite3.OperationalError("Synthetic CSV migration interruption")

        with patch.object(initialization, "apply_migration", fail):
            with self.assertRaises(initialization.UpgradeError):
                initialization.upgrade_database(prior)
        with closing(sqlite3.connect(prior)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (22,))
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'csv_%'").fetchall(), [])
        initialization.upgrade_database(prior)
        with closing(sqlite3.connect(prior)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (initialization.SCHEMA_VERSION,))
            self.assertEqual(db.execute("SELECT shop_id, name FROM customers").fetchall(), [(1, "Prior customer")])
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
