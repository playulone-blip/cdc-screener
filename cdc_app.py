"""
CDC ActionZone V3 + ATR Stop — Web Screener (ดึงราคาจริงผ่าน yfinance)

ต้องมี cdc_backtest.py (เวอร์ชันล่าสุด) อยู่โฟลเดอร์เดียวกัน  (cdc_scanner.py ไม่จำเป็น)
    pip install yfinance pandas numpy requests
    python cdc_app.py                    # เปิด http://localhost:8000
    python cdc_app.py --alert-every 60   # สแกน watchlist ทุก 60 นาที แล้วส่ง Telegram เมื่อมี BUY/SELL วันนี้
Telegram: ตั้ง env TG_TOKEN และ TG_CHAT_ID
"""
import json, math, re, os, time, argparse, threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs

import numpy as np
import cdc_backtest as B

RAW, LOCK, TTL = {}, threading.Lock(), 300  # แคชราคา 5 นาที


def clean(o):
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


def get_raw(sym, years):
    key = (sym, years)
    with LOCK:
        hit = RAW.get(key)
        if hit and time.time() - hit[0] < TTL:
            return hit[1]
    df = B.fetch(sym, years)
    with LOCK:
        RAW[key] = (time.time(), df)
    return df


def parse(q):
    g = lambda k, d, t=float: t(q.get(k, [d])[0])
    p = SimpleNamespace(
        mode=q.get("mode", ["long"])[0], fast=g("fast", 12, int), slow=g("slow", 26, int), smooth=g("smooth", 1, int),
        atr_len=g("atr_len", 14, int), atr_mult=g("atr_mult", 2.0), rr=g("rr", 0.0), trail=g("trail", 0, int) == 1,
        risk=g("risk", 1.0), equity=g("equity", 100000.0), years=g("years", 8, int),
        fee=g("fee", 0.15), slip=g("slip", 0.05))
    if p.mode not in ("long", "short", "both") or p.fast >= p.slow:
        raise ValueError("พารามิเตอร์ไม่ถูกต้อง (EMA fast ต้องน้อยกว่า slow)")
    return p


def symbols(q):
    s = [x.strip().upper() for x in re.split(r"[,\s]+", q.get("symbols", [""])[0]) if x.strip()]
    return s or B.WATCHLIST


def run(df, p):
    d = B.cdc_v3(df, p.fast, p.slow, p.smooth, p.atr_len)
    eq, t, inpos = B.backtest(d, p.mode, p.fee / 100, p.slip / 100, p.equity, None, None, True,
                              p.atr_mult, p.rr, p.trail, p.risk / 100)
    return d, eq, t, inpos


def analyse(sym, df, p):
    d, eq, t, inpos = run(df, p)
    m = B.metrics(eq, t, inpos, d["Close"])
    last, n = d.iloc[-1], len(d)
    bi, si = np.flatnonzero(d["Buy"].values), np.flatnonzero(d["Sell"].values)
    lb, ls = (int(bi[-1]) if len(bi) else -1), (int(si[-1]) if len(si) else -1)
    if lb < 0 and ls < 0:
        sig, ago = None, None
    elif lb > ls:
        sig, ago = "BUY", n - 1 - lb
    else:
        sig, ago = "SELL", n - 1 - ls

    close, atr = float(last["Close"]), float(last["ATR"])
    dist = p.atr_mult * atr
    plan = None
    if dist > 0:  # แผนถ้าเข้า ณ ราคาปัจจุบัน
        short = p.mode == "short" or (sig == "SELL" and p.mode == "both")
        sg = -1 if short else 1
        qty = p.equity / close
        if p.risk > 0:
            qty = min(qty, p.equity * p.risk / 100 / dist)
        plan = {"side": "SHORT" if short else "LONG", "stop": close - sg * dist, "stop_pct": dist / close * 100,
                "tp": close + sg * dist * p.rr if p.rr else None, "shares": int(qty)}
    op, pos = t.attrs.get("open"), None
    if op:
        sg = 1 if op["side"] == "LONG" else -1
        pos = {"side": op["side"], "entry": op["entry"], "entry_date": op["entry_date"].strftime("%Y-%m-%d"),
               "stop": op["stop"], "bars": int(op["bars"]), "trailing": bool(op["trailing"]),
               "pnl_pct": sg * (close / op["entry"] - 1) * 100}
    return {"symbol": sym, "date": d.index[-1].strftime("%Y-%m-%d"), "close": close, "zone": str(last["Zone"]),
            "atr": atr, "sig": sig, "ago": ago, "plan": plan, "pos": pos,
            "cagr": m["CAGR%"], "ret": m["Return%"], "maxdd": m["MaxDD%"], "win": m["Win%"], "pf": m["PF"],
            "sharpe": m["Sharpe"], "trades": int(m["Trades"]), "bh": m["B&H CAGR%"]}


