"""FakeHA：Home Assistant 的离线替身。

记录 script.turn_on 调用与实体状态；可注入脚本失败。
不连任何真实 HA。
"""
from __future__ import annotations


class FakeHA:
    def __init__(self) -> None:
        self.script_calls: list[tuple[str, dict]] = []   # (script_name, variables)
        self.states: dict[str, str] = {}
        self.fail_scripts: set[str] = set()              # 返回 ok=False 的脚本名
        self.stopped = False                             # 用户是否已停止（T02）

    def call_script(self, name: str, variables: dict | None = None) -> dict:
        if name in self.fail_scripts:
            return {"ok": False, "status": 404, "detail": "script not found: %s" % name}
        self.script_calls.append((name, dict(variables or {})))
        if name == "music_stop":
            self.stopped = True
        return {"ok": True, "status": 200}

    def state(self, entity_id: str) -> str:
        return self.states.get(entity_id, "")

    def scripts_called(self) -> list[str]:
        return [name for name, _ in self.script_calls]
