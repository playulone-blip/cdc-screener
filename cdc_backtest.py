#!/usr/bin/env python3
"""
CDC Action Zone V3 — ระบบ Backtest (พอร์ตจาก Pine v5 ของ piriya33)

ตัวอย่าง:
  python cdc_backtest.py                                  # ทั้ง watchlist, Long only, 10 ปี
  python cdc_backtest.py --symbols PTT.BK AOT.BK AAPL --years 8
  python cdc_backtest.py --mode both --sl 8 --tp 25       # Long+Short, ตัดขาดทุน 8% / ทำกำไร 25%
  python cdc_backtest.py --start 2020-01-01 --end 2024-12-31
  python cdc_backtest.py --grid                           # ไล่หา EMA fast/slow ที่ดีที่สุด (in-sample!)

ข้อสมมติ: สัญญาณเกิดเมื่อ "แท่งปิด" แล้วเข้า/ออกที่ราคาเปิดแท่งถัดไป (ตรงกับ label "BUY next bar"),
เข้าเต็มพอร์ตต่อหุ้น 1 ตัว (all-in, ทบต้น), หักค่าคอมและ slippage ทุกครั้งที่ซื้อ/ขาย
"""
import argparse, copy, itertools
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

try:
    from cdc_scanner import WATCHLIST
except Exception:
    WATCHLIST = ["BTC-USD", "ETH-USD", "BNB-USD", "SOL-USD", "AAPL", "TSLA", "STX", "NVDA", "GOOG"]

TRADE_COLS = ["side", "entry_date", "entry", "exit_date", "exit", "bars", "pnl", "ret_pct", "reason"]


# ----------------------------------------------------------------------------
# 1) Indicator: CDC ActionZone V3 (ตรงกับ Pine)
# ----------------------------------------------------------------------------
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()  # seed ด้วยค่าแรก เหมือน ta.ema


def cdc_v3(df, fast=12, slow=26, smooth=1, atr_len=14):
    price = ema(df["Close"], smooth)
    f, s = ema(price, fast), ema(price, slow)
    bull, bear = f > s, f < s
    green = bull & (price > f)
    red = bear & (price < f)
    zone = np.select(
        [green, bear & (price > f) & (price > s), bear & (price > f) & (price < s),
         red, bull & (price < f) & (price < s), bull & (price < f) & (price > s)],
        ["Green", "Blue", "LBlue", "Red", "Orange", "Yellow"], "Neutral")

    # buycond = Green and Green[1]==0 ; sellcond = Red and Red[1]==0 (แท่งแรกเป็น na → false)
    buycond = (green & ~green.shift(1, fill_value=True)).values
    sellcond = (red & ~red.shift(1, fill_value=True)).values

    # bullish = barssince(buycond) < barssince(sellcond) (ต้องเกิดครบทั้งสองฝั่งก่อน ไม่งั้น na → false)
    n = len(df)
    bullish, bearish = np.zeros(n, bool), np.zeros(n, bool)
    lb = ls = -1
    for i in range(n):
        if buycond[i]: lb = i
        if sellcond[i]: ls = i
        if lb >= 0 and ls >= 0:
            bullish[i], bearish[i] = lb > ls, ls > lb

    out = df.copy()
    out["Fast"], out["Slow"], out["Zone"] = f, s, zone
    pc = df["Close"].shift(1)  # ATR แบบ Wilder (RMA) เหมือน ta.atr
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    out["ATR"] = tr.ewm(alpha=1 / atr_len, adjust=False).mean()
    out["Buy"] = np.r_[False, bearish[:-1]] & buycond    # buy  = bearish[1] and buycond
    out["Sell"] = np.r_[False, bullish[:-1]] & sellcond  # sell = bullish[1] and sellcond
    return out


