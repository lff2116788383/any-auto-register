"""Use stored session_token to get Codeium apiKey, then push to remote WindsurfAPI."""
import json
import sys
import time
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import requests
from sqlmodel import Session, select, create_engine
from core.db import AccountModel
from core.account_graph import load_account_graphs, sync_account_graph
from infrastructure.accounts_repository import _to_record

# --- Config ---
DB_PATH = str(PROJECT_ROOT / "account_manager.db")
REMOTE_URL = "http://216.36.124.237:3003"
REMOTE_PASSWORD = "admin"
PROXY = None  # set if needed, e.g. "http://127.0.0.1:7890"

# --- Windsurf API endpoints ---
WINDSURF_OTT_URL = "https://server.self-serve.windsurf.com/exa.seat_management_pb.SeatManagementService/GetOneTimeAuthToken"
WINDSURF_POST_AUTH_URL = "https://server.self-serve.windsurf.com/exa.seat_management_pb.SeatManagementService/WindsurfPostAuth"
WINDSURF_AUTH1_LOGIN_URL = "https://windsurf.com/_devin-auth/password/login"
CODEIUM_REGISTER_URL = "https://api.codeium.com/register_user/"

REQUEST_HEADERS = {
    "Content-Type": "application/json",
    "Connect-Protocol-Version": "1",
    "Origin": "https://windsurf.com",
    "Referer": "https://windsurf.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
}


def _cred_value(item, *keys):
    for key in keys:
        for c in item.credentials or []:
            if c.get("scope") == "platform" and c.get("key") == key and c.get("value"):
                return str(c["value"])
    return ""


def _as_text(v):
    return str(v or "").strip()


def get_ott(session_token):
    """session_token → one-time auth token."""
    r = requests.post(WINDSURF_OTT_URL, json={"authToken": session_token},
                       headers=REQUEST_HEADERS, proxies=_proxies(), timeout=30)
    d = r.json()
    ott = d.get("authToken") or d.get("auth_token") or ""
    if not ott:
        raise RuntimeError(f"OTT failed: {json.dumps(d)[:200]}")
    return ott


def post_auth(auth1_token, org_id=""):
    """auth1_token → session_token via WindsurfPostAuth."""
    r = requests.post(WINDSURF_POST_AUTH_URL,
                       json={"auth1Token": auth1_token, "orgId": org_id},
                       headers=REQUEST_HEADERS, proxies=_proxies(), timeout=30)
    d = r.json()
    st = d.get("sessionToken") or d.get("session_token") or ""
    if not st:
        raise RuntimeError(f"PostAuth failed: {json.dumps(d)[:200]}")
    return {"session_token": st, "account_id": d.get("accountId") or ""}


def auth1_login(email, password):
    """email+password → auth1_token → session_token."""
    r = requests.post(WINDSURF_AUTH1_LOGIN_URL,
                       json={"email": email, "password": password},
                       headers={k: v for k, v in REQUEST_HEADERS.items() if k != "Connect-Protocol-Version"},
                       proxies=_proxies(), timeout=30)
    d = r.json()
    t = d.get("token") or ""
    if not t:
        raise RuntimeError(f"Auth1 login failed: {json.dumps(d)[:200]}")
    return post_auth(t)


def register_codeium(token):
    """OTT or idToken → Codeium apiKey."""
    r = requests.post(CODEIUM_REGISTER_URL,
                       json={"firebase_id_token": token},
                       headers={k: v for k, v in REQUEST_HEADERS.items() if k != "Connect-Protocol-Version"},
                       proxies=_proxies(), timeout=30)
    d = r.json()
    if not d.get("api_key"):
        raise RuntimeError(f"Codeium reg failed: {json.dumps(d)[:200]}")
    return {"apiKey": d["api_key"], "name": d.get("name", ""), "apiServerUrl": d.get("api_server_url", "")}


def _proxies():
    return {"http": PROXY, "https": PROXY} if PROXY else None


def push_account_to_remote(api_key, name, method="token", refresh_token=""):
    """Push a single account to remote WindsurfAPI via /auth/login."""
    headers = {"Content-Type": "application/json", "X-Dashboard-Password": REMOTE_PASSWORD}
    r = requests.post(f"{REMOTE_URL}/dashboard/api/accounts",
                       json={"api_key": api_key, "label": name},
                       headers=headers, proxies=_proxies(), timeout=30)
    d = r.json()
    if d.get("success"):
        acct = d.get("account", {})
        print(f"    → Pushed to remote: id={acct.get('id')} status={acct.get('status')}")
    else:
        print(f"    → Push FAILED: {d.get('error', '?')}")


def process_account(item, idx, total):
    email = item.email
    session_token = _cred_value(item, "session_token", "sessionToken", "legacy_token")
    auth_token = _cred_value(item, "auth_token", "authToken")
    org_id = _cred_value(item, "org_id", "orgId")
    password = item.password

    print(f"[{idx}/{total}] {email}")
    print(f"    session_token={'yes' if session_token else 'no'} auth_token={'yes' if auth_token else 'no'} password={'yes' if password else 'no'}")

    # Strategy 1: session_token → OTT → Codeium
    if session_token:
        try:
            ott = get_ott(session_token)
            reg = register_codeium(ott)
            print(f"    ✓ Via session_token: apiKey={reg['apiKey'][:20]}...")
            push_account_to_remote(reg["apiKey"], reg["name"] or email)
            return True
        except Exception as e:
            print(f"    ✗ session_token path failed: {e}")

    # Strategy 2: auth_token → PostAuth → session → OTT → Codeium
    if auth_token:
        try:
            pa = post_auth(auth_token, org_id)
            ott = get_ott(pa["session_token"])
            reg = register_codeium(ott)
            print(f"    ✓ Via auth_token: apiKey={reg['apiKey'][:20]}...")
            push_account_to_remote(reg["apiKey"], reg["name"] or email)
            return True
        except Exception as e:
            print(f"    ✗ auth_token path failed: {e}")

    # Strategy 3: email+password → Auth1 login → OTT → Codeium
    if email and password:
        try:
            pa = auth1_login(email, password)
            ott = get_ott(pa["session_token"])
            reg = register_codeium(ott)
            print(f"    ✓ Via Auth1 login: apiKey={reg['apiKey'][:20]}...")
            push_account_to_remote(reg["apiKey"], reg["name"] or email, method="email")
            return True
        except Exception as e:
            print(f"    ✗ Auth1 login failed: {e}")

    print(f"    ✗ ALL STRATEGIES FAILED for {email}")
    return False


def main():
    engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
    with Session(engine) as session:
        stmt = select(AccountModel).where(AccountModel.platform == "windsurf")
        models = session.exec(stmt.order_by(AccountModel.created_at.desc())).all()
        if not models:
            print("No windsurf accounts found"); return

        ids = [int(m.id or 0) for m in models if m.id]
        graphs = load_account_graphs(session, ids)
        missing = [m for m in models if int(m.id or 0) not in graphs]
        if missing:
            for m in missing:
                sync_account_graph(session, m)
            session.commit()
            graphs = load_account_graphs(session, ids)
        records = [_to_record(m, graphs.get(int(m.id or 0), {})) for m in models]

    print(f"Found {len(records)} Windsurf accounts\n")
    ok = 0
    fail = 0
    for i, item in enumerate(records, 1):
        if process_account(item, i, len(records)):
            ok += 1
        else:
            fail += 1
        if i < len(records):
            time.sleep(1)

    print(f"\n===== DONE =====")
    print(f"Success: {ok}, Failed: {fail}, Total: {len(records)}")


if __name__ == "__main__":
    main()
