"""DeepSeek 官方 API 的最小客户端（标准库，无第三方依赖）。

密钥来源优先级：
1. 环境变量 DEEPSEEK_API_KEY
2. 文件 <BRAIN_HOME>/secrets/deepseek.key（单行）
密钥不写入日志、不进数据库、不回显。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import db

DEFAULT_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"


class LLMError(RuntimeError):
    pass


def api_key() -> str:
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if key:
        return key
    f = db.home() / "secrets" / "deepseek.key"
    if f.is_file():
        return f.read_text(encoding="utf-8").strip()
    raise LLMError("未找到 DeepSeek API Key：请设置环境变量 DEEPSEEK_API_KEY，"
                   f"或写入 {f}")


def chat_json(messages: list[dict], model: str = DEFAULT_MODEL, base: str = DEFAULT_BASE,
              timeout: int = 90, retries: int = 2, max_tokens: int = 900) -> dict:
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }, ensure_ascii=False).encode("utf-8")
    url = base.rstrip("/") + "/chat/completions"
    last = ""
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key()}",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            text = payload["choices"][0]["message"]["content"]
            return json.loads(text)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            last = f"HTTP {exc.code}: {detail}"
            if exc.code in (400, 401, 403, 404):
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = f"网络错误: {exc}"
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            last = f"响应格式错误: {type(exc).__name__}"
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise LLMError(last or "未知错误")
