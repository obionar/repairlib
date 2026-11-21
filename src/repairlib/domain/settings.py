"""Active shop settings; deployment configuration stays with the workspace owner."""

from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from repairlib import i18n, money
from repairlib.contracts import SettingsPatch, ShopSettings
from repairlib.domain.customers import authorized as shop_authorized
from repairlib.domain.users import MAX_ID, UserAccess, UserError, authorized, validate_key
from repairlib.paths import database_path
from repairlib.storage import settings

SettingsAccess = UserAccess
SettingsError = UserError


def read_settings(workspace: Path, access: SettingsAccess) -> ShopSettings:
    with authorized(workspace, access, capability="view shop settings") as (db, principal, _):
        return settings.find(db, principal.shop_id)


def read_settings_page(workspace: Path, access: SettingsAccess) -> tuple[ShopSettings, int]:
    with authorized(workspace, access, capability="view shop settings") as (db, principal, _):
        return settings.find(db, principal.shop_id), settings.diagnosis_fee(db, principal.shop_id)


def read_workspace_language(workspace: Path) -> str:
    """The stored default language; needs no sign-in so it can shape the sign-in page."""
    return settings.workspace_language(database_path(workspace))


def read_timezone(workspace: Path, access: SettingsAccess) -> str:
    with shop_authorized(workspace, access) as (db, principal, _, _):
        return settings.find(db, principal.shop_id).timezone


def normalize_name(name: str) -> str:
    name = name.strip()
    if not 1 <= len(name) <= 160 or not name.isprintable():
        raise SettingsError(422, "validation", "Check the shop settings.",
                            {"name": "Enter 1 to 160 printable characters."})
    return name


def normalize_timezone(timezone: str) -> str:
    timezone = timezone.strip()
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError):
        raise SettingsError(422, "validation", "Check the shop settings.",
                            {"timezone": "Enter a valid IANA timezone."}) from None
    if len(timezone) > 100:
        raise SettingsError(422, "validation", "Check the shop settings.",
                            {"timezone": "Use at most 100 characters."})
    return timezone


def normalize_language(language: str) -> str:
    if language not in i18n.LANGUAGES:
        raise SettingsError(422, "validation", "Check the shop settings.",
                            {"language": "Choose an available language."})
    return language


def normalize_contact(name: str, value: str, limit: int) -> str:
    value = value.strip()
    if len(value) > limit or (value and not value.isprintable()):
        raise SettingsError(422, "validation", "Check the shop settings.",
                            {name: f"Use at most {limit} printable characters."})
    return value


BOUNDS = {"max_upload_mib": (1, 2048), "inline_preview_mib": (1, 64)}


def normalize_limit(name: str, value: int) -> int:
    low, high = BOUNDS[name]
    if type(value) is not int or not low <= value <= high:
        raise SettingsError(422, "validation", "Check the shop settings.",
                            {name: f"Enter a whole number of MiB from {low} to {high}."})
    return value


def update_settings(workspace: Path, access: SettingsAccess, patch: SettingsPatch, *,
                    key: str, diagnosis_fee_cents: int | None = None) -> ShopSettings:
    return update_settings_page(workspace, access, patch, key=key,
                                diagnosis_fee_cents=diagnosis_fee_cents)[0]


def update_settings_page(workspace: Path, access: SettingsAccess, patch: SettingsPatch, *,
                         key: str, diagnosis_fee_cents: int | None = None) -> tuple[ShopSettings, int | None]:
    validate_key(key)
    if not 1 <= patch.revision < MAX_ID:
        raise SettingsError(422, "validation", "Supply the reviewed settings revision.")
    values = {}
    if patch.name is not None:
        values["name"] = normalize_name(patch.name)
    if patch.timezone is not None:
        values["timezone"] = normalize_timezone(patch.timezone)
    if patch.language is not None:
        values["language"] = normalize_language(patch.language)
    for name in BOUNDS:
        if getattr(patch, name) is not None:
            values[name] = normalize_limit(name, getattr(patch, name))
    for name, limit in (("address", 200), ("phone", 40), ("terms", 2000)):
        if getattr(patch, name) is not None:
            values[name] = normalize_contact(name, getattr(patch, name), limit)
    with authorized(workspace, access, write=True, capability="change shop settings") as (
        db, principal, token_label,
    ):
        current = settings.find(db, principal.shop_id)
        current_fee = settings.diagnosis_fee(db, principal.shop_id)
        before = vars(current).copy()
        if diagnosis_fee_cents is not None:
            if type(diagnosis_fee_cents) is not int or not 0 <= diagnosis_fee_cents <= money.MAX_CENTS:
                raise SettingsError(422, "validation", "Check the shop settings.",
                                    {"diagnosis_fee": "Amount is out of range."})
            values["diagnosis_fee_cents"] = diagnosis_fee_cents
            before["diagnosis_fee_cents"] = current_fee
        payload = {"revision": patch.revision, "fields": values}
        fingerprint = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        previous = settings.previous_request(db, principal.shop_id, principal.user_id, key)
        if previous is not None:
            if previous["fingerprint"] != fingerprint:
                raise SettingsError(409, "conflict", "This key already belongs to a different request.")
            result = json.loads(previous["result"])
            # Older receipts do not record the fee. An explicit fee in the
            # identical request is known; otherwise do not substitute today's fee.
            fee = result.pop("diagnosis_fee_cents", diagnosis_fee_cents)
            return ShopSettings(**result), fee
        if current.revision != patch.revision:
            raise SettingsError(409, "stale_revision", "The settings changed. Review the latest version.")
        changes = {name: {"before": before[name], "after": value}
                   for name, value in values.items() if value != before[name]}
        if not changes:
            raise SettingsError(422, "validation", "Change at least one shop setting.")
        updated = settings.update(
            db, principal.shop_id, patch.revision,
            values.get("name", current.name), values.get("timezone", current.timezone),
            values.get("max_upload_mib", current.max_upload_mib),
            values.get("inline_preview_mib", current.inline_preview_mib),
            values.get("language", current.language),
            values.get("address", current.address), values.get("phone", current.phone),
            values.get("terms", current.terms),
        )
        if updated is None:
            raise SettingsError(409, "stale_revision", "The settings changed. Review the latest version.")
        if "diagnosis_fee_cents" in changes:
            current_fee = changes["diagnosis_fee_cents"]["after"]
            settings.set_diagnosis_fee(db, principal.shop_id, current_fee)
        settings.save_result(db, principal.shop_id, principal.user_id, key, fingerprint, updated, current_fee)
        settings.audit(db, principal.shop_id, principal.user_id, principal.username,
                       access.source, token_label, changes)
        return updated, current_fee
