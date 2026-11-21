from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from repairlib.client import local
from repairlib.domain.setup import initialize_workspace
from repairlib.lifecycle.workspace import maintenance_lock


class LocalMaintenanceLockTests(unittest.TestCase):
    def test_maintenance_refuses_new_and_existing_local_clients(self):
        with TemporaryDirectory() as directory:
            workspace = Path(directory) / 'workspace'
            initialize_workspace(workspace, shop='Synthetic', timezone='UTC',
                                 username='owner', password='synthetic-test-password')
            ctx = local.open_workspace(workspace)
            with maintenance_lock(workspace, exclusive=True, create=False):
                with self.assertRaises(local.ClientError) as existing:
                    local.create_customer(ctx, {'name': 'Synthetic Customer'}, 'lock-test')
                self.assertEqual(existing.exception.code, 'busy')
                for actor in ('cli', 'mcp'):
                    with self.subTest(actor=actor), self.assertRaises(local.ClientError) as new:
                        local.open_workspace(workspace, actor=actor)
                    self.assertEqual(new.exception.code, 'busy')
            with maintenance_lock(workspace, exclusive=False, create=False):
                customer = local.create_customer(ctx, {'name': 'Synthetic Customer'}, 'lock-test')
            self.assertEqual(customer.name, 'Synthetic Customer')
