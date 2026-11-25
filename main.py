"""轻量戳一戳插件。

只做一件事：别人在 QQ 里戳 Bot 时，按概率回一句、按概率反戳。
没有戳事件时它一行都不跑：不注入 prompt、不拦普通消息、无后台任务。
"""

from __future__ import annotations

import os
import random
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

DEBUG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "poke_debug.log")


def _dbg(text: str) -> None:
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {text}\n")
    except Exception:  # noqa: BLE001
        pass


class PokeLight(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._cooldown: dict[tuple[str, str], float] = {}

    # ---------------- 配置 ----------------
    def _cfg(self, key: str, default=None):
        try:
            value = self.config.get(key, default)
        except Exception:  # noqa: BLE001
            value = default
        return default if value is None else value

    def _groups(self) -> set[str]:
        raw = str(self._cfg("groups", "") or "")
        for sep in ("，", "；", ";", " ", "\t", "\n"):
            raw = raw.replace(sep, ",")
        return {item.strip() for item in raw.split(",") if item.strip()}

    def _prob(self, key: str, default: float) -> float:
        try:
            return max(0.0, min(1.0, float(self._cfg(key, default))))
        except Exception:  # noqa: BLE001
            return default

    @staticmethod
    def _parse_replies(raw) -> list[str]:
        if isinstance(raw, str):
            items = [x.strip() for x in raw.replace("，", ",").split(",")]
        elif isinstance(raw, (list, tuple, set)):
            items = [str(x).strip() for x in raw]
        else:
            items = []
        return [x for x in items if x]

    def _replies(self) -> list[str]:
        """普通池：群里任何人（含陌生人）戳出来的那句话。"""
        return self._parse_replies(self._cfg("replies", None)) or ["在呢在呢"]

    def _owner_replies(self) -> list[str]:
        """老爹专属池：只有 owner_ids 里的人戳才用。"""
        return self._parse_replies(self._cfg("replies_owner", None))

    def _owner_ids(self) -> set[str]:
        raw = self._cfg("owner_ids", "")
        if isinstance(raw, (list, tuple, set)):
            items = [str(x) for x in raw]
        else:
            text = str(raw or "")
            for sep in ("，", "；", ";", " ", "\t", "\n"):
                text = text.replace(sep, ",")
            items = text.split(",")
        return {x.strip() for x in items if x.strip()}

    def _pick_reply(self, user_id: str) -> str:
        """按戳的人分流池子：老爹走专属池，其他人只走普通池。

        以前只有一个池子，里面混着“老爹戳我”“主人在戳我”，
        群里别人一戳也会吃到那句，所以按 user_id 分开。
        """
        if user_id and user_id in self._owner_ids():
            pool = self._owner_replies()
            if pool:
                return random.choice(pool)
        return random.choice(self._replies())

    # ---------------- 事件识别 ----------------
    @staticmethod
    def _raw_message(event: AstrMessageEvent):
        obj = getattr(event, "message_obj", None)
        raw = getattr(obj, "raw_message", None)
        if raw is None:
            raw = getattr(event, "raw_message", None)
        return raw

    @classmethod
    def _is_poke(cls, raw) -> bool:
        getter = getattr(raw, "get", None)
        if not callable(getter):
            return False
        try:
            if str(getter("post_type") or "").lower() != "notice":
                return False
            return str(getter("sub_type") or "").lower() == "poke"
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _self_id(event: AstrMessageEvent, raw) -> str:
        getter = getattr(raw, "get", None)
        if callable(getter):
            try:
                value = str(getter("self_id") or "")
                if value:
                    return value
            except Exception:  # noqa: BLE001
                pass
        for attr in ("self_id",):
            value = getattr(getattr(event, "message_obj", None), attr, None)
            if value:
                return str(value)
        fn = getattr(event, "get_self_id", None)
        if callable(fn):
            try:
                return str(fn() or "")
            except Exception:  # noqa: BLE001
                return ""
        return ""

    # ---------------- 反戳 ----------------
    async def _poke_back(self, event: AstrMessageEvent, group_id: str, user_id: str) -> bool:
        times = 1
        try:
            times = max(1, min(5, int(self._cfg("poke_back_times", 1))))
        except Exception:  # noqa: BLE001
            times = 1
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None)
        ok = False
        for _ in range(times):
            done = False
            if api is not None:
                payload = {"user_id": int(user_id)}
                if group_id:
                    payload["group_id"] = int(group_id)
                for action in ("send_poke", "friend_poke"):
                    if not group_id and action == "friend_poke":
                        continue
                    try:
                        await api.call_action(action, **payload)
                        done = True
                        break
                    except Exception:  # noqa: BLE001
                        continue
                if not done and group_id:
                    try:
                        await api.call_action("group_poke", group_id=int(group_id), user_id=int(user_id))
                        done = True
                    except Exception:  # noqa: BLE001
                        done = False
            ok = ok or done
        return ok

    # ---------------- 主处理 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=9999)
    async def on_poke(self, event: AstrMessageEvent, *args, **kwargs):
        try:
            raw = self._raw_message(event)
            getter = getattr(raw, "get", None)
            post_type = str(getter("post_type") or "") if callable(getter) else ""
            if callable(getter) and post_type == "notice":
                _dbg(
                    "notice 到达: sub_type={} notice_type={} target_id={} self_id={} user_id={} group_id={} msg_str={!r}".format(
                        getter("sub_type"),
                        getter("notice_type"),
                        getter("target_id"),
                        getter("self_id"),
                        getter("user_id"),
                        getter("group_id"),
                        getattr(event, "message_str", "")[:20],
                    )
                )
            if not bool(self._cfg("enable", True)):
                return
            if not self._is_poke(raw):
                return
            target = str(getter("target_id") or "") if callable(getter) else ""
            self_id = self._self_id(event, raw)
            if not target or (self_id and target != self_id):
                _dbg(f"poke 但不是戳我: target={target} self={self_id}")
                return

            try:
                group_id = str(event.get_group_id() or "")
            except Exception:  # noqa: BLE001
                group_id = ""
            try:
                user_id = str(event.get_sender_id() or "")
            except Exception:  # noqa: BLE001
                user_id = ""

            whitelist = self._groups()
            if group_id and whitelist and group_id not in whitelist:
                _dbg(f"poke 群不在白名单: group={group_id} white={sorted(whitelist)}")
                return

            # 冷却
            now = time.time()
            try:
                cooldown = max(0, int(self._cfg("cooldown_seconds", 60)))
            except Exception:  # noqa: BLE001
                cooldown = 60
            key = (group_id, user_id)
            last = self._cooldown.get(key, 0.0)
            if cooldown and now - last < cooldown:
                _dbg(f"冷却中 group={group_id or '私聊'} user={user_id} 剩{cooldown - (now - last):.0f}s")
                if bool(self._cfg("swallow_silent", True)):
                    event.stop_event()
                return
            self._cooldown[key] = now
            if len(self._cooldown) > 500:
                for stale in [k for k, t in self._cooldown.items() if now - t > 3600]:
                    self._cooldown.pop(stale, None)

            _dbg(f"进入处理 group={group_id or '私聊'} user={user_id}")
            replied = False
            if random.random() < self._prob("reply_probability", 0.7):
                try:
                    await event.send(event.plain_result(self._pick_reply(user_id)))
                    replied = True
                    pool = "owner" if user_id and user_id in self._owner_ids() else "normal"
                    _dbg(f"回话成功 group={group_id} user={user_id} pool={pool}")
                except Exception as exc:  # noqa: BLE001
                    _dbg(f"回话失败 group={group_id} user={user_id} err={exc}")
                    logger.debug("[poke_light] 回话失败: %s", exc)

            if user_id and random.random() < self._prob("poke_back_probability", 0.2):
                try:
                    ok = await self._poke_back(event, group_id, user_id)
                    _dbg(f"反戳 group={group_id} user={user_id} ok={ok}")
                except Exception as exc:  # noqa: BLE001
                    _dbg(f"反戳失败 group={group_id} user={user_id} err={exc}")
                    logger.debug("[poke_light] 反戳失败: %s", exc)

            if not replied or bool(self._cfg("swallow_silent", True)):
                event.stop_event()
        except Exception as exc:  # noqa: BLE001
            _dbg(f"处理异常: {exc!r}")
            logger.debug("[poke_light] 处理异常: %s", exc)



