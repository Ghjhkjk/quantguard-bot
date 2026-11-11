import os
import time
import ccxt
import feedparser
import hashlib
import json
import subprocess
import urllib.request
from datetime import datetime, timezone, timedelta

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

# 台灣標準時間 (UTC+8)
TW_TZ = timezone(timedelta(hours=8))

# 🌟 本份資金正式起算基準點：2026/11/05 17:01:00 (台灣時間，對應 17:01:16 BTC 單起算)
ACCOUNT_EPOCH_DT = datetime(2026, 11, 5, 17, 1, 0, tzinfo=TW_TZ)
ACCOUNT_EPOCH_MS = int(ACCOUNT_EPOCH_DT.timestamp() * 1000)

# ==========================================================
# 👇 1. 金鑰與環境變數設定 (自動從 GitHub Secrets 讀取)
# ==========================================================
OKX_API_KEY         = os.environ.get("OKX_API_KEY", "請填入你的_OKX_API_Key")
OKX_SECRET_KEY      = os.environ.get("OKX_SECRET_KEY", "請填入你的_OKX_SECRET_KEY")
OKX_PASSPHRASE      = os.environ.get("OKX_PASSPHRASE", "請填入你的_OKX_Passphrase")
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY", "請填入你的_Gemini_API_Key")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# ==========================================================
# ⚙️ 2. v10.5 純實戰完全體參數設定
# ==========================================================
WHITELIST_SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']

POOL_NORMAL_RATIO       = 0.80  # 80% 常態雙核心池
POOL_HEDGE_RATIO        = 0.10  # 10% 緊急急跌對沖池
POOL_SNIPER_RATIO       = 0.10  # 10% 黑天鵝突發狙擊池

MAX_ORDERS_PER_HALF     = 3
MAX_SINGLE_MARGIN_RATIO = 0.28  # 單筆訂單保證金上限為總資產 28%

# 🛑 鐵律一：含槓桿 ROI 止損絕對上限設定 (死守 -10% 鐵律！)
MAX_SL_ROI_TRIGGER_PCT  = 8.8   # 掛單觸發止損設為含槓桿 ROI -8.8% (預留滑價，確保結算死守在 -10% 內)
HARD_CUT_SL_ROI_PCT     = -9.2  # 快軌每 20 秒硬斷頭保護：一旦實時含槓桿 ROI <= -9.2%，立即市價全平砍單！

# 🎯 四階段動態鎖利與無限追蹤止損門檻 (Stage 1 ~ Stage 4)
STAGE1_ROI_TRIGGER_PCT  = 5.0   # 階段一：順勢單 ROI 達 +5.0% -> 撤舊單，止損移至開倉價 +0.25% 保本
STAGE1_PRICE_LOCK_PCT   = 0.25
STAGE2_ROI_TRIGGER_PCT  = 9.0   # 階段二：順勢單 ROI 達 +9.0% -> 平倉 50% 落袋，剩餘 50% 止損鎖在開倉價 +1.0%
STAGE2_PRICE_LOCK_PCT   = 1.00
STAGE3_ROI_TRIGGER_PCT  = 14.0  # 階段三 (v10.5新增)：剩餘半倉 ROI 達 +14.0% -> 止損上推至開倉價 +1.8%，止盈外推 1.3 倍
STAGE3_PRICE_LOCK_PCT   = 1.80
STAGE4_ROI_TRIGGER_PCT  = 20.0  # 階段四 (v10.5新增)：剩餘半倉 ROI 達 +20.0% -> 止損上推至開倉價 +2.6%，止盈外推 1.6 倍
STAGE4_PRICE_LOCK_PCT   = 2.60

# ⚡ 逆勢搶反彈/回調單 快進快出鎖利門檻
COUNTER_STAGE1_ROI_PCT  = 4.0   # 逆勢單 ROI 達 +4.0% -> 立即移至開倉價 +0.25% 保本
COUNTER_STAGE2_ROI_PCT  = 7.5   # 逆勢單 ROI 達 +7.5% -> 立即平倉 50% 落袋，剩餘鎖在開倉價 +0.8%
COUNTER_STAGE2_LOCK_PCT = 0.80

EARLY_HARVEST_ROI_PCT   = 4.2   # 若帳面 ROI >= +4.2% 且短線均線動能反轉衰竭，提前收割全平

# ⏳ 殭屍單超時清理參數 (v10.5新增)
ZOMBIE_MAX_HOLD_MINUTES = 210   # 持倉超過 210 分鐘 (3.5 小時)
ZOMBIE_MIN_ROI_PCT      = -2.5  # 且含槓桿 ROI 卡在 -2.5% ~ +2.5% 之間毫無動能，自動市價清理釋放資金
ZOMBIE_MAX_ROI_PCT      = 2.5

# 🛡️ 單日盈虧動態火力調節參數 (v10.5新增)
DAILY_DRAWDOWN_HALF_PCT = -3.0  # 若今日已實現虧損達總資產 -3.0%，自動切換為 50% 半火力防守模式
DAILY_PROFIT_LOCK_USDT  = 8000  # 若今日已實現獲利達 +$8,000，切換為 80% 穩健鎖利模式

IS_GITHUB_ACTIONS     = os.environ.get("GITHUB_ACTIONS") == "true"
RUN_CONTINUOUS_LOOP   = True
FAST_TRACK_INTERVAL   = 20    # 快軌：每 20 秒高速巡檢一次 OKX
SLOW_AI_EVERY_N_TICKS = 30    # 慢軌：每 30 個快軌或有空倉時呼叫 Gemini AI
MAX_JOB_RUNTIME_SEC   = int(os.environ.get("MAX_JOB_RUNTIME_SEC", "240"))

LAST_AI_PLAN_CACHE = None
NOTIFIED_POS_IDS   = set()
POSITION_STAGE_MAP = {}
HEDGE_ACTIVE_SYMS  = set()
LAST_GIT_PUSH_TS   = 0.0

AI_MEMORY_BANK = {
    "entry_snapshots": {},
    "historical_reasons": {},
    "processed_pos_keys": [],
    "pattern_stats": {},
    "symbol_direction_stats": {},
    "consecutive_losses": {"BTC/USDT:USDT": 0, "ETH/USDT:USDT": 0, "SOL/USDT:USDT": 0},
    "lessons_learned": []
}


def clean_credential(text, name):
    cleaned = str(text).strip().replace("！", "!").replace("＠", "@").replace("＃", "#")
    try:
        cleaned.encode('latin-1')
    except UnicodeEncodeError:
        raise ValueError(f"❌ 【{name}】內含中文字或全形符號，請確認已在 GitHub Secrets 設定！")
    if not cleaned or "請填入" in cleaned or "貼上" in cleaned:
        raise ValueError(f"❌ 【{name}】尚未填寫！")
    return cleaned


def init_okx_exchange():
    exchange = ccxt.okx({
        'apiKey': clean_credential(OKX_API_KEY, "OKX_API_KEY"),
        'secret': clean_credential(OKX_SECRET_KEY, "OKX_SECRET_KEY"),
        'password': clean_credential(OKX_PASSPHRASE, "OKX_PASSPHRASE"),
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'}
    })
    exchange.set_sandbox_mode(True)
    exchange.load_markets()
    return exchange


def send_discord_alert(title, description, color=3066993, fields=None):
    if not DISCORD_WEBHOOK_URL or not DISCORD_WEBHOOK_URL.startswith("http"):
        return
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "QuantGuard AI v10.5 | 四階段無限鎖利 × BTC領頭羊連動終端"}
    }
    if fields:
        embed["fields"] = fields
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=json.dumps({"embeds": [embed]}).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'User-Agent': 'QuantGuardBot/10.5'}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"⚠️ Discord 推播異常: {e}", flush=True)


def load_previous_state():
    global NOTIFIED_POS_IDS, POSITION_STAGE_MAP, AI_MEMORY_BANK
    if not os.path.exists("live_data.json"):
        return
    try:
        with open("live_data.json", "r", encoding="utf-8") as f:
            old_data = json.load(f)
        saved_mem = old_data.get("ai_memory_bank", {})
        if isinstance(saved_mem, dict) and saved_mem:
            for k in AI_MEMORY_BANK.keys():
                if k in saved_mem:
                    AI_MEMORY_BANK[k] = saved_mem[k]

        hist_reasons = AI_MEMORY_BANK.setdefault("historical_reasons", {})
        for o in old_data.get("orders", []):
            fid = str(o.get("full_id") or o.get("id") or "")
            if fid:
                NOTIFIED_POS_IDS.add(fid)
                if o.get("reason") and fid not in hist_reasons:
                    hist_reasons[fid] = o["reason"]

        saved_stages = old_data.get("position_stage_map", {})
        if isinstance(saved_stages, dict):
            POSITION_STAGE_MAP.update(saved_stages)

        print(f"🧠 [AI 永久記憶庫載入成功] 已累積學習 {len(AI_MEMORY_BANK.get('processed_pos_keys', []))} 筆經驗，封存 {len(hist_reasons)} 筆歷史理由！", flush=True)
    except Exception as e:
        print(f"⚠️ 載入歷史狀態提示: {e}", flush=True)


