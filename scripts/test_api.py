"""WindsurfAPI remote testing: health, models, probe, chat, batch test."""
import requests
import json
import sys
import time

URL = "http://216.36.124.237:3003"
API_KEY = "Y2Tz8J4NXTm0l5WhzRd_Yv-nwYCZIgjaopJpGa3YIxM"
DASHBOARD_PASSWORD = "admin"

api_headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
dash_headers = {"Content-Type": "application/json", "X-Dashboard-Password": DASHBOARD_PASSWORD}


# ─── Health & Models ─────────────────────────────────────────────

def cmd_health():
    r = requests.get(f"{URL}/health", timeout=10)
    d = r.json()
    print(f"Status: {d.get('status')}  Accounts: {d.get('accounts')}  Uptime: {d.get('uptime')}s")


def cmd_models():
    r = requests.get(f"{URL}/v1/models", headers=api_headers, timeout=15)
    models = sorted([m["id"] for m in r.json().get("data", [])])
    print(f"Models: {len(models)}")
    for m in models:
        print(f"  {m}")


# ─── Account Probe ───────────────────────────────────────────────

def cmd_list():
    r = requests.get(f"{URL}/dashboard/api/accounts", headers=dash_headers, timeout=30)
    accts = r.json().get("accounts", [])
    tiers = {}
    for a in accts:
        t = a.get("tier", "unknown")
        tiers[t] = tiers.get(t, 0) + 1
    print(f"Total: {len(accts)}  Tiers: {tiers}")
    return accts


def cmd_probe(n=5):
    """Probe first N accounts."""
    accts = cmd_list()
    print(f"\nProbing first {n} accounts...\n")
    for i, a in enumerate(accts[:n], 1):
        aid = a.get("id", "?")
        email = a.get("email", "?")
        try:
            r = requests.post(f"{URL}/dashboard/api/accounts/{aid}/probe",
                              headers=dash_headers, timeout=60)
            d = r.json()
            tier = d.get("tier", "?")
            caps = d.get("capabilities", {})
            models = list(caps.keys()) if isinstance(caps, dict) else []
            glm = [m for m in models if "glm" in m.lower()]
            print(f"  [{i}] {email}: tier={tier} models={len(models)} glm={glm}")
        except Exception as e:
            print(f"  [{i}] {email}: ERROR {e}")
        time.sleep(1)


def cmd_dump(account_id):
    """Dump raw capabilities for one account."""
    r = requests.get(f"{URL}/dashboard/api/accounts", headers=dash_headers, timeout=30)
    a = next((x for x in r.json().get("accounts", []) if x.get("id") == account_id), None)
    if not a:
        print(f"Account {account_id} not found"); return
    caps = a.get("capabilities", {})
    glm = {k: v for k, v in caps.items() if "glm" in k.lower()}
    claude = {k: v for k, v in caps.items() if "claude-3.5" in k}
    print(f"Account: {a.get('email')} tier={a.get('tier')} tierManual={a.get('tierManual')}")
    print(f"userStatusLastFetched={a.get('userStatusLastFetched')}")
    print(f"GLM caps:")
    for k, v in glm.items(): print(f"  {k}: {json.dumps(v, ensure_ascii=False)}")
    print(f"Claude 3.5 caps:")
    for k, v in claude.items(): print(f"  {k}: {json.dumps(v, ensure_ascii=False)}")


# ─── Chat Test ───────────────────────────────────────────────────

def cmd_chat(model="glm-4.7", stream=False):
    print(f"\n--- Chat: model={model} stream={stream} ---")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Say hello in one word."}],
        "max_tokens": 50,
        "stream": stream,
    }
    r = requests.post(f"{URL}/v1/chat/completions", headers=api_headers,
                      json=body, timeout=60, stream=stream)
    if stream:
        print(f"Status: {r.status_code}")
        full = ""
        for line in r.iter_lines():
            if not line: continue
            line = line.decode("utf-8", errors="replace")
            if line.startswith("data: "):
                chunk = line[6:]
                if chunk.strip() == "[DONE]": break
                try:
                    d = json.loads(chunk)
                    c = d.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if c: full += c; print(c, end="", flush=True)
                except: pass
        print(f"\nResponse: {full}")
    else:
        print(f"Status: {r.status_code}")
        try:
            d = r.json()
            if "error" in d:
                print(f"Error: {json.dumps(d['error'], ensure_ascii=False)}")
            else:
                content = d.get("choices", [{}])[0].get("message", {}).get("content", "")
                print(f"Model: {d.get('model','?')}")
                print(f"Response: {content}")
        except Exception as e:
            print(f"Parse error: {e}")
            print(r.text[:500])


# ─── Quick Check ─────────────────────────────────────────────────