def api_screen(p, syms):
    with ThreadPoolExecutor(8) as ex:
        dfs = list(ex.map(lambda s: get_raw(s, p.years), syms))
    rows, failed = [], []
    for s, df in zip(syms, dfs):
        try:
            if df is None:
                raise ValueError("no data")
            rows.append(analyse(s, df, p))
        except Exception as e:
            failed.append(s)
            print(f"skip {s}: {e}")
    return {"updated": time.strftime("%H:%M:%S"), "rows": rows, "failed": failed}


def api_chart(sym, p):
    df = get_raw(sym, p.years)
    if df is None:
        raise ValueError("ไม่พบข้อมูลของ " + sym)
    d, eq, t, inpos = run(df, p)
    tt = [i.strftime("%Y-%m-%d") for i in d.index]
    marks = []
    for r in t.itertuples():
        marks.append({"time": r.entry_date.strftime("%Y-%m-%d"), "kind": "in", "side": r.side})
        if r.reason != "END":
            marks.append({"time": r.exit_date.strftime("%Y-%m-%d"), "kind": "out", "side": r.side,
                          "win": bool(r.ret_pct > 0), "text": f"{r.reason} {r.ret_pct:+.1f}%"})
    op = t.attrs.get("open")
    return {
        "symbol": sym,
        "candles": [{"time": tt[i], "open": float(r.Open), "high": float(r.High), "low": float(r.Low),
                     "close": float(r.Close), "zone": str(r.Zone)} for i, r in enumerate(d.itertuples())],
        "fast": [{"time": tt[i], "value": float(v)} for i, v in enumerate(d["Fast"])],
        "slow": [{"time": tt[i], "value": float(v)} for i, v in enumerate(d["Slow"])],
        "marks": marks,
        "stop": op["stop"] if op else None, "entry": op["entry"] if op else None,
    }


