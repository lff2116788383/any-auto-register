"""
注册流程引擎
参考 `openai-cpa` 的纯协议链路实现，移除浏览器依赖。
"""

from __future__ import annotations

import base64
import json
import logging
import random
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from curl_cffi import requests as cffi_requests

from .constants import (
    DEFAULT_PASSWORD_LENGTH,
    OPENAI_API_ENDPOINTS,
    OTP_CODE_PATTERN,
    PASSWORD_CHARSET,
    generate_random_user_info,
)
from .http_client import OpenAIHTTPClient
from .oauth import OAuthManager, OAuthStart

logger = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class RegistrationResult:
    """注册结果"""

    success: bool
    email: str = ""
    password: str = ""
    account_id: str = ""
    workspace_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""
    error_message: str = ""
    logs: list | None = None
    metadata: dict | None = None
    source: str = "register"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "email": self.email,
            "password": self.password,
            "account_id": self.account_id,
            "workspace_id": self.workspace_id,
            "access_token": self.access_token[:20] + "..." if self.access_token else "",
            "refresh_token": self.refresh_token[:20] + "..." if self.refresh_token else "",
            "id_token": self.id_token[:20] + "..." if self.id_token else "",
            "session_token": self.session_token[:20] + "..." if self.session_token else "",
            "error_message": self.error_message,
            "logs": self.logs or [],
            "metadata": self.metadata or {},
            "source": self.source,
        }


@dataclass
class SentinelPayload:
    """Sentinel 请求结果"""

    p: str
    c: str
    flow: str
    t: str = ""


