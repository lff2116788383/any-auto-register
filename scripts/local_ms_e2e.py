#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "interrupted"}


def _join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _request_json(url: str, method: str = "GET", payload: dict[str, Any] | None = None, timeout: int = 15) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def _parse_platform_extra(raw: str) -> dict[str, dict[str, Any]]:
    """格式: chatgpt={...};cursor={...} 或直接传 JSON 对象字符串。"""
    text = (raw or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return {str(k): dict(v or {}) for k, v in parsed.items()}
        raise ValueError("--platform-extra JSON 必须是对象")

    result: dict[str, dict[str, Any]] = {}
    for segment in text.split(";"):
        seg = segment.strip()
        if not seg:
            continue
        if "=" not in seg:
            raise ValueError(f"无效平台扩展配置片段: {seg}")
        name, raw_json = seg.split("=", 1)
        name = name.strip()
        obj = json.loads(raw_json.strip())
        if not isinstance(obj, dict):
            raise ValueError(f"平台扩展配置必须是 JSON 对象: {name}")
        result[name] = obj
    return result


def _build_payload(platform: str, args, extra_overrides: dict[str, Any]) -> dict[str, Any]:
    extra = {
        "mail_provider": args.mail_provider,
        "identity_provider": "mailbox",
        "local_ms_mode": args.local_ms_mode,
        "local_ms_pool": args.local_ms_pool,
        "local_ms_pool_fission": str(args.local_ms_pool_fission).lower(),
        "local_ms_fetch_mode": args.local_ms_fetch_mode,
        "local_ms_alias_strategy_map": args.local_ms_alias_strategy_map,
        "local_ms_lease_ttl": str(args.local_ms_lease_ttl),
        "local_ms_poll_interval_sec": str(args.local_ms_poll_interval_sec),
        "local_ms_max_wait_sec": str(args.local_ms_max_wait_sec),
    }
    extra.update(extra_overrides)
    return {
        "platform": platform,
        "count": args.count,
        "concurrency": args.concurrency,
        "executor_type": args.executor_type,
        "captcha_solver": args.captcha_solver,
        "extra": extra,
    }


def _wait_task(base_api: str, task_id: str, timeout_sec: int, poll_sec: float) -> dict[str, Any]:
    started = time.time()
    while True:
        task = _request_json(_join_url(base_api, f"tasks/{task_id}"))
        status = str(task.get("status") or "")
        if status in TERMINAL_STATUSES:
            return task
        if time.time() - started > timeout_sec:
            raise TimeoutError(f"任务超时: {task_id}")
        time.sleep(max(poll_sec, 0.2))


def main() -> int:
    parser = argparse.ArgumentParser(description="local_microsoft 三平台 E2E 验收脚本")
    parser.add_argument("--base-api", default="http://127.0.0.1:8000/api", help="API 根地址")
    parser.add_argument("--platforms", default="chatgpt,cursor,grok", help="逗号分隔的平台列表")
    parser.add_argument("--count", type=int, default=1, help="每个平台创建账号数量")
    parser.add_argument("--concurrency", type=int, default=1, help="每个平台任务并发")
    parser.add_argument("--executor-type", default="protocol", help="执行器类型")
    parser.add_argument("--captcha-solver", default="auto", help="验证码模式")

    parser.add_argument("--mail-provider", default="local_microsoft", help="邮箱 provider key")
    parser.add_argument("--local-ms-mode", default="pool", help="local_microsoft 模式")
    parser.add_argument("--local-ms-pool", default="default", help="邮箱池名称")
    parser.add_argument("--local-ms-pool-fission", action="store_true", help="池模式启用 alias")
    parser.add_argument("--local-ms-fetch-mode", default="auto", choices=["auto", "graph", "imap"], help="收件模式")
    parser.add_argument("--local-ms-alias-strategy-map", default="chatgpt:plus,cursor:raw_only,grok:plus", help="平台别名策略")
    parser.add_argument("--local-ms-lease-ttl", type=int, default=300, help="租约秒数")
    parser.add_argument("--local-ms-poll-interval-sec", type=int, default=5, help="邮箱轮询间隔")
    parser.add_argument("--local-ms-max-wait-sec", type=int, default=180, help="邮箱最大等待")

    parser.add_argument("--task-timeout-sec", type=int, default=900, help="单任务最大等待秒数")
    parser.add_argument("--task-poll-sec", type=float, default=2.0, help="任务轮询间隔秒")
    parser.add_argument("--platform-extra", default="", help="平台额外 extra，支持 JSON 或 chatgpt={...};cursor={...}")
    args = parser.parse_args()

    try:
        platform_extra = _parse_platform_extra(args.platform_extra)
    except Exception as exc:
        print(f"[FATAL] 解析 --platform-extra 失败: {exc}")
        return 2

    platforms = [p.strip() for p in str(args.platforms or "").split(",") if p.strip()]
    if not platforms:
        print("[FATAL] --platforms 不能为空")
        return 2

    results: list[dict[str, Any]] = []
    failed = False

    for platform in platforms:
        payload = _build_payload(platform, args, platform_extra.get(platform, {}))
        print(f"\n==> 提交任务: {platform}")
        try:
            created = _request_json(_join_url(args.base_api, "tasks/register"), method="POST", payload=payload, timeout=20)
            task_id = str(created.get("id") or created.get("task_id") or "")
            if not task_id:
                raise RuntimeError(f"创建任务返回异常: {created}")
            print(f"[OK] 已创建任务 {task_id}，等待完成...")
            task = _wait_task(args.base_api, task_id, timeout_sec=args.task_timeout_sec, poll_sec=args.task_poll_sec)
            status = str(task.get("status") or "")
            error = str(task.get("error") or "")
            success = int(task.get("success") or 0)
            err_count = int(task.get("error_count") or 0)
            print(f"[{status.upper()}] {platform} success={success} errors={err_count} error='{error}'")
            if status != "succeeded":
                failed = True
            results.append({
                "platform": platform,
                "task_id": task_id,
                "status": status,
                "success": success,
                "error_count": err_count,
                "error": error,
                "result": task.get("result"),
            })
        except urllib.error.HTTPError as exc:
            failed = True
            body = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
            print(f"[HTTP {exc.code}] {platform} -> {body}")
            results.append({"platform": platform, "status": "http_error", "error": body or str(exc)})
        except Exception as exc:
            failed = True
            print(f"[FAIL] {platform} -> {exc}")
            results.append({"platform": platform, "status": "exception", "error": str(exc)})

    print("\n===== 验收汇总 =====")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
