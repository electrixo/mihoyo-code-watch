"""去重状态：已经推送过的码不再打扰，只看新增。

状态文件建议提交回仓库（GitHub Actions 每次都是全新容器，
不提交就等于每次都当第一次跑，会重复推送同一批码）。

写入策略很关键：只在"真的有变化"时才落盘。否则每轮 cron 都会改一次
状态文件，仓库每半小时多一个 commit，很快就没法看了。
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("codewatch.state")

SCHEMA = {
    "codes": {},
    # 已推送过的「福利公告」（TapTap 签到活动这类，本身不带码，
    # 按 post_id 去重，否则每天都会重复推同一条活动）
    "announced": {},
    # 签到活动的「每日提醒」去重：{slug: "YYYY-MM-DD"}。
    # 累计签到型活动需要天天提醒（断一天就白费），但一天只能提醒一次。
    # 只存最后一次提醒的日期，天然不会膨胀。
    "sign_reminders": {},
    # 推送时段去重：{"YYYY-MM-DD": ["09:00", "20:00"]}。
    # 同一个时段只推一次，避免 :07 / :37 两轮 cron 在同一时段重复发。
    "push_slots": {},
    "last_heartbeat": 0,
}


def load(path):
    path = Path(path)
    if not path.exists():
        log.info("状态文件不存在，视为首次运行：%s", path)
        return json.loads(json.dumps(SCHEMA)), True
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("状态文件损坏（%s），重建：%s", exc, path)
        return json.loads(json.dumps(SCHEMA)), True

    for key, default in SCHEMA.items():
        data.setdefault(key, json.loads(json.dumps(default)))
    return data, False


def record(state, code, source, game, region, url, score):
    state["codes"][code] = {
        "first_seen": int(time.time()),
        "source": source,
        "game": game or "",
        "region": region or "",
        "url": url or "",
        "score": score,
    }


def save(path, state, dry_run=False):
    """只在有实质变化时写盘，避免每轮 cron 都产生一个无意义 commit。"""
    if dry_run:
        log.info("dry-run：跳过状态写盘")
        return False
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists() and _read_raw(path) == body:
        log.info("状态无变化，跳过写盘")
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(body)
    tmp.replace(path)
    log.info("状态已保存：%s（累计 %d 个码）", path, len(state.get("codes", {})))
    return True


def _read_raw(path):
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            return fh.read()
    except OSError:
        return None


def remember_announced(state, post_id):
    """登记一条已经推送过的福利公告。"""
    if post_id:
        state["announced"][post_id] = int(time.time())


def sign_reminder_due(state, slug, today):
    """这个签到活动今天还没提醒过吗？

    today 是 "YYYY-MM-DD" 字符串。用日期而不是时间戳做键，
    是为了「一天一次」这个语义天然成立 —— 轮询频率改成什么样都不影响。
    """
    if not slug:
        return False
    return (state.get("sign_reminders") or {}).get(slug) != today


def remember_sign_reminder(state, slug, today):
    if slug:
        state.setdefault("sign_reminders", {})[slug] = today


def push_slot_pushed(state, date, slot):
    """这个时段的推送是不是已经发过了？

    cron 每 30 分钟跑一轮，同一个时段（比如 09:00-10:00）会跑到两三次，
    用它保证一个时段只真正推送一次。
    """
    if not date or not slot:
        return False
    return slot in ((state.get("push_slots") or {}).get(date) or [])


def remember_push_slot(state, date, slot):
    if not date or not slot:
        return
    slots = state.setdefault("push_slots", {}).setdefault(date, [])
    if slot not in slots:
        slots.append(slot)


def prune_announced(state, keep_days=180):
    """公告记录会一直涨，定期清掉老条目，避免状态文件无限膨胀。"""
    cutoff = int(time.time()) - keep_days * 86400
    old = [k for k, ts in state.get("announced", {}).items()
           if isinstance(ts, int) and ts < cutoff]
    for key in old:
        del state["announced"][key]
    return len(old)


def prune_sign_reminders(state, keep_days=180):
    """同样定期清签到提醒记录。

    键是 "YYYY-MM-DD" 字符串，且格式定长，所以直接按字典序比较即可判断新旧。
    """
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - keep_days * 86400))
    reminders = state.get("sign_reminders") or {}
    old = [k for k, day in reminders.items()
           if isinstance(day, str) and day < cutoff]
    for key in old:
        del reminders[key]
    return len(old)


def prune_push_slots(state, keep_days=7):
    """时段记录只用于当天去重，留 7 天足够排查，之后清掉。"""
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - keep_days * 86400))
    slots = state.get("push_slots") or {}
    old = [k for k in slots if isinstance(k, str) and k < cutoff]
    for key in old:
        del slots[key]
    return len(old)
