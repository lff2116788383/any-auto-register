from __future__ import annotations

from infrastructure.local_microsoft_mailboxes_repository import LocalMicrosoftMailboxesRepository


class LocalMicrosoftMailboxesService:
    def __init__(self, repository: LocalMicrosoftMailboxesRepository | None = None):
        self.repository = repository or LocalMicrosoftMailboxesRepository()

    def list_mailboxes(self, *, pool: str = "default", status: str = "", page: int = 1, page_size: int = 10) -> dict:
        result = self.repository.list(pool=pool, status=status, page=page, page_size=page_size)
        items = result.get("items") or []
        return {
            "items": [self._serialize(item) for item in items],
            "total": int(result.get("total") or 0),
            "page": int(result.get("page") or 1),
            "page_size": int(result.get("page_size") or 10),
            "pages": int(result.get("pages") or 1),
        }


    def import_mailboxes(self, *, pool: str, items: list[dict], replace: bool = False) -> dict:
        result = self.repository.import_items(pool=pool, items=items, replace=replace)
        return {"ok": True, **result}

    def update_runtime(self, mailbox_id: int, payload: dict) -> dict:
        ok = self.repository.mark_runtime(
            mailbox_id,
            status=payload.get("status"),
            sub_status=payload.get("sub_status"),
            last_error=payload.get("last_error"),
            cooldown_seconds=int(payload.get("cooldown_seconds") or 0),
            release_lease=bool(payload.get("release_lease", False)),
            increment_fission=bool(payload.get("increment_fission", False)),
            mark_refresh=bool(payload.get("mark_refresh", False)),
            mark_success=bool(payload.get("mark_success", False)),
        )
        return {"ok": ok}

    def delete_mailbox(self, mailbox_id: int) -> dict:
        return {"ok": self.repository.delete(mailbox_id)}

    def pool_stats(self, *, pool: str = "default", platform: str = "") -> dict:
        return self.repository.pool_stats(pool=pool, platform=platform)


    @staticmethod
    def _serialize(item) -> dict:
        return {
            "id": int(item.id or 0),
            "pool": item.pool,
            "email": item.email,
            "password": item.password,
            "client_id": item.client_id,
            "refresh_token": item.refresh_token,
            "status": item.status,
            "sub_status": item.sub_status,
            "fission_enabled": bool(item.fission_enabled),
            "fission_count": int(item.fission_count or 0),
            "last_used_at": item.last_used_at,
            "last_refresh_at": item.last_refresh_at,
            "last_success_at": item.last_success_at,
            "last_error": item.last_error,
            "cooldown_until": item.cooldown_until,
            "leased_by_task_id": item.leased_by_task_id,
            "leased_until": item.leased_until,
            "current_platform": item.current_platform,
            "metadata": item.get_metadata(),
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