PAGE = """<!doctype html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CDC V3 + ATR Screener</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--mut:#6b7280;--card:#f6f7f9;--bd:#e3e5e9;--hl:rgba(46,204,113,.14)}
@media(prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e8e8ea;--mut:#9aa0a6;--card:#1d2026;--bd:#2c3038;--hl:rgba(46,204,113,.16)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px system-ui,"Noto Sans Thai",sans-serif}
header{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--bd)}
h1{font-size:16px;margin:0 8px 0 0}
input,select,button,textarea{background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:6px;padding:6px 10px;font:inherit}
button{cursor:pointer}button.pri{background:#2ecc71;color:#06260f;border-color:#2ecc71;font-weight:600}
.params{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px;padding:12px 16px 0}
.params label{display:flex;flex-direction:column;gap:3px;color:var(--mut);font-size:12px}
.params input,.params select{width:100%;color:var(--fg)}.params .chk{flex-direction:row;align-items:center;gap:6px}.params .chk input{width:auto}
.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:10px 16px}
textarea{width:100%;min-height:44px;resize:vertical}
main{display:grid;grid-template-columns:minmax(560px,1.1fr) minmax(360px,1fr);gap:12px;padding:0 16px 16px}
@media(max-width:1100px){main{grid-template-columns:1fr}}
.panel{background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:hidden}
.tw{max-height:78vh;overflow:auto}
table{width:100%;border-collapse:collapse}
th,td{padding:7px 9px;text-align:right;border-bottom:1px solid var(--bd);white-space:nowrap}
th:first-child,td:first-child,th:nth-child(3),td:nth-child(3){text-align:left}
th{position:sticky;top:0;background:var(--card);color:var(--mut);font-weight:500;cursor:pointer;user-select:none}
tr.r{cursor:pointer}tr.r:hover,tr.sel{background:rgba(128,128,128,.16)}tr.fresh{background:var(--hl)}
.z{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;color:#111}
#chart{height:520px}.mut{color:var(--mut)}.ch{padding:10px 14px;border-bottom:1px solid var(--bd)}
.up{color:#2ecc71}.dn{color:#e74c3c}
</style></head><body>
<header><h1>CDC V3 + ATR Screener</h1><button class="pri" id="scan">สแกน</button><span id="st" class="mut"></span><span id="cnt" class="mut"></span></header>
<section class="params" id="params"></section>
<div class="row"><textarea id="syms" placeholder="รายชื่อหุ้น คั่นด้วย , หรือเว้นวรรค เช่น PTT.BK AOT.BK AAPL BTC-USD  (เว้นว่าง = ใช้ watchlist เริ่มต้น)"></textarea></div>
<div class="row">
<select id="fl"><option value="all">ทั้งหมด</option><option value="b0">BUY วันนี้</option><option value="b3">BUY ภายใน 3 แท่ง</option><option value="hold">กำลังถืออยู่</option></select>
<input id="q" placeholder="ค้นหาในผล" style="width:130px">
<label class="mut">เทรดขั้นต่ำ <input id="mt" type="number" value="0" style="width:64px"></label>
<input id="cs" placeholder="ดูกราฟ: PTT.BK" style="width:140px"><button id="go">ดูกราฟ</button>
</div>
<main>
<div class="panel tw"><table><thead><tr id="th"></tr></thead><tbody id="tb"></tbody></table></div>
<div class="panel"><div class="ch" id="ct">เลือกหุ้นจากตาราง</div><div id="chart"></div>
<div class="ch mut" style="border-top:1px solid var(--bd);border-bottom:0">สีแท่ง = โซน CDC V3 · แดง/น้ำเงิน = EMA Fast/Slow · ลูกศร = จุดเข้า · วงกลม = จุดออก (SL/TRAIL/TP/SIGNAL) · เส้นประส้ม = ATR stop ของไม้ที่ถืออยู่ · แถวพื้นเขียว = BUY วันนี้ (เข้าที่ราคาเปิดวันถัดไป) · ระหว่างตลาดเปิด แท่งล่าสุดยังไม่ปิด สัญญาณอาจเปลี่ยน</div></div>
</main>
<script>
const $=id=>document.getElementById(id);
const ZC={Green:'#2ecc71',Blue:'#3498db',LBlue:'#00bcd4',Red:'#e74c3c',Orange:'#f39c12',Yellow:'#f1c40f',Neutral:'#9aa0a6'};
const SPEC=[
 ['mode','โหมด','sel',[['long','Long only'],['short','Short only'],['both','Both']],'long'],
 ['fast','EMA Fast','num',12],['slow','EMA Slow','num',26],['smooth','Smoothing','num',1],
 ['atr_len','ATR length','num',14],['atr_mult','ATR stop × (0=ปิด)','num',2],['rr','TP (R, 0=ปิด)','num',0],
 ['trail','Trailing หลัง +1R','chk',0],['risk','Risk %/ไม้ (0=all-in)','num',1],['equity','เงินทุน','num',100000],
 ['years','ย้อนหลัง (ปี)','sel',[[3,'3'],[5,'5'],[8,'8'],[10,'10']],8],['fee','ค่าคอม %/ข้าง','num',0.15],['slip','Slippage %','num',0.05]];
$('params').innerHTML=SPEC.map(s=>{const id=s[0],lb=s[1],t=s[2];
 if(t==='sel')return `<label>${lb}<select id="${id}">${s[3].map(o=>`<option value="${o[0]}">${o[1]}</option>`).join('')}</select></label>`;
 if(t==='chk')return `<label class="chk"><input id="${id}" type="checkbox"> ${lb}</label>`;
 return `<label>${lb}<input id="${id}" type="number" step="any"></label>`}).join('');
let saved=null;try{saved=JSON.parse(localStorage.getItem('cdcv3')||'null')}catch(e){}
SPEC.forEach(s=>{const id=s[0],t=s[2],el=$(id),d=t==='sel'?s[4]:s[3],x=(saved&&saved[id]!==undefined)?saved[id]:d;
 if(t==='chk')el.checked=!!+x;else el.value=x});
$('syms').value=(saved&&saved.symbols)||'';
const val=id=>{const el=$(id);return el.type==='checkbox'?(el.checked?1:0):el.value};
const vals=()=>({...Object.fromEntries(SPEC.map(s=>[s[0],val(s[0])])),symbols:$('syms').value});
const qs=()=>new URLSearchParams(vals()).toString();
const save=()=>{try{localStorage.setItem('cdcv3',JSON.stringify(vals()))}catch(e){}};
async function api(u){const r=await fetch(u),j=await r.json();if(!r.ok)throw new Error(j.error||r.status);return j}

const f=(x,d=1)=>x==null?'-':Number(x).toFixed(d);
const fp=x=>x==null?'-':Number(x).toFixed(Math.abs(x)>=1?2:4);
function sigCell(r){if(!r.sig)return'-';const c=r.sig==='BUY'?'up':'dn';
 return `<span class="${c}">${r.sig} ${r.ago===0?'วันนี้':r.ago+' แท่งก่อน'}</span>`}
function posCell(r){if(!r.pos)return'-';const c=r.pos.pnl_pct>=0?'up':'dn';
 return `<span class="${c}">${r.pos.side==='LONG'?'L':'S'} ${r.pos.pnl_pct>=0?'+':''}${f(r.pos.pnl_pct)}%</span>`}
function slCell(r){if(r.pos&&r.pos.stop!=null)return fp(r.pos.stop)+(r.pos.trailing?' ⇡':'');
 return r.plan?`${fp(r.plan.stop)} <span class="mut">(-${f(r.plan.stop_pct)}%)</span>`:'-'}
const COLS=[
 ['หุ้น',r=>r.symbol,r=>`<b>${r.symbol.replace('.BK','')}</b>`],
 ['ราคา',r=>r.close,r=>fp(r.close)],
 ['โซน',r=>r.zone,r=>`<span class="z" style="background:${ZC[r.zone]||ZC.Neutral}">${r.zone}</span>`],
 ['สัญญาณ',r=>r.sig?r.ago:999,sigCell],
 ['ถืออยู่',r=>r.pos?r.pos.pnl_pct:-999,posCell],
 ['SL',r=>r.plan?r.plan.stop_pct:999,slCell],
 ['จำนวน (ตาม risk)',r=>r.plan?r.plan.shares:0,r=>r.plan?r.plan.shares.toLocaleString():'-'],
 ['CAGR%',r=>r.cagr,r=>f(r.cagr)],['MaxDD%',r=>r.maxdd,r=>f(r.maxdd)],['Win%',r=>r.win,r=>f(r.win,0)],
 ['PF',r=>r.pf==null?99:r.pf,r=>r.pf==null?'∞':f(r.pf,2)],['เทรด',r=>r.trades,r=>r.trades],
 ['B&H CAGR%',r=>r.bh,r=>f(r.bh)]];
let rows=[],sel=null,chart=null,sk=null,sd=-1;
const fresh=r=>r.sig==='BUY'&&r.ago===0;
function view(){
 const q=$('q').value.toUpperCase(),fl=$('fl').value,mt=+$('mt').value||0;
 const L=rows.filter(r=>r.symbol.includes(q)&&r.trades>=mt&&(fl==='all'||(fl==='b0'&&fresh(r))||(fl==='b3'&&r.sig==='BUY'&&r.ago<=3)||(fl==='hold'&&r.pos)));
 if(sk===null)L.sort((a,b)=>(fresh(b)-fresh(a))||((b.cagr??-1e9)-(a.cagr??-1e9)));
 else L.sort((a,b)=>{const x=COLS[sk][1](a),y=COLS[sk][1](b);return(x<y?-1:x>y?1:0)*sd});
 return L}
function render(){
 const L=view();
 $('th').innerHTML=COLS.map((c,i)=>`<th data-i="${i}">${c[0]}${sk===i?(sd>0?' ▲':' ▼'):''}</th>`).join('');
 $('tb').innerHTML=L.map(r=>`<tr class="r ${r.symbol===sel?'sel':''} ${fresh(r)?'fresh':''}" data-s="${r.symbol}">${COLS.map(c=>`<td>${c[2](r)}</td>`).join('')}</tr>`).join('')
  ||`<tr><td colspan="${COLS.length}" class="mut">ไม่พบรายการ</td></tr>`;
 $('cnt').textContent=' · แสดง '+L.length+'/'+rows.length}
async function scan(){
 $('st').textContent='กำลังสแกน...';save();
 try{const j=await api('/api/screen?'+qs());rows=j.rows;
  $('st').textContent='อัปเดต '+j.updated+(j.failed.length?' · ดึงข้อมูลไม่ได้: '+j.failed.join(', '):'');
  render();const t=view()[0];if(!sel&&t)openChart(t.symbol)}
 catch(e){$('st').textContent='ผิดพลาด: '+e.message}}
async function openChart(sym){
 sel=sym;render();$('ct').textContent=sym+' กำลังโหลด...';
 try{const j=await api('/api/chart?'+qs()+'&symbol='+encodeURIComponent(sym));
  if(chart)chart.remove();
  chart=LightweightCharts.createChart($('chart'),{autoSize:true,layout:{background:{color:'transparent'},textColor:getComputedStyle(document.body).color},
   grid:{vertLines:{color:'rgba(128,128,128,.15)'},horzLines:{color:'rgba(128,128,128,.15)'}},rightPriceScale:{borderVisible:false},timeScale:{borderVisible:false}});
  const cs=chart.addCandlestickSeries();
  cs.setData(j.candles.map(c=>{const z=ZC[c.zone]||ZC.Neutral;return{time:c.time,open:c.open,high:c.high,low:c.low,close:c.close,color:z,wickColor:z,borderColor:z}}));
  chart.addLineSeries({color:'#e74c3c',lineWidth:1,priceLineVisible:false,lastValueVisible:false}).setData(j.fast);
  chart.addLineSeries({color:'#3498db',lineWidth:2,priceLineVisible:false,lastValueVisible:false}).setData(j.slow);
  cs.setMarkers(j.marks.map(m=>m.kind==='in'
   ?{time:m.time,position:m.side==='LONG'?'belowBar':'aboveBar',color:m.side==='LONG'?'#2ecc71':'#e74c3c',shape:m.side==='LONG'?'arrowUp':'arrowDown',text:'IN'}
   :{time:m.time,position:m.side==='LONG'?'aboveBar':'belowBar',color:m.win?'#2ecc71':'#e74c3c',shape:'circle',text:m.text}
  ).sort((a,b)=>a.time<b.time?-1:a.time>b.time?1:0));
  if(j.stop!=null)cs.createPriceLine({price:j.stop,color:'#f39c12',lineWidth:1,lineStyle:2,title:'SL'});
  if(j.entry!=null)cs.createPriceLine({price:j.entry,color:'#9aa0a6',lineWidth:1,lineStyle:2,title:'Entry'});
  const n=j.candles.length;chart.timeScale().setVisibleLogicalRange({from:Math.max(0,n-250),to:n+5});
  const L=j.candles[n-1],r=rows.find(x=>x.symbol===sym);
  $('ct').innerHTML=`<b>${sym}</b> · ${fp(L.close)} · <span class="z" style="background:${ZC[L.zone]||ZC.Neutral}">${L.zone}</span> · ${L.time}`+(r?` · ${sigCell(r)} · SL ${slCell(r)}`:'');
 }catch(e){$('ct').textContent=sym+' : '+e.message}}
$('tb').addEventListener('click',e=>{const tr=e.target.closest('tr.r');if(tr)openChart(tr.dataset.s)});
$('th').addEventListener('click',e=>{const th=e.target.closest('th');if(!th)return;const i=+th.dataset.i;if(sk===i)sd=-sd;else{sk=i;sd=-1}render()});
['q','mt'].forEach(i=>$(i).oninput=render);$('fl').onchange=render;
$('scan').onclick=scan;
$('go').onclick=()=>{const s=$('cs').value.trim().toUpperCase();if(s)openChart(s)};
$('cs').onkeydown=e=>{if(e.key==='Enter')$('go').click()};
scan();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, body, ctype="application/json", status=200):
        b = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/":
                return self.reply(PAGE, "text/html")
            if u.path == "/api/screen":
                return self.reply(json.dumps(clean(api_screen(parse(q), symbols(q)))))
            if u.path == "/api/chart":
                return self.reply(json.dumps(clean(api_chart(q.get("symbol", [""])[0].strip().upper(), parse(q)))))
            self.reply(json.dumps({"error": "not found"}), status=404)
        except Exception as e:
            self.reply(json.dumps({"error": str(e)}), status=500)


def notify(text):
    token, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT_ID")
    if not token or not chat:
        print("[alert]", text)
        return
    import requests
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={"chat_id": chat, "text": text}, timeout=15)


def alert_loop(minutes):
    seen = set()
    while True:
        try:
            p = parse({})
            for r in api_screen(p, B.WATCHLIST)["rows"]:
                key = (r["symbol"], r["date"], r["sig"])
                if r["sig"] and r["ago"] == 0 and key not in seen:
                    seen.add(key)
                    msg = f"CDC V3 {r['sig']}: {r['symbol']} @ {r['close']:.2f}"
                    if r["sig"] == "BUY" and r["plan"]:
                        pl = r["plan"]
                        msg += f" | SL {pl['stop']:.2f} (-{pl['stop_pct']:.1f}%) | {pl['shares']} หุ้น (risk {p.risk}%)"
                    notify(msg)
        except Exception as e:
            print("alert error:", e)
        time.sleep(minutes * 60)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--alert-every", type=int, default=0, help="สแกน+แจ้งเตือน Telegram ทุก N นาที")
    a = ap.parse_args()
    if a.alert_every:
        threading.Thread(target=alert_loop, args=(a.alert_every,), daemon=True).start()
    print(f"เปิดเบราว์เซอร์ที่ http://localhost:{a.port}")
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
