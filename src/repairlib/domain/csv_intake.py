"""Bounded CSV review, atomic apply and durable replay."""

from contextlib import contextmanager
import csv
from dataclasses import asdict, dataclass
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import re
from typing import Callable
from uuid import uuid4

from repairlib.contracts import (
    CSV_MAX_BYTES, CSV_MAX_ROWS, CsvAction, CsvAppliedRow, CsvApply, CsvIssue, CsvPreview,
)
from repairlib.domain.customers import CustomerAccess, CustomerError, KEY_FORMAT, authorized
from repairlib.storage import csv_intake as storage, settings

FIELD = r'(?:[^",\r\n]*|"(?:[^"]|"")*")'
RECORD = re.compile(FIELD + "(?:," + FIELD + r")*(?:\r\n|\r|\n)?")
NEWLINE = re.compile(rb"\r\n|\r|\n")


@dataclass(frozen=True)
class Intake:
    kind: str
    limits: dict[str, int]
    plan: Callable  # Returns proposed actions, issues and relevant record revisions.
    publish: Callable  # Applies the checked actions inside the batch transaction.


@contextmanager
def administrator(workspace: Path, access: CustomerAccess, *, write: bool = False):
    with authorized(workspace, access, write=write, resource="CSV intake") as context:
        if context[1].role != "admin":
            raise CustomerError(403, "forbidden", "Only shop administrators can use CSV intake.")
        yield context


def authorize_upload(workspace: Path, access: CustomerAccess) -> None:
    with administrator(workspace, access) as (_, _, _, can_write):
        if not can_write:
            raise CustomerError(403, "forbidden", "A write token is required for CSV intake.")


def template(workspace: Path, access: CustomerAccess, intake: Intake) -> str:
    with administrator(workspace, access):
        return ",".join(intake.limits) + "\r\n"


SAVE_AS = 'save it as "CSV UTF-8 (Comma delimited)"'  # the Excel choice that produces a valid file


def read_records(raw: bytes, limits: dict[str, int]):
    if len(raw) > CSV_MAX_BYTES:
        raise CustomerError(413, "csv_too_large", "The CSV exceeds its size limit.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        row = len(NEWLINE.findall(raw[:error.start])) + 1
        return [], [CsvIssue(row, "csv", f"Use valid UTF-8: {SAVE_AS}.")]
    text = text.removeprefix("\ufeff")  # Excel's "CSV UTF-8" starts with a byte-order mark
    stream = StringIO(text, newline="")
    reader = csv.reader(stream, strict=True)
    records = []
    errors = []
    for index in range(CSV_MAX_ROWS + 2):
        row = reader.line_num + 1
        start = stream.tell()
        try:
            fields = next(reader)
        except StopIteration:
            break
        except csv.Error:
            errors.append(CsvIssue(row, "csv", "Malformed CSV or oversized field."))
            break
        if index > CSV_MAX_ROWS:
            errors.append(CsvIssue(row, "csv", f"Use at most {CSV_MAX_ROWS} data rows."))
            break
        if index == 0:
            if len(fields) == 1 and fields[0].split(";") == list(limits):
                return [], [CsvIssue(1, "csv", f"Separate columns with commas, not semicolons: {SAVE_AS}.")]
            if fields != list(limits):
                return [], [CsvIssue(1, "csv", "Use the exact template header.")]
            continue
        if not fields:  # a blank line, such as the one editors leave at the end
            continue
        if len(fields) != len(limits):
            errors.append(CsvIssue(row, "csv", f"Expected {len(limits)} columns."))
            continue
        if not RECORD.fullmatch(text[start:stream.tell()]):
            errors.append(CsvIssue(row, "csv", "Invalid CSV quoting."))
            continue
        for field, value in zip(limits, fields):
            if len(value) > limits[field]:
                errors.append(CsvIssue(row, field, f"Use at most {limits[field]} characters."))
        if not errors or errors[-1].row != row:
            records.append((row, dict(zip(limits, fields))))
    if not records and not errors:
        errors.append(CsvIssue(1, "csv", "Provide the template header and at least one data row."))
    return records, errors


def fingerprint(*parts) -> str:
    return sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def review_context(db, shop_id: int, actions: list[CsvAction], revisions) -> str:
    return fingerprint(settings.find(db, shop_id).revision, [asdict(action) for action in actions], revisions)


def preview(workspace: Path, access: CustomerAccess, intake: Intake, raw: bytes) -> CsvPreview:
    with administrator(workspace, access, write=True) as (db, principal, _, _):
        records, errors = read_records(raw, intake.limits)
        input_hash = sha256(raw).hexdigest()
        actions, issues, revisions = intake.plan(db, principal.shop_id, records)
        errors += issues
        errors.sort(key=lambda issue: (issue.row, issue.field))
        preview_id = None
        if not errors:
            preview_id = uuid4().hex
            context = review_context(db, principal.shop_id, actions, revisions)
            storage.stage(db, principal, intake.kind, preview_id, input_hash, context)
        return CsvPreview(input_hash, preview_id, actions, errors)


def apply(workspace: Path, access: CustomerAccess, intake: Intake, raw: bytes, *,
          preview_id: str, input_hash: str, key: str) -> CsvApply:
    with administrator(workspace, access, write=True) as (db, principal, label, _):
        if not KEY_FORMAT.fullmatch(key):
            raise CustomerError(422, "validation", "Supply a valid idempotency key.")
        if len(raw) > CSV_MAX_BYTES:
            raise CustomerError(413, "csv_too_large", "The CSV exceeds its size limit.")
        actual_hash = sha256(raw).hexdigest()
        request_hash = fingerprint(preview_id, input_hash, actual_hash)
        previous = storage.previous_request(db, principal, intake.kind, key)
        if previous is not None:
            if previous["fingerprint"] != request_hash:
                raise CustomerError(409, "conflict", "This key already belongs to a different CSV request.")
            result = json.loads(previous["result"])
            return CsvApply(result["sha256"], [CsvAppliedRow(**row) for row in result["rows"]])
        review = storage.review(db, principal, intake.kind, preview_id)
        if review is None or review["input_sha256"] != input_hash or input_hash != actual_hash:
            raise CustomerError(409, "preview_conflict", "Supply the matching CSV and current preview.")
        records, errors = read_records(raw, intake.limits)
        actions, issues, revisions = intake.plan(db, principal.shop_id, records)
        if errors or issues:
            raise CustomerError(409, "preview_conflict", "The CSV requires correction. Preview it again.",
                                {f"row.{e.row}.{e.field}": e.message for e in errors + issues})
        if review["context_sha256"] != review_context(db, principal.shop_id, actions, revisions):
            raise CustomerError(409, "stale_revision", "The shop or reviewed records changed. Preview the CSV again.")
        rows = intake.publish(db, principal, access.source, label, actions)
        result = CsvApply(input_hash, rows)
        storage.save_result(db, principal, intake.kind, key, request_hash, result)
        storage.clear_review(db, principal, intake.kind)
        return result
