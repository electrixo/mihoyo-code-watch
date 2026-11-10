#!/usr/bin/env python3
"""米家兑换码哨兵（mihoyo-code-watch）

盯着米游社 / 社媒上的新兑换码，一发现就推到你微信。
只做「发现 + 提醒」，不碰你的游戏账号，所以没有封号风险。

用法：
    python run.py                # 正常跑一轮
    python run.py --dry-run      # 只打印，不推送、不改状态
    python run.py --reset        # 清空已推送记录，从头再来
"""

import argparse
import logging
import sys
import time

from watcher import __version__, extractor, notifier, state as state_mod
from watcher.config import load as load_config
from watcher.sources import build_sources

log = logging.getLogger("codewatch")

HEARTBEAT_SECONDS = 20 * 3600


def _local_parts(now, offset_hours):
    """把时间戳按固定时区换算成 (日期, 当天分钟数)。

    用 gmtime + 偏移而不是 localtime：GitHub Actions 的 runner 是 UTC，
    直接 localtime 会让「今天」和推送时段全部错 8 小时。
    """
    shifted = time.gmtime(now + offset_hours * 3600)
    return time.strftime("%Y-%m-%d", shifted), shifted.tm_hour * 60 + shifted.tm_min


def _slot_window(cfg, now):
    """返回 (本地日期, 当前所处时段标签)；首个时段之前（夜间）为 None。

    每个时段从它的时间点开始，一直有效到下一个时段开始，最后一个到当天结束。
    实测 GitHub Actions 的 cron 实际每 2-4 小时才跑一次（大量 scheduled run
    被直接丢弃），所以不能用「60 分钟窄窗口」——那会整天漏推。放宽后，
    9 点以后任意一轮运行都能把 9 点的内容补推出去，夜间仍然静默。
    """
    schedule = cfg.get("push_schedule") or {}
    offset = int(schedule.get("tz_offset", 8))
    date, minutes = _local_parts(now, offset)

    starts = []
    for slot in (schedule.get("slots") or []):
        try:
            hh, mm = str(slot).split(":")
            starts.append((int(hh) * 60 + int(mm), str(slot)))
        except (ValueError, AttributeError):
            continue
    starts.sort()

    for i, (start, label) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else 24 * 60
        if start <= minutes < end:
            return date, label
    return date, None


def pending_slot(cfg, now, state):
    """当前是否处于可推送时段？返回 (本地日期, 时段) / None。

    固定推送时间就是为了躲开凌晨 —— 不在时段内时不推送、也不记录状态，
    所以夜里发现的内容不会丢，会一直攒到下一个时段一起发。
    关掉 push_schedule.enable 就退回「有变化立刻推」。
    """
    if not (cfg.get("push_schedule") or {}).get("enable", True):
        return "", "always"

    date, slot = _slot_window(cfg, now)
    if slot is None or state_mod.push_slot_pushed(state, date, slot):
        return None
    return date, slot


def slot_status(cfg, now, slot):
    """把时段状态压成一行日志。

    每轮都打印是刻意的：没有新内容时不会走推送分支，光看「有没有推送」
    无法判断闸门是否生效，有这行就能直接确认。
    """
    schedule = cfg.get("push_schedule") or {}
    if not schedule.get("enable", True):
        return "已关闭（有变化立刻推）"

    slots = "、".join(str(s) for s in (schedule.get("slots") or []))
    _, current = _slot_window(cfg, now)
    if current is None:
        return f"夜间静默（{slots} 之前不推），本轮不推送"
    if slot is None:
        return f"{current} 时段今天已推送过，本轮不重复推送"
    return f"{current} 时段，本轮可推送"


def filter_excluded(posts, cfg):
    """按排除名单丢掉不玩的游戏的内容。

    只认明确识别出游戏的条目：识别不出游戏的（比如全站活动中心里
    其他厂商的活动）一律保留，避免误伤。
    """
    excluded = {str(g).strip() for g in (cfg.get("exclude_games") or []) if str(g).strip()}
    if not excluded:
        return posts

    kept, dropped = [], 0
    for post in posts:
        if post.game and str(post.game).strip() in excluded:
            dropped += 1
            log.debug("排除 %s：%s", post.game, (post.title or "")[:40])
            continue
        kept.append(post)

    if dropped:
        log.info("按排除名单丢弃 %d 条（%s）", dropped, "、".join(sorted(excluded)))
    return kept


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="米家兑换码哨兵")
    parser.add_argument("--root", default=".", help="工程根目录（config.json 所在位置）")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不推送也不写状态")
    parser.add_argument("--reset", action="store_true", help="清空已推送记录")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser.parse_args(argv)