def cmd_check():
    """Quick check: test 15 representative models."""
    r = requests.get(f"{URL}/v1/models", headers=api_headers, timeout=15)
    all_models = sorted([m["id"] for m in r.json().get("data", [])])
    print(f"Total models: {len(all_models)}")

    sample = [
        "gemini-2.5-flash", "gemini-3.0-flash-low",
        "glm-4.7", "glm-5.1",
        "claude-3.5-sonnet", "claude-4.5-sonnet",
        "gpt-4o", "gpt-5.1", "gpt-5.2-low",
        "o3-mini", "o4-mini",
        "grok-3", "kimi-k2",
        "minimax-m2.5", "swe-1.5",
    ]
    sample = [m for m in sample if m in all_models]
    print(f"Testing {len(sample)} sample models...\n")

    ok, untrusted, not_entitled = [], [], []
    for model in sample:
        try:
            r = requests.post(f"{URL}/v1/chat/completions", headers=api_headers,
                              json={"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
                              timeout=30)
            d = r.json()
            if r.status_code == 200 and "choices" in d:
                ok.append(model); print(f"  ✅ {model}")
            elif r.status_code == 403:
                not_entitled.append(model); print(f"  ❌ {model}: not_entitled")
            elif r.status_code == 502:
                untrusted.append(model); print(f"  ⚠️ {model}: untrusted workspace")
            else:
                print(f"  ❓ {model}: {r.status_code}")
        except Exception as e:
            print(f"  💥 {model}: {e}")

    print(f"\n===== RESULT =====")
    print(f"✅ Working: {ok if ok else 'NONE'}")
    print(f"⚠️ Untrusted: {untrusted}")
    print(f"❌ Not entitled: {not_entitled}")
    print(f"\n💡 Use 'python scripts/test_api.py batch' to test ALL models.")


# ─── Batch Test All Models ───────────────────────────────────────

def cmd_batch():
    """Test ALL models one by one."""
    r = requests.get(f"{URL}/v1/models", headers=api_headers, timeout=15)
    all_models = sorted([m["id"] for m in r.json().get("data", [])])
    print(f"Total models: {len(all_models)}\n")

    ok, untrusted, not_entitled, other = [], [], [], []
    for i, model in enumerate(all_models, 1):
        try:
            r = requests.post(f"{URL}/v1/chat/completions", headers=api_headers,
                              json={"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
                              timeout=30)
            d = r.json()
            if r.status_code == 200 and "choices" in d:
                ok.append(model); print(f"  ✅ [{i}/{len(all_models)}] {model}")
            elif r.status_code == 403:
                not_entitled.append(model); print(f"  ❌ [{i}/{len(all_models)}] {model}: not_entitled")
            elif r.status_code == 502:
                untrusted.append(model); print(f"  ⚠️ [{i}/{len(all_models)}] {model}: untrusted")
            else:
                other.append(model); print(f"  ❓ [{i}/{len(all_models)}] {model}: {r.status_code}")
        except Exception as e:
            other.append(model); print(f"  💥 [{i}/{len(all_models)}] {model}: {e}")

    print(f"\n===== SUMMARY =====")
    print(f"\n✅ Working ({len(ok)}):")
    for m in ok: print(f"   {m}")
    print(f"\n⚠️ Untrusted ({len(untrusted)}):")
    for m in untrusted: print(f"   {m}")
    print(f"\n❌ Not entitled ({len(not_entitled)}):")
    for m in not_entitled: print(f"   {m}")


# ─── CLI ─────────────────────────────────────────────────────────

USAGE = """Usage: python scripts/test_api.py <command> [args]

Commands:
  health              Service health & account count
  models              List all model names
  list                List accounts & tier distribution
  probe [n]           Probe first N accounts (default 5)
  dump <id>           Dump account capabilities
  check               Quick check 15 sample models (~30s)
  batch               Test ALL 119 models (~5-10min)
  chat [model]        Test single model chat (default glm-4.7)
  stream [model]      Test single model streaming chat
"""

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(USAGE); sys.exit(0)

    cmd = sys.argv[1]
    if cmd == "health":    cmd_health()
    elif cmd == "models":  cmd_models()
    elif cmd == "list":    cmd_list()
    elif cmd == "probe":   cmd_probe(int(sys.argv[2]) if len(sys.argv) > 2 else 5)
    elif cmd == "dump":    cmd_dump(sys.argv[2] if len(sys.argv) > 2 else "")
    elif cmd == "check":   cmd_check()
    elif cmd == "batch":   cmd_batch()
    elif cmd == "chat":    cmd_chat(sys.argv[2] if len(sys.argv) > 2 else "glm-4.7")
    elif cmd == "stream":  cmd_chat(sys.argv[2] if len(sys.argv) > 2 else "glm-4.7", stream=True)
    else: print(USAGE)
