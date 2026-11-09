"""TapTap 活动中心源 —— 渠道福利 / 签到活动的发现。

为什么值得单开一个源：TapTap 的「游戏签到」活动是**直接发兑换码**的。
官方活动页原话是「每次签到会发放一个 15 位数的兑换码」，
也就是说这类活动的终点就是兑换码，和本工程的目标完全一致。

实测结论（2026-09，本机逐条验证）：
  * `https://www.taptap.cn/events` 是 Nuxt SSR 页，活动列表内嵌在
    `<script id="__NUXT_DATA__">` 里（devalue 的「扁平数组 + 索引引用」格式），
    共 472 个活动，`?page=N` 正常翻页，**无需 Cookie、无需登录**。
  * 所有 `webapiv2` 接口都必须带 `X-UA` 查询参数（格式是公开的），
    否则一律返回 `INVALID_XUA: XUA is empty`。注意是 query 参数，不是请求头。
  * `/webapiv2/event/game-sign/detail/{slug}` 可匿名拿到签到活动的每日奖励
    和**剩余库存**（`award_pool.award_list[].inventory`）——
    礼包先到先得，库存只剩几十件的时候值得马上动手。

  * ⚠️ **最大的一个坑：游戏专属活动不在活动中心里。**
    活动中心 `/events` 只收录全站精选（472 条），米哈游游戏的签到活动
    **一条都不在里面**。它们只挂在游戏主页的「游戏活动」tab：
        https://www.taptap.cn/app/{app_id}/game-event
    实测（2026-09）：绝区零的「签到领好礼」正在进行（9.3 万人参加），
    而活动中心全量扫描里 `game-sign` 只有 3 条、没有一条属于米哈游。
    只扫活动中心会**整个漏掉米哈游的签到活动** —— 这正是本工程早期版本的 bug。
    所以下面额外实现了 `_game_events()`：按 app_id 直扫游戏活动页。

  * 游戏活动页里每条活动的类型标记是 `label_type`：
    `check_in`（签到）/ `gift`（礼包，点击即领）/ `activity`（创作征集）/
    `lottery`（抽奖）/ `in_app_event`（游戏内活动）。
    这些活动对象**没有稳定 id 字段**，所以 post_id 用 web_url 派生。
    另外它们的 `web_url` 有时是空串（例如星铁的新版本礼包只在客户端内领），
    这时退回用 title 做 key。

本工程刻意不做的事：不代替你点「签到」。
签到动作的端点已经挖出来了（见下），但它要登录态 + 防重放校验，
而那等于把账号交出去 —— 这正是本工程从头到尾在回避的东西。
这里只回答两个问题：什么时候有活动、今天该不该去签。

—— 签到接口摸底结论（2026-09，从活动页 app.js 里挖出，非猜测）——

  * 签到动作：`POST /webapiv2/event/game-sign/check_in?activity_code={slug}`
    - 参数名是 `activity_code`，且**必须放 query**（放 body 会报「ActivityCode 必填」）
    - 裸调（无登录态）返回「网页已过期, 请刷新后重试!」—— 说明还有一层防重放校验
  * 签到状态：`GET /webapiv2/event/game-sign/dynamic_data/{slug}`
    - 匿名可读，但「我」相关字段全是默认值（`check_in_button.disabled = true`）
    - 带登录 Cookie 才读得到 `check_in_log.total_check_in_days`（累计签到天数）
  * 领累计奖励：`POST /webapiv2/event/game-sign/check_in_accept_award`
  * 我的奖励：`GET /webapiv2/event/game-sign/get_my_award_list/{slug}`
  * 鉴权走 **Cookie**（app.js 里没有 X-Tap-Token / Authorization 之类的注入）

  * 一个实测出来的捷径：活动页 URL 支持 `?auto_checkin=true`。
    app.js 里的 `triggerAutoCheckin()` 会读这个参数，为 `true` 时
    在**已登录**状态下自动调用签到（重试 3 次、每次间隔 1 秒）。
    也就是说 —— 你浏览器里已经登录 TapTap 的话，点开带这个参数的
    链接就等于自动签到，不需要装任何东西。推送里会直接给这个链接。
"""