# ----------------------------------------------------------------------------
# 2) Backtest engine
# ----------------------------------------------------------------------------
def backtest(d, mode="long", fee=0.0015, slip=0.0005, capital=100000.0, sl=None, tp=None, next_open=True,
             atr_mult=0.0, rr=0.0, trail=False, risk=0.0):
    """atr_mult>0 : stop = ราคาเข้า ∓ atr_mult×ATR (ATR ณ แท่งสัญญาณ ล็อกไว้) — ตรงกับ Pine "ATR Stop"
       rr>0       : take profit = rr × ระยะ stop
       trail      : เมื่อกำไร +1R แล้ว stop ตามหลังจุดสูงสุด/ต่ำสุดห่าง 1R
       risk>0     : ขนาดไม้ = equity × risk / ระยะ stop (เช่น 0.01 = 1%) ไม่เกินเงินที่มี
       คืนค่า trades.attrs['open'] = สถานะที่ถืออยู่ ณ แท่งสุดท้าย (ถ้ามี)"""
    o, h, l, c = (d[k].values for k in ("Open", "High", "Low", "Close"))
    buy, sell, at = d["Buy"].values, d["Sell"].values, d["ATR"].values
    n = len(d)
    cash, pos, sh, entry, eq0, ei = float(capital), 0, 0.0, 0.0, 0.0, 0
    sdist, ext, tron = 0.0, 0.0, False
    eq, inpos, trades, pend, pdist = np.empty(n), np.zeros(n, bool), [], 0, 0.0

    def close_pos(i, px, why):
        nonlocal cash, pos, sh
        xp = px * (1 - slip) if pos == 1 else px * (1 + slip)
        cash += sh * xp * (1 - fee) if pos == 1 else -sh * xp * (1 + fee)
        trades.append(dict(side="LONG" if pos == 1 else "SHORT", entry_date=d.index[ei], entry=entry,
                           exit_date=d.index[i], exit=xp, bars=i - ei, pnl=cash - eq0,
                           ret_pct=(cash / eq0 - 1) * 100, reason=why))
        pos, sh = 0, 0.0

    def open_pos(i, px, side, dist):
        nonlocal cash, pos, sh, entry, eq0, ei, sdist, ext, tron
        eq0, ei, pos, sdist, tron = cash, i, side, dist, False
        entry = px * (1 + slip) if side == 1 else px * (1 - slip)
        full = cash / (entry * (1 + fee)) if side == 1 else cash / entry
        sh = min(full, cash * risk / dist) if (risk and dist) else full
        cash += -sh * entry * (1 + fee) if side == 1 else sh * entry * (1 - fee)
        ext = entry

    def act(i, px, sig, dist):
        if sig == 1:
            if pos == -1: close_pos(i, px, "SIGNAL")
            if pos == 0 and mode in ("long", "both"): open_pos(i, px, 1, dist)
        else:
            if pos == 1: close_pos(i, px, "SIGNAL")
            if pos == 0 and mode in ("short", "both"): open_pos(i, px, -1, dist)

    for i in range(n):
        if next_open and pend:
            act(i, o[i], pend, pdist)
            pend = 0
        if pos and sdist:  # ATR stop / TP / trailing (เช็ค stop ด้วยระดับก่อนอัปเดตแท่งนี้ = conservative)
            if pos == 1:
                stp = max(entry - sdist, ext - sdist) if tron else entry - sdist
                tpl = entry + sdist * rr if rr else None
                if l[i] <= stp: close_pos(i, min(o[i], stp), "TRAIL" if tron else "SL")
                elif tpl and h[i] >= tpl: close_pos(i, max(o[i], tpl), "TP")
                else:
                    ext = max(ext, h[i])
                    tron = bool(tron or (trail and h[i] >= entry + sdist))
            else:
                stp = min(entry + sdist, ext + sdist) if tron else entry + sdist
                tpl = entry - sdist * rr if rr else None
                if h[i] >= stp: close_pos(i, max(o[i], stp), "TRAIL" if tron else "SL")
                elif tpl and l[i] <= tpl: close_pos(i, min(o[i], tpl), "TP")
                else:
                    ext = min(ext, l[i])
                    tron = bool(tron or (trail and l[i] <= entry - sdist))
        elif pos and (sl or tp):  # SL/TP แบบ % (ถ้าไม่ได้ใช้ ATR)
            if pos == 1:
                if sl and l[i] <= entry * (1 - sl): close_pos(i, min(o[i], entry * (1 - sl)), "SL")
                elif tp and h[i] >= entry * (1 + tp): close_pos(i, max(o[i], entry * (1 + tp)), "TP")
            else:
                if sl and h[i] >= entry * (1 + sl): close_pos(i, max(o[i], entry * (1 + sl)), "SL")
                elif tp and l[i] <= entry * (1 - tp): close_pos(i, min(o[i], entry * (1 - tp)), "TP")
        sig = 1 if buy[i] else (-1 if sell[i] else 0)
        dist_now = atr_mult * at[i] if (atr_mult and not np.isnan(at[i])) else 0.0
        if sig and not next_open:
            act(i, c[i], sig, dist_now)
        eq[i] = cash + pos * sh * c[i]
        inpos[i] = pos != 0
        if sig and next_open:
            pend, pdist = sig, dist_now

    state = None
    if pos:  # ปิดสถานะค้างท้ายช่วงทดสอบ (mark-to-market)
        if sdist:
            stp = (max(entry - sdist, ext - sdist) if tron else entry - sdist) if pos == 1 else \
                  (min(entry + sdist, ext + sdist) if tron else entry + sdist)
        else:
            stp = None
        state = {"side": "LONG" if pos == 1 else "SHORT", "entry": entry, "entry_date": d.index[ei],
                 "stop": stp, "bars": n - 1 - ei, "trailing": bool(tron)}
        close_pos(n - 1, c[n - 1], "END")
        eq[-1] = cash
    tdf = pd.DataFrame(trades, columns=TRADE_COLS)
    tdf.attrs["open"] = state
    return pd.Series(eq, index=d.index), tdf, inpos


