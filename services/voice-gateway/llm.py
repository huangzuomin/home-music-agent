#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 5 — LLM 客户端（OpenAI 兼容协议）。

只依赖标准库（urllib），不引入 openai SDK —— 减少镜像体积与供应链依赖。
设计为可整体替换：换供应商只改 .env 里的 LLM_BASE / LLM_MODEL / LLM_API_KEY。

⚠️ 本模块不打印、不记录 API Key。
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any


def _prefer_ipv4() -> None:
    """把 DNS 解析结果中的 IPv4 记录排到前面（幂等）。

    ⚠️ 2026-09-13 血泪教训（一次耗掉几小时）：
       VM1 上 open.bigmodel.cn 的 DNS 会**同时**返回 IPv6 与 IPv4，而该 IPv6
       路径不通（SYN 黑洞）。Python 的 ``socket.create_connection`` 会**逐个**
       尝试 getaddrinfo 返回的地址 —— 于是每次请求都要先白等约 40 秒的 IPv6
       超时才回退到 IPv4。实测（同机同时段）：

           curl                 →  1.5s   （curl 有 Happy Eyeballs，v4/v6 并发）
           urllib / requests    → 40.4s   （逐个尝试，被 IPv6 拖死）

       这与 UA、HTTP 客户端库、请求体积**全都无关** —— 换 UA、换 requests、
       换 prompt 大小都稳定停在 40s，就是本问题。
       解法：IPv4 优先，IPv6 保留在后作为兜底（不彻底禁用，避免单栈环境失效）。
    """
    if getattr(socket, "_hm_ipv4_first", False):
        return
    orig = socket.getaddrinfo

    def patched(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        infos = orig(host, port, family, type, proto, flags)
        try:
            v4 = [i for i in infos if i[0] == socket.AF_INET]
            v6 = [i for i in infos if i[0] == socket.AF_INET6]
            if v4 and v6:
                return v4 + v6
        except Exception:
            pass
        return infos

    socket.getaddrinfo = patched  # type: ignore[assignment]
    socket._hm_ipv4_first = True  # type: ignore[attr-defined]


_prefer_ipv4()


class LLMError(RuntimeError):
    """LLM 调用失败（网络 / 鉴权 / 解析）。调用方应降级到规则解析。"""


class LLMClient:
    """OpenAI 兼容 Chat Completions 客户端（当前对接 DeepSeek）。

    ``extra_payload`` 会原样并入每次请求体，用于放供应商特有开关
    （当前主要用途：``{"thinking": {"type": "disabled"}}``，见 app.py 的
    LLM_DISABLE_THINKING）。
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-flash",
        timeout: float = 12.0,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.extra_payload = dict(extra_payload or {})

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float = 0.1,
        max_tokens: int = 200,
    ) -> dict[str, Any]:
        """返回 {"tool_calls": [...], "content": str, "elapsed": float, "usage": dict}。

        失败抛 LLMError。绝不把 api_key 写进异常信息。
        """
        if not self.enabled:
            raise LLMError("LLM not configured (missing api key)")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if self.extra_payload:
            payload.update(self.extra_payload)

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=body, method="POST"
        )
        req.add_header("Authorization", "Bearer " + self.api_key)
        req.add_header("Content-Type", "application/json")

        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            raise LLMError("HTTP %s %s" % (e.code, detail)) from None
        except urllib.error.URLError as e:
            raise LLMError("network error: %s" % (e.reason,)) from None
        except Exception as e:  # noqa: BLE001
            raise LLMError("%s: %s" % (type(e).__name__, e)) from None

        elapsed = round(time.time() - t0, 3)

        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("malformed response: %s" % (str(data)[:200],)) from None

        return {
            "tool_calls": msg.get("tool_calls") or [],
            "content": msg.get("content") or "",
            "elapsed": elapsed,
            "usage": data.get("usage") or {},
        }

    def chat(self, messages: list[dict[str, Any]], temperature: float = 0.3,
             max_tokens: int = 120) -> str:
        """纯文本对话（用于 TTS 播报语生成）。失败抛 LLMError。"""
        res = self.chat_with_tools(messages, tools=[], temperature=temperature,
                                   max_tokens=max_tokens)
        return (res["content"] or "").strip()
