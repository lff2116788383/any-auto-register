from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlmodel import Session, select


from core.datetime_utils import ensure_utc_datetime
from core.db import LocalMicrosoftMailboxModel, engine



def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_dt(value: datetime | None) -> datetime | None:
    return ensure_utc_datetime(value)


def _to_bool(value, default: bool = False) -> bool:

    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _to_float(value, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


class LocalMicrosoftMailboxesRepository:
    def list(self, *, pool: str = "default", status: str = "", page: int = 1, page_size: int = 10) -> dict:
        pool_name = str(pool or "default").strip() or "default"
        current_page = max(int(page or 1), 1)
        current_page_size = min(max(int(page_size or 10), 1), 200)

        with Session(engine) as session:
            where_clause = [LocalMicrosoftMailboxModel.pool == pool_name]
            if status:
                where_clause.append(LocalMicrosoftMailboxModel.status == status)

            total_query = select(func.count()).select_from(LocalMicrosoftMailboxModel).where(*where_clause)
            total = int(session.exec(total_query).one() or 0)
            pages = max((total + current_page_size - 1) // current_page_size, 1)
            current_page = min(current_page, pages)

            query = (
                select(LocalMicrosoftMailboxModel)
                .where(*where_clause)
                .order_by(LocalMicrosoftMailboxModel.id.desc())
                .offset((current_page - 1) * current_page_size)
                .limit(current_page_size)
            )
            items = session.exec(query).all()
            return {
                "items": items,
                "total": total,
                "page": current_page,
                "page_size": current_page_size,
                "pages": pages,
            }


    def import_items(self, *, pool: str, items: list[dict], replace: bool = False) -> dict:
        pool_name = str(pool or "default").strip() or "default"
        created = 0
        updated = 0
        with Session(engine) as session:
            if replace:
                existing = session.exec(
                    select(LocalMicrosoftMailboxModel).where(LocalMicrosoftMailboxModel.pool == pool_name)
                ).all()
                for row in existing:
                    session.delete(row)
                session.commit()

            for raw in items:
                email = str(raw.get("email") or "").strip().lower()
                if not email or "@" not in email:
                    continue
                row = session.exec(
                    select(LocalMicrosoftMailboxModel)
                    .where(LocalMicrosoftMailboxModel.pool == pool_name)
                    .where(LocalMicrosoftMailboxModel.email == email)
                ).first()
                now = _utcnow()
                if not row:
                    row = LocalMicrosoftMailboxModel(pool=pool_name, email=email)
                    row.created_at = now
                    created += 1
                else:
                    updated += 1

                row.password = str(raw.get("password") or row.password or "")
                row.client_id = str(raw.get("client_id") or row.client_id or "")
                row.refresh_token = str(raw.get("refresh_token") or row.refresh_token or "")
                row.status = str(raw.get("status") or row.status or "active")
                row.sub_status = str(raw.get("sub_status") or row.sub_status or "raw_master")
                row.fission_enabled = _to_bool(raw.get("fission_enabled"), row.fission_enabled)
                row.last_error = str(raw.get("last_error") or row.last_error or "")
                row.updated_at = now
                metadata = dict(row.get_metadata() or {})
                metadata.update(dict(raw.get("metadata") or {}))
                metadata.setdefault("success_count", int(metadata.get("success_count") or 0))
                metadata.setdefault("fail_count", int(metadata.get("fail_count") or 0))
                metadata.setdefault("platform_stats", dict(metadata.get("platform_stats") or {}))
                row.set_metadata(metadata)
                session.add(row)

            session.commit()

        return {"created": created, "updated": updated}

    @staticmethod
    def _resolve_route_weights(route_weights: dict | None = None) -> dict[str, float]:
        defaults = {
            "success_rate": 0.65,
            "freshness": 0.25,
            "affinity": 0.10,
        }
        merged = dict(defaults)
        if isinstance(route_weights, dict):
            merged.update({k: _to_float(v, defaults.get(k, 0.0)) for k, v in route_weights.items()})
        return merged

    @staticmethod
    def _runtime_health_score(
        row: LocalMicrosoftMailboxModel,
        *,
        now: datetime,
        platform: str,
        route_weights: dict[str, float],
    ) -> float:
        metadata = dict(row.get_metadata() or {})
        success_count = int(metadata.get("success_count") or 0)
        fail_count = int(metadata.get("fail_count") or 0)
        total = max(success_count + fail_count, 1)
        success_rate = success_count / total

        last_used = _normalize_dt(row.last_used_at) or (now - timedelta(days=365))
        freshness_seconds = max((now - last_used).total_seconds(), 0.0)
        freshness_score = min(freshness_seconds / 3600.0, 1.0)

        platform_stats = dict(row.get_metadata() or {})
        platform_key = str(platform or "").strip().lower()
        affinity_score = 0.5
        if platform_key and isinstance(platform_stats.get(platform_key), dict):
            p_success = int(platform_stats[platform_key].get("success") or 0)
            p_fail = int(platform_stats[platform_key].get("fail") or 0)
            p_total = max(p_success + p_fail, 1)
            affinity_score = p_success / p_total

        score = (
            route_weights.get("success_rate", 0.65) * success_rate
            + route_weights.get("freshness", 0.25) * freshness_score
            + route_weights.get("affinity", 0.10) * affinity_score
        )

        cooldown_until = _normalize_dt(row.cooldown_until)
        if row.status == "cooldown" and cooldown_until and cooldown_until > now:
            score -= 1.0
        return round(float(score), 6)


    def allocate(
        self,
        *,
        pool: str = "default",
        platform: str = "",
        leased_by_task_id: str = "",
        lease_seconds: int = 300,
        route_strategy: str = "fair",
        route_weights: dict | None = None,
    ) -> LocalMicrosoftMailboxModel | None:
        pool_name = str(pool or "default").strip() or "default"
        now = _utcnow()
        strategy = str(route_strategy or "fair").strip().lower()
        weights = self._resolve_route_weights(route_weights)

        with Session(engine) as session:
            candidates = session.exec(
                select(LocalMicrosoftMailboxModel)
                .where(LocalMicrosoftMailboxModel.pool == pool_name)
                .where(LocalMicrosoftMailboxModel.status.in_(["active", "cooldown"]))
                .where(LocalMicrosoftMailboxModel.email != "")
                .order_by(LocalMicrosoftMailboxModel.last_used_at, LocalMicrosoftMailboxModel.id)
            ).all()

            available: list[LocalMicrosoftMailboxModel] = []
            for row in candidates:
                cooldown_until = _normalize_dt(row.cooldown_until)
                leased_until = _normalize_dt(row.leased_until)
                if row.status == "cooldown" and cooldown_until and cooldown_until > now:
                    continue
                if leased_until and leased_until > now:
                    continue
                has_graph = bool(str(row.client_id or "").strip() and str(row.refresh_token or "").strip())
                has_imap = bool(str(row.password or "").strip())
                if not has_graph and not has_imap:
                    continue
                available.append(row)

            if not available:
                return None

            if strategy in {"score", "health", "success_rate"}:
                available.sort(
                    key=lambda item: (
                        self._runtime_health_score(item, now=now, platform=platform, route_weights=weights),
                        _normalize_dt(item.last_used_at) or datetime.min.replace(tzinfo=timezone.utc),
                    ),
                    reverse=True,
                )

            chosen = available[0]
            chosen.status = "leased"
            chosen.last_used_at = now
            chosen.leased_until = now + timedelta(seconds=max(int(lease_seconds or 0), 30))
            chosen.leased_by_task_id = str(leased_by_task_id or "")
            chosen.current_platform = str(platform or "")
            chosen.updated_at = now

            if _normalize_dt(chosen.cooldown_until) and _normalize_dt(chosen.cooldown_until) <= now:
                chosen.cooldown_until = None

            session.add(chosen)
            session.commit()
            session.refresh(chosen)
            return chosen

    def mark_runtime(
        self,
        mailbox_id: int,
        *,
        status: str | None = None,
        sub_status: str | None = None,
        last_error: str | None = None,
        cooldown_seconds: int = 0,
        release_lease: bool = False,
        increment_fission: bool = False,
        mark_refresh: bool = False,
        mark_success: bool = False,
    ) -> bool:
        with Session(engine) as session:
            row = session.get(LocalMicrosoftMailboxModel, int(mailbox_id or 0))
            if not row:
                return False
            now = _utcnow()
            if status:
                row.status = status
            if sub_status is not None:
                row.sub_status = str(sub_status)

            metadata = dict(row.get_metadata() or {})
            metadata.setdefault("success_count", int(metadata.get("success_count") or 0))
            metadata.setdefault("fail_count", int(metadata.get("fail_count") or 0))
            metadata.setdefault("platform_stats", dict(metadata.get("platform_stats") or {}))

            platform_key = str(row.current_platform or "").strip().lower()
            if platform_key:
                platform_stats = dict(metadata.get("platform_stats") or {})
                current = dict(platform_stats.get(platform_key) or {})
                current.setdefault("success", int(current.get("success") or 0))
                current.setdefault("fail", int(current.get("fail") or 0))
                platform_stats[platform_key] = current
                metadata["platform_stats"] = platform_stats

            if last_error is not None:
                normalized_error = str(last_error or "")
                row.last_error = normalized_error
                if normalized_error:
                    metadata["fail_count"] = int(metadata.get("fail_count") or 0) + 1
                    if platform_key:
                        platform_stats = dict(metadata.get("platform_stats") or {})
                        current = dict(platform_stats.get(platform_key) or {})
                        current["fail"] = int(current.get("fail") or 0) + 1
                        platform_stats[platform_key] = current
                        metadata["platform_stats"] = platform_stats

            if increment_fission:
                row.fission_count = int(row.fission_count or 0) + 1
            if mark_refresh:
                row.last_refresh_at = now
            if mark_success:
                row.last_success_at = now
                metadata["success_count"] = int(metadata.get("success_count") or 0) + 1
                if platform_key:
                    platform_stats = dict(metadata.get("platform_stats") or {})
                    current = dict(platform_stats.get(platform_key) or {})
                    current["success"] = int(current.get("success") or 0) + 1
                    platform_stats[platform_key] = current
                    metadata["platform_stats"] = platform_stats
            if cooldown_seconds and cooldown_seconds > 0:
                row.status = "cooldown"
                row.cooldown_until = now + timedelta(seconds=int(cooldown_seconds))
            if release_lease:
                row.leased_until = now - timedelta(seconds=1)
                row.leased_by_task_id = ""
                row.current_platform = ""
                if row.status == "leased":
                    row.status = "active"

            row.set_metadata(metadata)
            row.updated_at = now
            session.add(row)
            session.commit()
            return True

    def release_by_task(self, task_id: str) -> int:
        task = str(task_id or "").strip()
        if not task:
            return 0
        now = _utcnow()
        released = 0
        with Session(engine) as session:
            rows = session.exec(
                select(LocalMicrosoftMailboxModel)
                .where(LocalMicrosoftMailboxModel.leased_by_task_id == task)
                .where(LocalMicrosoftMailboxModel.leased_until.is_not(None))
            ).all()
            for row in rows:
                row.leased_until = now - timedelta(seconds=1)
                row.leased_by_task_id = ""
                row.current_platform = ""
                if row.status == "leased":
                    row.status = "active"
                row.updated_at = now
                session.add(row)
                released += 1
            session.commit()
        return released

    def pool_stats(self, *, pool: str = "default", platform: str = "") -> dict:
        pool_name = str(pool or "default").strip() or "default"
        platform_key = str(platform or "").strip().lower()
        now = _utcnow()
        weights = self._resolve_route_weights(None)

        with Session(engine) as session:
            rows = session.exec(
                select(LocalMicrosoftMailboxModel)
                .where(LocalMicrosoftMailboxModel.pool == pool_name)
                .order_by(LocalMicrosoftMailboxModel.id.desc())
            ).all()

        by_status: dict[str, int] = {}
        total_success = 0
        total_fail = 0
        scored: list[dict] = []

        for row in rows:
            by_status[row.status] = int(by_status.get(row.status, 0)) + 1
            metadata = dict(row.get_metadata() or {})
            total_success += int(metadata.get("success_count") or 0)
            total_fail += int(metadata.get("fail_count") or 0)
            scored.append(
                {
                    "id": int(row.id or 0),
                    "email": row.email,
                    "status": row.status,
                    "current_platform": row.current_platform,
                    "success_count": int(metadata.get("success_count") or 0),
                    "fail_count": int(metadata.get("fail_count") or 0),
                    "health_score": self._runtime_health_score(row, now=now, platform=platform_key, route_weights=weights),
                    "last_success_at": row.last_success_at,
                    "last_error": row.last_error,
                    "leased_until": row.leased_until,
                }
            )

        scored.sort(key=lambda item: (float(item.get("health_score") or 0), int(item.get("success_count") or 0)), reverse=True)
        total = len(rows)
        success_rate = round((total_success / max(total_success + total_fail, 1)) * 100, 2)
        return {
            "pool": pool_name,
            "platform": platform_key,
            "total": total,
            "status_distribution": by_status,
            "success_count": total_success,
            "fail_count": total_fail,
            "success_rate": success_rate,
            "top_candidates": scored[:10],
        }

    def delete(self, mailbox_id: int) -> bool:
        with Session(engine) as session:
            row = session.get(LocalMicrosoftMailboxModel, mailbox_id)
            if not row:
                return False
            session.delete(row)
            session.commit()
            return True
