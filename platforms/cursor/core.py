"""Cursor 注册协议核心实现"""
import re, uuid, json, urllib.parse, random, string
from typing import Optional, Callable

AUTH   = "https://authenticator.cursor.sh"
CURSOR = "https://cursor.com"

ACTION_SUBMIT_EMAIL    = "d0b05a2a36fbe69091c2f49016138171d5c1e4cd"
ACTION_SUBMIT_PASSWORD = "fef846a39073c935bea71b63308b177b113269b7"
ACTION_MAGIC_CODE      = "f9e8ae3d58a7cd11cccbcdbf210e6f2a6a2550dd"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/145.0.0.0 Safari/537.36")

TURNSTILE_SITEKEY = "0x4AAAAAAAMNIvC45A4Wjjln"


def _rand_password(n=16):
    chars = string.ascii_letters + string.digits + "!@#$"
    return "".join(random.choices(chars, k=n))


def _boundary():
    return "----WebKitFormBoundary" + "".join(
        random.choices(string.ascii_letters + string.digits, k=16))


def _multipart(fields: dict, boundary: str) -> bytes:
    parts = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        )
    parts.append(f"--{boundary}--\r\n")
    return "".join(parts).encode()


class CursorRegister:
    def __init__(self, proxy: str = None, log_fn: Callable = print):
        from curl_cffi import requests as curl_req
        self.log = log_fn
        self.s = curl_req.Session(impersonate="safari17_0")
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}

    def _base_headers(self, next_action, referer, boundary=None):
        ct = f"multipart/form-data; boundary={boundary}" if boundary else "application/x-www-form-urlencoded"
        return {
            "user-agent": UA,
            "accept": "text/x-component",
            "content-type": ct,
            "origin": AUTH,
            "referer": referer,
            "next-action": next_action,
            "next-router-state-tree": "%5B%22%22%2C%7B%22children%22%3A%5B%22(main)%22%2C%7B%22children%22%3A%5B%22(root)%22%2C%7B%22children%22%3A%5B%22(sign-in)%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%5D%7D%5D%7D%5D%7D%5D%7D%5D",
        }

    def _parse_action_error(self, response) -> str:
        """从 Next.js server action 响应中提取错误信息。"""
        if response.status_code < 400:
            return ""
        text = ""
        try:
            text = (response.text or "")[:500]
        except Exception:
            pass
        for pat in [
            r'"error"\s*:\s*"([^"]+)"',
            r'"message"\s*:\s*"([^"]+)"',
            r'"error_description"\s*:\s*"([^"]+)"',
        ]:
            m = re.search(pat, text)
            if m:
                return m.group(1)
        return text[:200].strip() if text else f"HTTP {response.status_code}"

    def _summarize_step1_failure(self, response) -> str:
        """汇总 Step1 首页失败的诊断信息，帮助判断是风控、代理还是页面改版。"""
        info = [f"HTTP {response.status_code}"]

        try:
            final_url = response.url
        except Exception:
            final_url = ""
        if final_url:
            info.append(f"url={str(final_url)[:180]}")

        header_keys = ["server", "cf-ray", "content-type", "location", "retry-after"]
        header_parts = []
        for key in header_keys:
            value = response.headers.get(key)
            if value:
                header_parts.append(f"{key}={value[:120]}")
        if header_parts:
            info.append("headers=" + ", ".join(header_parts))

        text = ""
        try:
            text = response.text or ""
        except Exception:
            text = ""
        text_lower = text.lower()

        signals = []
        if "cloudflare" in text_lower or "cf-browser-verification" in text_lower:
            signals.append("Cloudflare 页面特征")
        if "just a moment" in text_lower or "verify you are human" in text_lower:
            signals.append("疑似 CF challenge/interstitial")
        if "attention required" in text_lower:
            signals.append("疑似 Cloudflare 封禁页")
        if "access denied" in text_lower or "forbidden" in text_lower:
            signals.append("访问被拒绝")
        if "captcha" in text_lower or "turnstile" in text_lower:
            signals.append("页面含验证码/Turnstile 特征")
        if signals:
            info.append("signals=" + ", ".join(signals))

        body_snippet = re.sub(r"\s+", " ", text).strip()
        if body_snippet:
            info.append(f"body={body_snippet[:220]}")

        return "; ".join(info)

    def step1_get_session(self):
        nonce = str(uuid.uuid4())
        state = {"returnTo": "https://cursor.com/dashboard", "nonce": nonce}
        state_encoded = urllib.parse.quote(urllib.parse.quote(json.dumps(state)))
        url = f"{AUTH}/?state={state_encoded}"
        r = self.s.get(url, headers={"user-agent": UA, "accept": "text/html"}, allow_redirects=True)
        if r.status_code >= 400:
            detail = self._summarize_step1_failure(r)
            raise RuntimeError(f"Step1 获取 session 失败: {detail}")
        state_cookie_name = None
        for cookie in self.s.cookies.jar:
            if 'state-' in cookie.name:
                state_cookie_name = cookie.name
                break
        if not state_cookie_name:
            raise RuntimeError("Step1 获取 session 失败: 未获取到 state cookie")
        return state_encoded, state_cookie_name

    def step2_submit_email(self, email, state_encoded):
        bd = _boundary()
        referer = f"{AUTH}/sign-up?state={state_encoded}"
        body = _multipart({"1_state": state_encoded, "email": email}, bd)
        r = self.s.post(f"{AUTH}/sign-up",
                    headers=self._base_headers(ACTION_SUBMIT_EMAIL, referer, boundary=bd),
                    data=body, allow_redirects=False)
        if r.status_code >= 400:
            err = self._parse_action_error(r)
            raise RuntimeError(f"Step2 提交邮箱失败: {err}")
        return r

    def step3_submit_password(self, password, email, state_encoded, captcha_solver=None):
        captcha_token = ""
        if captcha_solver:
            self.log("获取 Turnstile token...")
            captcha_token = captcha_solver.solve_turnstile(AUTH, TURNSTILE_SITEKEY)
        bd = _boundary()
        referer = f"{AUTH}/sign-up?state={state_encoded}"
        body = _multipart({
            "1_state": state_encoded, "email": email,
            "password": password, "captchaToken": captcha_token,
        }, bd)
        r = self.s.post(f"{AUTH}/sign-up",
                    headers=self._base_headers(ACTION_SUBMIT_PASSWORD, referer, boundary=bd),
                    data=body, allow_redirects=False)
        if r.status_code >= 400:
            err = self._parse_action_error(r)
            raise RuntimeError(f"Step3 提交密码失败: {err}")
        return r

    def step4_submit_otp(self, otp, email, state_encoded):
        bd = _boundary()
        referer = f"{AUTH}/sign-up?state={state_encoded}"
        body = _multipart({"1_state": state_encoded, "email": email, "otp": otp}, bd)
        r = self.s.post(f"{AUTH}/sign-up",
                        headers=self._base_headers(ACTION_MAGIC_CODE, referer, boundary=bd),
                        data=body, allow_redirects=False)
        if r.status_code >= 400:
            err = self._parse_action_error(r)
            raise RuntimeError(f"Step4 提交验证码失败: {err}")
        loc = r.headers.get("location", "")
        m = re.search(r'code=([\w-]+)', loc)
        if not m:
            raise RuntimeError(f"Step4 提交验证码失败: 响应中未包含 auth code (location={loc[:200]})")
        return m.group(1)

    def step5_get_token(self, auth_code, state_encoded):
        url = f"{CURSOR}/api/auth/callback?code={auth_code}&state={state_encoded}"
        self.s.get(url, headers={"user-agent": UA, "accept": "text/html"}, allow_redirects=False)
        for cookie in self.s.cookies.jar:
            if cookie.name == "WorkosCursorSessionToken":
                return urllib.parse.unquote(cookie.value)
        self.s.get(url, headers={"user-agent": UA}, allow_redirects=True)
        for cookie in self.s.cookies.jar:
            if cookie.name == "WorkosCursorSessionToken":
                return urllib.parse.unquote(cookie.value)
        raise RuntimeError("Step5 获取 Token 失败: 未获取到 WorkosCursorSessionToken")

# CursorBrowserRegister 统一从 browser_register.py 导入，避免代码重复
from platforms.cursor.browser_register import CursorBrowserRegister  # noqa: F401