class RegistrationEngine:
    """ChatGPT 纯协议邮箱注册引擎"""

    def __init__(
        self,
        email_service: Any,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None,
    ):
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid

        self.http_client = OpenAIHTTPClient(proxy_url=proxy_url)
        self.oauth_manager = OAuthManager(proxy_url=proxy_url)

        self.email: Optional[str] = None
        self.password: Optional[str] = None
        self.email_info: Optional[Dict[str, Any]] = None
        self.oauth_start: Optional[OAuthStart] = None
        self.session: Optional[cffi_requests.Session] = None
        self.session_token: Optional[str] = None
        self.logs: list[str] = []
        self._otp_sent_at: Optional[float] = None
        self._is_existing_account: bool = False
        self._device_id: str = ""
        self._user_agent: str = _BROWSER_UA
        self._sentinel_context: dict[str, Any] = {}
        self._target_continue_url: str = ""

    def _log(self, message: str, level: str = "info") -> None:
        timestamp = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
        self.logs.append(f"[{timestamp}] {message}")
        try:
            self.callback_logger(message)
        except Exception:
            pass
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    def _generate_password(self, length: int = DEFAULT_PASSWORD_LENGTH) -> str:
        specials = ",._!@#"
        if length < 10:
            length = 10
        pool = PASSWORD_CHARSET + specials
        password = [
            secrets.choice("abcdefghijklmnopqrstuvwxyz"),
            secrets.choice("0123456789"),
            secrets.choice(specials),
        ]
        while len(password) < length:
            password.append(secrets.choice(pool))
        random.shuffle(password)
        return "".join(password[:length])

    def _build_proxy_map(self) -> Optional[dict[str, str]]:
        if not self.proxy_url:
            return None
        proxy = self.proxy_url
        if proxy.startswith("socks5://"):
            proxy = proxy.replace("socks5://", "socks5h://", 1)
        return {"http": proxy, "https": proxy}

    def _oai_headers(self, did: str, extra: Optional[dict[str, str]] = None) -> dict[str, str]:
        headers = {
            "accept": "application/json",
            "accept-language": "en-US,en;q=0.9",
            "origin": "https://auth.openai.com",
            "user-agent": self._user_agent,
            "oai-device-id": did,
        }
        if extra:
            headers.update(extra)
        return headers

    def _extract_next_url(self, payload: dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            return ""
        continue_url = str(payload.get("continue_url") or "").strip()
        if continue_url:
            return continue_url
        next_url = str(payload.get("next_url") or "").strip()
        if next_url:
            return next_url
        page = payload.get("page") or {}
        if isinstance(page, dict):
            for key in ("continue_url", "next_url", "url"):
                value = str(page.get(key) or "").strip()
                if value:
                    return value
        return ""

    def _decode_jwt_segment(self, segment: str) -> dict[str, Any]:
        raw = str(segment or "").strip()
        if not raw:
            return {}
        pad = "=" * ((4 - (len(raw) % 4)) % 4)
        try:
            decoded = base64.urlsafe_b64decode((raw + pad).encode("ascii"))
            return json.loads(decoded.decode("utf-8"))
        except Exception:
            return {}

    def _parse_workspace_from_auth_cookie(self, auth_cookie: str) -> list[dict[str, Any]]:
        if not auth_cookie or "." not in auth_cookie:
            return []
        parts = auth_cookie.split(".")
        if len(parts) >= 2:
            claims = self._decode_jwt_segment(parts[1])
            workspaces = claims.get("workspaces") or []
            if workspaces:
                return workspaces
        claims = self._decode_jwt_segment(parts[0])
        return claims.get("workspaces") or []

    def _follow_redirect_chain_local(self, start_url: str) -> tuple[Optional[Any], str]:
        current_url = str(start_url or "").strip()
        if not current_url:
            return None, ""
        response = None
        for _ in range(10):
            response = self.session.get(current_url, allow_redirects=False, timeout=20)
            location = str(response.headers.get("Location") or "").strip()
            if response.status_code not in (301, 302, 303, 307, 308) or not location:
                return response, current_url
            current_url = urllib.parse.urljoin(current_url, location)
            if "code=" in current_url and "state=" in current_url:
                return response, current_url
        return response, current_url

    def _post_json(self, url: str, *, headers: dict[str, str], json_body: Optional[dict[str, Any]] = None, timeout: int = 30, allow_redirects: bool = True):
        payload = json.dumps(json_body or {}, separators=(",", ":")) if json_body is not None else None
        return self.session.post(
            url,
            headers=headers,
            data=payload,
            timeout=timeout,
            allow_redirects=allow_redirects,
        )

    def _attach_sentinel(self, headers: dict[str, str], flow: str, referer: str) -> dict[str, str]:
        sentinel = self._check_sentinel(self._device_id, flow=flow)
        new_headers = dict(headers)
        new_headers["referer"] = referer
        new_headers["content-type"] = "application/json"
        if sentinel:
            new_headers["openai-sentinel-token"] = json.dumps(
                {
                    "p": sentinel.p,
                    "t": sentinel.t,
                    "c": sentinel.c,
                    "id": self._device_id,
                    "flow": sentinel.flow,
                },
                separators=(",", ":"),
            )
        return new_headers

    def _check_ip_location(self) -> tuple[bool, Optional[str]]:
        try:
            return self.http_client.check_ip_location()
        except Exception as exc:
            self._log(f"检查 IP 地理位置失败: {exc}", "error")
            return False, None

    def _create_email(self) -> bool:
        try:
            self._log(f"正在创建 {self.email_service.service_type.value} 邮箱...")
            self.email_info = self.email_service.create_email()
            if not self.email_info or "email" not in self.email_info:
                self._log("创建邮箱失败: 返回信息不完整", "error")
                return False
            self.email = str(self.email_info["email"])
            self._log(f"成功创建邮箱: {self.email}")
            return True
        except Exception as exc:
            self._log(f"创建邮箱失败: {exc}", "error")
            return False

    def _start_oauth(self) -> bool:
        try:
            self.oauth_start = self.oauth_manager.start_oauth()
            self._log(f"OAuth URL 已生成: {self.oauth_start.auth_url[:80]}...")
            return True
        except Exception as exc:
            self._log(f"生成 OAuth URL 失败: {exc}", "error")
            return False

    def _init_session(self) -> bool:
        try:
            self.session = self.http_client.session
            self.session.headers.update({"Connection": "close", "User-Agent": self._user_agent})
            return True
        except Exception as exc:
            self._log(f"初始化会话失败: {exc}", "error")
            return False

    def _get_device_id(self) -> Optional[str]:
        if not self.oauth_start:
            return None
        try:
            self.session.get(self.oauth_start.auth_url, timeout=20)
            did = str(self.session.cookies.get("oai-did") or "").strip()
            self._device_id = did
            if did:
                self._log(f"Device ID: {did}")
                return did
            self._log("未获取到 oai-did", "warning")
            return None
        except Exception as exc:
            self._log(f"获取 Device ID 失败: {exc}", "error")
            return None

    def _check_sentinel(self, did: str, *, flow: str = "authorize_continue") -> Optional[SentinelPayload]:
        try:
            sent_p = ""
            if flow == "username_password_create":
                sent_p = (
                    "gAAAAACWzMwMDAsIlN1biBBcHIgMDUgMjAyNiAxNDowMzowMiBHTVQrMDgwMCAo5Lit5Zu95qCH5YeG5pe26Ze0KSIs"
                    "NDI5NDk2NzI5Niw1LCJNb3ppbGxhLzUuMCAoTWFjaW50b3NoOyBJbnRlbCBNYWMgT1MgWCAxMF8xNV83KSBBcHBsZVdl"
                    "YktpdC81MzcuMzYgKEtIVE1MLCBsaWtlIEdlY2tvKSBDaHJvbWUvMTQ2LjAuMC4wIFNhZmFyaS81MzcuMzYiLCJodHRw"
                    "czovL3NlbnRpbmVsLm9wZW5haS5jb20vYmFja2VuZC1hcGkvc2VudGluZWwvc2RrLmpzIixudWxsLCJ6aC1DTiIsInpo"
                    "LUNOLHpoIiw0LCJ3ZWJraXRUZW1wb3JhcnlTdG9yYWdl4oiSW29iamVjdCBEZXByZWNhdGVkU3RvcmFnZVF1b3RhXSIs"
                    "Il9yZWFjdExpc3RlbmluZ3dvMnk1YXV0eG1uIiwib25zY3JvbGxlbmQiLDIzOTQuNSwiYTY4OTQ0ZWYtZjI2Yi00MTc4"
                    "LWEwNTItZjE0NGZjOTYwMzgxIiwiIiwyLDE3NzUzNjg5Nzk2NjQuNiwwLDAsMCwwLDAsMCwwXQ==~S"
                )
            body = json.dumps({"p": sent_p, "id": did, "flow": flow}, separators=(",", ":"))
            response = self.http_client.post(
                OPENAI_API_ENDPOINTS["sentinel"],
                headers={
                    "origin": "https://sentinel.openai.com",
                    "referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
                    "content-type": "text/plain;charset=UTF-8",
                },
                data=body,
            )
            if response.status_code != 200:
                self._log(f"Sentinel 检查失败: flow={flow} status={response.status_code}", "warning")
                return None
            data = response.json() or {}
            turnstile = data.get("turnstile") or {}
            payload = SentinelPayload(
                p=sent_p,
                c=str(data.get("token") or ""),
                flow=flow,
                t=str(turnstile.get("dx") or ""),
            )
            self._log(f"Sentinel token 获取成功: flow={flow}")
            return payload
        except Exception as exc:
            self._log(f"Sentinel 检查异常: flow={flow} {exc}", "warning")
            return None

    def _submit_signup_or_takeover(self) -> bool:
        headers = self._attach_sentinel(
            self._oai_headers(self._device_id),
            "authorize_continue",
            "https://auth.openai.com/create-account",
        )
        response = self._post_json(
            OPENAI_API_ENDPOINTS["signup"],
            headers=headers,
            json_body={"username": {"value": self.email, "kind": "email"}, "screen_hint": "signup"},
        )
        self._log(f"提交注册表单状态: {response.status_code}")
        if response.status_code == 403:
            raise RuntimeError("注册请求触发 403 拦截")
        if response.status_code != 200:
            raise RuntimeError(f"提交注册表单失败: HTTP {response.status_code}: {response.text[:200]}")
        data = response.json() or {}
        continue_url = self._extract_next_url(data)
        page_type = str((data.get("page") or {}).get("type") or "")
        self._log(f"响应页面类型: {page_type or 'unknown'}")
        if "log-in" in continue_url:
            self._is_existing_account = True
            self._log("检测到已注册账号，将自动切换到无密码登录流程")
        return True

    def _register_password(self) -> tuple[bool, Optional[str]]:
        candidates: list[str] = []
        while len(candidates) < 3:
            pwd = self.password or self._generate_password()
            self.password = None
            if pwd not in candidates:
                candidates.append(pwd)
        for index, password in enumerate(candidates, start=1):
            self.password = password
            self._log(f"生成密码[{index}/{len(candidates)}]: {password}")
            headers = self._attach_sentinel(
                self._oai_headers(self._device_id),
                "username_password_create",
                "https://auth.openai.com/create-account/password",
            )
            response = self._post_json(
                OPENAI_API_ENDPOINTS["register"],
                headers=headers,
                json_body={"password": password, "username": self.email},
            )
            self._log(f"提交密码状态[{index}/{len(candidates)}]: {response.status_code}")
            if response.status_code == 200:
                data = response.json() or {}
                self._target_continue_url = self._extract_next_url(data)
                return True, password
            try:
                err = response.json().get("error", {})
                err_code = str(err.get("code") or "")
                err_msg = str(err.get("message") or "")
                if err_code == "user_exists" or "already" in err_msg.lower() or "exists" in err_msg.lower():
                    self._is_existing_account = True
                    self._log(f"邮箱 {self.email} 可能已在 OpenAI 注册过，切换到登录流程", "warning")
                    return True, None
            except Exception:
                pass
            self._log(f"密码注册失败[{index}/{len(candidates)}]: {response.text[:300]}", "warning")
        return False, None

    def _send_verification_code(self) -> bool:
        headers = self._attach_sentinel(
            self._oai_headers(self._device_id),
            "authorize_continue",
            "https://auth.openai.com/create-account/password",
        )
        response = self._post_json(
            OPENAI_API_ENDPOINTS["send_otp"],
            headers=headers,
            json_body={},
        )
        self._otp_sent_at = time.time()
        self._log(f"验证码发送状态: {response.status_code}")
        return response.status_code == 200

    def _get_verification_code(self) -> Optional[str]:
        try:
            self._log(f"正在等待邮箱 {self.email} 的验证码...")
            email_id = self.email_info.get("service_id") if self.email_info else None
            code = self.email_service.get_verification_code(
                email=self.email,
                email_id=email_id,
                timeout=120,
                pattern=OTP_CODE_PATTERN,
                otp_sent_at=self._otp_sent_at,
            )
            if code:
                self._log(f"成功获取验证码: {code}")
                return code
            self._log("等待验证码超时", "error")
            return None
        except Exception as exc:
            self._log(f"获取验证码失败: {exc}", "error")
            return None

    def _validate_verification_code(self, code: str) -> bool:
        headers = self._attach_sentinel(
            self._oai_headers(self._device_id),
            "authorize_continue",
            "https://auth.openai.com/email-verification",
        )
        response = self._post_json(
            OPENAI_API_ENDPOINTS["validate_otp"],
            headers=headers,
            json_body={"code": code},
        )
        self._log(f"验证码校验状态: {response.status_code}")
        if response.status_code != 200:
            return False
        data = response.json() or {}
        self._target_continue_url = self._extract_next_url(data)
        return True

    def _create_user_account(self) -> bool:
        user_info = generate_random_user_info()
        self._log(f"生成用户信息: {user_info['name']}, 生日: {user_info['birthdate']}")
        headers = self._attach_sentinel(
            self._oai_headers(self._device_id),
            "create_account",
            "https://auth.openai.com/about-you",
        )
        response = self._post_json(
            OPENAI_API_ENDPOINTS["create_account"],
            headers=headers,
            json_body=user_info,
        )
        self._log(f"账户创建状态: {response.status_code}")
        if response.status_code != 200:
            self._log(f"账户创建失败: {response.text[:200]}", "warning")
            return False
        data = response.json() or {}
        self._target_continue_url = self._extract_next_url(data)
        return True

    def _silent_login_after_registration(self) -> Optional[str]:
        oauth_start = self.oauth_manager.start_oauth()
        _, current_url = self._follow_redirect_chain_local(oauth_start.auth_url)
        if "code=" in current_url and "state=" in current_url:
            self.oauth_start = oauth_start
            return current_url

        log_did = str(self.session.cookies.get("oai-did") or self._device_id)
        headers = self._attach_sentinel(
            self._oai_headers(log_did),
            "authorize_continue",
            current_url or "https://auth.openai.com/",
        )
        login_start_resp = self._post_json(
            OPENAI_API_ENDPOINTS["signup"],
            headers=headers,
            json_body={"username": {"value": self.email, "kind": "email"}},
            allow_redirects=False,
        )
        if login_start_resp.status_code != 200:
            self._log(f"登录环节第一步请求被拒: HTTP {login_start_resp.status_code}", "error")
            return None

        if self._is_existing_account:
            send_headers = self._attach_sentinel(
                self._oai_headers(log_did),
                "authorize_continue",
                "https://auth.openai.com/email-verification",
            )
            send_resp = self._post_json(
                "https://auth.openai.com/api/accounts/passwordless/send-otp",
                headers=send_headers,
                json_body={},
            )
            if send_resp.status_code != 200:
                self._log(f"老账号 OAuth 阶段发信失败: {send_resp.status_code}", "error")
                return None
            self._otp_sent_at = time.time()
            code = self._get_verification_code()
            if not code:
                return None
            otp_headers = self._attach_sentinel(
                self._oai_headers(log_did),
                "authorize_continue",
                "https://auth.openai.com/email-verification",
            )
            otp_resp = self._post_json(
                OPENAI_API_ENDPOINTS["validate_otp"],
                headers=otp_headers,
                json_body={"code": code},
            )
            if otp_resp.status_code != 200:
                self._log(f"老账号 OAuth 阶段验证码未通过: {otp_resp.status_code}", "error")
                return None
            next_url = self._extract_next_url(otp_resp.json() or {})
            _, current_url = self._follow_redirect_chain_local(next_url)
        else:
            pwd_page_url = self._extract_next_url(login_start_resp.json() or {})
            _, current_url = self._follow_redirect_chain_local(pwd_page_url)
            pwd_headers = self._attach_sentinel(
                self._oai_headers(log_did),
                "password_verify",
                current_url or "https://auth.openai.com/log-in/password",
            )
            pwd_resp = self._post_json(
                "https://auth.openai.com/api/accounts/password/verify",
                headers=pwd_headers,
                json_body={"password": self.password},
            )
            if pwd_resp.status_code != 200:
                self._log(f"静默登录密码验证失败: {pwd_resp.status_code}", "error")
                return None
            next_url = self._extract_next_url(pwd_resp.json() or {})
            _, current_url = self._follow_redirect_chain_local(next_url)
            if current_url.endswith("/email-verification"):
                if not self._send_verification_code():
                    return None
                code = self._get_verification_code()
                if not code:
                    return None
                otp_headers = self._attach_sentinel(
                    self._oai_headers(log_did),
                    "authorize_continue",
                    "https://auth.openai.com/email-verification",
                )
                otp_resp = self._post_json(
                    OPENAI_API_ENDPOINTS["validate_otp"],
                    headers=otp_headers,
                    json_body={"code": code},
                )
                if otp_resp.status_code != 200:
                    self._log(f"静默登录二次安全验证失败: {otp_resp.status_code}", "error")
                    return None
                next_url = self._extract_next_url(otp_resp.json() or {})
                _, current_url = self._follow_redirect_chain_local(next_url)

        if "code=" in current_url and "state=" in current_url:
            self.oauth_start = oauth_start
            return current_url

        if current_url.endswith("/consent") or current_url.endswith("/workspace"):
            auth_cookie = str(self.session.cookies.get("oai-client-auth-session") or "")
            workspaces = self._parse_workspace_from_auth_cookie(auth_cookie)
            if workspaces:
                workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
                if workspace_id:
                    select_resp = self._post_json(
                        OPENAI_API_ENDPOINTS["select_workspace"],
                        headers=self._oai_headers(log_did, {"referer": current_url, "content-type": "application/json"}),
                        json_body={"workspace_id": workspace_id},
                    )
                    if select_resp.status_code == 200:
                        final_url = self._extract_next_url(select_resp.json() or {})
                        _, final_loc = self._follow_redirect_chain_local(final_url)
                        if "code=" in final_loc and "state=" in final_loc:
                            self.oauth_start = oauth_start
                            return final_loc
        return None

    def _get_workspace_id(self) -> Optional[str]:
        auth_cookie = str(self.session.cookies.get("oai-client-auth-session") or "")
        workspaces = self._parse_workspace_from_auth_cookie(auth_cookie)
        if not workspaces:
            self._log("授权 Cookie 里没有 workspace 信息", "error")
            return None
        workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
        if not workspace_id:
            self._log("无法解析 workspace_id", "error")
            return None
        self._log(f"Workspace ID: {workspace_id}")
        return workspace_id

    def _select_workspace(self, workspace_id: str) -> Optional[str]:
        response = self._post_json(
            OPENAI_API_ENDPOINTS["select_workspace"],
            headers=self._oai_headers(self._device_id, {"referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent", "content-type": "application/json"}),
            json_body={"workspace_id": workspace_id},
        )
        if response.status_code != 200:
            self._log(f"选择 workspace 失败: {response.status_code}", "error")
            self._log(f"响应: {response.text[:200]}", "warning")
            return None
        continue_url = self._extract_next_url(response.json() or {})
        if continue_url:
            self._log(f"Continue URL: {continue_url[:100]}...")
        return continue_url or None

    def _handle_oauth_callback(self, callback_url: str) -> Optional[Dict[str, Any]]:
        try:
            if not self.oauth_start:
                self._log("OAuth 流程未初始化", "error")
                return None
            return self.oauth_manager.handle_callback(
                callback_url=callback_url,
                expected_state=self.oauth_start.state,
                code_verifier=self.oauth_start.code_verifier,
            )
        except Exception as exc:
            self._log(f"处理 OAuth 回调失败: {exc}", "error")
            return None

    def run(self) -> RegistrationResult:
        result = RegistrationResult(success=False, logs=self.logs)
        try:
            self._log("=" * 60)
            self._log("开始纯协议注册流程")
            self._log("=" * 60)

            ip_ok, location = self._check_ip_location()
            if not ip_ok:
                result.error_message = f"IP 地理位置不支持: {location}"
                return result
            self._log(f"IP 位置: {location}")

            if not self.email:
                if not self._create_email():
                    result.error_message = "创建邮箱失败"
                    return result
            else:
                self.email_info = self.email_info or self.email_service.create_email()
            result.email = self.email or ""

            if not self._init_session():
                result.error_message = "初始化会话失败"
                return result
            if not self._start_oauth():
                result.error_message = "开始 OAuth 流程失败"
                return result
            did = self._get_device_id()
            if not did:
                result.error_message = "获取 Device ID 失败"
                return result

            self._submit_signup_or_takeover()

            if not self._is_existing_account:
                password_ok, password = self._register_password()
                if not password_ok:
                    result.error_message = "注册密码失败"
                    return result
                if password:
                    self.password = password
            else:
                self._otp_sent_at = time.time()

            if not self._is_existing_account:
                if not self._send_verification_code():
                    result.error_message = "发送验证码失败"
                    return result

            code = self._get_verification_code()
            if not code:
                result.error_message = "获取验证码失败"
                return result
            if not self._validate_verification_code(code):
                result.error_message = "验证验证码失败"
                return result

            if not self._is_existing_account:
                if not self._create_user_account():
                    result.error_message = "创建用户账户失败"
                    return result

            workspace_id = self._get_workspace_id()
            if workspace_id:
                result.workspace_id = workspace_id
                continue_url = self._select_workspace(workspace_id)
                callback_url = self._follow_redirect_chain_local(continue_url)[1] if continue_url else ""
            else:
                callback_url = ""

            if not callback_url or "code=" not in callback_url:
                callback_url = self._silent_login_after_registration() or ""

            if not callback_url:
                result.error_message = "纯协议 OAuth 回调提取失败"
                return result

            token_info = self._handle_oauth_callback(callback_url)
            if not token_info:
                result.error_message = "处理 OAuth 回调失败"
                return result

            result.account_id = token_info.get("account_id", "")
            result.access_token = token_info.get("access_token", "")
            result.refresh_token = token_info.get("refresh_token", "")
            result.id_token = token_info.get("id_token", "")
            result.password = self.password or ""
            result.source = "login" if self._is_existing_account else "register"

            session_cookie = self.session.cookies.get("__Secure-next-auth.session-token")
            if session_cookie:
                self.session_token = str(session_cookie)
                result.session_token = str(session_cookie)
                self._log("获取到 Session Token")

            result.success = True
            result.metadata = {
                "email_service": self.email_service.service_type.value,
                "proxy_used": self.proxy_url,
                "registered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "is_existing_account": self._is_existing_account,
                "mode": "protocol_only",
            }
            self._log("纯协议注册成功")
            self._log(f"邮箱: {result.email}")
            self._log(f"Account ID: {result.account_id}")
            self._log(f"Workspace ID: {result.workspace_id}")
            return result
        except Exception as exc:
            self._log(f"注册过程中发生未预期错误: {exc}", "error")
            result.error_message = str(exc)
            return result

    def save_to_database(self, result: RegistrationResult) -> bool:
        if not result.success:
            return False
        return True
