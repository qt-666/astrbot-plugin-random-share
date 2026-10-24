"""随机刷 B 站热门视频，按排期发到指定会话。"""

import asyncio
import base64
import io
import json
import os
import random
import re
import time
from datetime import datetime, timedelta

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.message.message_event_result import MessageChain
from pydantic import Field
from pydantic.dataclasses import dataclass

POPULAR_API = "https://api.bilibili.com/x/web-interface/popular"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
VIDEO_URL = "https://www.bilibili.com/video/{}"
COVER_SIZE = (480, 270)
TITLE_LIMIT = 40
TYPE_KEYWORDS = ("搞笑", "沙雕", "恐怖", "游戏", "实况", "动漫", "科技", "鬼畜", "音乐", "美食")
TRIGGER_VERBS = (
    "刷", "找", "分享", "推荐", "来一", "来点", "来个", "来条",
    "整点", "整个", "搞个", "搞点", "弄个", "弄点",
    "发一", "发个", "给我", "随便", "丢一", "甩一", "看个",
)


def _parse_hhmm(text: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        hour, minute = str(text).strip().split(":")
        return int(hour), int(minute)
    except Exception:
        return default


@dataclass(config=dict(arbitrary_types_allowed=True))
class ShareVideoTool(FunctionTool[AstrAgentContext]):
    """让模型自己判断什么时候该发视频。"""

    name: str = "share_random_bilibili_video"
    description: str = (
        "随机挑一条 B 站热门视频，以封面图+标题+链接的形式发到当前会话。"
        "当用户想让你找视频、刷视频、分享视频、推荐视频时调用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "可选类型关键词，如 恐怖/游戏/搞笑/科技，不填则随机",
                }
            },
            "required": [],
        }
    )
    plugin: object = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        keyword = str(kwargs.get("keyword") or "").strip()
        try:
            umo = context.context.event.unified_msg_origin
        except Exception:
            return "拿不到当前会话，发不了"
        result = await self.plugin.share(keyword, [umo])
        if result == "ok":
            return "已发送一条视频，简短说一句就行，不要重复链接"
        if result == "quota":
            return "这个群今天刷够了，告诉对方明天再来"
        return "没找到合适的视频，跟对方说一声"


