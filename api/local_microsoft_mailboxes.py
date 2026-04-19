from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from application.local_microsoft_mailboxes import LocalMicrosoftMailboxesService

router = APIRouter(prefix="/local-microsoft/mailboxes", tags=["local-microsoft-mailboxes"])
service = LocalMicrosoftMailboxesService()


class LocalMicrosoftMailboxImportItem(BaseModel):
    email: str
    password: str = ""
    client_id: str = ""
    refresh_token: str = ""
    status: str = "active"
    sub_status: str = "raw_master"
    fission_enabled: bool = True
    metadata: dict = Field(default_factory=dict)


class LocalMicrosoftMailboxImportRequest(BaseModel):
    pool: str = "default"
    replace: bool = False
    items: list[LocalMicrosoftMailboxImportItem] = Field(default_factory=list)


class LocalMicrosoftMailboxRuntimePatchRequest(BaseModel):
    status: str | None = None
    sub_status: str | None = None
    last_error: str | None = None
    cooldown_seconds: int = 0
    release_lease: bool = False
    increment_fission: bool = False
    mark_refresh: bool = False
    mark_success: bool = False


@router.get("")
def list_local_microsoft_mailboxes(pool: str = "default", status: str = "", page: int = 1, page_size: int = 10):
    return service.list_mailboxes(pool=pool, status=status, page=page, page_size=page_size)



@router.get("/stats")
def local_microsoft_mailbox_stats(pool: str = "default", platform: str = ""):
    return service.pool_stats(pool=pool, platform=platform)



@router.post("/import")
def import_local_microsoft_mailboxes(body: LocalMicrosoftMailboxImportRequest):
    payload = [item.model_dump() for item in body.items]
    return service.import_mailboxes(pool=body.pool, items=payload, replace=body.replace)


@router.patch("/{mailbox_id}")
def patch_local_microsoft_mailbox_runtime(mailbox_id: int, body: LocalMicrosoftMailboxRuntimePatchRequest):
    result = service.update_runtime(mailbox_id, body.model_dump())
    if not result["ok"]:
        raise HTTPException(404, "mailbox 不存在")
    return result


@router.delete("/{mailbox_id}")
def delete_local_microsoft_mailbox(mailbox_id: int):
    result = service.delete_mailbox(mailbox_id)
    if not result["ok"]:
        raise HTTPException(404, "mailbox 不存在")
    return result
