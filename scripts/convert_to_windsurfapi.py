"""Convert any-auto-register Windsurf accounts to WindsurfAPI-2.0.42 accounts.json.

Two modes:
  1. --mode api   (default): Use stored session_token to obtain Codeium apiKey,
     then write accounts.json directly. Requires network access.
  2. --mode batch: Export email+password pairs for WindsurfAPI's batch-import
     endpoint. No network needed, but WindsurfAPI must be running to import.

Usage:
  # Direct: generate accounts.json with Codeium apiKey
  python scripts/convert_to_windsurfapi.py --mode api --db account_manager.db -o accounts.json

  # Batch: generate import text for WindsurfAPI dashboard
  python scripts/convert_to_windsurfapi.py --mode batch --db account_manager.db -o import.txt

  # Batch: also POST to a running WindsurfAPI instance
  python scripts/convert_to_windsurfapi.py --mode batch --db account_manager.db \
      --api-url http://localhost:8080 --api-password yourpass
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Bootstrap: add project root to sys.path so we can import from any-auto-register
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Account loading from any-auto-register database
# ---------------------------------------------------------------------------

def _load_windsurf_accounts_from_db(db_path: str) -> list[Any]:
    """Load Windsurf accounts from the any-auto-register SQLite database."""
    from sqlmodel import Session, select, create_engine
    from core.db import AccountModel
    from core.account_graph import load_account_graphs, sync_account_graph
    from infrastructure.accounts_repository import _to_record

    db_url = f"sqlite:///{db_path}"
    local_engine = create_engine(db_url, connect_args={"check_same_thread": False})

    with Session(local_engine) as session:
        statement = select(AccountModel).where(AccountModel.platform == "windsurf")
        models = session.exec(statement.order_by(AccountModel.created_at.desc())).all()
        if not models:
            return []

        account_ids = [int(m.id or 0) for m in models if m.id]
        graphs = load_account_graphs(session, account_ids)
        missing = [m for m in models if int(m.id or 0) not in graphs]
        if missing:
            for m in missing:
                sync_account_graph(session, m)
            session.commit()
            graphs = load_account_graphs(session, account_ids)

        records = [_to_record(m, graphs.get(int(m.id or 0), {})) for m in models]

    return records


def _load_windsurf_accounts_from_json(json_path: str) -> list[dict[str, Any]]:
    """Load Windsurf accounts from an exported JSON file."""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array of accounts")
    # Filter for windsurf platform
    return [item for item in data if item.get("platform") == "windsurf" or not item.get("platform")]


def _credential_value(item: AccountRecord, *keys: str) -> str:
    for key in keys:
        for credential in item.credentials or []:
            if credential.get("scope") == "platform" and credential.get("key") == key and credential.get("value"):
                return str(credential["value"])
    return ""


def _extract_windsurf_context(item: AccountRecord) -> dict[str, str]:
    """Extract Windsurf session_token, auth_token, account_id, org_id from an AccountRecord."""
    extra = dict(item.overview or {})
    legacy_extra = dict(extra.get("legacy_extra") or {})
    session_token = (
        _credential_value(item, "session_token", "sessionToken", "legacy_token")
        or _as_text(extra.get("session_token") or extra.get("sessionToken") or legacy_extra.get("session_token"))
    )
    auth_token = (
        _credential_value(item, "auth_token", "authToken")
        or _as_text(extra.get("auth_token") or extra.get("authToken") or legacy_extra.get("auth_token"))
    )
    account_id = (
        _credential_value(item, "account_id", "accountId")
        or _as_text(extra.get("account_id") or extra.get("accountId") or legacy_extra.get("account_id"))
    )
    org_id = (
        _credential_value(item, "org_id", "orgId")
        or _as_text(extra.get("org_id") or extra.get("orgId") or legacy_extra.get("org_id"))
    )
    return {
        "session_token": session_token,
        "auth_token": auth_token,
        "account_id": account_id,
        "org_id": org_id,
    }


def _as_text(value: Any) -> str:
    return str(value or "").strip()


# ---------------------------------------------------------------------------
# Mode 1: API mode — obtain Codeium apiKey and generate accounts.json
# ---------------------------------------------------------------------------

CODEIUM_REGISTER_URL = "https://api.codeium.com/register_user/"
WINDSURF_SEAT_SERVICE = "https://server.self-serve.windsurf.com/exa.seat_management_pb.SeatManagementService"
WINDSURF_ONE_TIME_TOKEN_URL = f"{WINDSURF_SEAT_SERVICE}/GetOneTimeAuthToken"
WINDSURF_POST_AUTH_URL = f"{WINDSURF_SEAT_SERVICE}/WindsurfPostAuth"
WINDSURF_AUTH1_PASSWORD_LOGIN_URL = "https://windsurf.com/_devin-auth/password/login"
FIREBASE_API_KEY = "AIzaSyDsOl-1XpT5err0Tcnx8FFod1H8gVGIycY"
FIREBASE_AUTH_URL = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}"


def _register_with_codeium(token: str, proxy: str | None = None) -> dict[str, str]:
    """Register with Codeium using an auth token (OTT or Firebase idToken)."""
    import requests

    headers = {
        "Content-Type": "application/json",
        "Origin": "https://windsurf.com",
        "Referer": "https://windsurf.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    resp = requests.post(
        CODEIUM_REGISTER_URL,
        json={"firebase_id_token": token},
        headers=headers,
        proxies=proxies,
        timeout=30,
    )
    data = resp.json()
    if not data.get("api_key"):
        raise RuntimeError(f"Codeium registration failed: {json.dumps(data)[:200]}")
    return {
        "apiKey": data["api_key"],
        "name": data.get("name", ""),
        "apiServerUrl": data.get("api_server_url", ""),
    }


def _get_one_time_auth_token(session_token: str, proxy: str | None = None) -> str:
    """Use session_token to get a one-time auth token from Windsurf."""
    import requests

    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        "Origin": "https://windsurf.com",
        "Referer": "https://windsurf.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    resp = requests.post(
        WINDSURF_ONE_TIME_TOKEN_URL,
        json={"authToken": session_token},
        headers=headers,
        proxies=proxies,
        timeout=30,
    )
    data = resp.json()
    ott = data.get("authToken") or data.get("auth_token") or ""
    if not ott:
        raise RuntimeError(f"GetOneTimeAuthToken failed: {json.dumps(data)[:200]}")
    return ott


def _login_auth1(email: str, password: str, proxy: str | None = None) -> dict[str, str]:
    """Login via Auth1 password flow and get session token."""
    import requests

    headers = {
        "Content-Type": "application/json",
        "Origin": "https://windsurf.com",
        "Referer": "https://windsurf.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None

    # Step 1: Auth1 password login
    resp = requests.post(
        WINDSURF_AUTH1_PASSWORD_LOGIN_URL,
        json={"email": email, "password": password},
        headers=headers,
        proxies=proxies,
        timeout=30,
    )
    data = resp.json()
    auth1_token = data.get("token") or ""
    if not auth1_token:
        raise RuntimeError(f"Auth1 login failed: {json.dumps(data)[:200]}")

    # Step 2: WindsurfPostAuth bridge
    bridge_resp = requests.post(
        WINDSURF_POST_AUTH_URL,
        json={"auth1Token": auth1_token, "orgId": ""},
        headers={**headers, "Connect-Protocol-Version": "1"},
        proxies=proxies,
        timeout=30,
    )
    bridge_data = bridge_resp.json()
    session_token = bridge_data.get("sessionToken") or bridge_data.get("session_token") or ""
    if not session_token:
        raise RuntimeError(f"WindsurfPostAuth failed: {json.dumps(bridge_data)[:200]}")

    return {
        "session_token": session_token,
        "auth1_token": auth1_token,
        "account_id": bridge_data.get("accountId") or bridge_data.get("account_id") or "",
    }


def _login_firebase(email: str, password: str, proxy: str | None = None) -> dict[str, str]:
    """Login via Firebase and get idToken."""
    import requests

    headers = {
        "Content-Type": "application/json",
        "Origin": "https://windsurf.com",
        "Referer": "https://windsurf.com/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None

    resp = requests.post(
        FIREBASE_AUTH_URL,
        json={"email": email, "password": password, "returnSecureToken": True},
        headers=headers,
        proxies=proxies,
        timeout=30,
    )
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Firebase login failed: {data['error'].get('message', 'unknown')}")
    id_token = data.get("idToken") or ""
    if not id_token:
        raise RuntimeError(f"Firebase login: no idToken returned")
    return {
        "id_token": id_token,
        "refresh_token": data.get("refreshToken") or "",
    }


def _obtain_api_key(item: AccountRecord, proxy: str | None = None) -> dict[str, Any] | None:
    """Try to obtain a Codeium apiKey for a Windsurf account.

    Strategy:
      1. Use stored session_token → GetOneTimeAuthToken → Codeium register
      2. If session_token expired, try auth_token → WindsurfPostAuth → session → OTT → Codeium
      3. If both fail, try email+password login (Auth1 then Firebase)
    """
    context = _extract_windsurf_context(item)
    email = item.email
    password = item.password

    # Strategy 1: session_token → OTT → Codeium
    if context["session_token"]:
        try:
            ott = _get_one_time_auth_token(context["session_token"], proxy)
            reg = _register_with_codeium(ott, proxy)
            return {
                "apiKey": reg["apiKey"],
                "name": reg["name"] or email,
                "apiServerUrl": reg["apiServerUrl"],
                "method": "token",
                "refreshToken": "",
            }
        except Exception as exc:
            print(f"  [WARN] session_token→OTT failed for {email}: {exc}")

    # Strategy 2: auth_token → PostAuth → session → OTT → Codeium
    if context["auth_token"]:
        try:
            import requests
            headers = {
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
                "Origin": "https://windsurf.com",
                "Referer": "https://windsurf.com/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
            }
            proxies = {"http": proxy, "https": proxy} if proxy else None
            resp = requests.post(
                WINDSURF_POST_AUTH_URL,
                json={"auth1Token": context["auth_token"], "orgId": context.get("org_id", "")},
                headers=headers,
                proxies=proxies,
                timeout=30,
            )
            bridge_data = resp.json()
            new_session = bridge_data.get("sessionToken") or bridge_data.get("session_token") or ""
            if new_session:
                ott = _get_one_time_auth_token(new_session, proxy)
                reg = _register_with_codeium(ott, proxy)
                return {
                    "apiKey": reg["apiKey"],
                    "name": reg["name"] or email,
                    "apiServerUrl": reg["apiServerUrl"],
                    "method": "token",
                    "refreshToken": "",
                }
        except Exception as exc:
            print(f"  [WARN] auth_token→PostAuth failed for {email}: {exc}")

    # Strategy 3: email+password login
    if email and password:
        # Try Auth1 first
        try:
            auth1_result = _login_auth1(email, password, proxy)
            ott = _get_one_time_auth_token(auth1_result["session_token"], proxy)
            reg = _register_with_codeium(ott, proxy)
            return {
                "apiKey": reg["apiKey"],
                "name": reg["name"] or email,
                "apiServerUrl": reg["apiServerUrl"],
                "method": "email",
                "refreshToken": "",
            }
        except Exception as exc:
            print(f"  [WARN] Auth1 login failed for {email}: {exc}")

        # Try Firebase
        try:
            fb_result = _login_firebase(email, password, proxy)
            reg = _register_with_codeium(fb_result["id_token"], proxy)
            return {
                "apiKey": reg["apiKey"],
                "name": reg["name"] or email,
                "apiServerUrl": reg["apiServerUrl"],
                "method": "email",
                "refreshToken": fb_result["refresh_token"],
            }
        except Exception as exc:
            print(f"  [WARN] Firebase login failed for {email}: {exc}")

    return None


def _build_windsurfapi_account(api_key_info: dict[str, Any], email: str) -> dict[str, Any]:
    """Build a single WindsurfAPI account entry."""
    return {
        "id": uuid.uuid4().hex[:8],
        "email": api_key_info.get("name") or email,
        "apiKey": api_key_info["apiKey"],
        "apiServerUrl": api_key_info.get("apiServerUrl") or "",
        "method": api_key_info.get("method") or "api_key",
        "status": "active",
        "addedAt": int(time.time() * 1000),
        "tier": "unknown",
        "tierManual": False,
        "capabilities": {},
        "lastProbed": 0,
        "credits": None,
        "blockedModels": [],
        "refreshToken": api_key_info.get("refreshToken") or "",
        "userStatus": None,
        "userStatusLastFetched": 0,
    }


def run_api_mode(records: list[AccountRecord], output_path: str, proxy: str | None = None) -> None:
    """API mode: obtain Codeium apiKey for each account and write accounts.json."""
    accounts = []
    success = 0
    failed = 0

    for i, item in enumerate(records, 1):
        print(f"[{i}/{len(records)}] Processing {item.email}...")
        try:
            api_key_info = _obtain_api_key(item, proxy)
            if api_key_info:
                account = _build_windsurfapi_account(api_key_info, item.email)
                accounts.append(account)
                success += 1
                print(f"  ✓ Got apiKey: {api_key_info['apiKey'][:20]}...")
            else:
                failed += 1
                print(f"  ✗ Could not obtain apiKey for {item.email}")
        except Exception as exc:
            failed += 1
            print(f"  ✗ Error for {item.email}: {exc}")

        # Throttle to avoid rate limiting
        if i < len(records):
            time.sleep(1)

    content = json.dumps(accounts, indent=2, ensure_ascii=False)
    Path(output_path).write_text(content, encoding="utf-8")
    print(f"\nDone: {success} succeeded, {failed} failed")
    print(f"accounts.json written to: {output_path}")


# ---------------------------------------------------------------------------
# Mode 2: Batch mode — export for WindsurfAPI batch import
# ---------------------------------------------------------------------------

def run_batch_mode(
    records: list[AccountRecord],
    output_path: str | None = None,
    api_url: str | None = None,
    api_password: str | None = None,
    proxy: str | None = None,
) -> None:
    """Batch mode: export email+password pairs for WindsurfAPI import."""
    lines = []
    for item in records:
        email = item.email.strip()
        password = item.password.strip() if item.password else ""
        if email and password:
            lines.append(f"{email} {password}")

    text = "\n".join(lines)

    if output_path:
        Path(output_path).write_text(text, encoding="utf-8")
        print(f"Batch import text written to: {output_path} ({len(lines)} accounts)")

    if api_url:
        _post_batch_import(api_url, api_password, text, proxy)
    elif not output_path:
        # Default: print to stdout
        print(text)


def _post_batch_import(api_url: str, password: str | None, text: str, proxy: str | None = None) -> None:
    """POST accounts to WindsurfAPI's /dashboard/api/batch-import endpoint."""
    import requests

    base = api_url.rstrip("/")
    dashboard_url = f"{base}/dashboard/api/batch-import"
    headers = {"Content-Type": "application/json"}
    if password:
        headers["X-Dashboard-Password"] = password

    proxies = {"http": proxy, "https": proxy} if proxy else None

    print(f"Posting {len(text.splitlines())} accounts to {dashboard_url}...")
    try:
        resp = requests.post(
            dashboard_url,
            json={"text": text, "autoAdd": True},
            headers=headers,
            proxies=proxies,
            timeout=120,
        )
        data = resp.json()
        if data.get("success"):
            print(f"  ✓ Import succeeded: {data.get('successCount', '?')} ok, {data.get('failCount', '?')} failed")
            for r in data.get("results", []):
                if not r.get("success"):
                    print(f"    ✗ {r.get('email', '?')}: {r.get('error', 'unknown')}")
        else:
            print(f"  ✗ Import failed: {data.get('error', resp.text[:200])}")
    except Exception as exc:
        print(f"  ✗ POST failed: {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert any-auto-register Windsurf accounts to WindsurfAPI-2.0.42 format"
    )
    parser.add_argument(
        "--mode", choices=["api", "batch"], default="api",
        help="api: obtain Codeium apiKey and write accounts.json; "
             "batch: export email+password for WindsurfAPI batch import",
    )
    parser.add_argument(
        "--db", default="account_manager.db",
        help="Path to any-auto-register SQLite database (default: account_manager.db)",
    )
    parser.add_argument(
        "--json", default=None,
        help="Path to exported JSON file (alternative to --db)",
    )
    parser.add_argument(
        "-o", "--output", default=None,
        help="Output file path (default: accounts.json for api mode, import.txt for batch mode)",
    )
    parser.add_argument(
        "--proxy", default=None,
        help="HTTP/SOCKS proxy URL for network requests",
    )
    parser.add_argument(
        "--api-url", default=None,
        help="WindsurfAPI base URL for batch import (e.g. http://localhost:8080)",
    )
    parser.add_argument(
        "--api-password", default=None,
        help="WindsurfAPI dashboard password for batch import",
    )

    args = parser.parse_args()

    # Load accounts
    if args.json:
        print(f"Loading accounts from JSON: {args.json}")
        raw_accounts = _load_windsurf_accounts_from_json(args.json)
        # Convert raw dicts to AccountRecord-like objects
        from domain.accounts import AccountRecord
        records = []
        for item in raw_accounts:
            # Build a minimal AccountRecord from exported JSON
            rec = AccountRecord(
                id=int(item.get("id") or 0),
                platform=item.get("platform") or "windsurf",
                email=item.get("email") or "",
                password=item.get("password") or "",
                user_id=item.get("user_id") or item.get("account_id") or "",
                primary_token=item.get("access_token") or item.get("session_token") or "",
                credentials=[
                    {"scope": "platform", "key": k, "value": v}
                    for k, v in {
                        "session_token": item.get("session_token") or item.get("sessionToken"),
                        "auth_token": item.get("auth_token") or item.get("authToken"),
                        "account_id": item.get("account_id") or item.get("accountId"),
                        "org_id": item.get("org_id") or item.get("orgId"),
                    }.items()
                    if v
                ],
                overview={},
            )
            records.append(rec)
    else:
        db_path = args.db
        if not os.path.isabs(db_path):
            db_path = os.path.join(_PROJECT_ROOT, db_path)
        if not os.path.exists(db_path):
            print(f"Error: database not found at {db_path}")
            print("Use --db to specify the correct path, or --json to load from an exported file")
            sys.exit(1)
        print(f"Loading Windsurf accounts from database: {db_path}")
        records = _load_windsurf_accounts_from_db(db_path)

    if not records:
        print("No Windsurf accounts found.")
        sys.exit(0)

    print(f"Found {len(records)} Windsurf account(s)")

    if args.mode == "api":
        output = args.output or os.path.join(_PROJECT_ROOT, "accounts.json")
        run_api_mode(records, output, proxy=args.proxy)
    else:
        output = args.output or os.path.join(_PROJECT_ROOT, "import.txt")
        run_batch_mode(
            records,
            output_path=output,
            api_url=args.api_url,
            api_password=args.api_password,
            proxy=args.proxy,
        )


if __name__ == "__main__":
    main()
