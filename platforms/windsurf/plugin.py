"""Windsurf 平台插件。"""
from __future__ import annotations

import random

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register
from platforms.windsurf.core import load_windsurf_account_state


def _status_from_overview(overview: dict) -> AccountStatus:
    plan_state = str((overview or {}).get("plan_state") or "").strip().lower()
    if plan_state == "subscribed":
        return AccountStatus.SUBSCRIBED
    if plan_state == "trial":
        return AccountStatus.TRIAL
    if plan_state == "free":
        return AccountStatus.REGISTERED
    if plan_state == "expired":
        return AccountStatus.EXPIRED
    return AccountStatus.REGISTERED


def _default_name(email: str) -> str:
    local = (email or "").split("@", 1)[0].strip()
    cleaned = "".join(ch if ch.isalnum() else " " for ch in local).strip()
    if cleaned:
        return " ".join(part.capitalize() for part in cleaned.split()[:2])
    return f"Windsurf User {random.randint(1000, 9999)}"


@register
class WindsurfPlatform(BasePlatform):
    name = "windsurf"
    display_name = "Windsurf"
    version = "1.0.0"
    supported_executors = ["protocol"]
    supported_identity_modes = ["mailbox"]
    supported_oauth_providers = []

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _map_windsurf_result(self, result: dict) -> RegistrationResult:
        overview = dict(result.get("account_overview") or {})
        return RegistrationResult(
            email=result["email"],
            password=result.get("password", ""),
            user_id=result.get("user_id", ""),
            token=result.get("session_token", ""),
            status=_status_from_overview(overview),
            extra={
                "name": result.get("name", ""),
                "auth_token": result.get("auth_token", ""),
                "session_token": result.get("session_token", ""),
                "account_id": result.get("account_id", ""),
                "org_id": result.get("org_id", ""),
                "account_overview": overview,
                "state_summary": result.get("state_summary", {}),
            },
        )

    def build_protocol_mailbox_adapter(self):
        def _build_worker(ctx, artifacts):
            from platforms.windsurf.protocol_mailbox import WindsurfProtocolMailboxWorker

            return WindsurfProtocolMailboxWorker(proxy=ctx.proxy, log_fn=ctx.log)

        def _run_worker(worker, ctx, artifacts):
            return worker.run(
                email=ctx.identity.email,
                password=ctx.password or "",
                name=str(ctx.extra.get("name") or _default_name(ctx.identity.email)),
                otp_callback=artifacts.otp_callback,
            )

        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_windsurf_result(result),
            worker_builder=_build_worker,
            register_runner=_run_worker,
            otp_spec=OtpSpec(
                keyword="Windsurf",
                code_pattern=r"\b(\d{6})\b",
                wait_message="等待 Windsurf 邮箱验证码...",
                success_label="验证码",
                timeout=resolve_timeout(self.config.extra or {}, ("otp_timeout",), 120),
            ),
        )

    def _load_state(self, account: Account) -> dict:
        return load_windsurf_account_state(
            account,
            proxy=self.config.proxy if self.config else None,
            log_fn=self.log,
        )

    def check_valid(self, account: Account) -> bool:
        try:
            state = self._load_state(account)
        except Exception:
            return False
        return bool((state.get("summary") or {}).get("valid"))

    def get_platform_actions(self) -> list:
        return [
            {"id": "get_account_state", "label": "查询账号状态/额度", "params": []},
            {"id": "generate_trial_link", "label": "生成 Pro Trial 链接", "params": [
                {"key": "turnstile_token", "label": "Turnstile Token（可空，自动打码）", "type": "text"},
            ]},
            {"id": "check_trial_eligibility", "label": "检查 Pro Trial 资格", "params": []},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id in {"get_user_info", "get_account_state"}:
            state = self._load_state(account)
            summary = dict(state.get("summary") or {})
            overview = dict(summary.get("account_overview") or {})
            return {
                "ok": True,
                "data": {
                    **summary,
                    "account_overview": overview,
                    "quota_note": "Windsurf 额度来自网站 application/proto 接口；当前解析 Prompt Credits 与 Flow Action Credits 的上限和剩余百分比。",
                },
            }

        if action_id == "check_trial_eligibility":
            from platforms.windsurf.core import WindsurfClient, extract_windsurf_account_context

            context = extract_windsurf_account_context(account)
            if not context["session_token"]:
                return {"ok": False, "error": "账号缺少 Windsurf session_token"}
            client = WindsurfClient(proxy=self.config.proxy if self.config else None, log_fn=self.log)
            eligible = client.check_pro_trial_eligibility(
                context["session_token"],
                account_id=context["account_id"],
                org_id=context["org_id"],
            )
            return {"ok": True, "data": {"trial_eligible": eligible, "message": "可试用" if eligible else "不可试用"}}

        if action_id in {"generate_trial_link", "generate_checkout_link", "payment_link", "get_cashier_url"}:
            from platforms.windsurf.core import WINDSURF_TURNSTILE_SITEKEY, WINDSURF_BASE, WindsurfClient, extract_windsurf_account_context

            context = extract_windsurf_account_context(account)
            if not context["session_token"]:
                return {"ok": False, "error": "账号缺少 Windsurf session_token"}

            turnstile_token = str(params.get("turnstile_token") or "").strip()
            if not turnstile_token:
                try:
                    self.log("自动获取 Windsurf Turnstile token...")
                    turnstile_token = self.solve_turnstile_with_fallback(
                        f"{WINDSURF_BASE}/billing/individual?plan=9",
                        WINDSURF_TURNSTILE_SITEKEY,
                    )
                except Exception as exc:
                    return {
                        "ok": False,
                        "error": (
                            "生成 Windsurf Pro Trial 链接需要 Turnstile token。"
                            f"自动打码失败: {exc}。"
                            "可以在动作参数里手动填入 turnstile_token，"
                            "或者启用 local_solver / 2Captcha 后重试。"
                        ),
                    }

            client = WindsurfClient(proxy=self.config.proxy if self.config else None, log_fn=self.log)
            trial_eligible = False
            try:
                trial_eligible = client.check_pro_trial_eligibility(
                    context["session_token"],
                    account_id=context["account_id"],
                    org_id=context["org_id"],
                )
            except Exception as exc:
                self.log(f"Windsurf trial eligibility 检查失败，继续尝试生成链接: {exc}")

            refreshed_auth: dict[str, str] = {}
            try:
                checkout = client.subscribe_to_plan(
                    context["session_token"],
                    account_id=context["account_id"],
                    org_id=context["org_id"],
                    turnstile_token=turnstile_token,
                )
            except RuntimeError as exc:
                if "HTTP 401" not in str(exc) or not context.get("auth_token"):
                    raise
                self.log("Windsurf SubscribeToPlan 返回 401，尝试用 auth_token 刷新 session 后重试...")
                refreshed_auth = client.post_auth(context["auth_token"])
                checkout = client.subscribe_to_plan(
                    refreshed_auth.get("session_token", "") or context["session_token"],
                    account_id=refreshed_auth.get("account_id", "") or context["account_id"],
                    org_id=refreshed_auth.get("org_id", "") or context["org_id"],
                    turnstile_token=turnstile_token,
                )
            url = str(checkout.get("checkout_url") or "").strip()
            refreshed_credentials = {
                key: value
                for key, value in {
                    "session_token": refreshed_auth.get("session_token", "") or context.get("session_token", ""),
                    "account_id": refreshed_auth.get("account_id", "") or context.get("account_id", ""),
                    "org_id": refreshed_auth.get("org_id", "") or context.get("org_id", ""),
                    "auth_token": refreshed_auth.get("auth_token", "") or context.get("auth_token", ""),
                }.items()
                if value
            }
            return {
                "ok": True,
                "data": {
                    "url": url,
                    "cashier_url": url,
                    "checkout_url": url,
                    "trial_eligible": trial_eligible,
                    "session_refreshed": bool(refreshed_auth),
                    **refreshed_credentials,
                    "message": "Windsurf Pro Trial 链接已生成",
                },
            }

        raise NotImplementedError(f"未知操作: {action_id}")

    def get_quota(self, account: Account) -> dict:
        state = self._load_state(account)
        return dict((state.get("summary") or {}).get("account_overview") or {})
