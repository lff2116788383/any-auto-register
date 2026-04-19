"""邮箱池基类 - 抽象临时邮箱/收件服务"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
import html
import re
from urllib.parse import urlencode, urlparse

from core.tls import insecure_request, mark_session_insecure, suppress_insecure_request_warning


@dataclass
class MailboxAccount:
    email: str
    account_id: str = ""
    extra: dict = None  # 平台额外信息


class BaseMailbox(ABC):
    @abstractmethod
    def get_email(self) -> MailboxAccount:
        """获取一个可用邮箱"""
        ...

    @abstractmethod
    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None,
                      code_pattern: str = None) -> str:
        """等待并返回验证码，code_pattern 为自定义正则（默认匹配6位数字）"""
        ...

    @abstractmethod
    def get_current_ids(self, account: MailboxAccount) -> set:
        """返回当前邮件 ID 集合（用于过滤旧邮件）"""
        ...

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        """等待并返回验证链接。默认由具体 provider 自行实现。"""
        raise NotImplementedError(f"{self.__class__.__name__} 暂不支持 wait_for_link()")


class FallbackMailbox(BaseMailbox):
    """按顺序尝试多个 provider，创建邮箱成功后固定使用同一 provider 收件。"""

    def __init__(self, providers: list[tuple[str, 'BaseMailbox']]):
        self.providers = [(str(key or "").strip(), mailbox) for key, mailbox in providers if str(key or "").strip() and mailbox]
        self._accounts: dict[str, BaseMailbox] = {}

    @staticmethod
    def _inject_provider_metadata(account: MailboxAccount, provider_key: str) -> MailboxAccount:
        account.extra = dict(account.extra or {})
        account.extra["mailbox_provider_key"] = provider_key
        provider_resource = dict((account.extra.get("provider_resource") or {}))
        if provider_resource and not provider_resource.get("provider_name"):
            provider_resource["provider_name"] = provider_key
            account.extra["provider_resource"] = provider_resource
        return account

    def _resolve_mailbox(self, account: MailboxAccount) -> BaseMailbox:
        provider_key = str((account.extra or {}).get("mailbox_provider_key") or "").strip()
        if provider_key:
            for key, mailbox in self.providers:
                if key == provider_key:
                    return mailbox
        mailbox = self._accounts.get(str(account.email or "").strip())
        if mailbox is not None:
            return mailbox
        raise RuntimeError(f"未找到邮箱 provider 上下文: {account.email}")

    def get_email(self) -> MailboxAccount:
        errors: list[str] = []
        for provider_key, mailbox in self.providers:
            try:
                print(f"[Mailbox] 尝试 provider: {provider_key}")
                account = mailbox.get_email()
                self._accounts[str(account.email or "").strip()] = mailbox
                self._inject_provider_metadata(account, provider_key)
                print(f"[Mailbox] 使用 provider 成功: {provider_key} -> {account.email}")
                return account
            except Exception as exc:
                message = str(exc).strip() or exc.__class__.__name__
                errors.append(f"{provider_key}: {message}")
                print(f"[Mailbox] provider 失败: {provider_key} -> {message}")
                continue
        raise RuntimeError("所有邮箱 provider 均创建失败: " + " | ".join(errors))

    def get_current_ids(self, account: MailboxAccount) -> set:
        return self._resolve_mailbox(account).get_current_ids(account)

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None,
                      code_pattern: str = None) -> str:
        return self._resolve_mailbox(account).wait_for_code(
            account,
            keyword=keyword,
            timeout=timeout,
            before_ids=before_ids,
            code_pattern=code_pattern,
        )

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        return self._resolve_mailbox(account).wait_for_link(
            account,
            keyword=keyword,
            timeout=timeout,
            before_ids=before_ids,
        )


def _extract_verification_link(text: str, keyword: str = "") -> str | None:
    combined = str(text or "")
    lowered = combined.lower()
    if keyword and keyword.lower() not in lowered:
        return None

    urls = [
        html.unescape(raw).rstrip(").,;")
        for raw in re.findall(r'https?://[^\s<>"\']+', combined, re.IGNORECASE)
    ]
    if not urls:
        return None

    primary_link_hints = ("verif", "confirm", "magic", "auth", "callback", "signin", "signup", "continue")
    primary_host_hints = ("tavily", "firecrawl", "clerk", "stytch", "auth", "login")
    for url in urls:
        url_lower = url.lower()
        if any(token in url_lower for token in primary_link_hints) and any(host in url_lower for host in primary_host_hints):
            return url

    verification_hints = ("verify", "verification", "confirm", "magic link", "sign in", "login", "auth", "tavily", "firecrawl")
    if not any(token in lowered for token in verification_hints):
        return None

    for url in urls:
        url_lower = url.lower()
        if any(token in url_lower for token in primary_link_hints):
            return url

    return urls[0]


def _normalize_api_base_url(value: str | None, *, default: str, label: str) -> str:
    raw = str(value or "").strip() or default
    if "://" not in raw:
        raw = f"https://{raw.lstrip('/')}"
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"{label} 无效: {value!r}")
    return raw.rstrip("/")


def _create_tempmail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return TempMailLolMailbox(proxy=proxy)


def _create_tempmail_web(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return TempMailWebMailbox(
        base_url=extra.get("tempmail_web_base_url", ""),
        proxy=proxy,
    )


def _create_duckmail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return DuckMailMailbox(
        api_url=extra.get("duckmail_api_url", ""),
        provider_url=extra.get("duckmail_provider_url", ""),
        bearer=extra.get("duckmail_bearer", ""),
        proxy=proxy,
    )


def _create_freemail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return FreemailMailbox(
        api_url=extra.get("freemail_api_url", ""),
        admin_token=extra.get("freemail_admin_token", ""),
        username=extra.get("freemail_username", ""),
        password=extra.get("freemail_password", ""),
        proxy=proxy,
    )


def _create_moemail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return MoeMailMailbox(
        api_url=extra.get("moemail_api_url"),
        username=extra.get("moemail_username", ""),
        password=extra.get("moemail_password", ""),
        session_token=extra.get("moemail_session_token", ""),
        proxy=proxy,
    )


def _create_cfworker(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return CFWorkerMailbox(
        api_url=extra.get("cfworker_api_url", ""),
        admin_token=extra.get("cfworker_admin_token", ""),
        domain=extra.get("cfworker_domain", ""),
        fingerprint=extra.get("cfworker_fingerprint", ""),
        proxy=proxy,
    )


def _create_testmail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return TestmailMailbox(
        api_url=extra.get("testmail_api_url", ""),
        api_key=extra.get("testmail_api_key", ""),
        namespace=extra.get("testmail_namespace", ""),
        tag_prefix=extra.get("testmail_tag_prefix", ""),
        proxy=proxy,
    )


def _create_laoudo(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return LaoudoMailbox(
        auth_token=extra.get("laoudo_auth", ""),
        email=extra.get("laoudo_email", ""),
        account_id=extra.get("laoudo_account_id", ""),
    )


def _split_email(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if "@" not in raw:
        return "", ""
    local, domain = raw.split("@", 1)
    return local.strip(), domain.strip().lower()


class LocalMicrosoftMailbox(BaseMailbox):
    """本地微软邮箱（Outlook/Hotmail）provider，支持固定母号与邮箱池模式。"""

    TOKEN_ENDPOINT = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    GRAPH_MESSAGES_ENDPOINT = "https://graph.microsoft.com/v1.0/me/messages"
    DEFAULT_GRAPH_SCOPE = "Mail.Read offline_access openid profile"


    def __init__(
        self,
        *,
        master_email: str,
        client_id: str,
        refresh_token: str,
        enable_fission: bool = True,
        alias_strategy: str = "plus",
        alias_prefix: str = "aar",
        alias_length: int = 8,
        fetch_mode: str = "auto",
        poll_interval_sec: int = 5,
        max_wait_sec: int = 180,
        graph_scope: str = DEFAULT_GRAPH_SCOPE,

        mode: str = "master_fission",
        pool: str = "default",
        pool_fission: bool = False,
        lease_ttl: int = 300,
        cooldown_on_timeout: int = 0,
        route_strategy: str = "fair",
        route_success_rate_weight: float = 0.65,
        route_freshness_weight: float = 0.25,
        route_affinity_weight: float = 0.10,
        alias_strategy_map: str = "",
        imap_host: str = "outlook.office365.com",

        imap_port: int = 993,
        imap_ssl: bool = True,
        imap_folder: str = "INBOX",
        imap_username: str = "",
        imap_password: str = "",
        imap_timeout_sec: int = 20,
        imap_top_n: int = 30,
        task_id: str = "",
        platform_name: str = "",
        proxy: str | None = None,
    ):

        self.master_email = str(master_email or "").strip().lower()
        self.client_id = str(client_id or "").strip()
        self.refresh_token = str(refresh_token or "").strip()
        self.enable_fission = bool(enable_fission)
        self.alias_strategy = str(alias_strategy or "plus").strip().lower()
        self.alias_prefix = str(alias_prefix or "aar").strip() or "aar"
        self.alias_length = max(4, int(alias_length or 8))
        self.fetch_mode = str(fetch_mode or "auto").strip().lower()
        self.poll_interval_sec = max(2, int(poll_interval_sec or 5))
        self.max_wait_sec = max(30, int(max_wait_sec or 180))
        self.graph_scope = self._normalize_graph_scope(graph_scope)
        self.mode = str(mode or "master_fission").strip().lower()

        self.pool = str(pool or "default").strip() or "default"
        self.pool_fission = bool(pool_fission)
        self.lease_ttl = max(30, int(lease_ttl or 300))
        self.cooldown_on_timeout = max(0, int(cooldown_on_timeout or 0))
        self.alias_strategy_map = str(alias_strategy_map or "")
        self.imap_host = str(imap_host or "outlook.office365.com").strip()
        self.imap_port = max(1, int(imap_port or 993))
        self.imap_ssl = bool(imap_ssl)
        self.imap_folder = str(imap_folder or "INBOX").strip() or "INBOX"
        self.imap_username = str(imap_username or "").strip()
        self.imap_password = str(imap_password or "").strip()
        self.imap_timeout_sec = max(5, int(imap_timeout_sec or 20))
        self.imap_top_n = max(5, int(imap_top_n or 30))
        self.task_id = str(task_id or "").strip()
        self.platform_name = str(platform_name or "").strip().lower()
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._access_token_cache: dict[str, str] = {}


    @staticmethod
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

    @staticmethod
    def _to_int(value, default: int) -> int:
        try:
            return int(str(value or "").strip())
        except Exception:
            return int(default)

    @staticmethod
    def _normalize_graph_scope(value: str | None) -> str:
        raw = str(value or "").strip()
        if not raw:
            return LocalMicrosoftMailbox.DEFAULT_GRAPH_SCOPE
        scopes: list[str] = []
        seen: set[str] = set()
        prefix = "https://graph.microsoft.com/"
        for token in raw.split():
            item = str(token or "").strip()
            if not item:
                continue
            if item.startswith(prefix):
                item = item[len(prefix):].strip()
            if item and item not in seen:
                scopes.append(item)
                seen.add(item)
        return " ".join(scopes) or LocalMicrosoftMailbox.DEFAULT_GRAPH_SCOPE

    @staticmethod
    def _parse_alias_strategy_map(raw: str) -> dict[str, str]:

        mapping: dict[str, str] = {}
        text = str(raw or "").strip()
        if not text:
            return mapping
        for token in text.split(","):
            pair = str(token or "").strip()
            if not pair or ":" not in pair:
                continue
            platform, strategy = pair.split(":", 1)
            p = str(platform or "").strip().lower()
            s = str(strategy or "").strip().lower()
            if p and s:
                mapping[p] = s
        return mapping

    def _effective_alias_strategy(self) -> str:
        mapping = self._parse_alias_strategy_map(self.alias_strategy_map)
        return str(mapping.get(self.platform_name) or self.alias_strategy or "plus").strip().lower()

    def _generate_email(self, master_email: str, allow_fission: bool, *, alias_strategy: str) -> tuple[str, bool, str]:

        import random
        import string

        local, domain = _split_email(master_email)
        if not local or not domain:
            raise RuntimeError(f"local_microsoft 配置错误：母邮箱无效 {master_email!r}")

        if (not allow_fission) or alias_strategy in {"raw", "raw_only"}:
            return master_email, False, ""


        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=self.alias_length))
        alias_tag = f"{self.alias_prefix}{suffix}"
        alias_email = f"{local}+{alias_tag}@{domain}"
        return alias_email, True, alias_tag

    def _allocate_pool_source(self) -> dict | None:
        from infrastructure.local_microsoft_mailboxes_repository import LocalMicrosoftMailboxesRepository

        row = LocalMicrosoftMailboxesRepository().allocate(
            pool=self.pool,
            platform=self.platform_name,
            leased_by_task_id=self.task_id,
            lease_seconds=self.lease_ttl,
        )

        if not row:
            return None
        return {
            "mailbox_id": int(row.id or 0),
            "master_email": str(row.email or "").strip().lower(),
            "client_id": str(row.client_id or "").strip(),
            "refresh_token": str(row.refresh_token or "").strip(),
            "imap_username": str(row.email or "").strip().lower(),
            "password": str(row.password or "").strip(),
            "fission_enabled": bool(row.fission_enabled),
            "pool": str(row.pool or self.pool),
            "source": "pool",
        }


    def _resolve_source(self) -> dict:
        if self.mode in {"pool", "hybrid"}:
            source = self._allocate_pool_source()
            if source:
                return source
            if self.mode == "pool":
                raise RuntimeError(f"local_microsoft 邮箱池为空或全部不可用: pool={self.pool}")

        if not self.master_email:
            raise RuntimeError("local_microsoft 缺少 master_email")
        has_graph = bool(self.client_id and self.refresh_token)
        has_imap = bool((self.imap_username or self.master_email) and self.imap_password)
        if not has_graph and not has_imap:
            raise RuntimeError("local_microsoft 缺少可用凭据（Graph 或 IMAP）")

        return {
            "mailbox_id": 0,
            "master_email": self.master_email,
            "client_id": self.client_id,
            "refresh_token": self.refresh_token,
            "imap_username": self.imap_username or self.master_email,
            "password": self.imap_password,
            "fission_enabled": bool(self.enable_fission),
            "pool": self.pool,
            "source": "master",
        }


    @staticmethod
    def _sanitize_imap_login(value: str | None) -> str:
        raw = str(value or "").strip().lower()
        if not raw:
            return ""
        match = re.search(r'[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}', raw, re.IGNORECASE)
        return str(match.group(0) if match else raw)

    def _refresh_graph_access_token(self, *, client_id: str, refresh_token: str, cache_key: str) -> str:
        import requests

        requested_scopes: list[str] = []
        for scope in ["https://graph.microsoft.com/.default offline_access", self.graph_scope]:
            normalized = self._normalize_graph_scope(scope) if scope != "https://graph.microsoft.com/.default offline_access" else scope
            if normalized and normalized not in requested_scopes:
                requested_scopes.append(normalized)

        last_error = ""
        for current_scope in requested_scopes:
            response = requests.post(
                self.TOKEN_ENDPOINT,
                data={
                    "client_id": client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": current_scope,
                },
                proxies=self.proxy,
                timeout=20,
            )
            payload = response.json() if response.content else {}
            if response.status_code < 400:
                access_token = str(payload.get("access_token") or "").strip()
                if not access_token:
                    raise RuntimeError("local_microsoft token 刷新失败：未返回 access_token")
                self._access_token_cache[cache_key] = access_token
                return access_token
            code = str(payload.get("error") or "")
            desc = str(payload.get("error_description") or response.text or "")[:300]
            last_error = f"local_microsoft token 刷新失败: {code or response.status_code} {desc}"
            error_text = f"{code} {desc}".lower()
            retryable_scope_error = ("aadsts70000" in error_text) or ("invalid_scope" in error_text)
            if not retryable_scope_error:
                break

        raise RuntimeError(last_error or "local_microsoft token 刷新失败")


    def _refresh_outlook_imap_access_token(self, *, client_id: str, refresh_token: str, cache_key: str) -> str:
        import requests

        response = requests.post(
            self.TOKEN_ENDPOINT,
            data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
            },
            proxies=self.proxy,
            timeout=20,
        )
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            code = str(payload.get("error") or "")
            desc = str(payload.get("error_description") or response.text or "")[:300]
            raise RuntimeError(f"local_microsoft IMAP token 刷新失败: {code or response.status_code} {desc}")

        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("local_microsoft IMAP token 刷新失败：未返回 access_token")
        self._access_token_cache[cache_key] = access_token
        return access_token

    def _account_credentials(self, account: MailboxAccount) -> tuple[str, str, str, int, str, str]:

        provider_account = dict((account.extra or {}).get("provider_account") or {})
        credentials = dict(provider_account.get("credentials") or {})
        master_email = str(credentials.get("master_email") or "").strip().lower()
        client_id = str(credentials.get("client_id") or "").strip()
        refresh_token = str(credentials.get("refresh_token") or "").strip()
        mailbox_id = self._to_int(credentials.get("mailbox_id"), 0)
        imap_username = str(credentials.get("imap_username") or "").strip()
        password = str(credentials.get("password") or "").strip()
        if not master_email:
            raise RuntimeError("local_microsoft 账号凭据缺失：master_email")
        return master_email, client_id, refresh_token, mailbox_id, imap_username, password


    def _list_graph_messages(self, account: MailboxAccount) -> list[dict]:
        import requests

        master_email, client_id, refresh_token, _, _, _ = self._account_credentials(account)
        if not client_id or not refresh_token:
            raise RuntimeError("local_microsoft Graph 凭据缺失")
        cache_key = f"{master_email}:{client_id}"
        token = self._access_token_cache.get(cache_key)
        if not token:
            token = self._refresh_graph_access_token(
                client_id=client_id,
                refresh_token=refresh_token,
                cache_key=cache_key,
            )
            self._mark_runtime(account, mark_refresh=True)

        params = {
            "$top": 25,
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,bodyPreview,body,from,toRecipients,receivedDateTime",
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        response = requests.get(
            self.GRAPH_MESSAGES_ENDPOINT,
            params=params,
            headers=headers,
            proxies=self.proxy,
            timeout=20,
        )

        if response.status_code == 401:
            token = self._refresh_graph_access_token(
                client_id=client_id,
                refresh_token=refresh_token,
                cache_key=cache_key,
            )
            self._mark_runtime(account, mark_refresh=True)

            headers["Authorization"] = f"Bearer {token}"
            response = requests.get(
                self.GRAPH_MESSAGES_ENDPOINT,
                params=params,
                headers=headers,
                proxies=self.proxy,
                timeout=20,
            )

        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            message = str((payload.get("error") or {}).get("message") or response.text or "")[:300]
            raise RuntimeError(f"local_microsoft 拉取邮件失败: HTTP {response.status_code} {message}")
        return list(payload.get("value") or [])

    def _list_imap_messages(self, account: MailboxAccount) -> list[dict]:
        import email
        import imaplib
        from email.header import decode_header
        from email.utils import getaddresses
        import socket

        master_email, client_id, refresh_token, _, imap_username, _ = self._account_credentials(account)
        provider_account = dict((account.extra or {}).get("provider_account") or {})
        username = self._sanitize_imap_login(
            imap_username or provider_account.get("login_identifier") or master_email
        )

        if not self.imap_host or not username:

            raise RuntimeError("local_microsoft IMAP 凭据缺失")
        if not client_id or not refresh_token:
            raise RuntimeError("local_microsoft IMAP OAuth 凭据缺失")

        socket.setdefaulttimeout(self.imap_timeout_sec)
        client = None
        messages: list[dict] = []

        def _decode(value: str) -> str:
            chunks = decode_header(value or "")
            text_parts: list[str] = []
            for part, encoding in chunks:
                if isinstance(part, bytes):
                    text_parts.append(part.decode(encoding or "utf-8", errors="ignore"))
                else:
                    text_parts.append(str(part or ""))
            return "".join(text_parts).strip()

        try:
            access_token = self._refresh_outlook_imap_access_token(
                client_id=client_id,
                refresh_token=refresh_token,
                cache_key=f"imap:{master_email}:{client_id}",
            )
            auth_string = f"user={username}\x01auth=Bearer {access_token}\x01\x01"
            auth_bytes = auth_string.encode("utf-8")

            if self.imap_ssl:
                client = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
            else:
                client = imaplib.IMAP4(self.imap_host, self.imap_port)
            client.authenticate("XOAUTH2", lambda _: auth_bytes)

            folders: list[str] = []
            primary_folder = str(self.imap_folder or "INBOX").strip() or "INBOX"
            for folder in [primary_folder, "Junk", '"Junk Email"', "Spam", '"垃圾邮件"']:
                normalized = str(folder or "").strip()
                if normalized and normalized not in folders:
                    folders.append(normalized)

            seen_ids: set[str] = set()
            for folder in folders:
                status, _ = client.select(folder)
                if status != "OK":
                    continue
                status, data = client.search(None, "ALL")
                if status != "OK":
                    continue
                ids = [item for item in (data[0] or b"").split() if item][-self.imap_top_n:]
                for raw_id in reversed(ids):
                    status, content = client.fetch(raw_id, "(RFC822)")
                    if status != "OK" or not content:
                        continue
                    for item in content:
                        if not isinstance(item, tuple) or len(item) < 2:
                            continue
                        raw_bytes = item[1]
                        if not raw_bytes:
                            continue
                        msg = email.message_from_bytes(raw_bytes)
                        subject = _decode(msg.get("Subject", ""))
                        message_id = str(msg.get("Message-ID") or raw_id.decode(errors="ignore") or "").strip()
                        if not message_id or message_id in seen_ids:
                            continue
                        seen_ids.add(message_id)
                        recipients = [addr.strip().lower() for _, addr in getaddresses([msg.get("To", "")]) if addr]

                        body_parts: list[str] = []
                        if msg.is_multipart():
                            for part in msg.walk():
                                ctype = str(part.get_content_type() or "").lower()
                                if ctype not in {"text/plain", "text/html"}:
                                    continue
                                payload = part.get_payload(decode=True)
                                if payload is None:
                                    continue
                                charset = part.get_content_charset() or "utf-8"
                                body_parts.append(payload.decode(charset, errors="ignore"))
                        else:
                            payload = msg.get_payload(decode=True)
                            if payload is not None:
                                charset = msg.get_content_charset() or "utf-8"
                                body_parts.append(payload.decode(charset, errors="ignore"))

                        body_text = "\n".join(body_parts).strip()
                        messages.append(
                            {
                                "id": message_id,
                                "subject": subject,
                                "bodyPreview": body_text[:240],
                                "body": {"content": body_text},
                                "toRecipients": [
                                    {"emailAddress": {"address": addr}}
                                    for addr in recipients
                                ],
                            }
                        )
            return messages
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass




    def _list_messages(self, account: MailboxAccount) -> list[dict]:
        mode = str(self.fetch_mode or "auto").strip().lower()
        if mode == "graph":
            return self._list_graph_messages(account)
        if mode == "imap":
            return self._list_imap_messages(account)

        try:
            return self._list_graph_messages(account)
        except Exception as graph_exc:
            try:
                return self._list_imap_messages(account)
            except Exception as imap_exc:
                raise RuntimeError(
                    f"local_microsoft 自动收件失败: graph={str(graph_exc)[:120]} | imap={str(imap_exc)[:120]}"
                ) from imap_exc

    @staticmethod
    def _extract_recipients(message: dict) -> set[str]:

        recipients = set()
        for item in list(message.get("toRecipients") or []):
            address = str(((item or {}).get("emailAddress") or {}).get("address") or "").strip().lower()
            if address:
                recipients.add(address)
        return recipients

    @staticmethod
    def _message_text(message: dict) -> str:
        subject = str(message.get("subject") or "")
        preview = str(message.get("bodyPreview") or "")
        body_content = str(((message.get("body") or {}).get("content")) or "")
        return f"{subject} {preview} {body_content}"

    def _matches_alias(self, message: dict, account: MailboxAccount) -> bool:
        recipients = self._extract_recipients(message)
        if not recipients:
            return True
        current = str(account.email or "").strip().lower()
        if current in recipients:
            return True
        master_email, _, _, _, _, _ = self._account_credentials(account)

        return master_email in recipients and current == master_email

    def _handle_service_abuse_mode(self, account: MailboxAccount, message: str) -> bool:
        text = str(message or "")
        if "aadsts70000" not in text.lower() or "service abuse mode" not in text.lower():
            return False
        self._mark_runtime(
            account,
            status="dead",
            sub_status="service_abuse_mode",
            last_error=text[:300],
            release_lease=True,
        )
        return True

    def _mark_runtime(self, account: MailboxAccount, *, status: str | None = None,
                      sub_status: str | None = None, last_error: str | None = None,
                      cooldown_seconds: int = 0, release_lease: bool = False,
                      increment_fission: bool = False, mark_refresh: bool = False,
                      mark_success: bool = False) -> None:
        provider_account = dict((account.extra or {}).get("provider_account") or {})
        credentials = dict(provider_account.get("credentials") or {})
        mailbox_id = self._to_int(credentials.get("mailbox_id"), 0)
        if mailbox_id <= 0:
            return
        from infrastructure.local_microsoft_mailboxes_repository import LocalMicrosoftMailboxesRepository

        LocalMicrosoftMailboxesRepository().mark_runtime(
            mailbox_id,
            status=status,
            sub_status=sub_status,
            last_error=last_error,
            cooldown_seconds=cooldown_seconds,
            release_lease=release_lease,
            increment_fission=increment_fission,
            mark_refresh=mark_refresh,
            mark_success=mark_success,
        )


    def get_email(self) -> MailboxAccount:
        source = self._resolve_source()
        allow_fission = bool(source["fission_enabled"]) if source["source"] == "master" else bool(source["fission_enabled"] and self.pool_fission)
        effective_alias_strategy = self._effective_alias_strategy()
        email, is_alias, alias_tag = self._generate_email(
            source["master_email"],
            allow_fission,
            alias_strategy=effective_alias_strategy,
        )
        resource_id = f"{source['master_email']}:{alias_tag or 'raw'}"
        provider_name = "local_microsoft"


        account = MailboxAccount(
            email=email,
            account_id=resource_id,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": provider_name,
                    "login_identifier": source["master_email"],
                    "display_name": source["master_email"],
                    "credentials": {
                        "client_id": source["client_id"],
                        "refresh_token": source["refresh_token"],
                        "master_email": source["master_email"],
                        "imap_username": source.get("imap_username", ""),
                        "password": source.get("password", ""),
                        "mailbox_id": source["mailbox_id"],
                    },
                    "metadata": {
                        "fetch_mode": self.fetch_mode,
                        "alias_strategy": effective_alias_strategy,
                        "alias_strategy_map": self.alias_strategy_map,
                        "platform": self.platform_name,
                        "is_alias": is_alias,

                        "alias_tag": alias_tag,
                        "source": source["source"],
                        "pool": source["pool"],
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": provider_name,
                    "resource_type": "mailbox",
                    "resource_identifier": resource_id,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "master_email": source["master_email"],
                        "is_alias": is_alias,
                        "alias_tag": alias_tag,
                        "fetch_mode": self.fetch_mode,
                        "source": source["source"],
                        "pool": source["pool"],
                        "mailbox_id": source["mailbox_id"],
                    },
                },
            },
        )
        if is_alias:
            self._mark_runtime(account, increment_fission=True)
        return account

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            messages = self._list_messages(account)
            return {
                str(message.get("id") or "").strip()
                for message in messages
                if str(message.get("id") or "").strip() and self._matches_alias(message, account)
            }
        except Exception:
            return set()


    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None,
                      code_pattern: str = None) -> str:
        import re
        import time

        wait_timeout = min(max(int(timeout or 0), 1), self.max_wait_sec)
        seen = set(before_ids or [])
        start = time.time()
        pattern = re.compile(code_pattern) if code_pattern else re.compile(r'(?<!#)(?<!\d)(\d{6})(?!\d)')

        try:
            while time.time() - start < wait_timeout:
                messages = self._list_messages(account)
                for message in messages:

                    mid = str(message.get("id") or "").strip()
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    if not self._matches_alias(message, account):
                        continue

                    text = self._message_text(message)
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    matched = pattern.search(text)
                    if matched:
                        self._mark_runtime(account, status="active", last_error="", mark_success=True, release_lease=True)
                        return matched.group(1) if matched.groups() else matched.group(0)
                time.sleep(self.poll_interval_sec)
        except Exception as exc:
            handled = self._handle_service_abuse_mode(account, str(exc))
            if not handled:
                self._mark_runtime(account, status="active", last_error=str(exc)[:300], release_lease=True)
            raise



        self._mark_runtime(
            account,
            status="cooldown" if self.cooldown_on_timeout > 0 else "active",
            sub_status="mail_timeout",
            last_error=f"等待验证码超时 ({wait_timeout}s)",
            cooldown_seconds=self.cooldown_on_timeout,
            release_lease=True,
        )
        raise TimeoutError(f"等待验证码超时 ({wait_timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time

        wait_timeout = min(max(int(timeout or 0), 1), self.max_wait_sec)
        seen = set(before_ids or [])
        start = time.time()

        try:
            while time.time() - start < wait_timeout:
                messages = self._list_messages(account)
                for message in messages:

                    mid = str(message.get("id") or "").strip()
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    if not self._matches_alias(message, account):
                        continue

                    link = _extract_verification_link(self._message_text(message), keyword)
                    if link:
                        self._mark_runtime(account, status="active", last_error="", mark_success=True, release_lease=True)
                        return link
                time.sleep(self.poll_interval_sec)
        except Exception as exc:
            handled = self._handle_service_abuse_mode(account, str(exc))
            if not handled:
                self._mark_runtime(account, status="active", last_error=str(exc)[:300], release_lease=True)
            raise



        self._mark_runtime(
            account,
            status="cooldown" if self.cooldown_on_timeout > 0 else "active",
            sub_status="mail_timeout",
            last_error=f"等待验证链接超时 ({wait_timeout}s)",
            cooldown_seconds=self.cooldown_on_timeout,
            release_lease=True,
        )
        raise TimeoutError(f"等待验证链接超时 ({wait_timeout}s)")


def _create_local_microsoft(extra: dict, proxy: str | None) -> 'BaseMailbox':

    def _bool(value, default=False):
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if not text:
            return default
        return text in {"1", "true", "yes", "on", "enabled"}

    def _int(value, default):
        try:
            return int(str(value or "").strip())
        except Exception:
            return int(default)

    return LocalMicrosoftMailbox(
        master_email=extra.get("local_ms_master_email", ""),
        client_id=extra.get("local_ms_client_id", ""),
        refresh_token=extra.get("local_ms_refresh_token", ""),
        enable_fission=_bool(extra.get("local_ms_enable_fission", "true"), True),
        alias_strategy=extra.get("local_ms_alias_strategy", "plus"),
        alias_prefix=extra.get("local_ms_alias_prefix", "aar"),
        alias_length=_int(extra.get("local_ms_alias_length", 8), 8),
        fetch_mode=extra.get("local_ms_fetch_mode", "auto"),
        poll_interval_sec=_int(extra.get("local_ms_poll_interval_sec", 5), 5),
        max_wait_sec=_int(extra.get("local_ms_max_wait_sec", 180), 180),
        graph_scope=extra.get("local_ms_graph_scope", LocalMicrosoftMailbox.DEFAULT_GRAPH_SCOPE),

        mode=extra.get("local_ms_mode", "pool"),
        pool=extra.get("local_ms_pool", "default"),
        pool_fission=_bool(extra.get("local_ms_pool_fission", "false"), False),
        lease_ttl=_int(extra.get("local_ms_lease_ttl", 300), 300),
        cooldown_on_timeout=_int(extra.get("local_ms_cooldown_on_timeout", 0), 0),
        route_strategy=extra.get("local_ms_route_strategy", "fair"),
        route_success_rate_weight=float(extra.get("local_ms_route_success_rate_weight", 0.65) or 0.65),
        route_freshness_weight=float(extra.get("local_ms_route_freshness_weight", 0.25) or 0.25),
        route_affinity_weight=float(extra.get("local_ms_route_affinity_weight", 0.10) or 0.10),
        alias_strategy_map=extra.get("local_ms_alias_strategy_map", ""),

        imap_host=extra.get("local_ms_imap_host", "outlook.office365.com"),
        imap_port=_int(extra.get("local_ms_imap_port", 993), 993),
        imap_ssl=_bool(extra.get("local_ms_imap_ssl", "true"), True),
        imap_folder=extra.get("local_ms_imap_folder", "INBOX"),
        imap_username=extra.get("local_ms_imap_username", ""),
        imap_password=extra.get("local_ms_imap_password", ""),
        imap_timeout_sec=_int(extra.get("local_ms_imap_timeout_sec", 20), 20),
        imap_top_n=_int(extra.get("local_ms_imap_top_n", 30), 30),
        task_id=extra.get("local_ms_task_id", ""),
        platform_name=extra.get("local_ms_platform", ""),
        proxy=proxy,
    )





MAILBOX_FACTORY_REGISTRY = {

    "tempmail_lol_api": _create_tempmail,
    "tempmail_web_api": _create_tempmail_web,
    "duckmail_api": _create_duckmail,
    "freemail_api": _create_freemail,
    "moemail_api": _create_moemail,
    "cfworker_admin_api": _create_cfworker,
    "testmail_api": _create_testmail,
    "laoudo_api": _create_laoudo,
    "localmicrosoftoauth": _create_local_microsoft,
    "local_microsoft_oauth": _create_local_microsoft,
    # backward-compat fallback
    "tempmail_lol": _create_tempmail,

    "tempmail_web": _create_tempmail_web,
    "duckmail": _create_duckmail,
    "freemail": _create_freemail,
    "moemail": _create_moemail,
    "cfworker": _create_cfworker,
    "testmail": _create_testmail,
    "laoudo": _create_laoudo,
    "local_microsoft": _create_local_microsoft,
}



def create_mailbox(provider: str, extra: dict = None, proxy: str = None) -> 'BaseMailbox':
    """工厂方法：根据 provider 创建对应的 mailbox 实例"""
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from infrastructure.provider_settings_repository import ProviderSettingsRepository

    definitions_repo = ProviderDefinitionsRepository()
    settings_repo = ProviderSettingsRepository()
    provider_key = str(provider or "").strip()
    if not provider_key:
        raise RuntimeError("未选择邮箱 provider，请先在设置页配置并启用默认邮箱 provider")
    definition = definitions_repo.get_by_key("mailbox", provider_key)
    if not definition or not definition.enabled:
        raise RuntimeError(f"邮箱 provider 不存在或未启用: {provider_key}")

    def _sanitize_override_values(payload: dict[str, object], *, current_provider_key: str) -> dict[str, object]:
        placeholder_values: dict[str, set[str]] = {
            "local_microsoft": {
                "yourname@outlook.com",
                "00000000-0000-0000-0000-000000000000",
            },
        }
        ignored_values = placeholder_values.get(str(current_provider_key or "").strip(), set())
        cleaned: dict[str, object] = {}
        for key, value in payload.items():
            if value is None:
                continue
            if isinstance(value, str):
                text = value.strip()
                if text == "":
                    continue
                if text in ignored_values:
                    continue
            cleaned[key] = value
        return cleaned

    base_extra = _sanitize_override_values(dict(extra or {}), current_provider_key=provider_key)



    raw_fallbacks = base_extra.get("mail_provider_fallbacks")
    explicit_fallbacks: list[str] = []
    if isinstance(raw_fallbacks, str):
        explicit_fallbacks = [item.strip() for item in raw_fallbacks.split(",") if item.strip()]
    elif isinstance(raw_fallbacks, (list, tuple, set)):
        explicit_fallbacks = [str(item or "").strip() for item in raw_fallbacks if str(item or "").strip()]

    ordered_keys: list[str] = [provider_key]
    for key in explicit_fallbacks:
        if key not in ordered_keys:
            ordered_keys.append(key)


    providers: list[tuple[str, BaseMailbox]] = []
    for key in ordered_keys:
        current_definition = definitions_repo.get_by_key("mailbox", key)
        if not current_definition or not current_definition.enabled:
            continue
        resolved_extra = settings_repo.resolve_runtime_settings("mailbox", key, base_extra)
        lookup_key = current_definition.driver_type if current_definition else key
        factory = MAILBOX_FACTORY_REGISTRY.get(lookup_key)
        if not factory:
            continue
        providers.append((key, factory(resolved_extra, proxy)))

    if not providers:
        raise RuntimeError("没有可用的邮箱 provider 实例")
    if len(providers) == 1:
        return providers[0][1]
    return FallbackMailbox(providers)


class LaoudoMailbox(BaseMailbox):
    """laoudo.com 邮箱服务"""
    def __init__(self, auth_token: str, email: str, account_id: str):
        self.auth = auth_token
        self._email = email
        self._account_id = account_id
        self.api = "https://laoudo.com/api/email"
        self._ua = "Mozilla/5.0"

    def get_email(self) -> MailboxAccount:
        return MailboxAccount(
            email=self._email,
            account_id=self._account_id,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "laoudo",
                    "login_identifier": self._email,
                    "display_name": self._email,
                    "credentials": {
                        "authorization": self.auth,
                    },
                    "metadata": {
                        "account_id": self._account_id,
                        "email": self._email,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "laoudo",
                    "resource_type": "mailbox",
                    "resource_identifier": self._account_id,
                    "handle": self._email,
                    "display_name": self._email,
                    "metadata": {
                        "account_id": self._account_id,
                        "email": self._email,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        from curl_cffi import requests as curl_requests
        try:
            r = curl_requests.get(
                f"{self.api}/list",
                params={"accountId": account.account_id, "allReceive": 0,
                        "emailId": 0, "timeSort": 1, "size": 50, "type": 0},
                headers={"authorization": self.auth, "user-agent": self._ua},
                timeout=15, impersonate="chrome131"
            )
            if r.status_code == 200:
                mails = r.json().get("data", {}).get("list", []) or []
                return {m.get("id") or m.get("emailId") for m in mails if m.get("id") or m.get("emailId")}
        except Exception:
            pass
        return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "trae",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time
        from curl_cffi import requests as curl_requests
        seen = set(before_ids) if before_ids else set()
        start = time.time()
        h = {"authorization": self.auth, "user-agent": self._ua}
        while time.time() - start < timeout:
            try:
                r = curl_requests.get(
                    f"{self.api}/list",
                    params={"accountId": account.account_id, "allReceive": 0,
                            "emailId": 0, "timeSort": 1, "size": 50, "type": 0},
                    headers=h, timeout=15, impersonate="chrome131"
                )
                if r.status_code == 200:
                    mails = r.json().get("data", {}).get("list", []) or []
                    for mail in mails:
                        mid = mail.get("id") or mail.get("emailId")
                        if not mid or mid in seen:
                            continue
                        seen.add(mid)
                        text = (str(mail.get("subject", "")) + " " +
                                str(mail.get("content") or mail.get("html") or ""))
                        if keyword and keyword.lower() not in text.lower():
                            continue
                        m = re.search(code_pattern or r'(?<!#)(?<!\d)(\d{6})(?!\d)', text)
                        if m:
                            return m.group(1) if m.groups() else m.group(0)
            except Exception:
                pass
            time.sleep(4)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time
        from curl_cffi import requests as curl_requests
        seen = set(before_ids or [])
        start = time.time()
        headers = {"authorization": self.auth, "user-agent": self._ua}
        while time.time() - start < timeout:
            try:
                r = curl_requests.get(
                    f"{self.api}/list",
                    params={"accountId": account.account_id, "allReceive": 0,
                            "emailId": 0, "timeSort": 1, "size": 50, "type": 0},
                    headers=headers, timeout=15, impersonate="chrome131"
                )
                if r.status_code == 200:
                    mails = r.json().get("data", {}).get("list", []) or []
                    for mail in mails:
                        mid = mail.get("id") or mail.get("emailId")
                        if not mid or mid in seen:
                            continue
                        seen.add(mid)
                        text = (str(mail.get("subject", "")) + " " +
                                str(mail.get("content") or mail.get("html") or ""))
                        link = _extract_verification_link(text, keyword)
                        if link:
                            return link
            except Exception:
                pass
            time.sleep(4)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class AitreMailbox(BaseMailbox):
    """mail.aitre.cc 临时邮箱"""
    def __init__(self, email: str):
        self._email = email
        self.api = "https://mail.aitre.cc/api/tempmail"

    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email=self._email)

    def get_current_ids(self, account: MailboxAccount) -> set:
        import requests
        try:
            r = requests.get(f"{self.api}/emails", params={"email": account.email}, timeout=10)
            emails = r.json().get("emails", [])
            return {str(m["id"]) for m in emails if "id" in m}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "trae",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time, requests
        seen = set(before_ids) if before_ids else set()
        last_check = None
        start = time.time()
        while time.time() - start < timeout:
            params = {"email": account.email}
            if last_check:
                params["lastCheck"] = last_check
            try:
                r = requests.get(f"{self.api}/poll", params=params, timeout=10)
                data = r.json()
                last_check = data.get("lastChecked")
                if data.get("count", 0) > 0:
                    r2 = requests.get(f"{self.api}/emails", params={"email": account.email}, timeout=10)
                    for mail in r2.json().get("emails", []):
                        mid = str(mail.get("id", ""))
                        if mid in seen:
                            continue
                        seen.add(mid)
                        text = mail.get("preview", "") + mail.get("content", "")
                        if keyword and keyword.lower() not in text.lower():
                            continue
                        m = re.search(code_pattern or r'(?<!#)(?<!\d)(\d{6})(?!\d)', text)
                        if m:
                            return m.group(1) if m.groups() else m.group(0)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time, requests
        seen = set(before_ids or [])
        last_check = None
        start = time.time()
        while time.time() - start < timeout:
            params = {"email": account.email}
            if last_check:
                params["lastCheck"] = last_check
            try:
                r = requests.get(f"{self.api}/poll", params=params, timeout=10)
                data = r.json()
                last_check = data.get("lastChecked")
                if data.get("count", 0) > 0:
                    r2 = requests.get(f"{self.api}/emails", params={"email": account.email}, timeout=10)
                    for mail in r2.json().get("emails", []):
                        mid = str(mail.get("id", ""))
                        if mid in seen:
                            continue
                        seen.add(mid)
                        text = str(mail.get("preview", "")) + " " + str(mail.get("content", ""))
                        link = _extract_verification_link(text, keyword)
                        if link:
                            return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class TempMailLolMailbox(BaseMailbox):
    """tempmail.lol 免费临时邮箱（无需注册，自动生成）"""

    def __init__(self, proxy: str = None):
        self.api = "https://api.tempmail.lol/v2"
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._token = None
        self._email = None

    def get_email(self) -> MailboxAccount:
        import requests
        r = requests.post(f"{self.api}/inbox/create",
            json={},
            proxies=self.proxy, timeout=15)
        data = r.json()
        self._email = data.get("address") or data.get("email", "")
        self._token = data.get("token", "")
        return MailboxAccount(
            email=self._email,
            account_id=self._token,
            extra={
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "tempmail_lol",
                    "resource_type": "mailbox",
                    "resource_identifier": self._token,
                    "handle": self._email,
                    "display_name": self._email,
                    "metadata": {
                        "email": self._email,
                        "token": self._token,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        import requests
        try:
            r = requests.get(f"{self.api}/inbox",
                params={"token": account.account_id},
                proxies=self.proxy, timeout=10)
            return {str(m["id"]) for m in r.json().get("emails", [])}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time, requests
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = requests.get(f"{self.api}/inbox",
                    params={"token": account.account_id},
                    proxies=self.proxy, timeout=10)
                for mail in sorted(r.json().get("emails", []), key=lambda x: x.get("date", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if mid in seen:
                        continue
                    seen.add(mid)
                    text = mail.get("subject", "") + " " + mail.get("body", "") + " " + mail.get("html", "")
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    m = re.search(code_pattern or r'(?<!#)(?<!\d)(\d{6})(?!\d)', text)
                    if m:
                        return m.group(1) if m.groups() else m.group(0)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time, requests
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = requests.get(f"{self.api}/inbox",
                    params={"token": account.account_id},
                    proxies=self.proxy, timeout=10)
                for mail in sorted(r.json().get("emails", []), key=lambda x: x.get("date", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if mid in seen:
                        continue
                    seen.add(mid)
                    text = str(mail.get("subject", "")) + " " + str(mail.get("body", "")) + " " + str(mail.get("html", ""))
                    link = _extract_verification_link(text, keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class TempMailWebMailbox(BaseMailbox):
    """参考项目同款 Temp-Mail Web API。"""

    def __init__(self, base_url: str = "", proxy: str = None):
        self.base_url = _normalize_api_base_url(
            base_url,
            default="https://web2.temp-mail.org",
            label="Temp-Mail Web URL",
        )
        self.proxy = str(proxy or "").strip()
        self._accounts: dict[str, str] = {}
        self._executor = None
        self._browser = None
        self._page = None

    def _ensure_browser(self):
        if self._page is not None:
            return self._page
        from camoufox.sync_api import Camoufox

        launch_opts = {"headless": True}
        if self.proxy:
            parsed = urlparse(self.proxy)
            if parsed.scheme and parsed.hostname and parsed.port:
                proxy_config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
                if parsed.username:
                    proxy_config["username"] = parsed.username
                if parsed.password:
                    proxy_config["password"] = parsed.password
                launch_opts["proxy"] = proxy_config
            else:
                launch_opts["proxy"] = {"server": self.proxy}
            launch_opts["geoip"] = True
        self._browser = Camoufox(**launch_opts)
        browser = self._browser.__enter__()
        self._page = browser.new_page()
        self._page.goto(self.base_url, wait_until="domcontentloaded", timeout=30000)
        return self._page

    def _run_in_browser_thread(self, fn):
        from concurrent.futures import ThreadPoolExecutor

        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tempmail-web")
        future = self._executor.submit(fn)
        return future.result()

    @staticmethod
    def _decode_json_response(response: dict, action: str):
        import json

        status = int((response or {}).get("status", 0) or 0)
        text = str((response or {}).get("body", "") or "")
        if status != 200:
            raise RuntimeError(
                f"Temp-Mail Web {action} 失败: HTTP {status} {text[:300]}"
            )
        try:
            return json.loads(text)
        except Exception as exc:
            raise RuntimeError(
                f"Temp-Mail Web {action} 返回非 JSON: {exc}; body={text[:300]}"
            ) from exc

    def _request_json(self, method: str, path: str, *, auth_header: str = "") -> dict | list:
        import random
        import time

        target_url = f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"
        action = "创建邮箱" if path.lstrip("/") == "mailbox" else "拉取消息"
        max_attempts = 4 if path.lstrip("/") == "mailbox" else 2

        for attempt in range(1, max_attempts + 1):
            def _browser_call():
                page = self._ensure_browser()
                return page.evaluate(
                    """
                    async ({ targetUrl, method, authHeader, baseUrl }) => {
                      try {
                        const response = await fetch(targetUrl, {
                          method,
                          credentials: 'include',
                          referrer: baseUrl,
                          headers: {
                            'Accept': 'application/json',
                            ...(method === 'GET' ? { 'Cache-Control': 'no-cache' } : {}),
                            ...(authHeader ? { 'Authorization': authHeader } : {}),
                          },
                          ...(method === 'POST' ? { body: '{}' } : {}),
                        });
                        return {
                          status: response.status,
                          body: await response.text(),
                        };
                      } catch (error) {
                        return {
                          status: 0,
                          body: error instanceof Error ? error.message : String(error),
                        };
                      }
                    }
                    """,
                    {
                        "targetUrl": target_url,
                        "method": method,
                        "authHeader": auth_header,
                        "baseUrl": self.base_url,
                    },
                )

            result = self._run_in_browser_thread(_browser_call)
            status = int((result or {}).get("status", 0) or 0)
            if status != 429 or attempt >= max_attempts:
                return self._decode_json_response(result, action)
            wait_seconds = min(20, 3 * attempt + random.uniform(0.5, 2.5))
            print(f"[TempMailWeb] {action} 遇到 429，等待 {wait_seconds:.1f}s 后重试 ({attempt}/{max_attempts})")
            time.sleep(wait_seconds)

        return self._decode_json_response(result, action)

    def get_email(self) -> MailboxAccount:
        import json

        data = self._request_json("POST", "/mailbox")
        address = str(data.get("address") or data.get("mailbox") or data.get("email") or "").strip()
        token = str(data.get("token") or "").strip()
        if not address or not token:
            raise RuntimeError(f"Temp-Mail Web 创建邮箱失败: {json.dumps(data, ensure_ascii=False)[:300]}")
        self._accounts[address] = token
        print(f"[TempMailWeb] 生成邮箱: {address}")
        return MailboxAccount(
            email=address,
            account_id=token,
            extra={
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "tempmail_web",
                    "resource_type": "mailbox",
                    "resource_identifier": token,
                    "handle": address,
                    "display_name": address,
                    "metadata": {
                        "email": address,
                        "token": token,
                        "base_url": self.base_url,
                    },
                },
            },
        )

    def _fetch_messages(self, account: MailboxAccount) -> list[dict]:
        token = str(account.account_id or self._accounts.get(account.email) or "").strip()
        if not token:
            raise RuntimeError(f"Temp-Mail Web 缺少 token: {account.email}")
        data = self._request_json("GET", "/messages", auth_header=f"Bearer {token}")
        if isinstance(data, dict) and isinstance(data.get("messages"), list):
            return list(data.get("messages") or [])
        if isinstance(data, list):
            return data
        return []

    @staticmethod
    def _message_id(message: dict) -> str:
        return str(
            message.get("id")
            or message.get("_id")
            or f"{message.get('createdAt', '')}:{message.get('subject', '')}"
        )

    @staticmethod
    def _extract_code(message: dict, code_pattern: str | None = None) -> str:
        subject = str(message.get("subject") or "").strip()
        if subject:
            last_token = subject.split()[-1]
            if re.fullmatch(r"\d{6}", last_token):
                return last_token
        text = " ".join(
            str(message.get(key) or "")
            for key in ("subject", "body", "text", "content", "html")
        )
        match = re.search(code_pattern or r"(?<!#)(?<!\d)(\d{6})(?!\d)", text)
        if not match:
            return ""
        return match.group(1) if match.groups() else match.group(0)

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {
                self._message_id(item)
                for item in self._fetch_messages(account)
                if self._message_id(item)
            }
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import time

        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                messages = self._fetch_messages(account)
                for item in messages:
                    mid = self._message_id(item)
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    text = " ".join(
                        str(item.get(key) or "")
                        for key in ("subject", "body", "text", "content", "html")
                    )
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    code = self._extract_code(item, code_pattern=code_pattern)
                    if code:
                        print(f"[TempMailWeb] 收到验证码: {code}")
                        return code
            except Exception:
                pass
            time.sleep(5)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time

        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                messages = self._fetch_messages(account)
                for item in messages:
                    mid = self._message_id(item)
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    text = " ".join(
                        str(item.get(key) or "")
                        for key in ("subject", "body", "text", "content", "html")
                    )
                    link = _extract_verification_link(text, keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(5)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")

    def __del__(self):
        executor = getattr(self, "_executor", None)
        browser = getattr(self, "_browser", None)
        if executor is not None and browser is not None:
            try:
                executor.submit(browser.__exit__, None, None, None).result(timeout=5)
            except Exception:
                pass
        if executor is not None:
            try:
                executor.shutdown(wait=False, cancel_futures=False)
            except Exception:
                pass


class DuckMailMailbox(BaseMailbox):
    """DuckMail 自动生成邮箱（随机创建账号）"""

    def __init__(self, api_url: str = "",
                 provider_url: str = "",
                 bearer: str = "",
                 proxy: str = None):
        self.api = api_url.rstrip("/")
        self.provider_url = provider_url
        self.bearer = bearer
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._token = None
        self._address = None

    def _common_headers(self) -> dict:
        return {
            "authorization": f"Bearer {self.bearer}",
            "content-type": "application/json",
            "x-api-provider-base-url": self.provider_url,
        }

    def get_email(self) -> MailboxAccount:
        import requests, random, string
        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        password = "Test" + "".join(random.choices(string.digits, k=8)) + "!"
        domain = self.provider_url.replace("https://api.", "").replace("https://", "")
        address = f"{username}@{domain}"
        # 创建账号
        r = insecure_request(requests.post, f"{self.api}/api/mail?endpoint=%2Faccounts",
            json={"address": address, "password": password},
            headers=self._common_headers(), proxies=self.proxy, timeout=15)
        data = r.json()
        self._address = data.get("address", address)
        # 登录获取 token
        r2 = insecure_request(requests.post, f"{self.api}/api/mail?endpoint=%2Ftoken",
            json={"address": self._address, "password": password},
            headers=self._common_headers(), proxies=self.proxy, timeout=15)
        self._token = r2.json().get("token", "")
        return MailboxAccount(
            email=self._address,
            account_id=self._token,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "duckmail",
                    "login_identifier": self._address,
                    "display_name": self._address,
                    "credentials": {
                        "address": self._address,
                        "password": password,
                        "token": self._token,
                    },
                    "metadata": {
                        "provider_url": self.provider_url,
                        "api_url": self.api,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "duckmail",
                    "resource_type": "mailbox",
                    "resource_identifier": self._token,
                    "handle": self._address,
                    "display_name": self._address,
                    "metadata": {
                        "email": self._address,
                        "provider_url": self.provider_url,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        import requests
        try:
            r = insecure_request(requests.get, f"{self.api}/api/mail?endpoint=%2Fmessages%3Fpage%3D1",
                headers={"authorization": f"Bearer {account.account_id}",
                         "x-api-provider-base-url": self.provider_url},
                proxies=self.proxy, timeout=10)
            return {str(m["id"]) for m in r.json().get("hydra:member", [])}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time, requests
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = insecure_request(requests.get, f"{self.api}/api/mail?endpoint=%2Fmessages%3Fpage%3D1",
                    headers={"authorization": f"Bearer {account.account_id}",
                             "x-api-provider-base-url": self.provider_url},
                    proxies=self.proxy, timeout=10)
                msgs = r.json().get("hydra:member", [])
                for msg in msgs:
                    mid = str(msg.get("id") or msg.get("msgid") or "")
                    if mid in seen: continue
                    seen.add(mid)
                    # 请求邮件详情获取完整 text
                    try:
                        r2 = insecure_request(requests.get, f"{self.api}/api/mail?endpoint=%2Fmessages%2F{mid}",
                            headers={"authorization": f"Bearer {account.account_id}",
                                     "x-api-provider-base-url": self.provider_url},
                            proxies=self.proxy, timeout=10)
                        detail = r2.json()
                        body = str(detail.get("text") or "") + " " + str(detail.get("subject") or "")
                    except Exception:
                        body = str(msg.get("subject") or "")
                    body = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', body)
                    m = re.search(r"(?<!#)(?<!\d)(\d{6})(?!\d)", body)
                    if m: return m.group(1)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time, requests
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = insecure_request(requests.get, f"{self.api}/api/mail?endpoint=%2Fmessages%3Fpage%3D1",
                    headers={"authorization": f"Bearer {account.account_id}",
                             "x-api-provider-base-url": self.provider_url},
                    proxies=self.proxy, timeout=10)
                msgs = r.json().get("hydra:member", [])
                for msg in msgs:
                    mid = str(msg.get("id") or msg.get("msgid") or "")
                    if mid in seen:
                        continue
                    seen.add(mid)
                    try:
                        r2 = insecure_request(requests.get, f"{self.api}/api/mail?endpoint=%2Fmessages%2F{mid}",
                            headers={"authorization": f"Bearer {account.account_id}",
                                     "x-api-provider-base-url": self.provider_url},
                            proxies=self.proxy, timeout=10)
                        detail = r2.json()
                        body = str(detail.get("text") or "") + " " + str(detail.get("html") or "") + " " + str(detail.get("subject") or "")
                    except Exception:
                        body = str(msg.get("subject") or "")
                    link = _extract_verification_link(body, keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class CFWorkerMailbox(BaseMailbox):
    """Cloudflare Worker 自建临时邮箱服务"""

    def __init__(self, api_url: str, admin_token: str = "", domain: str = "",
                 fingerprint: str = "", proxy: str = None):
        self.api = api_url.rstrip("/")
        self.admin_token = admin_token
        self.domain = domain
        self.fingerprint = fingerprint
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._token = None

    def _headers(self) -> dict:
        h = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "x-admin-auth": self.admin_token,
        }
        if self.fingerprint:
            h["x-fingerprint"] = self.fingerprint
        return h

    def get_email(self) -> MailboxAccount:
        import requests, random, string
        name = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        payload = {"enablePrefix": True, "name": name}
        if self.domain:
            payload["domain"] = self.domain
        r = requests.post(f"{self.api}/admin/new_address",
            json=payload, headers=self._headers(),
            proxies=self.proxy, timeout=15)
        print(f"[CFWorker] new_address status={r.status_code} resp={r.text[:200]}")
        data = r.json()
        email = data.get("email", data.get("address", ""))
        token = data.get("token", data.get("jwt", ""))
        self._token = token
        print(f"[CFWorker] 生成邮箱: {email} token={token[:40] if token else 'NONE'}...")
        return MailboxAccount(
            email=email,
            account_id=token,
            extra={
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "cfworker",
                    "resource_type": "mailbox",
                    "resource_identifier": token or email,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "token": token,
                        "api_url": self.api,
                        "domain": self.domain,
                    },
                },
            },
        )

    def _get_mails(self, email: str) -> list:
        import requests
        r = requests.get(f"{self.api}/admin/mails",
            params={"limit": 20, "offset": 0, "address": email},
            headers=self._headers(), proxies=self.proxy, timeout=10)
        data = r.json()
        return data.get("results", data) if isinstance(data, dict) else data

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            mails = self._get_mails(account.email)
            return {str(m.get("id", "")) for m in mails}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                mails = self._get_mails(account.email)
                for mail in sorted(mails, key=lambda x: x.get("id", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    raw = str(mail.get("raw", ""))
                    # 1. 优先匹配 <span>XXXXXX</span> （Trae 邮件格式）
                    code_m = re.search(r'<span[^>]*>\s*(\d{6})\s*</span>', raw)
                    if code_m:
                        return code_m.group(1)
                    # 2. 跳过 MIME header，只搜 body 部分，避免匹配时间戳
                    body_start = raw.find('\r\n\r\n')
                    search_text = raw[body_start:] if body_start != -1 else raw
                    search_text = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', search_text)
                    # 排除时间戳模式 m=+XXXXXX. 和 t=XXXXXXXXXX
                    search_text = re.sub(r'm=\+\d+\.\d+', '', search_text)
                    search_text = re.sub(r'\bt=\d+\b', '', search_text)
                    m = re.search(code_pattern or r'(?<!#)(?<!\d)(\d{6})(?!\d)', search_text)
                    if m:
                        return m.group(1) if m.groups() else m.group(0)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                mails = self._get_mails(account.email)
                for mail in sorted(mails, key=lambda x: x.get("id", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    raw = str(mail.get("raw", ""))
                    link = _extract_verification_link(raw, keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class MoeMailMailbox(BaseMailbox):
    """MoeMail (sall.cc) 邮箱服务 - 自动注册账号并生成临时邮箱"""

    def __init__(
        self,
        api_url: str = "",
        username: str = "",
        password: str = "",
        session_token: str = "",
        proxy: str = None,
    ):
        self.api = _normalize_api_base_url(api_url, default="", label="MoeMail API URL")
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._configured_username = str(username or "").strip()
        self._configured_password = str(password or "")
        self._configured_session_token = str(session_token or "").strip()
        self._session_token = self._configured_session_token or None
        self._email = None
        self._session = None
        self._username = self._configured_username
        self._password = self._configured_password

    def _new_session(self):
        import requests

        s = requests.Session()
        s.proxies = self.proxy
        mark_session_insecure(s)
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        s.headers.update({"user-agent": ua, "origin": self.api, "referer": f"{self.api}/zh-CN/login"})
        return s

    def _extract_session_token(self, session) -> str:
        for cookie in session.cookies:
            if "session-token" in cookie.name:
                return cookie.value
        return ""

    def _apply_session_token(self, session, token: str) -> None:
        domain = urlparse(self.api).hostname or ""
        cookie_names = [
            "__Secure-authjs.session-token",
            "authjs.session-token",
            "__Secure-next-auth.session-token",
            "next-auth.session-token",
        ]
        for name in cookie_names:
            session.cookies.set(name, token, domain=domain, path="/")
            session.cookies.set(name, token, path="/")

    def _login_with_existing_account(self) -> str:
        s = self._new_session()

        if self._configured_session_token:
            self._apply_session_token(s, self._configured_session_token)
            self._session = s
            self._session_token = self._configured_session_token
            print("[MoeMail] 使用已提供的 session-token")
            return self._configured_session_token

        if not (self._configured_username and self._configured_password):
            raise RuntimeError("MoeMail 未配置可复用账号，请提供用户名密码或 session-token")

        with suppress_insecure_request_warning():
            csrf_r = s.get(f"{self.api}/api/auth/csrf", timeout=10)
        csrf = csrf_r.json().get("csrfToken", "")
        with suppress_insecure_request_warning():
            login_resp = s.post(
                f"{self.api}/api/auth/callback/credentials",
                headers={"content-type": "application/x-www-form-urlencoded"},
                data=urlencode({
                    "username": self._configured_username,
                    "password": self._configured_password,
                    "csrfToken": csrf,
                    "redirect": "false",
                    "callbackUrl": self.api,
                }),
                allow_redirects=True,
                timeout=15,
            )
        self._session = s
        self._username = self._configured_username
        self._password = self._configured_password
        token = self._extract_session_token(s)
        if token:
            self._session_token = token
            print("[MoeMail] 使用手动注册账号登录成功")
            return token
        raise RuntimeError(
            f"MoeMail 登录失败: 已提供用户名密码，但未获取到 session-token (HTTP {login_resp.status_code})"
        )

    def _ensure_session(self) -> str:
        if self._session_token and self._session is not None:
            return self._session_token
        if self._configured_session_token or self._configured_username:
            return self._login_with_existing_account()
        return self._register_and_login()

    def _register_and_login(self) -> str:
        import random, string

        s = self._new_session()
        # 注册
        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        password = "Test" + "".join(random.choices(string.digits, k=8)) + "!"
        self._username = username
        self._password = password
        print(f"[MoeMail] 注册账号: {username} / {password}")
        with suppress_insecure_request_warning():
            r_reg = s.post(f"{self.api}/api/auth/register",
                json={"username": username, "password": password, "turnstileToken": ""},
                timeout=15)
        print(f"[MoeMail] 注册结果: {r_reg.status_code} {r_reg.text[:80]}")
        if r_reg.status_code >= 400:
            try:
                register_error = r_reg.json().get("error") or r_reg.text
            except Exception:
                register_error = r_reg.text
            raise RuntimeError(f"MoeMail 注册失败: {str(register_error).strip() or f'HTTP {r_reg.status_code}'}")
        # 获取 CSRF
        with suppress_insecure_request_warning():
            csrf_r = s.get(f"{self.api}/api/auth/csrf", timeout=10)
        csrf = csrf_r.json().get("csrfToken", "")
        # 登录
        with suppress_insecure_request_warning():
            login_resp = s.post(f"{self.api}/api/auth/callback/credentials",
                headers={"content-type": "application/x-www-form-urlencoded"},
                data=urlencode({
                    "username": username,
                    "password": password,
                    "csrfToken": csrf,
                    "redirect": "false",
                    "callbackUrl": self.api,
                }),
                allow_redirects=True, timeout=15)
        self._session = s
        token = self._extract_session_token(s)
        if token:
            self._session_token = token
            print(f"[MoeMail] 登录成功")
            return token
        print(f"[MoeMail] 登录失败，cookies: {[c.name for c in s.cookies]}")
        raise RuntimeError(
            f"MoeMail 登录失败: 未获取到 session-token (HTTP {login_resp.status_code})"
        )

    # 优先用这些域名（信誉较好，不易被 AWS/Google 等拒绝）
    _PREFERRED_DOMAINS = ("sall.cc", "cnmlgb.de", "zhooo.org", "coolkid.icu")

    def get_email(self) -> MailboxAccount:
        self._session_token = self._configured_session_token or None
        self._session = None
        self._ensure_session()
        import random, string
        name = "".join(random.choices(string.ascii_letters + string.digits, k=8))
        # 获取可用域名列表，优先选信誉好的域名，避免被 AWS 等平台拒绝
        domain = "sall.cc"
        try:
            with suppress_insecure_request_warning():
                cfg_r = self._session.get(f"{self.api}/api/config", timeout=10)
            all_domains = [d.strip() for d in cfg_r.json().get("emailDomains", "sall.cc").split(",") if d.strip()]
            if all_domains:
                # 从可用域名中筛选优先域名，按 _PREFERRED_DOMAINS 顺序选择
                preferred = [d for d in self._PREFERRED_DOMAINS if d in all_domains]
                if preferred:
                    domain = random.choice(preferred)
                else:
                    # 无优先域名可用，从剩余中随机选
                    domain = random.choice(all_domains)
        except Exception:
            pass
        with suppress_insecure_request_warning():
            r = self._session.post(f"{self.api}/api/emails/generate",
                json={"name": name, "domain": domain, "expiryTime": 86400000},
                timeout=15)
        data = r.json()
        self._email = data.get("email", data.get("address", ""))
        email_id = data.get("id", "")
        print(f"[MoeMail] 生成邮箱: {self._email} id={email_id} domain={domain} status={r.status_code}")
        if not email_id:
            print(f"[MoeMail] 生成失败: {data}")
            generate_error = data.get("error") or data.get("message") or r.text
            raise RuntimeError(f"MoeMail 生成邮箱失败: {str(generate_error).strip() or f'HTTP {r.status_code}'}")
        if not self._email:
            raise RuntimeError("MoeMail 生成邮箱失败: 返回结果缺少 email")
        self._email_count = getattr(self, '_email_count', 0) + 1
        return MailboxAccount(
            email=self._email,
            account_id=str(email_id),
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "moemail",
                    "login_identifier": getattr(self, "_username", ""),
                    "display_name": getattr(self, "_username", "") or self._email,
                    "credentials": {
                        "username": getattr(self, "_username", ""),
                        "password": getattr(self, "_password", ""),
                        "session_token": self._session_token,
                    },
                    "metadata": {
                        "api_url": self.api,
                        "email": self._email,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "moemail",
                    "resource_type": "mailbox",
                    "resource_identifier": str(email_id),
                    "handle": self._email,
                    "display_name": self._email,
                    "metadata": {
                        "email": self._email,
                        "api_url": self.api,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            with suppress_insecure_request_warning():
                r = self._session.get(f"{self.api}/api/emails/{account.account_id}", timeout=10)
            return {str(m.get("id", "")) for m in r.json().get("messages", [])}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None,
                      code_pattern: str = None) -> str:
        import re, time
        seen = set(before_ids or [])
        start = time.time()
        pattern = re.compile(code_pattern) if code_pattern else None
        while time.time() - start < timeout:
            try:
                with suppress_insecure_request_warning():
                    r = self._session.get(f"{self.api}/api/emails/{account.account_id}",
                        timeout=10)
                msgs = r.json().get("messages", [])
                for msg in msgs:
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen: continue
                    seen.add(mid)
                    body = str(msg.get("content") or msg.get("text") or msg.get("body") or msg.get("html") or "") + " " + str(msg.get("subject") or "")
                    body = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', body)
                    if pattern:
                        m = pattern.search(body)
                    else:
                        m = re.search(code_pattern or r'(?<!#)(?<!\d)(\d{6})(?!\d)', body)
                    if m: return m.group(1) if m.groups() else m.group(0) if code_pattern else m.group(1)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                with suppress_insecure_request_warning():
                    r = self._session.get(f"{self.api}/api/emails/{account.account_id}",
                        timeout=10)
                msgs = r.json().get("messages", [])
                for msg in msgs:
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    body = (
                        str(msg.get("content") or "") + " " +
                        str(msg.get("text") or "") + " " +
                        str(msg.get("body") or "") + " " +
                        str(msg.get("html") or "") + " " +
                        str(msg.get("subject") or "")
                    )
                    link = _extract_verification_link(body, keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class FreemailMailbox(BaseMailbox):
    """
    Freemail 自建邮箱服务（基于 Cloudflare Worker）
    项目: https://github.com/idinging/freemail
    支持管理员令牌或账号密码两种认证方式
    """

    def __init__(self, api_url: str, admin_token: str = "",
                 username: str = "", password: str = "",
                 proxy: str = None):
        self.api = api_url.rstrip("/")
        self.admin_token = admin_token
        self.username = username
        self.password = password
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._session = None
        self._email = None

    def _get_session(self):
        import requests
        s = requests.Session()
        s.proxies = self.proxy
        if self.admin_token:
            s.headers.update({"Authorization": f"Bearer {self.admin_token}"})
        elif self.username and self.password:
            s.post(f"{self.api}/api/login",
                json={"username": self.username, "password": self.password},
                timeout=15)
        self._session = s
        return s

    def get_email(self) -> MailboxAccount:
        if not self._session:
            self._get_session()
        import requests
        r = self._session.get(f"{self.api}/api/generate", timeout=15)
        data = r.json()
        email = data.get("email", "")
        self._email = email
        print(f"[Freemail] 生成邮箱: {email}")
        provider_account = {
            "provider_type": "mailbox",
            "provider_name": "freemail",
            "login_identifier": self.username or email,
            "display_name": self.username or email,
            "credentials": {},
            "metadata": {
                "api_url": self.api,
                "auth_mode": "admin_token" if self.admin_token else "username_password",
            },
        }
        if self.admin_token:
            provider_account["credentials"]["admin_token"] = self.admin_token
        if self.username:
            provider_account["credentials"]["username"] = self.username
        if self.password:
            provider_account["credentials"]["password"] = self.password
        return MailboxAccount(
            email=email,
            account_id=email,
            extra={
                "provider_account": provider_account,
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "freemail",
                    "resource_type": "mailbox",
                    "resource_identifier": email,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "api_url": self.api,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            r = self._session.get(f"{self.api}/api/emails",
                params={"mailbox": account.email, "limit": 50}, timeout=10)
            return {str(m["id"]) for m in r.json() if "id" in m}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = self._session.get(f"{self.api}/api/emails",
                    params={"mailbox": account.email, "limit": 20}, timeout=10)
                for msg in r.json():
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen: continue
                    seen.add(mid)
                    # 直接用 verification_code 字段
                    code = str(msg.get("verification_code") or "")
                    if code and code != "None":
                        return code
                    # 兜底：从 preview 提取
                    text = str(msg.get("preview", "")) + " " + str(msg.get("subject", ""))
                    m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
                    if m: return m.group(1)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = self._session.get(f"{self.api}/api/emails",
                    params={"mailbox": account.email, "limit": 20}, timeout=10)
                for msg in r.json():
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    text = " ".join(
                        str(msg.get(key, ""))
                        for key in ("preview", "subject", "html", "text", "content", "body")
                    )
                    link = _extract_verification_link(text, keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class TestmailMailbox(BaseMailbox):
    """testmail.app 邮箱服务，地址格式为 {namespace}.{tag}@inbox.testmail.app。"""

    def __init__(
        self,
        api_url: str = "",
        api_key: str = "",
        namespace: str = "",
        tag_prefix: str = "",
        proxy: str = None,
    ):
        self.api = _normalize_api_base_url(api_url, default="", label="Testmail API URL")
        self.api_key = str(api_key or "").strip()
        self.namespace = str(namespace or "").strip()
        self.tag_prefix = str(tag_prefix or "").strip().strip(".")
        self.proxy = {"http": proxy, "https": proxy} if proxy else None

    def _assert_ready(self) -> None:
        if not self.api_key:
            raise RuntimeError("Testmail 未配置 API Key")
        if not self.namespace:
            raise RuntimeError("Testmail 未配置 namespace")

    def _build_tag(self) -> str:
        import random
        import string

        suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        return f"{self.tag_prefix}.{suffix}" if self.tag_prefix else suffix

    def _query_inbox(
        self,
        *,
        tag: str,
        timestamp_from: int | None,
        livequery: bool = False,
        limit: int = 20,
    ) -> list[dict]:
        import requests

        params = {
            "apikey": self.api_key,
            "namespace": self.namespace,
            "tag": tag,
            "limit": limit,
        }
        if timestamp_from is not None:
            params["timestamp_from"] = int(timestamp_from)
        if livequery:
            params["livequery"] = "true"
        response = requests.get(self.api, params=params, proxies=self.proxy, timeout=15)
        payload = response.json()
        if payload.get("result") == "fail":
            raise RuntimeError(f"Testmail 查询失败: {payload.get('message') or response.text}")
        return payload.get("emails", []) or []

    @staticmethod
    def _message_id(mail: dict) -> str:
        return str(
            mail.get("id")
            or mail.get("message_id")
            or f"{mail.get('timestamp', '')}:{mail.get('tag', '')}:{mail.get('subject', '')}"
        )

    @staticmethod
    def _message_text(mail: dict) -> str:
        return " ".join(
            str(mail.get(key, "") or "")
            for key in ("subject", "text", "html")
        )

    def get_email(self) -> MailboxAccount:
        import time

        self._assert_ready()
        tag = self._build_tag()
        email = f"{self.namespace}.{tag}@inbox.testmail.app"
        created_at_ms = int(time.time() * 1000)
        return MailboxAccount(
            email=email,
            account_id=tag,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "testmail",
                    "login_identifier": self.namespace,
                    "display_name": self.namespace,
                    "credentials": {
                        "api_key": self.api_key,
                    },
                    "metadata": {
                        "api_url": self.api,
                        "namespace": self.namespace,
                        "tag_prefix": self.tag_prefix,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "testmail",
                    "resource_type": "mailbox",
                    "resource_identifier": email,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "namespace": self.namespace,
                        "tag": tag,
                        "api_url": self.api,
                        "created_at_ms": created_at_ms,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        tag = str(account.account_id or "")
        if not tag:
            return set()
        started = ((account.extra or {}).get("provider_resource") or {}).get("metadata", {}).get("created_at_ms")
        try:
            mails = self._query_inbox(tag=tag, timestamp_from=started, limit=20)
            return {self._message_id(mail) for mail in mails if self._message_id(mail)}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re
        import time

        tag = str(account.account_id or "")
        if not tag:
            raise RuntimeError("Testmail mailbox 缺少 tag")
        seen = set(before_ids or [])
        started = ((account.extra or {}).get("provider_resource") or {}).get("metadata", {}).get("created_at_ms")
        pattern = re.compile(code_pattern) if code_pattern else None
        start = time.time()
        while time.time() - start < timeout:
            try:
                mails = self._query_inbox(tag=tag, timestamp_from=started, limit=20)
                for mail in sorted(mails, key=lambda item: item.get("timestamp", 0), reverse=True):
                    mid = self._message_id(mail)
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    text = self._message_text(mail)
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    text = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', text)
                    match = pattern.search(text) if pattern else re.search(r'(?<!#)(?<!\d)(\d{6})(?!\d)', text)
                    if match:
                        return match.group(1) if match.groups() else match.group(0)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time

        tag = str(account.account_id or "")
        if not tag:
            raise RuntimeError("Testmail mailbox 缺少 tag")
        seen = set(before_ids or [])
        started = ((account.extra or {}).get("provider_resource") or {}).get("metadata", {}).get("created_at_ms")
        start = time.time()
        while time.time() - start < timeout:
            try:
                mails = self._query_inbox(tag=tag, timestamp_from=started, limit=20)
                for mail in sorted(mails, key=lambda item: item.get("timestamp", 0), reverse=True):
                    mid = self._message_id(mail)
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    link = _extract_verification_link(self._message_text(mail), keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")