@register(
    "astrbot_plugin_random_share",
    "QT-666",
    "随机刷 B 站热门视频并分享（智能排期、白名单、额度、图文）",
    "1.0.0",
)
class RandomSharePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.cfg = dict(config or {})
        self._task = None
        self._state_path = os.path.join(
            "data", "plugin_data", "astrbot_plugin_random_share", "state.json"
        )
        self.state = {
            "window_date": "",
            "times": [],
            "sent": [],
            "paused": False,
            "group_counts": {},
            "last_request": {},
        }
        self._load_state()

    # ---------- 状态 ----------
    def _load_state(self):
        try:
            if os.path.exists(self._state_path):
                self.state.update(json.load(open(self._state_path, encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[random-share] 读取状态失败: {exc}")

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
            json.dump(
                self.state,
                open(self._state_path, "w", encoding="utf-8"),
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[random-share] 保存状态失败: {exc}")

    # ---------- 生命周期 ----------
    async def initialize(self):
        self._task = asyncio.create_task(self._loop())
        try:
            self.context.add_llm_tools(ShareVideoTool(plugin=self))
            logger.info("[random-share] 已注册视频工具")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[random-share] 注册工具失败: {exc}")
        logger.info("[random-share] 定时任务已启动")

    async def terminate(self):
        if self._task:
            self._task.cancel()

    # ---------- 排期 ----------
    def _window(self, now: datetime):
        weekday = now.weekday()  # 0 = 周一
        ws_h, ws_m = _parse_hhmm(self.cfg.get("weekday_start", "16:00"), (16, 0))
        we_h, we_m = _parse_hhmm(self.cfg.get("weekend_start", "12:00"), (12, 0))
        de_h, de_m = _parse_hhmm(self.cfg.get("daytime_end", "22:00"), (22, 0))
        qa_h, qa_m = _parse_hhmm(self.cfg.get("quiet_after", "00:30"), (0, 30))

        if weekday <= 3:
            start = now.replace(hour=ws_h, minute=ws_m, second=0, microsecond=0)
        else:
            start = now.replace(hour=we_h, minute=we_m, second=0, microsecond=0)
        daytime_end = now.replace(hour=de_h, minute=de_m, second=0, microsecond=0)
        end = (now + timedelta(days=1)).replace(
            hour=qa_h, minute=qa_m, second=0, microsecond=0
        )
        return start, end, daytime_end

    @staticmethod
    def _random_between(start: datetime, end: datetime) -> datetime:
        span = max(1, int((end - start).total_seconds()))
        return start + timedelta(seconds=random.randint(0, span))

    def _plan(self, now: datetime):
        start, end, daytime_end = self._window(now)
        low = max(now, start)
        if low >= end:
            self.state["window_date"] = start.strftime("%Y-%m-%d")
            self.state["times"] = []
            self._save_state()
            return

        cap = int(
            (self.cfg.get("weekday_max", 2) if now.weekday() <= 3 else self.cfg.get("weekend_max", 3))
            or 2
        )
        count = random.choices(range(cap + 1), weights=[2] + [3] * cap, k=1)[0]
        day_weight = float(self.cfg.get("day_weight", 68)) / 100.0
        day_low, day_high = low, min(daytime_end, end)

        times: list[datetime] = []
        if count == 1:
            if random.random() < day_weight and day_low < day_high:
                times = [self._random_between(day_low, day_high)]
            else:
                times = [self._random_between(max(day_low, day_high), end)]
        elif count == 2:
            middle = day_low + (end - day_low) / 2
            times = [
                self._random_between(day_low, middle),
                self._random_between(middle, end),
            ]
        elif count >= 3:
            if day_low < day_high:
                first = self._random_between(day_low, day_high)
                second = self._random_between(
                    min(first + timedelta(minutes=90), day_high), day_high
                )
                times = [first, second, self._random_between(max(day_high, day_low), end)]
            else:
                middle = day_low + (end - day_low) / 2
                times = [
                    self._random_between(day_low, middle),
                    self._random_between(middle, end),
                ]

        times.sort()
        filtered: list[datetime] = []
        for moment in times:
            if not filtered or (moment - filtered[-1]) >= timedelta(minutes=150):
                filtered.append(moment)

        self.state["window_date"] = start.strftime("%Y-%m-%d")
        self.state["times"] = [moment.isoformat() for moment in filtered]
        self._save_state()
        logger.info(
            f"[random-share] 新排期 {len(filtered)} 次: "
            f"{[moment.strftime('%m-%d %H:%M') for moment in filtered]}"
        )

    async def _loop(self):
        while True:
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[random-share] 循环异常: {exc}")
            await asyncio.sleep(30)

    async def _tick(self):
        now = datetime.now()
        start, _, _ = self._window(now)
        if (
            self.state.get("window_date") != start.strftime("%Y-%m-%d")
            and not self.state.get("times")
        ):
            self._plan(now)
        if self.state.get("paused"):
            return
        for item in list(self.state.get("times", [])):
            try:
                when = datetime.fromisoformat(item)
            except Exception:
                self.state["times"].remove(item)
                continue
            if when <= now:
                self.state["times"].remove(item)
                self._save_state()
                await self.share()
                break

    # ---------- 数据 ----------
    async def _fetch(self, keyword: str = "") -> list[dict]:
        min_minutes = int(self.cfg.get("min_duration_min", 1) or 1)
        max_minutes = int(self.cfg.get("max_duration_min", 30) or 30)
        blocked = [word for word in (self.cfg.get("blacklist") or []) if word]
        sent = set(self.state.get("sent", []))
        headers = {"User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"}
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=15, headers=headers) as client:
                    data = (await client.get(POPULAR_API, params={"ps": 50, "pn": 1})).json()
                result = []
                for video in (data.get("data") or {}).get("list") or []:
                    bvid = video.get("bvid") or ""
                    title = video.get("title") or ""
                    duration = int(video.get("duration") or 0)
                    if not bvid or bvid in sent:
                        continue
                    if duration < min_minutes * 60 or duration > max_minutes * 60:
                        continue
                    if any(word in title for word in blocked):
                        continue
                    if keyword and keyword not in title:
                        continue
                    result.append(
                        {
                            "bvid": bvid,
                            "title": title,
                            "owner": (video.get("owner") or {}).get("name", ""),
                            "pic": video.get("pic") or "",
                            "duration": duration,
                        }
                    )
                if result:
                    return result
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[random-share] 拉取失败({attempt + 1}/3): {exc}")
                await asyncio.sleep(2)
        return []

    # ---------- 配文与封面 ----------
    async def _caption(self, video: dict) -> str:
        if not self.cfg.get("ai_caption", True):
            return random.choice(["刷到这个，感觉有点意思", "顺手分享一个"])
        prompt = (
            f"你在群里随手分享一个B站视频，标题：《{video['title']}》，UP主：{video['owner']}。"
            "用一句口语化的话点评，别剧透，别客服腔，20字以内。"
        )
        try:
            provider = self.cfg.get("caption_provider") or "deepseek/deepseek-flash"
            resp = await self.context.llm_generate(chat_provider_id=provider, prompt=prompt)
            text = (getattr(resp, "completion_text", None) or str(resp)).strip()
            return text.splitlines()[0][:30] or "刷到这个，感觉有点意思"
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[random-share] 配文失败: {exc}")
            return "刷到这个，感觉有点意思"

    async def _cover(self, url: str) -> bytes | None:
        if not url:
            return None
        try:
            if url.startswith("//"):
                url = "https:" + url
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
                raw = (await client.get(url)).content
            from PIL import Image as PILImage

            image = PILImage.open(io.BytesIO(raw)).convert("RGB").resize(COVER_SIZE)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=80)
            return buffer.getvalue()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[random-share] 封面处理失败: {exc}")
            return None

    # ---------- 发送与额度 ----------
    def _targets(self) -> list[str]:
        targets = []
        for group_id in self.cfg.get("group_whitelist") or []:
            targets.append(f"default:GroupMessage:{group_id}")
        for user_id in self.cfg.get("private_whitelist") or []:
            targets.append(f"default:FriendMessage:{user_id}")
        return targets

    def _quota_left(self, target: str) -> bool:
        if not target.startswith("default:GroupMessage:"):
            return True  # 私聊不限
        limit = int(self.cfg.get("group_daily_limit", 3) or 0)
        if limit <= 0:
            return True
        group_id = target.rsplit(":", 1)[-1]
        today = datetime.now().strftime("%Y-%m-%d")
        info = (self.state.get("group_counts") or {}).get(group_id) or {}
        used = int(info.get("count", 0)) if info.get("date") == today else 0
        return used < limit

    def _bump_quota(self, targets: list[str]) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        counts = self.state.setdefault("group_counts", {})
        for target in targets:
            if not target.startswith("default:GroupMessage:"):
                continue
            group_id = target.rsplit(":", 1)[-1]
            info = counts.get(group_id) or {}
            if info.get("date") != today:
                info = {"date": today, "count": 0}
            info["count"] = int(info.get("count", 0)) + 1
            counts[group_id] = info
        self._save_state()

    async def _send(self, chain, targets: list[str]) -> list[str]:
        ok_targets: list[str] = []
        for target in targets:
            try:
                await self.context.send_message(target, chain)
                ok_targets.append(target)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[random-share] 发送失败 {target}: {type(exc).__name__}")
        return ok_targets

    async def share(self, keyword: str = "", targets: list[str] | None = None) -> str:
        """发一条视频，返回 ok / quota / none。"""
        target_list = [
            target
            for target in (targets if targets is not None else self._targets())
            if self._quota_left(target)
        ]
        if not target_list:
            logger.info("[random-share] 群额度已用完，跳过")
            return "quota"

        videos = await self._fetch(keyword)
        if not videos and keyword:
            videos = await self._fetch("")
        if not videos:
            logger.info("[random-share] 没有合适的视频")
            return "none"

        video = random.choice(videos)
        url = VIDEO_URL.format(video["bvid"])
        title = video["title"][:TITLE_LIMIT]
        caption = await self._caption(video)
        cover = await self._cover(video.get("pic", ""))

        parts = []
        if cover:
            parts.append(Image(file="base64://" + base64.b64encode(cover).decode()))
        minutes = max(1, int(video.get("duration", 0)) // 60)
        parts.append(
            Plain(
                text=f"《{title}》\nUP主：{video.get('owner', '')} · {minutes}分钟\n{url}"
            )
        )
        ok_targets = await self._send(MessageChain(parts), target_list)
        if not ok_targets:
            return "none"

        # 吐槽单独一条
        await self._send(MessageChain([Plain(text=caption)]), ok_targets)

        self._bump_quota(ok_targets)
        self.state.setdefault("sent", []).append(video["bvid"])
        self.state["sent"] = self.state["sent"][-100:]
        self._save_state()
        logger.info(f"[random-share] 已发送 {video['bvid']} 《{title}》")
        return "ok"

    # ---------- 手动触发 ----------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=1)
    async def handle_command(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        if not getattr(event, "is_at_or_wake_command", False):
            return
        if "视频" not in raw:
            return
        if not any(verb in raw for verb in TRIGGER_VERBS):
            return

        arg = raw.split("视频", 1)[1].strip().lower()
        logger.info(f"[random-share] 手动触发 arg={arg!r}")
        event.stop_event()

        if arg in ("off", "关", "停止"):
            self.state["paused"] = True
            self._save_state()
            yield event.plain_result("行，先不刷了")
            return
        if arg in ("on", "开", "启动"):
            self.state["paused"] = False
            self._save_state()
            yield event.plain_result("行，继续刷")
            return

        umo = event.unified_msg_origin
        if "GroupMessage" in umo:
            cooldown = int(self.cfg.get("cooldown_seconds", 60) or 0)
            last = float((self.state.get("last_request") or {}).get(umo, 0))
            if cooldown > 0 and time.time() - last < cooldown:
                yield event.plain_result("刚刷过，等会儿再刷")
                return
            self.state.setdefault("last_request", {})[umo] = time.time()
            self._save_state()

        keyword = next((word for word in TYPE_KEYWORDS if word in raw), "")
        result = await self.share(keyword, [umo])
        if result == "ok":
            yield event.plain_result("刷到一条，发出去了")
        elif result == "quota":
            yield event.plain_result("这个群今天刷够了，明天再来")
        else:
            yield event.plain_result("没刷到合适的，等会儿再试")