# ----------------------------------------------------------------------------
# 3) Metrics
# ----------------------------------------------------------------------------
def curve_stats(eq):
    r = eq.pct_change().dropna()
    yrs = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    tot = eq.iloc[-1] / eq.iloc[0] - 1
    return {"Return%": tot * 100,
            "CAGR%": ((1 + tot) ** (1 / yrs) - 1) * 100 if tot > -1 else -100.0,
            "MaxDD%": (eq / eq.cummax() - 1).min() * 100,
            "Sharpe": r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0.0}


def metrics(eq, t, inpos, close):
    m, b = curve_stats(eq), curve_stats(close)
    n = len(t)
    gp, gl = t.loc[t.pnl > 0, "pnl"].sum(), -t.loc[t.pnl <= 0, "pnl"].sum()
    m.update({"B&H%": b["Return%"], "B&H CAGR%": b["CAGR%"], "B&H MaxDD%": b["MaxDD%"],
              "Trades": n, "Win%": (t.pnl > 0).mean() * 100 if n else 0.0,
              "PF": gp / gl if gl > 0 else (np.inf if gp > 0 else 0.0),
              "Avg%": t.ret_pct.mean() if n else 0.0, "AvgBars": t.bars.mean() if n else 0.0,
              "Exposure%": inpos.mean() * 100})
    return m


def run_all(raw, p):
    rows, trs, curves, bh = [], [], {}, {}
    for sym, df in raw.items():
        d = cdc_v3(df, p.fast, p.slow, p.smooth, getattr(p, "atr_len", 14)).loc[p.start:p.end]
        if len(d) < 60:
            continue
        eq, t, inpos = backtest(d, p.mode, p.fee / 100, p.slip / 100, p.capital,
                                p.sl / 100 if p.sl else None, p.tp / 100 if p.tp else None, not p.same_bar,
                                getattr(p, "atr_mult", 0.0), getattr(p, "rr", 0.0), getattr(p, "trail", False),
                                getattr(p, "risk", 0.0) / 100)
        rows.append({"Symbol": sym, **metrics(eq, t, inpos, d["Close"])})
        t["Symbol"] = sym
        trs.append(t)
        curves[sym], bh[sym] = eq, d["Close"]
    if not rows:
        return pd.DataFrame(), pd.DataFrame(), None, None
    idx = pd.DatetimeIndex(sorted(set().union(*[c.index for c in curves.values()])))

    def comb(cs):  # เฉลี่ยแบบ equal-weight: แต่ละตัวมีเงินก้อนของตัวเอง (ไม่ rebalance)
        return pd.concat({k: (v / v.iloc[0]).reindex(idx).ffill().fillna(1.0) for k, v in cs.items()},
                         axis=1).mean(axis=1) * p.capital
    return pd.DataFrame(rows), pd.concat(trs, ignore_index=True), comb(curves), comb(bh)


# ----------------------------------------------------------------------------
# 4) Data + report
# ----------------------------------------------------------------------------
def fetch(sym, years):
    try:
        import yfinance as yf
        import datetime as dt
        start = (dt.date.today() - dt.timedelta(days=int(years * 365.25) + 5)).isoformat()  # period="8y" ใช้ไม่ได้ใน yfinance
        df = yf.Ticker(sym).history(start=start, interval="1d", auto_adjust=True)
        if df is None or len(df) < 80:
            print(f"skip {sym}: ไม่พบข้อมูล (ตรวจชื่อ symbol เช่น PTT.BK) หรือข้อมูลน้อยกว่า 80 แท่ง")
            return None
        df.index = df.index.tz_localize(None).normalize()
        return df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    except Exception as e:
        print(f"skip {sym}: {e}")
        return None