def push_live_data_immediately(force=False):
    global LAST_GIT_PUSH_TS
    if not IS_GITHUB_ACTIONS:
        return
    now_ts = time.time()
    if not force and (now_ts - LAST_GIT_PUSH_TS < 60):
        return
    try:
        subprocess.run(["git", "config", "--local", "user.email", "bot@quantguard.ai"], check=False)
        subprocess.run(["git", "config", "--local", "user.name", "QuantGuard AutoBot"], check=False)
        subprocess.run(["git", "add", "live_data.json"], check=False)
        diff_res = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if diff_res.returncode != 0:
            subprocess.run(["git", "commit", "-m", "Real-time sync live_data.json v10.5"], check=False)
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
            subprocess.run(["git", "push", "origin", "main"], check=False)
            LAST_GIT_PUSH_TS = now_ts
            print("🚀 [即時雲端同步] 已將最新 live_data.json 推送至雲端！", flush=True)
    except Exception as e:
        print(f"ℹ️ 背景 Git 同步提示: {e}", flush=True)


def fetch_live_crypto_news():
    rss_urls = [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss"
    ]
    headlines = []
    for url in rss_urls:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:3]:
                headlines.append(entry.title)
        except Exception:
            continue
    return headlines[:5] if headlines else ["Institutional crypto volume steady amid macro watch."]


