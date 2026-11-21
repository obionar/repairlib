"""A browser visibility retry must replay the original target state."""
import re
import unittest
from types import SimpleNamespace
from uuid import uuid4
from test_tokens import TokenFixture, ACCOUNT, issued_secret, hidden
from test_repairs import DEVICE, API


class EntryVisibilityRetryTests(TokenFixture, unittest.TestCase):
    def test_same_delete_and_restore_forms_replay(self):
        with self.client() as client:
            self.sign_in(client)
            token = issued_secret(client.post(ACCOUNT, data=self.issue_form(client)))
            auth = {'Authorization': f'Bearer {token}'}
            def create(path, payload):
                response = client.post(path, headers={**auth, 'Idempotency-Key': uuid4().hex}, json=payload)
                self.assertEqual(response.status_code, 201, response.text)
                return response.json()
            repair = create(API, DEVICE)
            entry = create(f"{API}/{repair['id']}/entries", {'kind': 'note', 'body': 'Synthetic'})
            url = f"/repairs/{repair['id']}/entries/{entry['id']}/visibility"
            for target in ('1', '0'):
                page = client.get(url)
                self.assertEqual(page.status_code, 200)
                fields = {name: hidden(page, name) for name in ('csrf', 'key', 'revision', 'deleted')}
                self.assertEqual(fields['deleted'], target)
                fields['reason'] = 'Synthetic change'
                if target == '1':
                    invalid = client.post(url, data={**fields, 'deleted': 'invalid'})
                    self.assertEqual(invalid.status_code, 422)
                    self.assertEqual(self.snapshot('SELECT deleted_at FROM work_entries WHERE id = ?',
                                                   (entry['id'],)), [(None,)])
                    rejected = client.post(url, data={**fields, 'reason': ''})
                    self.assertEqual(rejected.status_code, 422)
                    self.assertEqual(hidden(rejected, 'deleted'), '1')
                first = client.post(url, data=fields)
                self.assertEqual(first.status_code, 303, first.text)
                after_first = self.snapshot('SELECT revision, deleted_at IS NOT NULL FROM work_entries WHERE id = ?', (entry['id'],))
                history = self.snapshot('SELECT revision, deleted FROM work_entry_revisions WHERE entry_id = ? ORDER BY revision', (entry['id'],))
                self.assertEqual(after_first[0][1], int(target))
                second = client.post(url, data=fields)
                self.assertEqual(second.status_code, 303, second.text)
                self.assertEqual(self.snapshot('SELECT revision, deleted_at IS NOT NULL FROM work_entries WHERE id = ?', (entry['id'],)), after_first)
                self.assertEqual(self.snapshot('SELECT revision, deleted FROM work_entry_revisions WHERE entry_id = ? ORDER BY revision', (entry['id'],)), history)
                self.assertEqual(client.post(url, data={**fields, 'reason': 'Different'}).status_code, 409)
                conflict = client.post(url, data={**fields, 'key': uuid4().hex})
                self.assertEqual(conflict.status_code, 409, conflict.text)
                self.assertIn(f'href="{url}">Open latest version</a>', conflict.text)
                latest = client.get(url)
                self.assertEqual(latest.status_code, 200)
                self.assertNotEqual(hidden(latest, 'key'), fields['key'])
                self.assertNotEqual(hidden(latest, 'revision'), fields['revision'])

            page = client.get(f"/repairs/{repair['id']}")
            form = re.search(r'<form\b[^>]*action="' + re.escape(url) + r'"[^>]*>(.*?)</form>',
                             page.text, re.S)
            self.assertIsNotNone(form, page.text)
            inline = SimpleNamespace(text=form[1])
            fields = {name: hidden(inline, name) for name in ('csrf', 'key', 'revision', 'deleted')}
            self.assertEqual(fields['deleted'], '1')
            fields['reason'] = 'Synthetic inline change'
            self.assertEqual(client.post(url, data=fields).status_code, 303)
            self.assertEqual(client.post(url, data=fields).status_code, 303)