CSS = """:root{--bg:#fff;--fg:#1a1a1a;--mut:#6b7280;--card:#f6f7f9;--bd:#e3e5e9}
@media(prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e8e8ea;--mut:#9aa0a6;--card:#1d2026;--bd:#2c3038}}
body{margin:0;padding:20px;background:var(--bg);color:var(--fg);font:14px system-ui,"Noto Sans Thai",sans-serif}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:24px 0 8px}.mut{color:var(--mut)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:10px 14px}
.card b{display:block;font-size:20px;margin-top:2px}
.box{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px}
.tw{overflow-x:auto;background:var(--card);border:1px solid var(--bd);border-radius:10px}
table{border-collapse:collapse;width:100%}th,td{padding:6px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--bd)}
th{color:var(--mut);font-weight:500}th:first-child,td:first-child{text-align:left}"""


def write_report(path, p, summary, trades, port, bh):
    ps, bs = curve_stats(port), curve_stats(bh)
    lo, hi = min(port.min(), bh.min()), max(port.max(), bh.max())
    pts = lambda s: " ".join(f"{i / (len(s) - 1) * 900:.1f},{300 - (v - lo) / (hi - lo + 1e-9) * 300:.1f}"
                             for i, v in enumerate(s.values))
    svg = ('<svg viewBox="0 0 900 300" preserveAspectRatio="none" style="width:100%;height:300px">'
           f'<polyline fill="none" stroke="#7f8c8d" stroke-width="1.5" vector-effect="non-scaling-stroke" points="{pts(bh)}"/>'
           f'<polyline fill="none" stroke="#2ecc71" stroke-width="2" vector-effect="non-scaling-stroke" points="{pts(port)}"/></svg>')
    card = lambda k, v: f'<div class="card"><span class="mut">{k}</span><b>{v}</b></div>'
    fmt = lambda x: x.round({c: 2 for c in x.select_dtypes("number").columns}).to_html(index=False, border=0)
    recent = trades.sort_values("exit_date", ascending=False).head(100)[["Symbol"] + TRADE_COLS]
    assume = (f"โหมด {p.mode} · EMA {p.fast}/{p.slow} · ค่าคอม {p.fee}%/ข้าง · slippage {p.slip}% · "
              f"SL {p.sl or '-'}% · TP {p.tp or '-'}% · ATR stop {getattr(p, 'atr_mult', 0) or '-'}× · เข้าที่ {'ราคาปิดแท่งสัญญาณ' if p.same_bar else 'ราคาเปิดแท่งถัดไป'} · "
              f"{len(summary)} หุ้น equal-weight")
    body = (f'<h1>CDC Action Zone V3 — Backtest</h1><div class="mut">{assume}</div><div class="cards">'
            + card("CAGR กลยุทธ์", f"{ps['CAGR%']:.1f}%") + card("CAGR ถือยาว", f"{bs['CAGR%']:.1f}%")
            + card("Max DD กลยุทธ์", f"{ps['MaxDD%']:.1f}%") + card("Max DD ถือยาว", f"{bs['MaxDD%']:.1f}%")
            + card("Sharpe", f"{ps['Sharpe']:.2f}") + card("จำนวนเทรด", f"{len(trades)}") + "</div>"
            f'<div class="box">{svg}<div class="mut">เขียว = กลยุทธ์ CDC · เทา = ซื้อถือ (equal-weight) · '
            f'{port.index[0]:%Y-%m-%d} → {port.index[-1]:%Y-%m-%d} · {lo:,.0f} – {hi:,.0f}</div></div>'
            f'<h2>สรุปรายหุ้น</h2><div class="tw">{fmt(summary)}</div>'
            f'<h2>เทรดล่าสุด 100 รายการ</h2><div class="tw">{fmt(recent)}</div>'
            '<p class="mut">ผลย้อนหลังไม่ได้รับประกันอนาคต · ไม่ใช่คำแนะนำการลงทุน</p>')
    with open(path, "w", encoding="utf-8") as f:
        f.write(f'<!doctype html><html lang="th"><head><meta charset="utf-8"><meta name="viewport" '
                f'content="width=device-width,initial-scale=1"><title>CDC Backtest</title><style>{CSS}</style></head><body>{body}</body></html>')


