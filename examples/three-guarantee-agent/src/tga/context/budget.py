"""token 估算与消息占用统计。

为什么不做精确分词：示例不引入 tokenizer 依赖。估算规则是"CJK 及全角字符 1 token/字、
其余 4 字符 1 token"，一律向上取整——宁可高估撞不上窗口，也不要低估后被 API 截断。
真实接入时把 estimate_tokens 换成模型自带 tokenizer，其余逻辑不用动。
"""

from __future__ import annotations

import math
from typing import Any

# 宽字符起点：CJK 统一表意文字与全角标号都落在这个区间之后
_WIDE_CHAR_FROM = 0x2E80

# 每条消息的固定开销：角色名、分隔符、协议字段名
_MESSAGE_OVERHEAD = 4


def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数。

    :param text: 任意文本
    :return: 估算 token 数；空文本返回 0
    """
    # 1. 逐字符分类：宽字符按 1 token/字，窄字符按 4 字符 1 token
    wide = sum(1 for ch in text if ord(ch) > _WIDE_CHAR_FROM)
    narrow = len(text) - wide

    # 2. 向上取整，避免低估
    return wide + math.ceil(narrow / 4)


def estimate_message(message: dict[str, Any]) -> int:
    """估算一条消息的 token 占用，含角色与协议字段开销。

    :param message: 一条消息，含 role / content，可能带 tool_calls 等字段
    :return: 估算 token 数
    """
    # 1. 固定开销
    total = _MESSAGE_OVERHEAD

    # 2. 正文
    total += estimate_tokens(str(message.get("content") or ""))

    # 3. 协议字段：tool_calls、tool_call_id、name 同样占 token，漏算会低估
    for key in ("tool_calls", "tool_call_id", "name"):
        value = message.get(key)
        if value is not None:
            total += estimate_tokens(str(value))

    return total