def collect(cfg, now):
    """跑所有数据源。单个源失败只记警告，绝不让整轮任务挂掉。"""
    posts = []
    for source in build_sources(cfg, now):
        try:
            fetched = source.fetch()
            log.info("  %-16s 返回 %d 条", source.name, len(fetched))
            posts.extend(fetched)
        except Exception as exc:  # noqa: BLE001 - 数据源是外部世界，什么都可能发生
            log.warning("  %-16s 失败：%s: %s", source.name, type(exc).__name__, exc)
    return posts


def analyse(cfg, posts, seen_codes):
    """从帖子里提取候选码，过滤掉已经推送过的。"""
    extract_cfg = cfg.get("extract", {})
    novel = []

    for post in posts:
        if post.announce:
            # 福利公告本身不带码，走另一条推送链路，不参与码提取
            continue

        if post.preset_codes:
            # 结构化数据源：码是明确字段，直接采信，不走启发式
            for code, region, note in post.preset_codes:
                if code in seen_codes:
                    continue
                seen_codes[code] = True
                hit = extractor.CodeHit(code, region, 10, [note or ""])
                novel.append((code, hit, post))
            continue

        hits = extractor.extract(
            post.blob,
            min_length=int(extract_cfg.get("min_length", 8)),
            max_length=int(extract_cfg.get("max_length", 32)),
            min_score=int(extract_cfg.get("min_score", 4)),
        )
        for hit in hits:
            if hit.code in seen_codes:
                continue
            seen_codes[hit.code] = True  # 同一轮内也去重
            novel.append((hit.code, hit, post))

    novel.sort(key=lambda item: (item[2].game or "", item[0]))
    return novel


def collect_announce(posts, state):
    """挑出没推送过的福利公告（TapTap 签到活动这类）。按 post_id 去重。"""
    seen = state.get("announced") or {}
    out, local = [], set()
    for post in posts:
        if not post.announce or not post.post_id:
            continue
        if post.post_id in seen or post.post_id in local:
            continue
        local.add(post.post_id)
        out.append(post)
    return out


