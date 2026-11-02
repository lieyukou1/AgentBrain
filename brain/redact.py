"""写入前脱敏：宁可少记一行证据，也不让密钥进库。

设计取舍：
- 只做**高置信模式**匹配，不做泛化的"看起来像随机串"。误伤一条正常经验
  （例如 `contain: layout`）比漏掉一个罕见密钥更伤信任，所以宁严勿宽。
- 脱敏失败不阻断写入：这个库的职责是留痕，不是当安全网关。命中时只把
  `[REDACTED:kind]` 写进库并把类型回给调用方。
- 锚点 excerpt 是例外：它必须逐字可查，替换后锚点必失效。调用方应改为
  **丢弃 excerpt**（path/commit 锚点仍然可校验）或整条跳过，而不是写一个
  被改过的"原文"。
"""
from __future__ import annotations

import re

REDACTED = "[REDACTED:{kind}]"

# 顺序有意义：私钥块与长 token 先匹配，避免被后面的通用规则切碎。
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private_key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.DOTALL)),
    ("github_token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}=*")),
    # 只擦值、保留键名：`api_key=...` 变成 `api_key=[REDACTED:credential]`，
    # 读者仍知道这里原本有个凭据，但看不到值。
    ("credential", re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|secret[_-]?key|client[_-]?secret"
        r"|password|passwd|token)\b\s*[:=]\s*[\"']?[A-Za-z0-9_\-./+=]{16,}[\"']?")),
]

# 通用规则命中后要保留键名，单独处理
_CREDENTIAL_KEY = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*([:=])")


def clean_text(value: str) -> tuple[str, list[str]]:
    """返回 (清洗后的文本, 命中的密钥类型列表)。空值原样返回。"""
    if not isinstance(value, str) or not value:
        return value, []
    hits: list[str] = []

    def replace(kind: str):
        def _sub(match: re.Match) -> str:
            hits.append(kind)
            if kind == "credential":
                text = match.group(0)
                head = _CREDENTIAL_KEY.match(text)
                if head:
                    return f"{head.group(1)}{head.group(2)}{REDACTED.format(kind=kind)}"
                return REDACTED.format(kind=kind)
            return REDACTED.format(kind=kind)
        return _sub

    for kind, pattern in _PATTERNS:
        value = pattern.sub(replace(kind), value)
    return value, hits


def has_secret(value: str) -> bool:
    return bool(clean_text(value)[1])


def clean_fields(payload: dict, keys: tuple[str, ...] = ()) -> tuple[dict, list[str]]:
    """清洗 payload 中指定字段，返回 (新 dict, 有命中的字段名)。

    不改原 dict：调用方可能还要用原值做去重或展示。未传 keys 时清洗常用文本字段。
    """
    keys = keys or ("claim", "mechanism", "conditions", "counter", "note", "summary")
    out = dict(payload)
    touched: list[str] = []
    for key in keys:
        if key in out:
            cleaned, hits = clean_text(out[key])
            if hits:
                out[key] = cleaned
                touched.append(key)
    return out, touched
