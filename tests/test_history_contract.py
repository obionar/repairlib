"""The customer history contract describes every change shape the domain actually records."""

import unittest
import warnings

from pydantic import TypeAdapter

from repairlib.contracts import CustomerHistoryEvent


def event(changes):
    return CustomerHistoryEvent(id=1, operation="x", occurred_at="t", actor_user_id=1, actor_username="u",
                                source="web", token_label=None, actor_kind="user", changes=changes)


class HistoryContractTests(unittest.TestCase):
    def test_every_recorded_change_shape_serializes_without_warnings(self):
        shapes = (
            {"name": {"before": "A", "after": "B"}},                 # customer.update, archive, restore
            {"anonymized_at": "2026-11-20T10:00:00.000Z", "erased_fields": ["name", "phone"]},  # anonymize
            {"archived_at": {"before": None, "after": "2026-11-20T10:00:00.000Z"}},
        )
        adapter = TypeAdapter(CustomerHistoryEvent)
        for changes in shapes:
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                adapter.dump_json(event(changes))


if __name__ == "__main__":
    unittest.main()