def compute_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def fetch_whitelist_market_data(exchange):
    """
    抓取雙周期指標、反推死守 -10% ROI 的安全槓桿，並計算『BTC 領頭羊大盤風向鎖 (btc_master_bias)』！
    """
    market_summary = {}
    for sym in WHITELIST_SYMBOLS:
        try:
            ticker = exchange.fetch_ticker(sym)
            ohlcv_15m = exchange.fetch_ohlcv(sym, timeframe='15m', limit=28)
            ohlcv_1h  = exchange.fetch_ohlcv(sym, timeframe='1h', limit=24)

            closes = [c[4] for c in ohlcv_15m]
            highs  = [c[2] for c in ohlcv_15m]
            lows   = [c[3] for c in ohlcv_15m]
            vols   = [c[5] for c in ohlcv_15m]
            current_price = float(ticker['last'])

            ma5  = sum(closes[-5:]) / 5 if len(closes) >= 5 else current_price
            ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else current_price
            ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else current_price
            std20 = (sum((x - ma20) ** 2 for x in closes[-20:]) / 20) ** 0.5 if len(closes) >= 20 else current_price * 0.01
            bb_upper = round(ma20 + 2.0 * std20, 2)
            bb_lower = round(ma20 - 2.0 * std20, 2)
            bb_pos_pct = round(((current_price - bb_lower) / max(0.0001, (bb_upper - bb_lower))) * 100, 1)

            rsi_14 = compute_rsi(closes, 14)
            if rsi_14 >= 74:
                rsi_zone = "EXTREME_OVERBOUGHT"
            elif rsi_14 <= 26:
                rsi_zone = "EXTREME_OVERSOLD"
            else:
                rsi_zone = "HEALTHY_TREND_ZONE"

            closes_1h = [c[4] for c in ohlcv_1h]
            ma5_1h  = sum(closes_1h[-5:]) / 5 if len(closes_1h) >= 5 else current_price
            ma20_1h = sum(closes_1h[-20:]) / 20 if len(closes_1h) >= 20 else current_price
            trend_1h  = "BULLISH" if ma5_1h > ma20_1h else "BEARISH"
            trend_15m = "BULLISH" if ma5 > ma20 else "BEARISH"

            if trend_1h == "BULLISH" and trend_15m == "BULLISH":
                multi_tf_regime = "RESONANCE_BULL"
            elif trend_1h == "BEARISH" and trend_15m == "BEARISH":
                multi_tf_regime = "RESONANCE_BEAR"
            elif trend_1h == "BULLISH" and trend_15m == "BEARISH":
                multi_tf_regime = "BULL_PULLBACK"
            else:
                multi_tf_regime = "BEAR_REBOUND"

            tr_list   = [(highs[i] - lows[i]) / closes[i] * 100 for i in range(len(closes))]
            atr_pct   = sum(tr_list) / len(tr_list) if tr_list else 0.8
            swing_pct = ((max(highs[-12:]) - min(lows[-12:])) / current_price) * 100 if len(highs) >= 12 else 2.2
            change_24h = float(ticker.get('percentage') or 0.0)

            bar_change_pct = ((closes[-1] - closes[-2]) / closes[-2] * 100) if len(closes) >= 2 else 0.0
            avg_vol = (sum(vols[:-1]) / max(1, len(vols) - 1)) if len(vols) > 1 else 1.0
            vol_spike_ratio = round(vols[-1] / avg_vol, 2) if avg_vol > 0 else 1.0

            anchored_tp_pct = round(max(1.6, min(3.2, swing_pct * 0.75)), 2)
            natural_sl_pct  = round(max(1.2, min(2.2, swing_pct * 0.50)), 2)
            max_safe_lev    = max(3, min(7, int(MAX_SL_ROI_TRIGGER_PCT // natural_sl_pct)))
            enforced_sl_pct = round(min(natural_sl_pct, MAX_SL_ROI_TRIGGER_PCT / max_safe_lev), 2)

            funding_rate_pct = 0.01
            try:
                fr = exchange.fetch_funding_rate(sym)
                funding_rate_pct = round(float(fr.get('fundingRate') or 0.0001) * 100, 4)
            except Exception:
                pass

            open_interest_val = 0.0
            try:
                oi = exchange.fetch_open_interest(sym)
                open_interest_val = round(float(oi.get('openInterestAmount') or oi.get('openInterestValue') or 0.0), 2)
            except Exception:
                pass

            market_summary[sym] = {
                "last_price": round(current_price, 2),
                "change_24h_pct": round(change_24h, 2),
                "bar_15m_change_pct": round(bar_change_pct, 2),
                "vol_spike_ratio": vol_spike_ratio,
                "funding_rate_pct": funding_rate_pct,
                "open_interest": open_interest_val,
                "ma5": round(ma5, 2),
                "ma10": round(ma10, 2),
                "ma20": round(ma20, 2),
                "rsi_14": rsi_14,
                "rsi_zone": rsi_zone,
                "bb_upper": bb_upper,
                "bb_lower": bb_lower,
                "bb_pos_pct": bb_pos_pct,
                "trend_1h": trend_1h,
                "trend": trend_15m,
                "multi_tf_regime": multi_tf_regime,
                "atr_15m_pct": round(atr_pct, 2),
                "recent_swing_pct": round(swing_pct, 2),
                "anchored_tp_pct": anchored_tp_pct,
                "anchored_sl_pct": enforced_sl_pct,
                "max_safe_leverage": max_safe_lev,
                "momentum_weakening_long": (current_price < ma5 and ma5 < ma10),
                "momentum_weakening_short": (current_price > ma5 and ma5 > ma10)
            }
        except Exception as e:
            print(f"⚠️ 抓取 {sym} 行情異常: {e}", flush=True)

    # 🌟 v10.5 新增：計算 BTC 領頭羊大盤風向鎖，注入所有幣種供建倉過濾
    btc_info = market_summary.get('BTC/USDT:USDT', {})
    btc_15m_chg = float(btc_info.get('bar_15m_change_pct', 0.0))
    btc_t1h     = btc_info.get('trend_1h', 'BULLISH')
    btc_t15m    = btc_info.get('trend', 'BULLISH')

    if btc_15m_chg <= -0.55 or (btc_t1h == 'BEARISH' and btc_t15m == 'BEARISH' and btc_15m_chg < -0.2):
        btc_master_bias = "DUMP_WARNING_NO_ALT_LONG"    # BTC 急殺中，嚴禁 ETH/SOL 開立多單
    elif btc_15m_chg >= 0.55 or (btc_t1h == 'BULLISH' and btc_t15m == 'BULLISH' and btc_15m_chg > 0.2):
        btc_master_bias = "PUMP_WARNING_NO_ALT_SHORT"   # BTC 急拉中，嚴禁 ETH/SOL 開立空單
    else:
        btc_master_bias = "NEUTRAL_NORMAL"

    for s_k in market_summary:
        market_summary[s_k]["btc_master_bias"] = btc_master_bias

    return market_summary


def update_ai_memory_bank_from_history(history_records):
    processed_set = set(AI_MEMORY_BANK.get("processed_pos_keys", []))
    sym_stats     = AI_MEMORY_BANK.get("symbol_direction_stats", {})
    pat_stats     = AI_MEMORY_BANK.get("pattern_stats", {})
    snapshots     = AI_MEMORY_BANK.get("entry_snapshots", {})
    consec_losses = AI_MEMORY_BANK.get("consecutive_losses", {s: 0 for s in WHITELIST_SYMBOLS})

    for rec in reversed(history_records):
        ukey = str(rec.get("full_id") or rec.get("id") or "")
        if not ukey or ukey in processed_set:
            continue

        processed_set.add(ukey)

        sym   = rec.get("asset", "BTC/USDT:USDT")
        is_lg = "LONG" in str(rec.get("direction", "")).upper()
        side  = "LONG" if is_lg else "SHORT"
        pnl   = float(rec.get("realized_pnl_num", 0.0))

        if pnl < 0:
            consec_losses[sym] = consec_losses.get(sym, 0) + 1
        elif pnl > 0:
            consec_losses[sym] = 0

        sd_key = f"{sym}_{side}"
        if sd_key not in sym_stats:
            sym_stats[sd_key] = {"wins": 0, "losses": 0, "net_pnl": 0.0}
        if pnl > 0:
            sym_stats[sd_key]["wins"] += 1
        elif pnl < 0:
            sym_stats[sd_key]["losses"] += 1
        sym_stats[sd_key]["net_pnl"] = round(sym_stats[sd_key]["net_pnl"] + pnl, 2)

        snap = snapshots.get(sym, {})
        regime   = snap.get("multi_tf_regime", "GENERAL_TREND")
        rsi_zone = snap.get("rsi_zone", "NEUTRAL")
        pat_key  = f"{side}_under_{regime}_{rsi_zone}"

        if pat_key not in pat_stats:
            pat_stats[pat_key] = {"wins": 0, "losses": 0, "net_pnl": 0.0}
        if pnl > 0:
            pat_stats[pat_key]["wins"] += 1
        elif pnl < 0:
            pat_stats[pat_key]["losses"] += 1
        pat_stats[pat_key]["net_pnl"] = round(pat_stats[pat_key]["net_pnl"] + pnl, 2)

    lessons = [
        "🛑【止損鐵律】全倉位強制執行『含槓桿最大止損 -8.8% 觸發 (死守 -10% 上限)』！",
        "🚀【四階無限鎖利】滿+5%鎖保本 ➔ 滿+9%平半倉鎖+1% ➔ 滿+14%鎖+1.8% ➔ 滿+20%鎖+2.6%！",
        "🧭【BTC領頭羊保護】當 BTC 急殺時嚴禁 ETH/SOL 逆勢接刀做多；持倉逾 3.5 小時無動能自動清理！"
    ]
    for s_sym, c_loss in consec_losses.items():
        if c_loss >= 2:
            lessons.append(f"🛡️【馬可維茲連敗降權】{s_sym} 近期連續虧損 {c_loss} 筆，系統自動將其配置權重減半 (0.5x)！")

    for sd_k, st in sym_stats.items():
        tot = st["wins"] + st["losses"]
        if tot == 0:
            continue
        wr = round((st["wins"] / tot) * 100, 1)
        if wr < 35.0 and st["net_pnl"] < 0:
            lessons.append(f"⚠️【高敗率警告】{sd_k} 累計 {st['wins']}勝{st['losses']}敗 (勝率{wr}%，淨虧${st['net_pnl']})，請順應 1H 大趨勢方向！")
        elif wr >= 50.0 or st["net_pnl"] > 1500:
            lessons.append(f"✅【正期望值主力】{sd_k} 累計 {st['wins']}勝{st['losses']}敗 (勝率{wr}%，淨賺+${st['net_pnl']})，具顯著獲利優勢！")

    AI_MEMORY_BANK["processed_pos_keys"]     = list(processed_set)[-300:]
    AI_MEMORY_BANK["symbol_direction_stats"] = sym_stats
    AI_MEMORY_BANK["pattern_stats"]          = pat_stats
    AI_MEMORY_BANK["consecutive_losses"]     = consec_losses
    AI_MEMORY_BANK["lessons_learned"]        = lessons[:12]


def build_fallback_plan(market_data):
    half_a, half_b = [], []
    for sym in WHITELIST_SYMBOLS:
        info      = market_data.get(sym, {})
        trend_1h  = info.get("trend_1h", "BULLISH")
        trend_15m = info.get("trend", "BULLISH")
        rsi_val   = float(info.get("rsi_14", 50.0))
        safe_lev  = int(info.get("max_safe_leverage", 5))
        tp_v      = float(info.get("anchored_tp_pct", 2.2))
        sl_v      = float(info.get("anchored_sl_pct", 1.5))

        act = "BUY" if trend_1h == "BULLISH" else "SELL"

        half_a.append({
            "symbol": sym, "action": act, "weight": 1.3, "leverage": safe_lev,
            "tp_pct": tp_v, "sl_pct": sl_v,
            "reason": f"【Half-A 順應1H大勢】1H:{trend_1h}/15m:{trend_15m}，安全槓桿{safe_lev}x，含槓桿止損鎖定 -{round(sl_v*safe_lev,1)}%。"
        })
        half_b.append({
            "symbol": sym, "action": act, "weight": 1.1, "leverage": safe_lev,
            "tp_pct": round(tp_v * 1.1, 2), "sl_pct": sl_v,
            "reason": f"【Half-B 四階鎖利】RSI={rsi_val}，滿+5%保本/滿+9%平半倉/滿+14%無限上推。"
        })
    return {
        "overall_confidence": 89,
        "overall_summary": "啟動 v10.5 四階段無限追蹤鎖利、BTC 領頭羊風向連動與死守 -10% 止損鐵律。",
        "black_swan_detected": False,
        "sniper_order": None,
        "half_a_orders": half_a[:2],
        "half_b_orders": half_b[:2]
    }


def ask_gemini_quant_brain(market_data, news_headlines, current_held_symbols, mistake_memory):
    if not GENAI_AVAILABLE:
        return build_fallback_plan(market_data)

    gemini_key = clean_credential(GEMINI_API_KEY, "GEMINI_API_KEY")
    client = genai.Client(api_key=gemini_key)

    prompt = f"""
    你是 QuantGuard AI v10.5 旗艦量化決策核心。請根據以下五大實戰鐵律規劃本輪最佳建倉組合：

    【1. 三大幣種即時多維度指標 (含 trend_1h、btc_master_bias、vol_spike_ratio、max_safe_leverage)】：
    {json.dumps(market_data, ensure_ascii=False)}

    【2. 🧠 AI 永久累積記憶庫與連敗狀態】：
    - 提煉法則：{json.dumps(AI_MEMORY_BANK.get("lessons_learned", []), ensure_ascii=False)}
    - 各幣種近期連敗次數：{json.dumps(AI_MEMORY_BANK.get("consecutive_losses", {}), ensure_ascii=False)}
    - 各幣種多空歷史累計勝敗：{json.dumps(AI_MEMORY_BANK.get("symbol_direction_stats", {}), ensure_ascii=False)}
    - 最近 5 筆平倉明細：{json.dumps(mistake_memory[:5], ensure_ascii=False)}

    【3. 最新國際新聞】：{json.dumps(news_headlines, ensure_ascii=False)}
    【4. 目前持有倉位】：{json.dumps(current_held_symbols, ensure_ascii=False)}

    【v10.5 五大絕對建倉鐵律】：
    1. BTC 領頭羊大盤風向鎖：
       - 若 `btc_master_bias == "DUMP_WARNING_NO_ALT_LONG"`，代表 BTC 正在急殺，嚴禁對 ETH 或 SOL 開立 BUY 多單！
       - 若 `btc_master_bias == "PUMP_WARNING_NO_ALT_SHORT"`，代表 BTC 正在急拉，嚴禁對 ETH 或 SOL 開立 SELL 空單！
    2. 順應 1H 大趨勢：當 `trend_1h == "BULLISH"` 時優先做多 (`BUY`)；當 `trend_1h == "BEARISH"` 時優先做空 (`SELL`)，嚴禁大牛市同時開三張空單摸頂。
    3. 止損死守 -10% 鐵律：`leverage` 請採用 `max_safe_leverage` (4~6倍)，且 `sl_pct * leverage` 必須 `<= 8.8`。
    4. 量價配合：優先將高權重 (`weight`: 1.5~2.0) 配置給 `vol_spike_ratio >= 1.1` 且近期無連敗之標的。

    請嚴格只輸出 JSON 格式：
    {{
      "overall_confidence": 91,
      "overall_summary": "繁體中文60字內：說明本輪如何結合BTC領頭羊風向、1H大趨勢與-8.8%止損上限進行布局",
      "black_swan_detected": false,
      "sniper_order": null,
      "half_a_orders": [
        {{"symbol": "BTC/USDT:USDT", "action": "BUY", "weight": 1.6, "leverage": 5, "tp_pct": 2.2, "sl_pct": 1.6, "reason": "【Half-A 領頭羊順勢】..."}}
      ],
      "half_b_orders": [
        {{"symbol": "SOL/USDT:USDT", "action": "BUY", "weight": 1.4, "leverage": 5, "tp_pct": 2.4, "sl_pct": 1.7, "reason": "【Half-B 量價共振】..."}}
      ]
    }}
    """

    for model_name in ["gemini-2.5-flash", "gemini-2.5-flash-lite"]:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2)
            )
            return json.loads(response.text)
        except Exception as e:
            print(f"⚠️ 模型 {model_name} 呼叫提示 ({e})，切換備援...", flush=True)
            continue

    return build_fallback_plan(market_data)


def calculate_safe_contracts(exchange, symbol, desired_margin_usdt, leverage, price, total_equity):
    try:
        bal = exchange.fetch_balance()
        current_free = float(bal.get('USDT', {}).get('free', 0.0))
    except Exception:
        current_free = desired_margin_usdt

    max_allowed_margin = total_equity * MAX_SINGLE_MARGIN_RATIO if total_equity > 0 else desired_margin_usdt
    safe_margin = min(desired_margin_usdt, max_allowed_margin, current_free * 0.80)
    if safe_margin < 15.0 or price <= 0:
        return 0, 0.0

    market = exchange.market(symbol)
    contract_size = float(market.get('contractSize') or 1.0)
    notional_per_contract = contract_size * price
    if notional_per_contract <= 0:
        return 0, 0.0

    raw_contracts = int((safe_margin * leverage) // notional_per_contract)
    contracts = max(1, raw_contracts)

    required_margin = (contracts * notional_per_contract) / leverage
    if required_margin > current_free * 0.85:
        contracts = int((current_free * 0.80 * leverage) // notional_per_contract)

    actual_margin = round((contracts * notional_per_contract) / leverage, 2)
    return max(0, contracts), actual_margin


def cancel_existing_algo_orders_for_symbol(exchange, sym, pos_side_str=None):
    inst_id = sym.replace('/USDT:USDT', '-USDT-SWAP')
    for ord_type in ['conditional', 'oco']:
        try:
            res = exchange.privateGetTradeOrdersAlgoPending({'instId': inst_id, 'ordType': ord_type})
            pending_list = res.get('data', [])
            cancel_payload = []
            for item in pending_list:
                if pos_side_str and item.get('posSide') and item.get('posSide').lower() != pos_side_str.lower():
                    continue
                algo_id = item.get('algoId')
                if algo_id:
                    cancel_payload.append({'instId': inst_id, 'algoId': algo_id})
            if cancel_payload:
                exchange.privatePostTradeCancelAlgos(cancel_payload)
                print(f"   🧹 已撤銷 {sym} 舊的止盈止損策略單共 {len(cancel_payload)} 筆！", flush=True)
        except Exception:
            pass


def update_position_tpsl_on_okx(exchange, sym, side_upper, contracts_qty, new_tp_px, new_sl_px):
    inst_id    = sym.replace('/USDT:USDT', '-USDT-SWAP')
    close_side = 'sell' if side_upper == 'LONG' else 'buy'
    pos_side   = 'long' if side_upper == 'LONG' else 'short'

    cancel_existing_algo_orders_for_symbol(exchange, sym, pos_side)

    body = {
        'instId': inst_id,
        'tdMode': 'cross',
        'side': close_side,
        'posSide': pos_side,
        'ordType': 'oco',
        'sz': str(max(1, int(contracts_qty))),
        'tpTriggerPx': str(new_tp_px),
        'tpOrdPx': '-1',
        'slTriggerPx': str(new_sl_px),
        'slOrdPx': '-1',
        'reduceOnly': True
    }
    try:
        exchange.privatePostTradeOrderAlgo(body)
        print(f"   ✅ [OKX 止盈止損同步成功] {sym} ({side_upper}) 最新止盈: ${new_tp_px} | 最新止損: ${new_sl_px}", flush=True)
        return True
    except Exception:
        try:
            body.pop('posSide', None)
            exchange.privatePostTradeOrderAlgo(body)
            return True
        except Exception as e2:
            print(f"   ⚠️ 更新策略委託單提示: {e2}", flush=True)
            return False


def close_partial_or_full_position(exchange, sym, side_upper, close_contracts_qty, tag_reason):
    if close_contracts_qty <= 0:
        return False
    close_side = 'sell' if side_upper == 'LONG' else 'buy'
    pos_side   = 'long' if side_upper == 'LONG' else 'short'
    try:
        exchange.create_order(
            symbol=sym, type='market', side=close_side, amount=int(close_contracts_qty),
            params={'tdMode': 'cross', 'posSide': pos_side, 'reduceOnly': True}
        )
        print(f"   💰 [{tag_reason}] 成功市價平倉 {sym} ({side_upper}) 共 {int(close_contracts_qty)} 張合約！", flush=True)
        return True
    except Exception:
        try:
            exchange.create_order(
                symbol=sym, type='market', side=close_side, amount=int(close_contracts_qty),
                params={'tdMode': 'cross', 'reduceOnly': True}
            )
            return True
        except Exception as e:
            print(f"   ⚠️ 平倉執行異常: {e}", flush=True)
            return False


def place_okx_order_with_tpsl(exchange, sym, action, contracts, leverage, tp_price, sl_price, tag_label):
    if contracts <= 0:
        return None
    side = 'buy' if action == 'BUY' else 'sell'
    pos_side = 'long' if side == 'buy' else 'short'

    for p_param in [{'mgnMode': 'cross', 'posSide': pos_side}, {'mgnMode': 'cross'}]:
        try:
            exchange.set_leverage(leverage, sym, params=p_param)
            break
        except Exception:
            continue

    algo_params = [{
        'tpTriggerPx': str(tp_price), 'tpOrdPx': '-1',
        'slTriggerPx': str(sl_price), 'slOrdPx': '-1'
    }]

    try:
        order = exchange.create_order(
            symbol=sym, type='market', side=side, amount=contracts,
            params={'tdMode': 'cross', 'posSide': pos_side, 'attachAlgoOrds': algo_params}
        )
        print(f"      🎉 [{tag_label} 下單成功] {sym} {pos_side.upper()} {leverage}x {contracts}張 | 止盈:${tp_price} 止損:${sl_price}", flush=True)
        return order
    except Exception as first_err:
        if "51000" in str(first_err) or "posSide" in str(first_err):
            try:
                order = exchange.create_order(
                    symbol=sym, type='market', side=side, amount=contracts,
                    params={'tdMode': 'cross', 'attachAlgoOrds': algo_params}
                )
                return order
            except Exception as second_err:
                print(f"      ⚠️ [{tag_label}] 下單異常: {second_err}", flush=True)
        else:
            print(f"      ⚠️ [{tag_label}] 下單異常: {first_err}", flush=True)
    return None


def fast_track_smart_harvest_and_hedge(exchange, active_positions, market_data, total_equity, total_upl):
    """
    ⚡ v10.5 快軌五大實戰動作：
    1. -9.2% ROI 硬斷頭保護 (死守 -10% 鐵律)。
    2. 3.5 小時 (210 分鐘) 殭屍單自動清理釋放資金。
    3. 階段四 (+20% ROI)：止損上推至開倉價 +2.6%，止盈外推 1.6x。
    4. 階段三 (+14% ROI)：止損上推至開倉價 +1.8%，止盈外推 1.3x。
    5. 階段二 (+9% ROI 平半倉鎖 +1%) & 階段一 (+5% ROI 鎖保本 +0.25%)。
    """
    state_changed = False
    now_dt = datetime.now(TW_TZ)
    snapshots = AI_MEMORY_BANK.get("entry_snapshots", {})

    for pos in active_positions:
        sym       = pos['symbol']
        side      = pos['side']
        entry_px  = float(pos['entry_price'])
        contracts = int(float(pos['contracts']))
        roi_pct   = float(pos['roi_pct'])
        tp_pct    = float(pos.get('tp_pct', 2.2))
        pos_key   = f"{sym}_{side}"
        cur_stage = int(POSITION_STAGE_MAP.get(pos_key, 0))

        if entry_px <= 0 or contracts <= 0:
            continue

        # 🛑 1. 絕對止損鐵律攔截：若實時含槓桿 ROI <= -9.2%，立即市價砍單死守 -10% 底線！
        if roi_pct <= HARD_CUT_SL_ROI_PCT:
            print(f"🛑 [-10% 鐵律硬斷頭觸發] {sym} ({side}) 實時 ROI 達 {roi_pct:.2f}%，立即市價平倉止血！", flush=True)
            cancel_existing_algo_orders_for_symbol(exchange, sym)
            close_partial_or_full_position(exchange, sym, side, contracts, "死守-10%鐵律硬止損")
            POSITION_STAGE_MAP.pop(pos_key, None)
            state_changed = True
            continue

        # ⏳ 2. 殭屍單超時清理：若持倉超過 210 分鐘且 ROI 卡在 -2.5% ~ +2.5% 毫無動能，自動平倉換股！
        snap_info = snapshots.get(sym, {})
        entry_time_str = snap_info.get("entry_time")
        if entry_time_str and cur_stage == 0:
            try:
                entry_dt = datetime.strptime(entry_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TW_TZ)
                held_minutes = (now_dt - entry_dt).total_seconds() / 60.0
                if held_minutes >= ZOMBIE_MAX_HOLD_MINUTES and (ZOMBIE_MIN_ROI_PCT <= roi_pct <= ZOMBIE_MAX_ROI_PCT):
                    print(f"⏳ [殭屍單超時清理] {sym} ({side}) 已持倉 {int(held_minutes)} 分鐘且 ROI 僅 {roi_pct:+.2f}%，主動平倉釋放資金！", flush=True)
                    cancel_existing_algo_orders_for_symbol(exchange, sym)
                    close_partial_or_full_position(exchange, sym, side, contracts, f"持倉逾{int(held_minutes)}分無動能清理")
                    POSITION_STAGE_MAP.pop(pos_key, None)
                    state_changed = True
                    continue
            except Exception:
                pass

        m_info   = market_data.get(sym, {})
        trend_1h = m_info.get("trend_1h", "BULLISH")
        is_counter_trend = (side == "LONG" and trend_1h == "BEARISH") or (side == "SHORT" and trend_1h == "BULLISH")

        s1_trigger = COUNTER_STAGE1_ROI_PCT if is_counter_trend else STAGE1_ROI_TRIGGER_PCT
        s2_trigger = COUNTER_STAGE2_ROI_PCT if is_counter_trend else STAGE2_ROI_TRIGGER_PCT
        s2_lock_p  = COUNTER_STAGE2_LOCK_PCT if is_counter_trend else STAGE2_PRICE_LOCK_PCT

        base_tp_px = round(entry_px * (1 + tp_pct / 100.0) if side == 'LONG' else entry_px * (1 - tp_pct / 100.0), 2)

        # 🚀 3. 階段四 (ROI >= +20%)：止損上推至開倉價 +2.6% (鎖住約 +15% ROI)，止盈外推 1.6 倍吃大波段！
        if roi_pct >= STAGE4_ROI_TRIGGER_PCT and cur_stage < 4:
            lock_s4_sl = round(entry_px * (1 + STAGE4_PRICE_LOCK_PCT / 100.0) if side == 'LONG' else entry_px * (1 - STAGE4_PRICE_LOCK_PCT / 100.0), 2)
            ext_tp_px  = round(entry_px * (1 + (tp_pct * 1.6) / 100.0) if side == 'LONG' else entry_px * (1 - (tp_pct * 1.6) / 100.0), 2)
            update_position_tpsl_on_okx(exchange, sym, side, contracts, ext_tp_px, lock_s4_sl)
            POSITION_STAGE_MAP[pos_key] = 4
            pos['sl_trigger_px'] = lock_s4_sl
            pos['tp_trigger_px'] = ext_tp_px
            pos['stage_badge']   = f"🔥 四階暴利鎖 (+{STAGE4_PRICE_LOCK_PCT}%)"
            pos['reason']        = f"🔥【ROI衝破+20%無限推鎖】止損已上推至開倉價 +{STAGE4_PRICE_LOCK_PCT}% (${lock_s4_sl})，止盈外推至 ${ext_tp_px}！"
            state_changed = True

        # 🚀 4. 階段三 (ROI >= +14%)：止損上推至開倉價 +1.8% (鎖住約 +10% ROI)，止盈外推 1.3 倍！
        elif roi_pct >= STAGE3_ROI_TRIGGER_PCT and cur_stage < 3:
            lock_s3_sl = round(entry_px * (1 + STAGE3_PRICE_LOCK_PCT / 100.0) if side == 'LONG' else entry_px * (1 - STAGE3_PRICE_LOCK_PCT / 100.0), 2)
            ext_tp_px  = round(entry_px * (1 + (tp_pct * 1.3) / 100.0) if side == 'LONG' else entry_px * (1 - (tp_pct * 1.3) / 100.0), 2)
            update_position_tpsl_on_okx(exchange, sym, side, contracts, ext_tp_px, lock_s3_sl)
            POSITION_STAGE_MAP[pos_key] = 3
            pos['sl_trigger_px'] = lock_s3_sl
            pos['tp_trigger_px'] = ext_tp_px
            pos['stage_badge']   = f"🚀 三階推鎖 (+{STAGE3_PRICE_LOCK_PCT}%)"
            pos['reason']        = f"🚀【ROI滿+14%三階推鎖】止損已上推至開倉價 +{STAGE3_PRICE_LOCK_PCT}% (${lock_s3_sl})，讓剩餘半倉奔跑！"
            state_changed = True

        # 💰 5. 階段二 (ROI >= +9% / 逆勢 +7.5%)：平倉 50% 落袋 + 剩餘 50% 鎖在 +1%
        elif roi_pct >= s2_trigger and cur_stage < 2:
            half_qty = max(1, contracts // 2)
            remain_qty = max(1, contracts - half_qty)
            lock_sl = round(entry_px * (1 + s2_lock_p / 100.0) if side == 'LONG' else entry_px * (1 - s2_lock_p / 100.0), 2)

            print(f"💰 [階段二觸發：ROI +{roi_pct:.2f}% >= +{s2_trigger}%] {sym} ({side}) 平半倉 ({half_qty}張) 落袋！", flush=True)
            if contracts >= 2:
                close_partial_or_full_position(exchange, sym, side, half_qty, f"ROI滿+{s2_trigger}%半倉落袋")
            update_position_tpsl_on_okx(exchange, sym, side, remain_qty, base_tp_px, lock_sl)

            POSITION_STAGE_MAP[pos_key] = 2
            pos['contracts']     = remain_qty
            pos['sl_trigger_px'] = lock_sl
            pos['tp_trigger_px'] = base_tp_px
            pos['stage_badge']   = f"💰 已平半倉 & 止損鎖 +{s2_lock_p}%"
            pos['reason']        = f"💰【已落袋50%倉位】剩餘 {remain_qty} 張止損已鎖定於開倉價 +{s2_lock_p}% (${lock_sl})"
            state_changed = True

            send_discord_alert(
                title=f"💰 階段二半倉落袋 & +{s2_lock_p}%鎖利：{sym}",
                description=f"**{sym} ({side})** 含槓桿收益率達 **+{roi_pct:.2f}%**！\n已自動市價平倉 **50% ({half_qty}張)** 落袋為安，並將剩餘 **{remain_qty}張** 的止損單更新至 **${lock_sl:,.2f}**！",
                color=3066993
            )

        # 🛡️ 6. 階段一 (ROI >= +5% / 逆勢 +4%)：撤除舊止損單，上移至開倉價 +0.25% 保本
        elif roi_pct >= s1_trigger and cur_stage < 1:
            lock_be_sl = round(entry_px * (1 + STAGE1_PRICE_LOCK_PCT / 100.0) if side == 'LONG' else entry_px * (1 - STAGE1_PRICE_LOCK_PCT / 100.0), 2)
            update_position_tpsl_on_okx(exchange, sym, side, contracts, base_tp_px, lock_be_sl)
            POSITION_STAGE_MAP[pos_key] = 1
            pos['sl_trigger_px'] = lock_be_sl
            pos['tp_trigger_px'] = base_tp_px
            pos['stage_badge']   = "🛡️ 已鎖保本 (+0.25%)"
            pos['reason']        = f"🛡️【ROI滿+{s1_trigger}%已鎖保本】舊止損已撤除，新止損鎖定於 ${lock_be_sl}"
            state_changed = True

            send_discord_alert(
                title=f"🛡️ 階段一保本鎖利啟動：{sym}",
                description=f"**{sym} ({side})** 含槓桿收益率達 **+{roi_pct:.2f}%**！\n已撤銷原初始止損單，並將 OKX 止損價同步上移至 **${lock_be_sl:,.2f} (開倉價 +0.25%)**！",
                color=5763719
            )

        # 🌾 7. 動能衰竭提前收割
        elif roi_pct >= EARLY_HARVEST_ROI_PCT and cur_stage < 2:
            is_weak = m_info.get("momentum_weakening_long") if side == "LONG" else m_info.get("momentum_weakening_short")
            if is_weak:
                cancel_existing_algo_orders_for_symbol(exchange, sym)
                close_partial_or_full_position(exchange, sym, side, contracts, "動能轉弱提前收割")
                POSITION_STAGE_MAP.pop(pos_key, None)
                state_changed = True

    return state_changed


def execute_pool_orders(exchange, pool_name, orders_list, pool_budget_usdt, market_data, reason_lookup, held_symbols_map, total_equity):
    """
    🌟 v10.5 建倉引擎：
    結合『BTC 領頭羊風向鎖』、『量價確認權重微調』、『馬可維茲連敗降權』與『死守 -8.8% 觸發止損鐵律』！
    """
    valid_orders = [o for o in orders_list[:MAX_ORDERS_PER_HALF] if str(o.get("action", "")).upper() in ["BUY", "SELL"]]
    if not valid_orders or pool_budget_usdt < 30:
        return False

    any_placed = False
    consec_losses = AI_MEMORY_BANK.get("consecutive_losses", {})

    # 計算『連敗降權 × 量價確認』後的動態權重
    adjusted_weights = []
    for o in valid_orders:
        s_sym = o.get("symbol", "ETH/USDT:USDT")
        w = max(0.5, min(3.0, float(o.get("weight", 1.0))))
        if consec_losses.get(s_sym, 0) >= 2:
            w *= 0.5
        vol_ratio = float(market_data.get(s_sym, {}).get("vol_spike_ratio", 1.0))
        if vol_ratio < 0.65:
            w *= 0.70  # 極度縮量，防假突破降權 30%
        elif vol_ratio >= 1.35:
            w *= 1.20  # 帶量突破，加碼 20%
        adjusted_weights.append(round(w, 2))

    total_weight = sum(adjusted_weights) if sum(adjusted_weights) > 0 else 1.0

    counter_trend_held_count = 0
    for h_sym, h_side in held_symbols_map.items():
        h_t1h = market_data.get(h_sym, {}).get("trend_1h", "BULLISH")
        if (h_side == "LONG" and h_t1h == "BEARISH") or (h_side == "SHORT" and h_t1h == "BULLISH"):
            counter_trend_held_count += 1

    for idx, ord_spec in enumerate(valid_orders, start=1):
        sym = ord_spec.get("symbol", "ETH/USDT:USDT")
        if sym not in WHITELIST_SYMBOLS:
            sym = "ETH/USDT:USDT"

        m_info      = market_data.get(sym, {})
        anchored_tp = float(m_info.get("anchored_tp_pct", 2.2))
        anchored_sl = float(m_info.get("anchored_sl_pct", 1.5))
        safe_lev    = int(m_info.get("max_safe_leverage", 5))
        trend_1h    = m_info.get("trend_1h", "BULLISH")
        trend_15m   = m_info.get("trend", "BULLISH")
        rsi_val     = float(m_info.get("rsi_14", 50.0))
        btc_bias    = m_info.get("btc_master_bias", "NEUTRAL_NORMAL")

        action   = str(ord_spec.get("action", "BUY")).upper()
        weight   = adjusted_weights[idx - 1]

        # 🧭 1. BTC 領頭羊大盤風向鎖：防止 ETH/SOL 在 BTC 急殺時逆勢做多、或在 BTC 急拉時逆勢做空
        if sym != "BTC/USDT:USDT":
            if btc_bias == "DUMP_WARNING_NO_ALT_LONG" and action == "BUY":
                action = "SELL"
            elif btc_bias == "PUMP_WARNING_NO_ALT_SHORT" and action == "SELL":
                action = "BUY"

        # 🚫 2. 防摸頂擋火車保護：若 1H 與 15m 雙周期多頭共振且 RSI < 78，禁止逆勢做空
        if action == "SELL" and trend_1h == "BULLISH" and trend_15m == "BULLISH" and rsi_val < 78:
            action = "BUY"
        elif action == "BUY" and trend_1h == "BEARISH" and trend_15m == "BEARISH" and rsi_val > 22:
            action = "SELL"

        is_counter = (action == "BUY" and trend_1h == "BEARISH") or (action == "SELL" and trend_1h == "BULLISH")
        if is_counter and counter_trend_held_count >= 1:
            action = "BUY" if trend_1h == "BULLISH" else "SELL"
            is_counter = False

        # 🛑 3. 決定槓桿與『死守 -10% ROI (設 -8.8% 觸發)』的幣價止損百分比
        ai_lev   = int(ord_spec.get("leverage", safe_lev))
        leverage = max(3, min(6, min(ai_lev, safe_lev)))
        if is_counter:
            leverage = min(5, leverage)

        max_price_sl_for_10pct_roi = round(MAX_SL_ROI_TRIGGER_PCT / float(leverage), 2)
        raw_sl = float(ord_spec.get("sl_pct", anchored_sl))
        sl_pct = round(max(0.9, min(raw_sl, anchored_sl, max_price_sl_for_10pct_roi)), 2)

        raw_tp = float(ord_spec.get("tp_pct", anchored_tp))
        if is_counter:
            tp_pct   = round(max(1.3, min(1.8, anchored_tp * 0.75)), 2)
            mode_tag = f"⚡逆勢快單(止損鎖-{round(sl_pct*leverage,1)}%)"
        else:
            tp_pct   = round(max(1.6, min(3.2, min(raw_tp, anchored_tp * 1.15))), 2)
            mode_tag = f"🚀順應1H大勢(止損鎖-{round(sl_pct*leverage,1)}%)"

        loss_streak = consec_losses.get(sym, 0)
        streak_note = f"[連敗{loss_streak}次降權]" if loss_streak >= 2 else ""
        clean_ai_reason = str(ord_spec.get("reason", f"{pool_name} 順勢策略")).replace("【Half-B 逆勢搶反彈】", "")
        reason = f"[{mode_tag}]{streak_note} {clean_ai_reason}"

        reason_lookup[sym] = {
            "tp_pct": tp_pct, "sl_pct": sl_pct, "leverage": leverage,
            "pool": pool_name, "reason": reason
        }

        if sym in held_symbols_map:
            continue

        current_price = float(m_info.get("last_price", 0))
        if current_price <= 0:
            continue

        desired_margin = round(pool_budget_usdt * (weight / total_weight), 2)
        contracts, safe_margin = calculate_safe_contracts(exchange, sym, desired_margin, leverage, current_price, total_equity)
        if contracts <= 0:
            continue

        side = 'buy' if action == 'BUY' else 'sell'
        tp_price = round(current_price * (1 + tp_pct / 100.0) if side == 'buy' else current_price * (1 - tp_pct / 100.0), 2)
        sl_price = round(current_price * (1 - sl_pct / 100.0) if side == 'buy' else current_price * (1 + sl_pct / 100.0), 2)

        order = place_okx_order_with_tpsl(exchange, sym, action, contracts, leverage, tp_price, sl_price, pool_name)
        if order:
            held_symbols_map[sym] = 'LONG' if side == 'buy' else 'SHORT'
            if is_counter:
                counter_trend_held_count += 1
            AI_MEMORY_BANK.setdefault("entry_snapshots", {})[sym] = {
                "side": held_symbols_map[sym],
                "reason": reason,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "leverage": leverage,
                "multi_tf_regime": m_info.get("multi_tf_regime", "GENERAL"),
                "rsi_14": rsi_val,
                "rsi_zone": m_info.get("rsi_zone", "NEUTRAL"),
                "bb_pos_pct": m_info.get("bb_pos_pct", 50.0),
                "funding_rate_pct": m_info.get("funding_rate_pct", 0.01),
                "entry_time": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")
            }
            any_placed = True

    return any_placed


def fetch_okx_positions_history_and_analytics(exchange, market_data, reason_lookup, ai_summary, total_equity, total_upl):
    history_records = []
    seen_pos_ids = set()
    hist_reasons = AI_MEMORY_BANK.setdefault("historical_reasons", {})
    snapshots    = AI_MEMORY_BANK.get("entry_snapshots", {})

    raw_pos_history = []
    try:
        if hasattr(exchange, 'fetch_positions_history'):
            raw_pos_history = exchange.fetch_positions_history(WHITELIST_SYMBOLS, limit=50)
        else:
            res = exchange.privateGetAccountPositionsHistory({'instType': 'SWAP', 'limit': '50'})
            raw_pos_history = res.get('data', [])
    except Exception:
        try:
            res = exchange.privateGetAccountPositionsHistory({'instType': 'SWAP', 'limit': '50'})
            raw_pos_history = res.get('data', [])
        except Exception:
            raw_pos_history = []

    today_str = datetime.now(TW_TZ).strftime("%Y-%m-%d")
    now_ms    = int(time.time() * 1000)

    today_realized_pnl    = 0.0
    today_trades_count    = 0
    all_time_realized_pnl = 0.0
    gross_profit_usdt     = 0.0
    gross_loss_usdt       = 0.0
    total_fees_usdt       = 0.0
    win_count             = 0
    loss_count            = 0
    daily_pnl_map         = {}
    asset_win_map         = {s: {"wins": 0, "total": 0, "pnl": 0.0} for s in WHITELIST_SYMBOLS}

    for item in raw_pos_history:
        info = item.get('info', item) if isinstance(item, dict) else {}
        inst_id = info.get('instId', '')
        sym = item.get('symbol') or (inst_id.replace('-SWAP', '').replace('-', '/') + ':USDT' if inst_id else 'ETH/USDT:USDT')

        pos_id = str(info.get('posId') or item.get('id') or '')
        c_time = str(info.get('cTime') or item.get('timestamp') or '')
        u_time = str(info.get('uTime') or c_time or '')

        c_ts_ms = int(c_time) if c_time.isdigit() else now_ms
        u_ts_ms = int(u_time) if u_time.isdigit() else c_ts_ms

        if c_ts_ms < ACCOUNT_EPOCH_MS and u_ts_ms < ACCOUNT_EPOCH_MS:
            continue

        unique_key = f"{pos_id}_{u_time}"
        if unique_key in seen_pos_ids:
            continue
        seen_pos_ids.add(unique_key)

        direction_raw = str(info.get('direction') or item.get('side') or 'long').lower()
        side_label = "做多倉位結算 (LONG)" if 'long' in direction_raw or 'buy' in direction_raw else "做空倉位結算 (SHORT)"

        open_avg_px  = float(info.get('openAvgPx') or item.get('entryPrice') or 0.0)
        close_avg_px = float(info.get('closeAvgPx') or item.get('closePrice') or 0.0)
        close_qty    = float(info.get('closeTotalPos') or item.get('contracts') or 1.0)
        lev          = info.get('lever') or item.get('leverage') or 5

        realized_pnl  = float(info.get('realizedPnl') or info.get('pnl') or item.get('realizedPnl') or 0.0)
        pnl_ratio_raw = float(info.get('pnlRatio') or item.get('percentage') or 0.0)
        pnl_pct       = round(pnl_ratio_raw * 100, 2) if abs(pnl_ratio_raw) < 5 else round(pnl_ratio_raw, 2)
        fee_val       = abs(float(info.get('fee') or 0.0))

        dt_obj   = datetime.fromtimestamp(u_ts_ms / 1000.0, tz=TW_TZ)
        dt_clean = dt_obj.strftime("%Y-%m-%d %H:%M:%S")
        day_key  = dt_obj.strftime("%Y-%m-%d")

        all_time_realized_pnl += realized_pnl
        total_fees_usdt       += fee_val
        if realized_pnl > 0:
            win_count         += 1
            gross_profit_usdt += realized_pnl
        elif realized_pnl < 0:
            loss_count      += 1
            gross_loss_usdt += abs(realized_pnl)

        if day_key == today_str:
            today_realized_pnl += realized_pnl
            today_trades_count += 1

        daily_pnl_map[day_key] = round(daily_pnl_map.get(day_key, 0.0) + realized_pnl, 2)
        if sym in asset_win_map:
            asset_win_map[sym]["total"] += 1
            asset_win_map[sym]["pnl"] = round(asset_win_map[sym]["pnl"] + realized_pnl, 2)
            if realized_pnl > 0:
                asset_win_map[sym]["wins"] += 1

        if unique_key in hist_reasons:
            locked_reason = hist_reasons[unique_key]
        else:
            snap_reason = snapshots.get(sym, {}).get("reason")
            locked_reason = snap_reason or f"【{sym.split('/')[0]} 倉位結算】開倉 ${open_avg_px:,.2f} ➔ 平倉 ${close_avg_px:,.2f} ({lev}x 槓桿，結算 ROI {pnl_pct:+.2f}%)"
            hist_reasons[unique_key] = locked_reason

        pnl_sign = "+" if realized_pnl >= 0 else ""
        raw_audit = f"{pos_id}_{sym}_{open_avg_px}_{close_avg_px}_{realized_pnl}_{dt_clean}"
        sha_hash  = "0x" + hashlib.sha256(raw_audit.encode('utf-8')).hexdigest()[:16]
        short_id  = f"POS-{pos_id[-4:]}{u_time[-3:-1]}" if len(pos_id) >= 4 and len(u_time) >= 3 else f"POS-{u_ts_ms % 1000000}"

        history_records.append({
            "id": short_id,
            "full_id": unique_key,
            "timestamp_ms": u_ts_ms,
            "open_timestamp_ms": c_ts_ms,
            "time": dt_clean,
            "market": "crypto",
            "marketBadge": f"📦 歷史倉位 ({close_qty}張)",
            "asset": sym,
            "direction": side_label,
            "leverage": f"{lev}x 倉位槓桿",
            "pool": f"平倉張數: {close_qty} 張合約",
            "entryExit": f"開倉均價 ${open_avg_px:,.2f} ➔ 平倉均價 ${close_avg_px:,.2f}",
            "roi": f"已實現損益 {pnl_sign}${realized_pnl:,.2f} USDT ({pnl_sign}{pnl_pct:.2f}%)",
            "pnl": f"倉位結算收益: {pnl_sign}${realized_pnl:,.2f} USDT",
            "realized_pnl_num": round(realized_pnl, 2),
            "fee_num": round(fee_val, 4),
            "fee": f"倉位累計手續費 {fee_val:.4f} USDT",
            "rr": f"開倉 ${open_avg_px:,.2f} / 平倉 ${close_avg_px:,.2f}",
            "reason": locked_reason,
            "hash": sha_hash
        })

        if unique_key not in NOTIFIED_POS_IDS:
            NOTIFIED_POS_IDS.add(unique_key)
            if (now_ms - u_ts_ms) <= 1200000:
                is_win = realized_pnl >= 0
                send_discord_alert(
                    title=f"{'🟢 獲利平倉結算' if is_win else '🔴 止損平倉結算'} | {sym}",
                    description=f"**{side_label}** 已於 OKX 完成平倉結算！",
                    color=3066993 if is_win else 15158332,
                    fields=[
                        {"name": "💰 本單已實現損益", "value": f"**{pnl_sign}${realized_pnl:,.2f} USDT ({pnl_sign}{pnl_pct:.2f}%)**", "inline": False},
                        {"name": "📅 今日累計已實現盈虧", "value": f"`${today_realized_pnl:+,.2f} USDT` ({today_trades_count}筆)", "inline": True},
                        {"name": "🏆 本期累計總實現盈虧 (自11/05 17:01起)", "value": f"`${all_time_realized_pnl:+,.2f} USDT`", "inline": True},
                        {"name": "🏦 當前 OKX 總資產", "value": f"`${total_equity:,.2f} USDT`", "inline": False}
                    ]
                )

    history_records.sort(key=lambda x: x.get('timestamp_ms', 0), reverse=True)
    update_ai_memory_bank_from_history(history_records)

    total_settled = win_count + loss_count
    win_rate_pct  = round((win_count / total_settled) * 100, 1) if total_settled > 0 else 0.0
    profit_factor = round(gross_profit_usdt / gross_loss_usdt, 2) if gross_loss_usdt > 0 else (3.0 if gross_profit_usdt > 0 else 1.0)
    implied_initial_capital = round(total_equity - all_time_realized_pnl - total_upl, 2)

    daily_series = [
        {"date": d_k, "pnl": daily_pnl_map[d_k]}
        for d_k in sorted(daily_pnl_map.keys(), reverse=True)[:7]
    ]

    performance_stats = {
        "epoch_start": "2026-11-05 17:01:16",
        "today_date": today_str,
        "today_realized_pnl": round(today_realized_pnl, 2),
        "today_trades_count": today_trades_count,
        "all_time_realized_pnl": round(all_time_realized_pnl, 2),
        "gross_profit_usdt": round(gross_profit_usdt, 2),
        "gross_loss_usdt": round(gross_loss_usdt, 2),
        "total_fees_usdt": round(total_fees_usdt, 2),
        "implied_initial_capital": implied_initial_capital,
        "profit_factor": profit_factor,
        "total_settled_count": total_settled,
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate_pct": win_rate_pct,
        "daily_pnl_history": daily_series,
        "asset_performance": asset_win_map
    }

    return history_records, performance_stats


def compute_exact_position_roi(side_upper, entry_px, mark_px, lev_num, upl, margin_used):
    if margin_used > 0 and abs(upl) > 0:
        return round((upl / margin_used) * 100.0, 2)
    if entry_px > 0 and mark_px > 0:
        raw_move = ((mark_px - entry_px) / entry_px) if side_upper == 'LONG' else ((entry_px - mark_px) / entry_px)
        return round(raw_move * lev_num * 100.0, 2)
    return 0.0


def run_single_cycle(exchange, cycle_num=1):
    global LAST_AI_PLAN_CACHE
    now_str = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n==================== [巡檢 #{cycle_num} | {now_str} (TW)] ====================", flush=True)

    balance   = exchange.fetch_balance()
    real_usdt = round(float(balance.get('USDT', {}).get('total', 0.0)), 2)
    free_usdt = round(float(balance.get('USDT', {}).get('free', 0.0)), 2)

    held_symbols_map = {}
    try:
        cur_positions = exchange.fetch_positions(WHITELIST_SYMBOLS)
        for pos in cur_positions:
            if float(pos.get('contracts') or 0) > 0:
                sym = pos.get('symbol')
                held_symbols_map[sym] = pos.get('side', 'long').upper()
    except Exception as e:
        print(f"⚠️ 初始倉位讀取提示: {e}", flush=True)

    market_data = fetch_whitelist_market_data(exchange)
    prelim_history, perf_stats = fetch_okx_positions_history_and_analytics(exchange, market_data, {}, "", real_usdt, 0.0)

    mistake_memory = [
        {"asset": h["asset"], "direction": h["direction"], "roi": h["roi"]}
        for h in prelim_history[:5]
    ]

    should_call_ai = (
        LAST_AI_PLAN_CACHE is None
        or len(held_symbols_map) < 3
        or (cycle_num % SLOW_AI_EVERY_N_TICKS == 0)
    )

    if should_call_ai:
        news_list = fetch_live_crypto_news()
        LAST_AI_PLAN_CACHE = ask_gemini_quant_brain(
            market_data, news_list, held_symbols_map, mistake_memory
        )
        print(f"🧠 [AI 決策更新]: {LAST_AI_PLAN_CACHE.get('overall_summary')}", flush=True)
    else:
        news_list = ["快軌 20 秒級別高速監控中 (四階段無限鎖利 + 死守-10%止損 + 3.5h殭屍單清理)"]
        print(f"⚡ [快軌盯盤] 總資產: ${real_usdt:,.2f} | 今日已實現: ${perf_stats['today_realized_pnl']:+,.2f} | 本期累計: ${perf_stats['all_time_realized_pnl']:+,.2f}", flush=True)

    ai_plan = LAST_AI_PLAN_CACHE or build_fallback_plan(market_data)

    # 🛡️ v10.5 單日盈虧動態火力調節器：若今日已虧損逾 -3% 則切換 50% 半火力防守；若已大賺逾 +$8000 則切換 80% 鎖利火力
    today_pnl_val = float(perf_stats.get("today_realized_pnl", 0.0))
    daily_dd_pct  = (today_pnl_val / real_usdt * 100.0) if real_usdt > 0 else 0.0
    if daily_dd_pct <= DAILY_DRAWDOWN_HALF_PCT:
        daily_throttle_mult = 0.50
        print(f"🛡️ [單日回撤保護啟動] 今日已實現 {today_pnl_val:+.2f} ({daily_dd_pct:.2f}%)，自動切換為 50% 半火力防守模式！", flush=True)
    elif today_pnl_val >= DAILY_PROFIT_LOCK_USDT:
        daily_throttle_mult = 0.80
        print(f"💰 [單日獲利鎖定啟動] 今日已大賺 +${today_pnl_val:,.2f}，保留 20% 利潤，以 80% 穩健火力運行！", flush=True)
    else:
        daily_throttle_mult = 1.00

    used_margin_now = max(0.0, real_usdt - free_usdt)
    normal_pool_target = real_usdt * POOL_NORMAL_RATIO * daily_throttle_mult
    remaining_normal_budget = max(0.0, min(free_usdt * 0.78, normal_pool_target - used_margin_now))

    half_a_budget = round(remaining_normal_budget / 2.0, 2)
    half_b_budget = round(remaining_normal_budget / 2.0, 2)

    reason_lookup = {}
    order_placed = False
    if remaining_normal_budget >= 80 and len(held_symbols_map) < 3:
        p1 = execute_pool_orders(exchange, "Half-A 領頭羊順勢池 (40%)", ai_plan.get("half_a_orders", []), half_a_budget, market_data, reason_lookup, held_symbols_map, real_usdt)
        p2 = execute_pool_orders(exchange, "Half-B 量價定錨池 (40%)", ai_plan.get("half_b_orders", []), half_b_budget, market_data, reason_lookup, held_symbols_map, real_usdt)
        order_placed = p1 or p2

    balance   = exchange.fetch_balance()
    real_usdt = round(float(balance.get('USDT', {}).get('total', 0.0)), 2)
    free_usdt = round(float(balance.get('USDT', {}).get('free', 0.0)), 2)
    used_usdt = round(real_usdt - free_usdt, 2)

    active_positions = []
    total_upl        = 0.0
    snapshots        = AI_MEMORY_BANK.get("entry_snapshots", {})

    try:
        positions = exchange.fetch_positions(WHITELIST_SYMBOLS)
        for pos in positions:
            contracts = float(pos.get('contracts') or 0)
            if contracts > 0:
                sym          = pos.get('symbol')
                side_u       = pos.get('side', 'long').upper()
                upl          = round(float(pos.get('unrealizedPnl') or 0.0), 2)
                margin_used  = round(float(pos.get('initialMargin') or pos.get('collateral') or 0.0), 2)
                notional_val = round(float(pos.get('notional') or 0.0), 2)
                entry_px     = round(float(pos.get('entryPrice') or 0.0), 2)
                lev_num      = float(pos.get('leverage') or 5)
                m_info       = market_data.get(sym, {})
                mark_px      = round(float(pos.get('markPrice') or m_info.get('last_price', 0)), 2)

                roi_pct      = compute_exact_position_roi(side_u, entry_px, mark_px, lev_num, upl, margin_used)
                total_upl   += upl

                snap_info = snapshots.get(sym, {})
                r_info    = reason_lookup.get(sym, {})

                max_sl_p = round(MAX_SL_ROI_TRIGGER_PCT / max(1.0, lev_num), 2)
                tp_p_val = float(snap_info.get("tp_pct") or r_info.get("tp_pct") or m_info.get("anchored_tp_pct", 2.2))
                raw_sl_p = float(snap_info.get("sl_pct") or r_info.get("sl_pct") or m_info.get("anchored_sl_pct", 1.5))
                sl_p_val = round(min(raw_sl_p, max_sl_p), 2)

                pos_key   = f"{sym}_{side_u}"
                cur_stage = int(POSITION_STAGE_MAP.get(pos_key, 0))
                trend_1h  = m_info.get("trend_1h", "BULLISH")
                is_cnt    = (side_u == "LONG" and trend_1h == "BEARISH") or (side_u == "SHORT" and trend_1h == "BULLISH")

                if cur_stage == 4:
                    sl_trig = round(entry_px * (1 + STAGE4_PRICE_LOCK_PCT / 100.0) if side_u == 'LONG' else entry_px * (1 - STAGE4_PRICE_LOCK_PCT / 100.0), 2)
                    stage_badge = f"🔥 四階暴利鎖 (+{STAGE4_PRICE_LOCK_PCT}%)"
                elif cur_stage == 3:
                    sl_trig = round(entry_px * (1 + STAGE3_PRICE_LOCK_PCT / 100.0) if side_u == 'LONG' else entry_px * (1 - STAGE3_PRICE_LOCK_PCT / 100.0), 2)
                    stage_badge = f"🚀 三階推鎖 (+{STAGE3_PRICE_LOCK_PCT}%)"
                elif cur_stage == 2:
                    lock_p = COUNTER_STAGE2_LOCK_PCT if is_cnt else STAGE2_PRICE_LOCK_PCT
                    sl_trig = round(entry_px * (1 + lock_p / 100.0) if side_u == 'LONG' else entry_px * (1 - lock_p / 100.0), 2)
                    stage_badge = f"💰 已平半倉 & 止損鎖 +{lock_p}%"
                elif cur_stage == 1:
                    sl_trig = round(entry_px * (1 + STAGE1_PRICE_LOCK_PCT / 100.0) if side_u == 'LONG' else entry_px * (1 - STAGE1_PRICE_LOCK_PCT / 100.0), 2)
                    stage_badge = "🛡️ 已鎖保本 (+0.25%)"
                else:
                    sl_trig = round(entry_px * (1 - sl_p_val / 100.0) if side_u == 'LONG' else entry_px * (1 + sl_p_val / 100.0), 2)
                    stage_badge = f"🛑 止損死守-{round(sl_p_val*lev_num,1)}% (四階鎖利監控)"

                tp_trig = round(entry_px * (1 + tp_p_val / 100.0) if side_u == 'LONG' else entry_px * (1 - tp_p_val / 100.0), 2)

                pos_obj = {
                    "symbol": sym,
                    "side": side_u,
                    "contracts": contracts,
                    "leverage": f"{int(lev_num)}x",
                    "entry_price": entry_px,
                    "mark_price": mark_px,
                    "margin_usdt": margin_used,
                    "notional_usdt": notional_val,
                    "unrealized_pnl": upl,
                    "roi_pct": roi_pct,
                    "tp_pct": tp_p_val,
                    "sl_pct": sl_p_val,
                    "tp_trigger_px": tp_trig,
                    "sl_trigger_px": sl_trig,
                    "stage": cur_stage,
                    "stage_badge": stage_badge,
                    "reason": snap_info.get("reason") or r_info.get("reason") or f"1H:{trend_1h} | 含槓桿最大止損鎖定 -{round(sl_p_val*lev_num,1)}% (四階無限鎖利)"
                }
                active_positions.append(pos_obj)
    except Exception as e:
        print(f"⚠️ 讀取當前倉位提示: {e}", flush=True)

    fast_changed = fast_track_smart_harvest_and_hedge(exchange, active_positions, market_data, real_usdt, total_upl)

    positions_history_formatted, final_perf_stats = fetch_okx_positions_history_and_analytics(
        exchange, market_data, reason_lookup, ai_plan.get('overall_summary', ''), real_usdt, total_upl
    )

    live_payload = {
        "updated_at": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "epoch_cutoff": "2026-11-05 17:01:00",
        "real_usdt": real_usdt,
        "free_usdt": free_usdt,
        "used_usdt": used_usdt,
        "allocations": {
            "normal_80_usdt": round(real_usdt * POOL_NORMAL_RATIO * daily_throttle_mult, 2),
            "hedge_10_usdt": round(real_usdt * POOL_HEDGE_RATIO, 2),
            "sniper_10_usdt": round(real_usdt * POOL_SNIPER_RATIO, 2)
        },
        "performance_stats": final_perf_stats,
        "ai_memory_bank": AI_MEMORY_BANK,
        "position_stage_map": POSITION_STAGE_MAP,
        "total_unrealized_pnl": round(total_upl, 2),
        "total_crypto_roi_pct": round((total_upl / real_usdt) * 100, 2) if real_usdt > 0 else 0.0,
        "active_positions": active_positions,
        "market_prices": market_data,
        "latest_news": news_list,
        "ai_broadcast_signals": {
            "updated_at": datetime.now(TW_TZ).strftime("%H:%M:%S"),
            "confidence": ai_plan.get("overall_confidence", 91),
            "summary": ai_plan.get("overall_summary", ""),
            "half_a_orders": ai_plan.get("half_a_orders", []),
            "half_b_orders": ai_plan.get("half_b_orders", [])
        },
        "ai_last_decision": {
            "action": f"今日已實現 ${final_perf_stats['today_realized_pnl']:+,.2f} | 本期累計 ${final_perf_stats['all_time_realized_pnl']:+,.2f} (盈虧比 {final_perf_stats['profit_factor']}x)",
            "confidence": ai_plan.get("overall_confidence", 91),
            "reason": ai_plan.get("overall_summary", "")
        },
        "orders": positions_history_formatted
    }

    with open("live_data.json", "w", encoding="utf-8") as f:
        json.dump(live_payload, f, ensure_ascii=False, indent=2)

    if order_placed or fast_changed or cycle_num == 1:
        push_live_data_immediately(force=(order_placed or fast_changed))
    else:
        push_live_data_immediately(force=False)


def main():
    print("=========================================================================", flush=True)
    print("🚀 QuantGuard AI v10.5 - 四階無限鎖利 × BTC領頭羊風向鎖 × 單日動態火力 × 殭屍單清理", flush=True)
    print("=========================================================================", flush=True)
    load_previous_state()
    exchange = init_okx_exchange()
    start_ts = time.time()
    cycle = 0

    while True:
        cycle += 1
        try:
            run_single_cycle(exchange, cycle)
        except Exception as e:
            print(f"❌ 巡檢發生異常: {e}", flush=True)

        elapsed = time.time() - start_ts
        if IS_GITHUB_ACTIONS and elapsed >= MAX_JOB_RUNTIME_SEC:
            push_live_data_immediately(force=True)
            print(f"✅ 本輪雲端長駐巡檢已滿 {int(elapsed)} 秒，安全交棒給下一輪排程！", flush=True)
            break

        if not RUN_CONTINUOUS_LOOP:
            break

        time.sleep(FAST_TRACK_INTERVAL)


if __name__ == "__main__":
    main()
