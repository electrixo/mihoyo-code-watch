"""米游社（bbs-api.miyoushe.com）搜索源 —— 本工程的主力数据源。

为什么用搜索而不是版块列表：
  * searchPosts 无需登录、直接返回帖子正文，兑换码就在正文里
  * 前瞻直播结束后几分钟内，社区就会冒出「XX版本前瞻直播兑换码」这种帖
  * 版块列表接口只给标题和短摘要，拿不到码

已知限制：
  搜索结果按相关度排序，且单次最多 10 条，拿不到严格的"按时间倒序"。
  所以这里用 fresh_hours 做新鲜度闸门：只处理最近 N 小时内发布的帖子，
  老帖即使被搜出来也不会重复打扰（去重还有 state 兜底）。
"""

import logging
import time

from .. import extractor
from ..http import HttpError, get_json, quote
from . import Post, Source

log = logging.getLogger("codewatch.sources.miyoushe")

SEARCH_API = "https://bbs-api.miyoushe.com/post/wapi/searchPosts"
ARTICLE_URL = "https://www.miyoushe.com/ys/article/{post_id}"

HEADERS = {
    "Referer": "https://www.miyoushe.com/",
    "Accept": "application/json, text/plain, */*",
}

GAME_ID_MAP = {
    1: "崩坏3",
    2: "原神",
    3: "崩坏学园2",
    4: "未定事件簿",
    6: "崩坏：星穹铁道",
    8: "绝区零",
}


class MiyousheSearch(Source):
    name = "米游社搜索"

    def __init__(self, cfg, now=None):
        self.keywords = cfg.get("miyoushe.keywords", ["兑换码"])
        self.page_size = int(cfg.get("miyoushe.page_size", 10))
        self.fresh_hours = float(cfg.get("miyoushe.fresh_hours", 72))
        self.now = now or time.time()

    def fetch(self):
        cutoff = self.now - self.fresh_hours * 3600
        posts, seen_ids = [], set()

        for keyword in self.keywords:
            url = f"{SEARCH_API}?keyword={quote(keyword)}&page_size={self.page_size}"
            try:
                # timeout/retries 刻意收紧：米游社偶发 SSL 握手超时，
                # 默认 20 秒 × 3 轮 × 2 种 SSL 上下文 = 单关键词最坏 120 秒，
                # 5 个关键词足以吃满 Actions 的 10 分钟上限。
                data = get_json(url, headers=HEADERS, timeout=10, retries=2)
            except HttpError as exc:
                log.warning("关键词 %r 抓取失败：%s", keyword, exc)
                continue

            if data.get("retcode") != 0:
                log.warning("关键词 %r 返回 retcode=%s msg=%s",
                            keyword, data.get("retcode"), data.get("message"))
                continue

            for wrapper in (data.get("data") or {}).get("posts") or []:
                post = wrapper.get("post") or {}
                post_id = str(post.get("post_id") or "")
                if not post_id or post_id in seen_ids:
                    continue

                created = int(post.get("created_at") or 0)
                if created and created < cutoff:
                    continue

                seen_ids.add(post_id)
                title = post.get("subject") or ""
                body = post.get("content") or ""
                posts.append(Post(
                    source=f"米游社搜索({keyword})",
                    title=title,
                    text=body,
                    url=ARTICLE_URL.format(post_id=post_id),
                    game=extractor.detect_game(title, body) or GAME_ID_MAP.get(post.get("game_id")),
                    created_at=created,
                    post_id=post_id,
                ))

        log.info("米游社搜索命中 %d 条新鲜帖子", len(posts))
        return posts
