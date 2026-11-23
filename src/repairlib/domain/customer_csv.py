"""Customer-specific CSV validation, duplicate screening and publication."""

from collections import defaultdict
import re

from repairlib.contracts import CsvAction, CsvAppliedRow, CsvIssue, CustomerInput
from repairlib.domain.csv_intake import Intake
from repairlib.domain.customers import CustomerError, FIELD_LIMITS, normalize_fields
from repairlib.search import fold
from repairlib.storage import audit, customers

PHONE_SEPARATORS = re.compile(r"[\s().-]+")


def identities(name: str, phone: str, email: str):
    for field, value in (
        ("name", " ".join(fold(name).split())),
        ("phone", PHONE_SEPARATORS.sub("", phone)),
        ("email", email.strip().casefold()),
    ):
        if value:
            yield field, value


def plan(db, shop_id, records):
    errors = []
    normalized = {}
    occurrences = defaultdict(list)
    for row, fields in records:
        try:
            values = normalize_fields(fields)
        except CustomerError as error:
            errors.extend(CsvIssue(row, field, message) for field, message in error.fields.items())
            continue
        normalized[row] = values
        for identity in identities(values["name"], values["phone"], values["email"]):
            occurrences[identity].append(row)

    candidates = set(occurrences)
    existing = set()
    if candidates:
        for name, phone, email in customers.intake_identities(db, shop_id):
            existing.update(candidates.intersection(identities(name, phone, email)))
            if existing == candidates:
                break
    for identity, rows in occurrences.items():
        if identity in existing:
            message = "Matches an existing customer in this shop."
        elif len(rows) > 1:
            message = "Repeated customer identity in this CSV."
        else:
            continue
        errors.extend(CsvIssue(row, identity[0], message) for row in rows)
    blocked = {issue.row for issue in errors}
    return [CsvAction(row, "create", fields) for row, fields in normalized.items() if row not in blocked], errors, []


def publish(db, principal, source, token_label, actions):
    rows = []
    for action in actions:
        customer = customers.insert(db, principal.shop_id, CustomerInput(**action.fields))
        audit.record(db, principal, source, token_label, "customer.create", "customer", customer.id,
                     {"fields": list(action.fields)})
        rows.append(CsvAppliedRow(action.row, customer.id, customer.revision))
    return rows


CUSTOMERS = Intake("customers", FIELD_LIMITS, plan, publish)
