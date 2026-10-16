#!/usr/bin/env python3
"""
สร้างข้อมูลสำหรับเว็บ static (GitHub Pages) จากโค้ด CDC V3 เดิม

  python build_static.py              # อ่าน config.json -> เขียน docs/data/*.json
  python build_static.py --no-alert   # ไม่ส่ง Telegram

ผลลัพธ์
  docs/data/screen.json          ตารางสแกนทุกหุ้น
  docs/data/chart/<SYMBOL>.json  ข้อมูลกราฟรายหุ้น (แท่งเทียน, EMA, จุดเข้า/ออก, SL)
  docs/data/alerts_sent.json     บันทึกสัญญาณที่ส่ง Telegram ไปแล้ว (กันส่งซ้ำ)
ต้องมี cdc_backtest.py และ cdc_app.py อยู่โฟลเดอร์เดียวกัน
"""
import argparse, datetime as dt, json, os, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import cdc_app as A
import cdc_backtest as B

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "docs", "data")
CHART = os.path.join(DATA, "chart")
ICT = dt.timezone(dt.timedelta(hours=7))

DEFAULTS = dict(mode="long", fast=12, slow=26, smooth=1, atr_len=14, atr_mult=2.0, rr=0.0, trail=False,
                risk=1.0, equity=100000.0, years=8, fee=0.15, slip=0.05)


def fname(sym):
    return re.sub(r"[^A-Za-z0-9._-]", "_", sym) + ".json"  # ต้องตรงกับ fname() ใน docs/index.html


def dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(A.clean(obj), f, ensure_ascii=False, separators=(",", ":"))


def load(sym, years):
    df = None
    for k in range(3):  # Yahoo บางครั้งตอบว่าง ลองใหม่ได้
        df = B.fetch(sym, years)
        if df is not None:
            break
        time.sleep(2 * (k + 1))
    if df is not None and sym.endswith("-USD") and len(df) and df.index[-1].date() >= dt.datetime.now(dt.timezone.utc).date():
        df = df.iloc[:-1]  # คริปโตแท่งวันนี้ (UTC) ยังไม่ปิด ตัดทิ้งเพื่อไม่ให้สัญญาณกระพริบ
    return df


def chart_data(sym, df, p, bars):
    d, eq, t, inpos = A.run(df, p)
    tt = [i.strftime("%Y-%m-%d") for i in d.index]
    s0 = max(0, len(d) - bars)
    first = tt[s0]
    marks = []
    for r in t.itertuples():
        et, xt = r.entry_date.strftime("%Y-%m-%d"), r.exit_date.strftime("%Y-%m-%d")
        if et >= first:
            marks.append({"time": et, "kind": "in", "side": r.side})
        if r.reason != "END" and xt >= first:
            marks.append({"time": xt, "kind": "out", "side": r.side, "win": bool(r.ret_pct > 0),
                          "text": f"{r.reason} {r.ret_pct:+.1f}%"})
    op = t.attrs.get("open")
    rd = lambda v: round(float(v), 4)
    return {
        "symbol": sym,
        "candles": [{"time": tt[i], "open": rd(r.Open), "high": rd(r.High), "low": rd(r.Low), "close": rd(r.Close),
                     "zone": str(r.Zone)} for i, r in enumerate(d.itertuples()) if i >= s0],
        "fast": [{"time": tt[i], "value": rd(v)} for i, v in enumerate(d["Fast"]) if i >= s0],
        "slow": [{"time": tt[i], "value": rd(v)} for i, v in enumerate(d["Slow"]) if i >= s0],
        "marks": marks,
        "stop": op["stop"] if op else None, "entry": op["entry"] if op else None,
    }


def send_alerts(rows, p):
    path = os.path.join(DATA, "alerts_sent.json")
    try:
        with open(path, encoding="utf-8") as f:
            sent = list(json.load(f))
    except Exception:
        sent = []
    for r in rows:
        key = f"{r['symbol']}|{r['date']}|{r['sig']}"
        if r["sig"] and r["ago"] == 0 and key not in sent:
            msg = f"CDC V3 {r['sig']}: {r['symbol']} @ {r['close']:.2f}"
            if r["sig"] == "BUY" and r["plan"]:
                pl = r["plan"]
                msg += f" | SL {pl['stop']:.2f} (-{pl['stop_pct']:.1f}%) | {pl['shares']} หุ้น (risk {p.risk}%)"
            try:
                A.notify(msg)
                sent.append(key)
            except Exception as e:
                print("alert error:", e)
    dump(path, sent[-500:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--no-alert", action="store_true")
    a = ap.parse_args()

    with open(a.config, encoding="utf-8") as f:
        cfg = json.load(f)
    syms = list(dict.fromkeys(s.strip().upper() for s in cfg["symbols"] if s.strip()))
    prm = {**DEFAULTS, **cfg.get("params", {})}
    p = SimpleNamespace(**prm)
    if p.mode not in ("long", "short", "both") or p.fast >= p.slow:
        sys.exit("พารามิเตอร์ไม่ถูกต้อง (mode ต้องเป็น long/short/both และ fast < slow)")
    bars = int(cfg.get("chart_bars", 750))

    print(f"ดึงข้อมูล {len(syms)} ตัว ({p.years} ปี)...")
    with ThreadPoolExecutor(6) as ex:
        dfs = list(ex.map(lambda s: load(s, p.years), syms))

    rows, failed = [], []
    for s, df in zip(syms, dfs):
        try:
            if df is None:
                raise ValueError("no data")
            rows.append(A.analyse(s, df, p))
            dump(os.path.join(CHART, fname(s)), chart_data(s, df, p, bars))
        except Exception as e:
            failed.append(s)
            print(f"skip {s}: {e}")
    if not rows:
        sys.exit("ไม่มีข้อมูลเลย — ไม่เขียนทับไฟล์เดิม")

    keep = {fname(r["symbol"]) for r in rows}
    for fn in os.listdir(CHART):  # ลบไฟล์กราฟของหุ้นที่เอาออกจาก config แล้ว
        if fn.endswith(".json") and fn not in keep and fn.replace(".json", "") not in {fname(s)[:-5] for s in failed}:
            os.remove(os.path.join(CHART, fn))

    dump(os.path.join(DATA, "screen.json"),
         {"updated": dt.datetime.now(ICT).strftime("%Y-%m-%d %H:%M"), "params": prm, "rows": rows, "failed": failed})
    print(f"สำเร็จ {len(rows)} ตัว" + (f" · ล้มเหลว: {', '.join(failed)}" if failed else ""))
    if not a.no_alert:
        send_alerts(rows, p)


if __name__ == "__main__":
    main()
