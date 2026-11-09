"""配置加载：JSON 文件 + 环境变量覆盖。

用 JSON 而不是 YAML 是刻意的：这样整个工程零第三方依赖，
在任何 Python 3.8+ 环境里 clone 下来就能跑，不用先装东西。
"""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("codewatch.config")

DEFAULTS = {
    # 不玩的游戏，命中就丢掉（按游戏名精确匹配）。
    # 识别不出游戏的内容不受影响 —— 其他厂商的活动照常推送。
    "exclude_games": ["崩坏3", "未定事件簿", "崩坏学园2"],
    # 推送时段：只在下面几个时间点附近发推送，其余时间只扫描不发。
    # 目的：避免凌晨推送（夜里发现的内容不写状态，攒到下一个时段一起发）。
    # 时区按 tz_offset 换算，8 = 北京时间，不受 runner 的 UTC 影响。
    "push_schedule": {
        "enable": True,
        "tz_offset": 8,
        "slots": ["09:00", "20:00"],
        # 时段容差：GitHub Actions 的 cron 会延迟 5-15 分钟，
        # 而且同一时段有 :07 / :37 两轮可以重试，60 分钟足够兜住。
        "window_minutes": 60,
    },
    "miyoushe": {
        "enable": True,
        "keywords": ["兑换码", "前瞻直播兑换码", "礼包码", "兑换中心"],
        "page_size": 10,
        "fresh_hours": 168,
    },
    "hoyo_codes": {
        "enable": True,
        "games": ["genshin", "hkrpg", "nap"],
    },
    "taptap": {
        "enable": True,
        # 活动中心页数。实测单页偶发 HTTP 405，扫满 24 页既慢又容易丢后半段；
        # 而米哈游自己的签到/礼包活动并不在活动中心，只挂在各游戏活动页，
        # 所以这里只取前 8 页（约 160 条），噪音和请求都少三分之二。
        "max_pages": 8,
        # 命中这些词的普通活动（非签到类）也会推送
        "keywords": ["签到", "礼包", "兑换码", "CDK", "激活码", "兑换"],
        # 签到类活动额外调详情接口，拿每日奖励和剩余库存
        "with_detail": True,
        # 累计签到型活动在活动期间每天提醒一次（按天去重）。
        # 关掉就退回「只在发现时公告一次」的行为。
        "daily_reminder": True,
        # 可选。填了 TapTap 登录 Cookie，才能读到「今天签没签、累计几天」。
        # 不填也能用，只是提醒里不含进度（本工程不强制你交凭证）。
        "cookie": "",
        # 米哈游各游戏在 TapTap 的 app_id。**这个不能省**：
        # 游戏的签到/礼包活动只挂在 /app/{id}/game-event，
        # 活动中心 /events 里一条都没有（实测绝区零的「签到领好礼」就不在）。
        # 想加别的厂商的游戏，照格式填「游戏名: app_id」即可
        # （app_id 从 https://www.taptap.cn/app/<数字> 的地址里取）。
        # 只扫在玩的这三款；不玩的游戏连请求都不发（exclude_games 兜底剩下的来源）。
        "apps": {
            "原神": 168332,
            "崩坏：星穹铁道": 224267,
            "绝区零": 234493,
        },
        # 只推这几类活动：check_in 签到、gift 礼包（点击即领）。
        # 也可加 activity(创作征集) / lottery(抽奖) / in_app_event(游戏内活动)，
        # 但那三类噪音偏大。
        "event_types": ["check_in", "gift"],
    },
    "weibo": {
        "enable": False,
        "cookie": "",
        "accounts": {},
        "fresh_hours": 48,
    },
    "extract": {
        "min_length": 8,
        "max_length": 32,
        "min_score": 4,
    },
    "notify": {
        "channel": "pushplus",
        "pushplus": {"token": ""},
        "serverchan": {"sendkey": ""},
        "wecom": {"webhook": ""},
        "bark": {"url": ""},
        "telegram": {"bot_token": "", "chat_id": ""},
    },
    "state_path": "state/seen.json",
    "first_run": "seed",
}

ENV_MAP = {
    "PUSHPLUS_TOKEN": "notify.pushplus.token",
    "SERVERCHAN_SENDKEY": "notify.serverchan.sendkey",
    "WECOM_WEBHOOK": "notify.wecom.webhook",
    "BARK_URL": "notify.bark.url",
    "TELEGRAM_BOT_TOKEN": "notify.telegram.bot_token",
    "TELEGRAM_CHAT_ID": "notify.telegram.chat_id",
    "WEIBO_COOKIE": "weibo.cookie",
    "TAPTAP_COOKIE": "taptap.cookie",
    "NOTIFY_CHANNEL": "notify.channel",
}


class Config:
    def __init__(self, data, root):
        self.data = data
        self.root = Path(root)

    def get(self, dotted, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def enabled(self, section):
        return bool(self.get(f"{section}.enable", False))

    def state_path(self):
        return self.root / self.get("state_path", "state/seen.json")


def _merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load(root="."):
    root = Path(root).resolve()
    path = root / "config.json"

    data = json.loads(json.dumps(DEFAULTS))
    if path.exists():
        # utf-8-sig：Windows 记事本 / PowerShell 存出来的 JSON 常带 BOM，
        # 用 utf-8 直接读会在第一个字符就炸。
        try:
            with path.open("r", encoding="utf-8-sig") as fh:
                user = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            raise SystemExit(
                f"config.json 解析失败（{path}）：{exc}\n"
                "请检查 JSON 格式，常见问题是多余的逗号或中文引号。"
            ) from exc
        _merge(data, user)
        log.info("已加载配置 %s", path)
    else:
        log.warning("没找到 config.json，使用默认配置。请先复制 config.example.json")

    for env_key, dotted in ENV_MAP.items():
        value = os.environ.get(env_key)
        if value:
            node = data
            parts = dotted.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value

    return Config(data, root)
