"""FakeLLM：LLMClient 的确定性替身（不联网、不需要 Key）。"""
from __future__ import annotations


class FakeLLM:
    def __init__(self) -> None:
        self.enabled = True
        self.model = "fake-model"
        self.calls: list[dict] = []
        self.canned: dict | None = None   # 设置后 chat_with_tools 原样返回

    def chat_with_tools(self, messages, tools, **kwargs) -> dict:
        self.calls.append({"messages": messages, "tools": tools,
                           "kwargs": kwargs})
        if self.canned is not None:
            return dict(self.canned)
        return {"tool_calls": [], "content": "", "elapsed": 0.001, "usage": {}}
