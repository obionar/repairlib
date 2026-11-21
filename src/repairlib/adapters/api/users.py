"""Administrator-only user JSON resources."""

from functools import partial
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import TypeAdapter

from repairlib.adapters.body import API_PREFIX
from repairlib.adapters.api.customers import body_schema, idempotency_key, json_body, page_number as customer_page_number
from repairlib.contracts import User, UserInput, UserPage, UserPatch
from repairlib.domain.users import (
    UserAccess, create_user, list_users, read_user, update_user,
)

INPUT = TypeAdapter(UserInput)
PATCH = TypeAdapter(UserPatch)


user_json = json_body(INPUT, "user")
patch_json = json_body(PATCH, "user patch")


page_number = partial(customer_page_number, noun="user")


def user_routes(workspace: Path, access_dependency) -> APIRouter:
    router = APIRouter(prefix=API_PREFIX + "/users")

    @router.get("", response_model=UserPage)
    def index(access: Annotated[UserAccess, Depends(access_dependency)],
              before: str | None = None, limit: str | None = None):
        return list_users(workspace, access, before=page_number(before),
                          limit=page_number(limit, 25))

    @router.post("", response_model=User, status_code=201, openapi_extra=body_schema(INPUT))
    def create(access: Annotated[UserAccess, Depends(access_dependency)],
               details: Annotated[UserInput, Depends(user_json)],
               key: Annotated[str, Depends(idempotency_key)]):
        return create_user(workspace, access, details, key=key)

    @router.get("/{user_id}", response_model=User)
    def detail(user_id: int, access: Annotated[UserAccess, Depends(access_dependency)]):
        return read_user(workspace, access, user_id)

    @router.patch("/{user_id}", response_model=User, openapi_extra=body_schema(PATCH))
    def edit(user_id: int, access: Annotated[UserAccess, Depends(access_dependency)],
             changes: Annotated[UserPatch, Depends(patch_json)],
             key: Annotated[str, Depends(idempotency_key)]):
        return update_user(workspace, access, user_id, changes, key=key)

    return router