def grid(raw, p):
    rows = []
    for f, s in itertools.product([8, 10, 12, 15, 20], [21, 26, 34, 50, 89]):
        if f >= s:
            continue
        q = copy.copy(p)
        q.fast, q.slow = f, s
        sm = run_all(raw, q)[0]
        if len(sm):
            rows.append({"Fast": f, "Slow": s, "AvgCAGR%": sm["CAGR%"].mean(), "AvgSharpe": sm["Sharpe"].mean(),
                         "AvgMaxDD%": sm["MaxDD%"].mean(), "AvgWin%": sm["Win%"].mean(), "Trades": int(sm["Trades"].sum())})
    g = pd.DataFrame(rows).sort_values("AvgSharpe", ascending=False)
    print("\n=== Grid search (เรียงตาม Sharpe เฉลี่ย) ===")
    print(g.round(2).head(12).to_string(index=False))
    print("\n⚠ เป็นผล in-sample: ค่าที่ดีที่สุดอาจ overfit ควรทดสอบซ้ำคนละช่วงเวลา (--start/--end) ก่อนเชื่อ")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--mode", choices=["long", "short", "both"], default="long")
    ap.add_argument("--fast", type=int, default=12); ap.add_argument("--slow", type=int, default=26)
    ap.add_argument("--smooth", type=int, default=1)
    ap.add_argument("--fee", type=float, default=0.15, help="ค่าคอมต่อข้าง (%%)")
    ap.add_argument("--slip", type=float, default=0.05, help="slippage ต่อข้าง (%%)")
    ap.add_argument("--sl", type=float, default=0, help="stop-loss %% (0 = ปิด)")
    ap.add_argument("--tp", type=float, default=0, help="take-profit %% (0 = ปิด)")
    ap.add_argument("--capital", type=float, default=100000)
    ap.add_argument("--atr-mult", dest="atr_mult", type=float, default=0, help="ATR stop = k × ATR (0 = ปิด)")
    ap.add_argument("--atr-len", dest="atr_len", type=int, default=14)
    ap.add_argument("--rr", type=float, default=0, help="take profit เป็นกี่เท่าของระยะ stop (0 = ปิด)")
    ap.add_argument("--trail", action="store_true", help="trailing stop หลังกำไร +1R")
    ap.add_argument("--risk", type=float, default=0, help="เสี่ยงกี่ %% ของ equity ต่อไม้ (0 = all-in)")
    ap.add_argument("--same-bar", action="store_true", help="เข้า/ออกที่ราคาปิดแท่งสัญญาณ (ปกติ = เปิดแท่งถัดไป)")
    ap.add_argument("--grid", action="store_true")
    p = ap.parse_args()

    syms = p.symbols or WATCHLIST
    print(f"ดึงข้อมูล {len(syms)} ตัว ({p.years} ปี)...")
    with ThreadPoolExecutor(8) as ex:
        raw = {s: d for s, d in zip(syms, ex.map(lambda s: fetch(s, p.years), syms)) if d is not None}
    if not raw:
        raise SystemExit("ดึงข้อมูลไม่สำเร็จ")
    if p.grid:
        return grid(raw, p)

    summary, trades, port, bh = run_all(raw, p)
    if summary.empty:
        raise SystemExit("ข้อมูลไม่พอสำหรับ backtest")
    summary = summary.sort_values("CAGR%", ascending=False)
    pd.set_option("display.width", 220)
    print("\n=== สรุปรายหุ้น ===")
    print(summary.round(2).to_string(index=False))
    ps, bs = curve_stats(port), curve_stats(bh)
    print(f"\n=== พอร์ต equal-weight ({len(summary)} หุ้น) ===")
    print(f"กลยุทธ์ : CAGR {ps['CAGR%']:.1f}%  MaxDD {ps['MaxDD%']:.1f}%  Sharpe {ps['Sharpe']:.2f}")
    print(f"ซื้อถือ  : CAGR {bs['CAGR%']:.1f}%  MaxDD {bs['MaxDD%']:.1f}%  Sharpe {bs['Sharpe']:.2f}")
    summary.to_csv("cdc_bt_summary.csv", index=False)
    trades.to_csv("cdc_bt_trades.csv", index=False)
    write_report("cdc_bt_report.html", p, summary, trades, port, bh)
    print("\nบันทึก: cdc_bt_summary.csv, cdc_bt_trades.csv, cdc_bt_report.html")


if __name__ == "__main__":
    main()