import hashlib
import json
import logging
import re
import time
import uuid

from ..http import HttpError, get_text, quote
from . import Post, Source

log = logging.getLogger("codewatch.sources.taptap")

EVENTS_PAGE = "https://www.taptap.cn/events"
GAME_EVENT_PAGE = "https://www.taptap.cn/app/{aid}/game-event"
SIGN_DETAIL_API = "https://www.taptap.cn/webapiv2/event/game-sign/detail/{slug}"
SIGN_DYNAMIC_API = "https://www.taptap.cn/webapiv2/event/game-sign/dynamic_data/{slug}"
SIGN_PAGE = "https://www.taptap.cn/events/game-sign/{slug}"

# 游戏活动页里 label_type 的中文名，用来在推送里说清这条活动是什么性质
KIND_LABEL = {
    "check_in": "签到",
    "gift": "礼包（点击即领）",
    "activity": "创作征集",
    "lottery": "抽奖",
    "in_app_event": "游戏内活动",
}

HEADERS = {
    "Referer": "https://www.taptap.cn/events",
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

DEFAULT_KEYWORDS = ["签到", "礼包", "兑换码", "CDK", "激活码", "兑换"]

# 米哈游各游戏在 TapTap 的 app_id（2026-09 取自厂商页 /developer/3103，实测有效）。
# 签到这个字段不是可选项：游戏的签到/礼包活动只挂在 /app/{id}/game-event，
# 活动中心 /events 里一条都没有。
DEFAULT_APPS = {
    "原神": 168332,
    "崩坏：星穹铁道": 224267,
    "绝区零": 234493,
}

# 默认只推「真正能领东西」的两类：
#   check_in 签到、gift 礼包（点击即领）
# 创作征集(activity)/抽奖(lottery)/游戏内活动(in_app_event) 噪音偏大，
# 需要的话在 config 里显式加。
DEFAULT_EVENT_TYPES = ["check_in", "gift"]

_NUXT_RE = re.compile(r'id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S)
_SLUG_RE = re.compile(r"/events/game-sign/([A-Za-z0-9\-_]+)")


def _xua():
    """TapTap 网页端的设备指纹。UID 每轮随机即可，匿名请求不校验它与登录态的绑定。"""
    return ("V=1&PN=WebApp&LANG=zh_CN&VN_CODE=102&LOC=CN&PLT=PC&DS=Android"
            f"&UID={uuid.uuid4()}&OS=Windows&OSV=10&DT=PC")


def _with_xua(url):
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}X-UA={quote(_xua())}"


def _grab(url, timeout=20):
    return get_text(_with_xua(url), headers=HEADERS, timeout=timeout, retries=2)


def _slots(html):
    m = _NUXT_RE.search(html)
    if not m:
        raise HttpError("TapTap 活动页里没有 __NUXT_DATA__（页面结构可能改了）")
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise HttpError(f"__NUXT_DATA__ 不是合法 JSON：{exc}") from exc


def _deref(data, idx):
    """devalue 的对象字段值是指向槽位的索引，这里解一层拿到标量。

    不整体解引用整棵树：数据里混着各种 devalue 特殊类型
    （ShallowReactive / Ref / Date …），全量解析很容易炸，
    而我们需要的字段形态是固定的标量。
    """
    if not isinstance(idx, int) or isinstance(idx, bool):
        return None
    if not (0 <= idx < len(data)):
        return None
    value = data[idx]
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    return None


def _extract_events(data):
    """从扁平槽位里挖出活动列表。

    活动列表的特征很明确：一个同时带 list / total 的 dict 槽位。
    """
    events = []
    for slot in data:
        if not (isinstance(slot, dict) and "list" in slot and "total" in slot):
            continue
        refs = slot["list"]
        arr = data[refs] if isinstance(refs, int) and 0 <= refs < len(data) else None
        if not isinstance(arr, list):
            continue
        for ref in arr:
            obj = data[ref] if isinstance(ref, int) and 0 <= ref < len(data) else None
            if not isinstance(obj, dict):
                continue
            events.append({
                "id": _deref(data, obj.get("id")),
                "title": _deref(data, obj.get("title")),
                "label": _deref(data, obj.get("label")),
                "url": _deref(data, obj.get("web_url")),
                "expire": _deref(data, obj.get("expire_time")),
                "published": _deref(data, obj.get("published_time")),
            })
    return events


def _abs_url(url):
    if not url:
        return ""
    return "https://www.taptap.cn" + url if url.startswith("/") else url


def _game_events(aid):
    """游戏主页「活动」tab（/app/{aid}/game-event）里的专属活动。

    为什么不复用 _extract_events：那个函数是给活动中心设计的，
    靠「同时带 list 和 total 的槽位」定位列表；游戏活动页的结构不同，
    活动对象直接散在槽位里，特征字段是 title + web_url + label_type。
    两者硬凑会让任一边改版就同时炸，所以分开写。

    拿不到就返回空列表 —— 单款游戏失败不该影响其它游戏。
    """
    try:
        data = _slots(_grab(GAME_EVENT_PAGE.format(aid=aid)))
    except HttpError as exc:
        log.debug("游戏 %s 的 game-event 页抓取失败：%s", aid, exc)
        return []

    out = []
    for slot in data:
        if not (isinstance(slot, dict) and "title" in slot and "web_url" in slot):
            continue
        title = _deref(data, slot.get("title"))
        url = _deref(data, slot.get("web_url"))
        kind = _deref(data, slot.get("label_type"))
        if not isinstance(title, str) or not isinstance(kind, str) or not kind:
            # 没有 label_type 的是「厂商 / 编辑推荐 / 即点即玩」这类页面框架项。
            # 不能用「url 为空」来判 —— 星铁的新版本礼包(gift)恰好没有网页
            # 入口（只在客户端内领），那样会把它误杀。
            continue
        title = title.strip()
        if not (1 < len(title) < 80):
            continue
        out.append({
            "title": title,
            "content": str(_deref(data, slot.get("content")) or ""),
            "type": kind,
            "url": _abs_url(url) if isinstance(url, str) and url and url != "None" else "",
            "start": _deref(data, slot.get("start_time")),
        })
    return out


def _event_pid(aid, ev):
    """游戏活动没有稳定 id，用 app_id + url(或 title) 派生一个。"""
    key = f"{aid}|{ev.get('url') or ev.get('title')}"
    return "taptap-app-" + hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


def _fmt_ts(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except (TypeError, ValueError, OSError):
        return ""


# 只用来「正面确认」奖励里有码，不作为否定判断的依据 ——
# 判断错方向（说没码其实有）比漏判更伤。
_CODE_HINTS = ("兑换码", "cdkey", "cdk", "礼包码", "激活码", "兑换券")


def _looks_like_code(text):
    low = (text or "").lower()
    return any(hint in low for hint in _CODE_HINTS)


def _sign_detail(slug):
    """签到活动详情。拿不到就返回 None —— 详情只是锦上添花，不该拖垮整轮。"""
    try:
        payload = json.loads(_grab(SIGN_DETAIL_API.format(slug=slug), timeout=20))
    except (HttpError, json.JSONDecodeError) as exc:
        log.debug("签到活动 %s 详情获取失败：%s", slug, exc)
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def _dynamic_data(slug, cookie=""):
    """签到动态数据 —— 「今天签没签」只在这里。

    匿名也能调通，但此时服务器不知道你是谁，返回的
    `check_in_button.disabled` 恒为 true、`today_is_checked` 只是默认值，
    所以**不填 Cookie 时不能采信**，只能当活动框架用。
    """
    headers = dict(HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    try:
        payload = json.loads(
            get_text(_with_xua(SIGN_DYNAMIC_API.format(slug=slug)), headers=headers, timeout=20)
        )
    except (HttpError, json.JSONDecodeError) as exc:
        log.debug("签到动态数据 %s 获取失败：%s", slug, exc)
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def auto_checkin_url(slug):
    """带自动签到参数的链接。

    活动页 app.js 里的 triggerAutoCheckin() 会读 URL 上的 `auto_checkin`，
    为 true 且在已登录状态下会自动完成签到（重试 3 次）。所以这个链接
    对用户的实际含义是：点一下，签完。
    """
    return f"{SIGN_PAGE.format(slug=slug)}?auto_checkin=true"


class TapTapEvents(Source):
    name = "TapTap活动中心"

    def __init__(self, cfg, now=None):
        self.keywords = [str(k) for k in cfg.get("taptap.keywords", DEFAULT_KEYWORDS)]
        self.max_pages = max(1, int(cfg.get("taptap.max_pages", 3)))
        self.with_detail = bool(cfg.get("taptap.with_detail", True))
        # 可选：填了 TapTap 的登录 Cookie，就能读出「今天签没签」「累计几天」。
        # 不填也能用 —— 只是推送变成纯提醒（不知道你签没签）。
        self.cookie = cfg.get("taptap.cookie", "") or ""
        self.now = now or time.time()

        # 按游戏扫专属活动。这块和活动中心是两条独立链路，缺一不可：
        # 活动中心有全站精选活动，游戏活动页有该游戏的签到/礼包。
        raw_apps = cfg.get("taptap.apps", DEFAULT_APPS)
        self.apps = {}
        if isinstance(raw_apps, dict):
            for name, aid in raw_apps.items():
                try:
                    self.apps[str(name)] = int(aid)
                except (TypeError, ValueError):
                    log.warning("忽略非法的 taptap.apps 项：%s=%r", name, aid)
        elif raw_apps:
            log.warning("taptap.apps 必须是 {\"游戏名\": app_id} 形式，当前是 %s，已忽略",
                        type(raw_apps).__name__)

        raw_types = cfg.get("taptap.event_types", DEFAULT_EVENT_TYPES)
        self.event_types = {str(t).strip().lower() for t in (raw_types or [])}

    def fetch(self):
        events, seen_ids = [], set()

        for page in range(1, self.max_pages + 1):
            url = EVENTS_PAGE if page == 1 else f"{EVENTS_PAGE}?page={page}"
            try:
                html = _grab(url)
            except HttpError as exc:
                log.warning("TapTap 活动第 %d 页抓取失败：%s", page, exc)
                break
            try:
                slots = _slots(html)
            except HttpError as exc:
                log.warning("TapTap 活动第 %d 页解析失败：%s", page, exc)
                break

            got = 0
            for ev in _extract_events(slots):
                eid = ev.get("id")
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                got += 1
                post = self._to_post(ev)
                if post:
                    events.append(post)

            log.debug("TapTap 活动第 %d 页：解析 %d 条，命中 %d 条",
                      page, got, len(events))
            if got == 0:
                break
            time.sleep(0.3)

        centre_count = len(events)

        # ---- 游戏专属活动：活动中心的盲区 ----
        # 米哈游的签到/礼包活动一条都不在 /events 里，只挂在游戏主页的
        # /app/{id}/game-event。不做这一步就会整个漏掉（实测绝区零有
        # 一个 9.3 万人参加的签到活动，全站活动中心扫描完全看不到）。
        seen_pid = set()
        for app_name, aid in self.apps.items():
            for ev in _game_events(aid):
                kind = (ev.get("type") or "").lower()
                if kind not in self.event_types:
                    continue
                pid = _event_pid(aid, ev)
                if pid in seen_pid:
                    continue
                seen_pid.add(pid)
                post = self._to_post_game_event(app_name, aid, ev, pid)
                if post:
                    events.append(post)
            time.sleep(0.25)

        log.info("TapTap 共命中 %d 条（活动中心 %d + 游戏专属 %d，覆盖 %d 款游戏）",
                 len(events), centre_count, len(events) - centre_count, len(self.apps))
        return events

    def _to_post_game_event(self, app_name, aid, ev, pid):
        """把游戏主页活动页里的一条活动转成 Post。

        check_in 类型会额外补详情（签到奖励、剩余天数），并打上 sign_slug
        走「每日提醒」链路 —— 累计签到型活动断一天就白费，
        只公告一次是不够的。
        """
        url = ev.get("url") or ""
        kind = (ev.get("type") or "").lower()
        label = KIND_LABEL.get(kind, kind or "活动")
        title = ev.get("title") or ""

        game = app_name
        sign_slug = ""
        progress = ""
        info = None

        slug_match = _SLUG_RE.search(url) if url else None
        if kind == "check_in" and slug_match:
            info = self._sign_summary(slug_match.group(1))
            if info["finished"]:
                log.debug("跳过已结束的游戏签到活动：%s / %s", app_name, title)
                return None
            if info["game"]:
                game = info["game"]
            sign_slug = slug_match.group(1)
            progress = info["progress"]

        lines = [title]
        # 签到活动的详情摘要里已经带了「游戏：xxx」，这里就不再重复一行；
        # 礼包等没有详情摘要的类型才需要自己补游戏名。
        if not (info and info["game"]):
            lines.append(f"游戏：{app_name}")
        lines.append(f"类型：{label}")

        content = (ev.get("content") or "").strip()
        if content and content != "None":
            lines.append(f"说明：{content[:120]}")
        if info and info["text"]:
            lines.append(info["text"])

        if not url:
            # 例如星铁的新版本礼包，只在 TapTap 客户端内领，网页没有入口。
            lines.append("入口：TapTap 客户端内「我的 → 礼包」领取")
        lines.append(f"发现时间：{_fmt_ts(self.now)}")

        return Post(
            source=f"TapTap·{app_name}",
            title=title,
            text="\n".join(x for x in lines if x),
            url=url,
            game=game,
            created_at=int(ev.get("start") or 0),
            post_id=pid,
            announce=True,
            sign_slug=sign_slug,
            progress=progress,
        )

    def _to_post(self, ev):
        title = (ev.get("title") or "").strip()
        url = _abs_url(ev.get("url") or "")
        if not title or not url:
            return None

        expire = ev.get("expire")
        if isinstance(expire, (int, float)) and expire and expire < self.now:
            return None

        slug_match = _SLUG_RE.search(url)
        is_sign = bool(slug_match)

        if not is_sign and not any(k in title for k in self.keywords):
            return None

        lines = [title, f"活动页：{url}"]
        deadline = _fmt_ts(expire) if expire else ""
        lines.append(f"截止：{deadline or '未标注'}")

        game = None
        sign_slug = ""
        progress = ""
        if is_sign and self.with_detail:
            info = self._sign_summary(slug_match.group(1))
            if info["finished"]:
                # 列表页的 expire_time 经常是空的，光靠它滤不掉死活动，
                # 详情接口的 status_desc 才是准的。
                log.debug("跳过已结束的签到活动：%s", title)
                return None
            game = info["game"]
            progress = info["progress"]
            if info["text"]:
                lines.append(info["text"])
            # 累计签到型活动只推一次公告没用（断一天就白费），
            # 标上 slug 让它额外走「每日提醒」链路。
            sign_slug = slug_match.group(1)

        lines.append(f"发现时间：{_fmt_ts(self.now)}")

        return Post(
            source="TapTap活动中心" + ("（签到）" if is_sign else ""),
            title=title,
            text="\n".join(x for x in lines if x),
            url=url,
            game=game,
            created_at=int(ev.get("published") or 0),
            post_id=f"taptap-{ev.get('id')}",
            announce=True,
            sign_slug=sign_slug,
            progress=progress,
        )

    def _sign_summary(self, slug):
        """把签到活动详情压成一段人能读的摘要。

        返回 dict：
            text      摘要文本（可直接拼进推送正文）
            game      游戏名（app_title），拿不到为 None
            finished  活动是否已结束
            progress  签到进度，只有填了 Cookie 才读得到，否则为空串
        """
        result = {"text": "", "game": None, "finished": False, "progress": ""}
        detail = _sign_detail(slug)
        if not detail:
            return result

        info = detail.get("daily_check_in") or {}
        game = info.get("app_title") or None
        desc = str(info.get("status_desc") or "")
        duration = str(info.get("duration") or "")
        countdown = info.get("activity_count_down")

        result["game"] = game
        result["finished"] = ("已结束" in desc) or info.get("enable") is False

        rewards, has_code = [], False
        for item in (detail.get("history_sign_rewards") or [])[:8]:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            content = str(item.get("content") or "").strip()
            if label and content:
                rewards.append(f"{label}→{content}")
            if _looks_like_code(content):
                has_code = True
            for prize in ((item.get("group") or {}).get("prize_list") or []):
                if isinstance(prize, dict) and _looks_like_code(str(prize.get("title") or "")):
                    has_code = True

        stock = []
        pool = detail.get("award_pool") or {}
        for item in (pool.get("award_list") or []):
            if not isinstance(item, dict):
                continue
            total = item.get("total")
            left = item.get("inventory")
            if isinstance(total, int) and isinstance(left, int) and total > 0:
                stock.append(f"{left}/{total}")

        parts = []
        if game:
            parts.append(f"游戏：{game}")
        if desc:
            parts.append(f"状态：{desc}")
        if duration:
            parts.append(duration)
        if isinstance(countdown, int) and countdown > 0:
            # 单位是「天」，不是秒：实测绝区零的活动返回 6，
            # 而活动 09月18日 结束、抓取日 09月12日 —— 正好 6 天。
            # 早期版本按秒除以 86400，结果打印成「剩余 0 天」。
            # 保留一个兼容分支，万一将来 TapTap 改成秒也能兜住。
            days = countdown if countdown < 1000 else countdown // 86400
            parts.append(f"剩余 {days} 天")
        if rewards:
            parts.append("签到奖励：" + "，".join(rewards))
        if stock:
            parts.append("剩余库存：" + "，".join(stock))
        # 只在「确认发码」时正面标注。TapTap 平台自己的签到活动发的是
        # 点券/红包/头像框这类站内奖励，和游戏兑换码是两回事，
        # 不标注反而让人误以为签了就有码。
        if has_code:
            parts.append("奖励含兑换码")

        redeem = str(pool.get("redeem_mode") or "").strip()
        if redeem:
            parts.append("兑换方式：" + redeem.splitlines()[0][:60])

        result["text"] = "｜".join(parts)
        if self.cookie:
            result["progress"] = self._read_progress(slug)
        return result

    def _read_progress(self, slug):
        """用登录态读「今天签没签、累计几天」。读不到就返回空串。

        这一层是可选增强：读不到进度不该影响「该提醒还是提醒」。
        """
        dyn = _dynamic_data(slug, self.cookie)
        if not dyn:
            return ""
        dci = dyn.get("daily_check_in") or {}
        btn = dci.get("check_in_button") or {}
        clog = dci.get("check_in_log") or {}

        bits = []
        days = clog.get("total_check_in_days")
        if isinstance(days, int) and days:
            bits.append(f"累计签到 {days} 天")
        checked = btn.get("today_is_checked")
        if checked is True:
            bits.append("今日已签")
        elif checked is False:
            bits.append("今日未签")
        return "，".join(bits)