def collect_sign_reminders(posts, state, today):
    """挑出今天需要提醒的「累计签到」活动。

    一次性公告解决不了这类活动：奖励按累计天数发（「签到满 5 天领头像框」），
    断一天就白费。所以活动进行期间每天都要提醒一次 —— 但一天只提醒一次，
    哪怕 cron 跑得再勤。去重键是 (slug, 日期)，存在 state 里。
    """
    out, local = [], set()
    for post in posts:
        slug = getattr(post, "sign_slug", "")
        if not slug or slug in local:
            continue
        if not state_mod.sign_reminder_due(state, slug, today):
            continue
        local.add(slug)
        out.append(post)
    return out


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log.info("米家兑换码哨兵 v%s 启动", __version__)

    cfg = load_config(args.root)
    now = time.time()
    state_path = cfg.state_path()

    if args.reset and state_path.exists():
        state_path.unlink()
        log.warning("已清空状态文件 %s", state_path)

    state, first_run = state_mod.load(state_path)
    log.info("记录中已有 %d 个历史兑换码", len(state["codes"]))

    tz_offset = int((cfg.get("push_schedule") or {}).get("tz_offset", 8))
    today, _ = _local_parts(now, tz_offset)
    slot = pending_slot(cfg, now, state)
    log.info("推送时段：%s", slot_status(cfg, now, slot))

    posts = filter_excluded(collect(cfg, now), cfg)
    log.info("共取得 %d 条待分析内容", len(posts))

    novel = analyse(cfg, posts, dict(state["codes"]))
    daily_reminder = bool(cfg.get("taptap.daily_reminder", True))
    announced = collect_announce(posts, state)
    reminders = collect_sign_reminders(posts, state, today) if daily_reminder else []

    if daily_reminder:
        # 签到活动只走「每日提醒」这一条链路，不进活动公告。
        # 否则第二天会出现重复：那天「每日提醒」因为"今天已提醒过"而不发，
        # 于是活动公告把它当成新活动又推一遍 —— 实测踩过这个坑。
        announced = [p for p in announced if not getattr(p, "sign_slug", "")]

    seed_only = first_run and cfg.get("first_run", "seed") == "seed"

    if seed_only:
        log.warning("首次运行：只登记不推送（否则会一口气刷屏 %d 个码 + %d 条活动）",
                    len(novel), len(announced) + len(reminders))
        for code, hit, post in novel:
            state_mod.record(state, code, post.source, post.game, hit.region, post.url, hit.score)
        for post in announced:
            state_mod.remember_announced(state, post.post_id)
        for post in reminders:
            state_mod.remember_sign_reminder(state, post.sign_slug, today)
        novel, announced, reminders = [], [], []

    if novel or announced or reminders:
        if novel:
            _print_hits(novel)
        if announced:
            log.info("发现 %d 条新福利/签到活动：", len(announced))
            for post in announced:
                log.info("  %s", (post.title or "")[:60])
        if reminders:
            log.info("今天需要签到 %d 个活动：", len(reminders))
            for post in reminders:
                log.info("  %s", (post.title or "")[:60])

        title = _title(novel, announced, reminders)
        body = _render(novel, announced, reminders)
        if args.dry_run:
            log.info("dry-run：跳过推送。内容预览：\n%s", body)
        elif slot is None:
            # 夜间静默或本时段已推过：不推送、不记录，内容保持「新」状态
            log.info("本轮不推送（%s），%d 个码 / %d 条活动 / %d 条签到提醒暂存",
                     slot_status(cfg, now, slot), len(novel), len(announced), len(reminders))
        else:
            try:
                notifier.send(cfg, title, body)
                for code, hit, post in novel:
                    state_mod.record(state, code, post.source, post.game,
                                     hit.region, post.url, hit.score)
                for post in announced:
                    state_mod.remember_announced(state, post.post_id)
                for post in reminders:
                    state_mod.remember_sign_reminder(state, post.sign_slug, today)
                slot_date, slot_label = slot
                state_mod.remember_push_slot(state, slot_date, slot_label)
            except Exception as exc:  # noqa: BLE001
                log.error("推送失败，本轮不记录状态，下轮会重试：%s", exc)
                novel, announced, reminders = [], [], []
    else:
        log.info("没有新兑换码，也没有新的福利活动")

    if not args.dry_run:
        dropped = state_mod.prune_announced(state)
        if dropped:
            log.debug("清理了 %d 条过期公告记录", dropped)
        dropped_sign = state_mod.prune_sign_reminders(state)
        if dropped_sign:
            log.debug("清理了 %d 条过期签到提醒记录", dropped_sign)
        dropped_slots = state_mod.prune_push_slots(state)
        if dropped_slots:
            log.debug("清理了 %d 条过期推送时段记录", dropped_slots)
        if now - float(state.get("last_heartbeat", 0)) > HEARTBEAT_SECONDS:
            state["last_heartbeat"] = int(now)
            log.debug("更新心跳时间，保持 GitHub Actions 定时任务不被回收")
        state_mod.save(state_path, state)

    log.info("本轮结束：新增 %d 个码 / %d 条活动 / %d 条签到提醒，历史累计 %d 个码",
             len(novel), len(announced), len(reminders), len(state["codes"]))
    return 0


def _title(novel, announced, reminders):
    bits = []
    if novel:
        bits.append(f"{len(novel)} 个新兑换码")
    if announced:
        bits.append(f"{len(announced)} 个新活动")
    if reminders:
        bits.append(f"{len(reminders)} 个待签到")
    return "🎁 发现 " + " / ".join(bits)


def _render(novel, announced, reminders):
    parts = []
    if reminders:
        parts.append(notifier.render_sign_reminder(reminders))
    if announced:
        parts.append(notifier.render_announce(announced))
    if novel:
        parts.append(notifier.render(novel))
    return "\n\n".join(parts)


def _print_hits(novel):
    log.info("识别到 %d 个新兑换码：", len(novel))
    for code, hit, post in novel:
        region = hit.region or "区域未标注"
        game = post.game or "游戏未识别"
        log.info("  %-16s %s · %s  (score=%d)", code, game, region, hit.score)


if __name__ == "__main__":
    sys.exit(main())
