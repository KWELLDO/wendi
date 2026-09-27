#!/usr/bin/env python3
"""问底 · 内测日报

用法（在 /opt/wendi 下运行，或本机仓库根目录）：
    python3 daily_report.py            # 今天
    python3 daily_report.py 2026-09-27 # 指定日

做三件事：
  1. 重算该日与前一日的聚合（daily_metrics 幂等）；
  2. 把该日日报写成 Markdown（reports/YYYY-MM-DD.md），屏幕上同时打印一份；
  3. 顺手剪掉保留期外的事件流水（默认 90 天，聚合指标永久保留）。

阈值只在报告里标红提示，不推任何外部告警（这个阶段的已知缺口）。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 服务器上脚本与 db.py 同目录（/opt/wendi）；仓库里脚本在 scripts/ 下——两处都能跑
HERE = Path(__file__).resolve().parent
for _p in (HERE, HERE.parent):
    if (_p / "db.py").exists():
        sys.path.insert(0, str(_p))
        break
import db  # noqa: E402

KEEP_DAYS = 90
COST_ALERT_CENTS = 5000      # ¥50/天
ERROR_RATE_ALERT = 0.05      # 5%
STEPS = (("select_text", "划词"), ("explain_success", "解释成功"),
         ("drill_down", "下钻"), ("go_back", "回退"), ("share_create", "分享"))


def fmt_cents(cents: int) -> str:
    return f"¥{cents / 100:.2f}"


def build(day: str) -> str:
    db.rollup_day(db._day_shift(day, -1))     # 昨日重算，D1 才准
    m = db.rollup_day(day)
    fun = db.funnel(day)
    testers = db.tester_rows()
    recent = db.metrics_range(7)
    codes = db.list_codes(kind="invite")
    unused = [c["code"] for c in codes if c["status"] == "unused"]

    alerts = []
    if m["cost_cents"] > COST_ALERT_CENTS:
        alerts.append(f"成本 {fmt_cents(m['cost_cents'])} 超过阈值 {fmt_cents(COST_ALERT_CENTS)}")
    if m["explains"] and m["explain_errors"] / m["explains"] > ERROR_RATE_ALERT:
        alerts.append(f"解释失败率 {m['explain_errors']}/{m['explains']} 超过 {ERROR_RATE_ALERT:.0%}")
    if m["app_opens"] and m["d1_return"] == 0:
        alerts.append("有活跃但 D1 回访 = 0（还没有人第二天自己回来）")

    lines = [f"# 内测日报 · {day}", ""]
    if alerts:
        lines += ["## ⚠ 阈值提示", ""] + [f"- {a}" for a in alerts] + [""]
    lines += [
        "## 今日", "",
        "| 活跃 | 新增 | 会话 | 打开 | 解释 | 解释失败 | 下钻 | 回退 | 分享 | 导出 | 反馈 | 成本 | 错误 | D1 回访 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        f"| {m['active_users']} | {m['new_users']} | {m['sessions']} | {m['app_opens']} | "
        f"{m['explains']} | {m['explain_errors']} | {m['drill_downs']} | {m['go_backs']} | "
        f"{m['shares']} | {m['exports']} | {m['feedbacks']} | {fmt_cents(m['cost_cents'])} | "
        f"{m['errors']} | {m['d1_return']} |",
        "",
        "## 漏斗（独立会话）", "",
        "| 步骤 | 会话数 |", "|---|---|",
    ]
    lines += [f"| {label} | {fun.get(key, 0)} |" for key, label in STEPS]
    lines += ["", "## 近 7 天", "",
              "| 日 | 活跃 | 新增 | 解释 | 成本 | 错误 | D1 |", "|---|---|---|---|---|---|---|"]
    lines += [f"| {r['day']} | {r['active_users']} | {r['new_users']} | {r['explains']} | "
              f"{fmt_cents(r['cost_cents'])} | {r['errors']} | {r['d1_return']} |" for r in recent]
    lines += ["", "## 测试者", "",
              "| 标签 | 码 | 账号 | 解释 | 事件 | 成本 | 最近出现 | D1 | D7 |",
              "|---|---|---|---|---|---|---|---|---|"]
    if testers:
        for t in testers:
            last = t["last_day"] or "—"
            lines.append(
                f"| {t['tester_label'] or '—'} | {t['code']} | {t['email'] or '未注册'} | "
                f"{t['explains']} | {t['events']} | {fmt_cents(t['cost_cents'])} | {last} | "
                f"{'✓' if t['d1'] else '—'} | {'✓' if t['d7'] else '—'} |")
    else:
        lines.append("| （还没发过邀请码） | | | | | | | | |")
    lines += ["", "## 库存", "",
              f"- 未用邀请码：{len(unused)} 个" + (f"（{'、'.join(unused[:10])}{' …' if len(unused) > 10 else ''}）" if unused else ""),
              f"- 事件流水保留：{KEEP_DAYS} 天（聚合指标永久）", ""]
    return "\n".join(lines)


def main() -> None:
    db.init()
    day = sys.argv[1] if len(sys.argv) > 1 else db.today()
    text = build(day)
    out_dir = Path(db.__file__).resolve().parent / "reports"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"{day}.md").write_text(text, encoding="utf-8")
    print(text)
    print(f"\n[已写入 {out_dir / (day + '.md')}]")
    pruned = db.prune_events(KEEP_DAYS)
    if pruned:
        print(f"[剪枝] 删除 {pruned} 条超过 {KEEP_DAYS} 天的事件")


if __name__ == "__main__":
    main()
