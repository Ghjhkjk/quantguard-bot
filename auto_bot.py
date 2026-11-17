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
# ⚙️ 2. v10.9 機構級風控完全體參數 (全參數採用「含槓桿後真實保證金 ROI %」)
# ==========================================================
WHITELIST_SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']

POOL_NORMAL_RATIO       = 0.80  # 80% 常態雙核心池
POOL_HEDGE_RATIO        = 0.10  # 10% 緊急急跌對沖池
POOL_SNIPER_RATIO       = 0.10  # 10% 黑天鵝突發狙擊池

MAX_ORDERS_PER_HALF     = 3
MAX_SINGLE_MARGIN_RATIO = 0.28  # 單筆訂單保證金上限為總資產 28%

# 🛑 鐵律一：收緊初始止損至 -7.5% ROI 觸發 (防快市滑價超過 -10%，每單少賠 $300~$400！)
MAX_SL_ROI_TRIGGER_PCT  = 7.5   # 掛單觸發止損設於「含槓桿 ROI -7.5%」(加計滑價與手續費後死守在 -8.2% ~ -8.8% 內)
HARD_CUT_SL_ROI_PCT     = -8.2  # 快軌 20 秒硬斷頭保護：一旦實時含槓桿 ROI <= -8.2%，立即市價平倉！

# 💰 鐵律二：40% 落袋 + 60% 主力尾倉留足 5.5% ROI 緩衝空間 (解決贏幾百、賠幾千的不對稱！)
STAGE1_CLOSE_RATIO      = 0.40  # 【階段一】當含槓桿 ROI 達 +7.0% -> 先平倉 40% 落袋現金！
STAGE1_TRIGGER_ROI_PCT  = 7.0
STAGE1_LOCK_ROI_PCT     = 1.5   #           剩餘 60% 主力倉止損移至「含槓桿 ROI +1.5%」(留 5.5% ROI 抗洗盤緩衝，穩賺保底)！

STAGE2_TRIGGER_ROI_PCT  = 12.0  # 【階段二】當剩餘 60% 倉位含槓桿 ROI 達 +12.0% -> 止損上推鎖死在「含槓桿 ROI +6.0%」！
STAGE2_LOCK_ROI_PCT     = 6.0

STAGE3_TRIGGER_ROI_PCT  = 18.0  # 【階段三】當剩餘 60% 倉位含槓桿 ROI 達 +18.0% -> 止損上推鎖死在「含槓桿 ROI +12.0%」，止盈外推 1.4 倍！
STAGE3_LOCK_ROI_PCT     = 12.0

DEFAULT_TARGET_TP_ROI   = 15.0  # 預設初始總止盈目標為「含槓桿 ROI +15.0%」

# ⏳ 鐵律三：止損/止盈冷卻鎖與防追高低乖離限制 (解決 11/15 地板追空四連敗)
LOSS_COOLDOWN_MINUTES   = 60    # 止損出場後，該幣種強制冷卻 60 分鐘禁止重開！
WIN_COOLDOWN_MINUTES    = 15    # 止盈出場後，該幣種至少冷卻 15 分鐘等下一根 K 線回踩！
MAX_MA20_DIST_PCT       = 2.2   # 若現價偏離 15m MA20 超過 2.2%，視為已過度延伸(利多/利空出盡)，禁止追單！

IS_GITHUB_ACTIONS     = os.environ.get("GITHUB_ACTIONS") == "true"
RUN_CONTINUOUS_LOOP   = True
FAST_TRACK_INTERVAL   = 20    # 快軌：每 20 秒高速巡檢一次 OKX
SLOW_AI_EVERY_N_TICKS = 15    # 慢軌：每 15 個快軌 (約 5 分鐘) 或有空倉槽位時呼叫 Gemini AI
MAX_JOB_RUNTIME_SEC   = int(os.environ.get("MAX_JOB_RUNTIME_SEC", "210"))

LAST_AI_PLAN_CACHE = None
NOTIFIED_POS_IDS   = set()
POSITION_STAGE_MAP = {}
LAST_GIT_PUSH_TS   = 0.0

AI_MEMORY_BANK = {
    "entry_snapshots": {},
    "historical_reasons": {},
    "processed_pos_keys": [],
    "pattern_stats": {},
    "symbol_direction_stats": {},
    "consecutive_losses": {"BTC/USDT:USDT": 0, "ETH/USDT:USDT": 0, "SOL/USDT:USDT": 0},
    "last_exit_info": {},
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


def roi_to_price(entry_px, side_upper, target_leveraged_roi_pct, leverage_num):
    """
    🌟 核心換算器：將『含槓桿後的目標 ROI (%)』精準除以『槓桿倍率』，換算成 OKX 幣價觸發價！
    """
    lev = max(1.0, float(leverage_num))
    price_move_fraction = (float(target_leveraged_roi_pct) / lev) / 100.0
    if side_upper == 'LONG':
        return round(entry_px * (1.0 + price_move_fraction), 2)
    else:
        return round(entry_px * (1.0 - price_move_fraction), 2)


def is_us_market_open_chop_window():
    """
    🕒 華爾街美股開盤防雙巴插針鎖：
    每天台灣時間 21:15 ~ 22:00 為美股開盤與宏觀數據洗盤最劇烈時段，期間暫停開立新倉，舊倉照常鎖利保護。
    """
    now_tw = datetime.now(TW_TZ)
    if now_tw.weekday() < 5:  # 週一至週五
        mins_of_day = now_tw.hour * 60 + now_tw.minute
        if (21 * 60 + 15) <= mins_of_day <= (22 * 60):
            return True
    return False


def send_discord_alert(title, description, color=3066993, fields=None):
    if not DISCORD_WEBHOOK_URL or not DISCORD_WEBHOOK_URL.startswith("http"):
        return
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "QuantGuard AI v10.9 | 全場同向紀律 × 60分冷卻鎖 × 40/60鎖利終端"}
    }
    if fields:
        embed["fields"] = fields
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=json.dumps({"embeds": [embed]}).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'User-Agent': 'QuantGuardBot/10.9'}
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

        print(f"🧠 [AI 永久記憶庫載入成功] 已累積學習 {len(AI_MEMORY_BANK.get('processed_pos_keys', []))} 筆經驗！", flush=True)
    except Exception as e:
        print(f"⚠️ 載入歷史狀態提示: {e}", flush=True)


def push_live_data_immediately(force=False):
    global LAST_GIT_PUSH_TS
    if not IS_GITHUB_ACTIONS:
        return
    now_ts = time.time()
    if not force and (now_ts - LAST_GIT_PUSH_TS < 45):
        return
    try:
        with open("live_data.json", "r", encoding="utf-8") as f:
            latest_json_content = f.read()

        subprocess.run(["git", "config", "--local", "user.email", "bot@quantguard.ai"], check=False)
        subprocess.run(["git", "config", "--local", "user.name", "QuantGuard AutoBot"], check=False)
        subprocess.run(["git", "add", "live_data.json"], check=False)

        diff_res = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if diff_res.returncode != 0:
            subprocess.run(["git", "commit", "-m", "Real-time sync live_data.json v10.9"], check=False)
            pull_res = subprocess.run(["git", "pull", "--rebase", "-X", "ours", "origin", "main"], check=False)
            if pull_res.returncode != 0:
                subprocess.run(["git", "rebase", "--abort"], check=False)
                subprocess.run(["git", "fetch", "origin", "main"], check=False)
                subprocess.run(["git", "reset", "--hard", "origin/main"], check=False)
                with open("live_data.json", "w", encoding="utf-8") as f:
                    f.write(latest_json_content)
                subprocess.run(["git", "add", "live_data.json"], check=False)
                subprocess.run(["git", "commit", "-m", "Force sync live_data.json v10.9"], check=False)

            subprocess.run(["git", "push", "origin", "main"], check=False)
            LAST_GIT_PUSH_TS = now_ts
            print("🚀 [即時雲端同步] 已成功將最新 live_data.json 推送至網站！", flush=True)
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


def detect_pinbar_wick_signal(ohlcv_15m):
    """
    🕯️ K 線長上下影線防插針濾網 (Pinbar / Wick Filter)：
    檢查最近 2 根 K 線是否出現明顯的『長下影線抄底針 (BULL_PINBAR_NO_SHORT)』或『長上影線拋壓針 (BEAR_PINBAR_NO_LONG)』。
    """
    if len(ohlcv_15m) < 2:
        return "NONE"
    for candle in ohlcv_15m[-2:]:
        o_px, h_px, l_px, c_px = float(candle[1]), float(candle[2]), float(candle[3]), float(candle[4])
        total_range = h_px - l_px
        if total_range <= 0 or c_px <= 0:
            continue
        if (total_range / c_px * 100.0) < 0.35:
            continue
        body       = max(abs(c_px - o_px), total_range * 0.12)
        lower_wick = min(o_px, c_px) - l_px
        upper_wick = h_px - max(o_px, c_px)

        if lower_wick >= body * 1.45 and lower_wick >= total_range * 0.52:
            return "BULL_PINBAR_NO_SHORT"  # 底部出現強力買盤長下影線，嚴禁追空！
        if upper_wick >= body * 1.45 and upper_wick >= total_range * 0.52:
            return "BEAR_PINBAR_NO_LONG"   # 頂部出現強力拋壓長上影線，嚴禁追多！
    return "NONE"


def fetch_whitelist_market_data(exchange):
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
            bb_width_pct = round(((bb_upper - bb_lower) / max(0.0001, ma20)) * 100, 2)

            # 計算現價與 15m MA20 的乖離率 (%)：正值代表高於均線，負值代表低於均線
            ma20_dist_pct = round(((current_price - ma20) / max(0.0001, ma20)) * 100.0, 2)
            wick_signal   = detect_pinbar_wick_signal(ohlcv_15m)

            rsi_14 = compute_rsi(closes, 14)
            if rsi_14 >= 72:
                rsi_zone = "EXTREME_OVERBOUGHT"
            elif rsi_14 <= 28:
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

            natural_sl_pct = round(max(1.25, min(1.9, swing_pct * 0.50)), 2)
            max_safe_lev   = max(4, min(6, int(MAX_SL_ROI_TRIGGER_PCT // natural_sl_pct)))

            enforced_sl_pct = round(MAX_SL_ROI_TRIGGER_PCT / float(max_safe_lev), 2)
            anchored_tp_pct = round(max(2.4, min(3.8, DEFAULT_TARGET_TP_ROI / float(max_safe_lev))), 2)

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
                "ma20_dist_pct": ma20_dist_pct,
                "wick_signal": wick_signal,
                "rsi_14": rsi_14,
                "rsi_zone": rsi_zone,
                "bb_upper": bb_upper,
                "bb_lower": bb_lower,
                "bb_pos_pct": bb_pos_pct,
                "bb_width_pct": bb_width_pct,
                "trend_1h": trend_1h,
                "trend": trend_15m,
                "multi_tf_regime": multi_tf_regime,
                "atr_15m_pct": round(atr_pct, 2),
                "recent_swing_pct": round(swing_pct, 2),
                "anchored_tp_pct": anchored_tp_pct,
                "anchored_sl_pct": enforced_sl_pct,
                "max_safe_leverage": max_safe_lev
            }
        except Exception as e:
            print(f"⚠️ 抓取 {sym} 行情異常: {e}", flush=True)

    # 🧭 全場同向總司令：以 BTC 的 1H 大趨勢決定全場唯一允許的方向 (杜絕 BTC 做空、ETH 卻單獨做多！)
    btc_info    = market_summary.get('BTC/USDT:USDT', {})
    btc_t1h     = btc_info.get('trend_1h', 'BULLISH')
    global_allowed_action = "BUY" if btc_t1h == "BULLISH" else "SELL"

    for s_k in market_summary:
        market_summary[s_k]["btc_global_regime"]     = btc_t1h
        market_summary[s_k]["global_allowed_action"] = global_allowed_action

    return market_summary


def update_ai_memory_bank_from_history(history_records):
    processed_set  = set(AI_MEMORY_BANK.get("processed_pos_keys", []))
    sym_stats      = AI_MEMORY_BANK.get("symbol_direction_stats", {})
    pat_stats      = AI_MEMORY_BANK.get("pattern_stats", {})
    snapshots      = AI_MEMORY_BANK.get("entry_snapshots", {})
    consec_losses  = AI_MEMORY_BANK.get("consecutive_losses", {s: 0 for s in WHITELIST_SYMBOLS})
    last_exit_info = AI_MEMORY_BANK.setdefault("last_exit_info", {})

    # 1. 隨時更新每個幣種「最新一筆平倉」的時間、價格、方向與損益 (供冷卻鎖與防追高低鎖使用)
    for rec in history_records:
        sym = rec.get("asset", "BTC/USDT:USDT")
        ts_ms = int(rec.get("timestamp_ms") or 0)
        if sym not in last_exit_info or ts_ms > int(last_exit_info[sym].get("timestamp_ms", 0)):
            is_lg = "LONG" in str(rec.get("direction", "")).upper()
            last_exit_info[sym] = {
                "timestamp_ms": ts_ms,
                "time_str": rec.get("time", ""),
                "side": "LONG" if is_lg else "SHORT",
                "close_px": float(rec.get("close_px_num", 0.0)),
                "pnl": float(rec.get("realized_pnl_num", 0.0))
            }

    # 2. 由舊到新統計勝敗與連敗次數
    for rec in reversed(history_records):
        ukey = str(rec.get("full_id") or rec.get("id") or "")
        if not ukey or ukey in processed_set:
            continue

        processed_set.add(ukey)

        sym   = rec.get("asset", "BTC/USDT:USDT")
        is_lg = "LONG" in str(rec.get("direction", "")).upper()
        side  = "LONG" if is_lg else "SHORT"
        pnl   = float(rec.get("realized_pnl_num", 0.0))

        if pnl <= -80:
            consec_losses[sym] = consec_losses.get(sym, 0) + 1
        elif pnl >= 200:
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
        "💰【v10.9 40/60鎖利鐵律】滿 +7.0% ROI 平倉 40% 落袋，剩餘 60% 主力倉鎖在 +1.5% ROI (留 5.5% 抗洗盤空間衝大波段)！",
        "🛑【止損收緊至-7.5%】初始止損設於『含槓桿 ROI -7.5%』觸發 (快軌 -8.2% 硬砍)，徹底杜絕滑價超過 -10%！",
        "⏳【60分止損冷卻 & 全場同向】止損後強制冷卻 60 分鐘；三大幣強制對齊 BTC 1H 大方向，嚴禁多空精神分裂與地板追空！"
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
            lessons.append(f"⚠️【高敗率警告】{sd_k} 累計 {st['wins']}勝{st['losses']}敗 (勝率{wr}%，淨虧${st['net_pnl']})，嚴禁逆勢硬幹！")
        elif wr >= 50.0 or st["net_pnl"] > 1500:
            lessons.append(f"✅【正期望值主力】{sd_k} 累計 {st['wins']}勝{st['losses']}敗 (勝率{wr}%，淨賺+${st['net_pnl']})，具顯著獲利優勢！")

    AI_MEMORY_BANK["processed_pos_keys"]     = list(processed_set)[-300:]
    AI_MEMORY_BANK["symbol_direction_stats"] = sym_stats
    AI_MEMORY_BANK["pattern_stats"]          = pat_stats
    AI_MEMORY_BANK["consecutive_losses"]     = consec_losses
    AI_MEMORY_BANK["last_exit_info"]         = last_exit_info
    AI_MEMORY_BANK["lessons_learned"]        = lessons[:12]


def check_reentry_cooldown_and_extension(sym, action, current_price, market_data):
    """
    🔒 v10.9 五道進場防禦濾網 (攔截 11/15~11/17 所有虧損型態)：
    1. 止損後 60 分鐘冷卻鎖：剛止損出場未滿 60 分鐘，嚴禁重開！
    2. 止盈後 15 分鐘冷卻鎖：剛止盈出場未滿 15 分鐘，等下一根 K 線再進！
    3. 防地板追空 / 防山頂追多鎖：若剛止盈出場未滿 90 分鐘，禁止在比剛才平倉價更極端的位置(>0.5%)同向追單！
    4. 均線過度乖離濾網：若現價偏離 15m MA20 超過 ±2.2% 或 RSI 處於極端區，代表利空/利多已反應完畢，不追尾！
    5. K線長影線濾網：長下影線不追空、長上影線不追多！
    """
    m_info    = market_data.get(sym, {})
    ma20_dist = float(m_info.get("ma20_dist_pct", 0.0))
    rsi_val   = float(m_info.get("rsi_14", 50.0))
    wick_sig  = m_info.get("wick_signal", "NONE")

    # 檢查 1 & 2 & 3：歷史出場冷卻與價格位置檢查
    exit_info = AI_MEMORY_BANK.get("last_exit_info", {}).get(sym)
    if exit_info:
        now_ms = int(time.time() * 1000)
        elapsed_mins = (now_ms - int(exit_info.get("timestamp_ms", 0))) / 60000.0
        last_pnl     = float(exit_info.get("pnl", 0.0))
        last_side    = exit_info.get("side", "")
        last_close   = float(exit_info.get("close_px", 0.0))
        want_side    = "LONG" if action == "BUY" else "SHORT"

        if last_pnl < -50 and elapsed_mins < LOSS_COOLDOWN_MINUTES:
            return False, f"⏳ {sym} 剛止損出場 ({int(elapsed_mins)}分鐘前)，強制冷卻滿 {LOSS_COOLDOWN_MINUTES} 分鐘才准進場"

        if last_pnl >= 0 and elapsed_mins < WIN_COOLDOWN_MINUTES:
            return False, f"⏳ {sym} 剛止盈出場 ({int(elapsed_mins)}分鐘前)，等待 {WIN_COOLDOWN_MINUTES} 分鐘換線回踩"

        if last_pnl > 200 and elapsed_mins < 90 and want_side == last_side and last_close > 0:
            if want_side == "SHORT" and current_price < last_close * 0.996:
                return False, f"🚫 防地板追空鎖：{sym} 現價 ${current_price} 低於剛止盈價 ${last_close}，等待反彈回踩均線再空"
            if want_side == "LONG" and current_price > last_close * 1.004:
                return False, f"🚫 防山頂追多鎖：{sym} 現價 ${current_price} 高於剛止盈價 ${last_close}，等待回踩支撐再多"

    # 檢查 4：均線乖離率與極端 RSI 過濾 (防止落後新聞誘使 AI 追在最高/最低點)
    if action == "SELL":
        if ma20_dist <= -MAX_MA20_DIST_PCT or rsi_val <= 27.0:
            return False, f"🚫 防追空過濾：{sym} 偏離均線達 {ma20_dist}% (RSI={rsi_val})，短線跌深利空已反應，等反彈再空"
        if wick_sig == "BULL_PINBAR_NO_SHORT":
            return False, f"🕯️ 長下影線護盤訊號：{sym} 出現底部抄底長下影線，暫停追空防V轉反彈"
    elif action == "BUY":
        if ma20_dist >= MAX_MA20_DIST_PCT or rsi_val >= 73.0:
            return False, f"🚫 防追多過濾：{sym} 偏離均線達 +{ma20_dist}% (RSI={rsi_val})，短線過熱利多已反應，等回踩再多"
        if wick_sig == "BEAR_PINBAR_NO_LONG":
            return False, f"🕯️ 長上影線拋壓訊號：{sym} 出現頂部流星長上影線，暫停追多防急殺回落"

    return True, "OK"


def build_fallback_plan(market_data):
    half_a, half_b = [], []
    btc_t1h = market_data.get('BTC/USDT:USDT', {}).get("trend_1h", "BULLISH")
    global_act = "BUY" if btc_t1h == "BULLISH" else "SELL"

    for sym in WHITELIST_SYMBOLS:
        info      = market_data.get(sym, {})
        trend_15m = info.get("trend", "BULLISH")
        rsi_val   = float(info.get("rsi_14", 50.0))
        safe_lev  = int(info.get("max_safe_leverage", 5))
        tp_v      = float(info.get("anchored_tp_pct", 2.8))
        sl_v      = float(info.get("anchored_sl_pct", 1.5))

        half_a.append({
            "symbol": sym, "action": global_act, "weight": 1.4, "leverage": safe_lev,
            "tp_pct": tp_v, "sl_pct": sl_v,
            "reason": f"【Half-A 全場對齊BTC({btc_t1h})】15m:{trend_15m}，滿+7%ROI平40%倉並鎖+1.5%保底，止損收緊至-7.5%ROI。"
        })
        half_b.append({
            "symbol": sym, "action": global_act, "weight": 1.2, "leverage": safe_lev,
            "tp_pct": round(tp_v * 1.1, 2), "sl_pct": sl_v,
            "reason": f"【Half-B 60%主力衝波段】RSI={rsi_val}，尾倉滿+12%鎖+6%ROI、滿+18%鎖+12%ROI。"
        })
    return {
        "overall_confidence": 92,
        "overall_summary": f"啟動 v10.9 機構風控：全場對齊 BTC ({btc_t1h}) 同向操作，滿 +7% 平 40% 鎖保底、止損收緊至 -7.5% ROI。",
        "black_swan_detected": False,
        "sniper_order": None,
        "half_a_orders": half_a[:3],
        "half_b_orders": half_b[:3]
    }


def ask_gemini_quant_brain(market_data, news_headlines, current_held_symbols, mistake_memory):
    if not GENAI_AVAILABLE:
        return build_fallback_plan(market_data)

    gemini_key = clean_credential(GEMINI_API_KEY, "GEMINI_API_KEY")
    client = genai.Client(api_key=gemini_key)

    btc_regime = market_data.get('BTC/USDT:USDT', {}).get('trend_1h', 'BULLISH')
    allowed_dir = "BUY (做多)" if btc_regime == "BULLISH" else "SELL (做空)"

    prompt = f"""
    你是 QuantGuard AI v10.9 機構級量化風控決策核心。我們針對近期實盤對帳單進行了五大進化：
    1. 【全場同向鐵律】：目前老大哥 BTC 的 1H 大趨勢為 `{btc_regime}`，因此本輪全場所有幣種（BTC、ETH、SOL）只允許規劃 `{allowed_dir}` 方向！嚴禁出現 BTC 做空、ETH 卻單獨做多的精神分裂持倉！
    2. 【新聞落後性與乖離率過濾】：國際新聞常在暴跌後才發布恐慌標題。若某幣種 `ma20_dist_pct` 已經過度偏離均線（超過 ±2.2%）或出現反轉長影線 (`wick_signal`)，代表利空/利多已反應完畢 (Priced-in)，請調降權重或跳過，優先選擇剛回踩均線、準備發動的幣種！
    3. 【40/60 黃金鎖利與 -7.5% 止損】：含槓桿 ROI 滿 `+7.0%` 先平倉 40% 落袋、剩餘 60% 主力倉鎖在 `+1.5% ROI` 保底去衝 `+15%+` 大波段；初始止損統一收緊至含槓桿 `-7.5% ROI`。

    【1. 三大幣種即時多維度指標 (含 trend_1h、ma20_dist_pct、wick_signal、max_safe_leverage)】：
    {json.dumps(market_data, ensure_ascii=False)}

    【2. 🧠 AI 永久累積記憶庫與連敗/冷卻狀態】：
    - 提煉法則：{json.dumps(AI_MEMORY_BANK.get("lessons_learned", []), ensure_ascii=False)}
    - 各幣種近期連敗次數：{json.dumps(AI_MEMORY_BANK.get("consecutive_losses", {}), ensure_ascii=False)}
    - 各幣種最近一次出場資訊：{json.dumps(AI_MEMORY_BANK.get("last_exit_info", {}), ensure_ascii=False)}

    【3. 最新國際新聞 (注意辨別是否已 Priced-in)】：{json.dumps(news_headlines, ensure_ascii=False)}
    【4. 目前持有倉位】：{json.dumps(current_held_symbols, ensure_ascii=False)}

    請嚴格只輸出 JSON 格式（所有訂單 action 必須與 BTC 1H 方向 `{allowed_dir}` 一致）：
    {{
      "overall_confidence": 93,
      "overall_summary": "繁體中文60字內：說明本輪如何對齊BTC大方向、過濾新聞滯後乖離，並執行滿+7%平40%倉鎖保底策略",
      "black_swan_detected": false,
      "sniper_order": null,
      "half_a_orders": [
        {{"symbol": "BTC/USDT:USDT", "action": "{"BUY" if btc_regime == "BULLISH" else "SELL"}", "weight": 1.6, "leverage": 5, "tp_pct": 2.8, "sl_pct": 1.5, "reason": "【Half-A 領頭羊同向】..."}},
        {{"symbol": "SOL/USDT:USDT", "action": "{"BUY" if btc_regime == "BULLISH" else "SELL"}", "weight": 1.4, "leverage": 5, "tp_pct": 3.0, "sl_pct": 1.5, "reason": "【Half-A 均線回踩】..."}}
      ],
      "half_b_orders": [
        {{"symbol": "ETH/USDT:USDT", "action": "{"BUY" if btc_regime == "BULLISH" else "SELL"}", "weight": 1.3, "leverage": 5, "tp_pct": 2.8, "sl_pct": 1.5, "reason": "【Half-B 全場同向共振】..."}}
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


def has_pending_tpsl_algo_order(exchange, sym):
    """
    🛡️ 孤兒單檢查器：查詢 OKX 上該幣種是否掛有有效的條件單 / OCO 止損單。
    """
    inst_id = sym.replace('/USDT:USDT', '-USDT-SWAP')
    for ord_type in ['oco', 'conditional']:
        try:
            res = exchange.privateGetTradeOrdersAlgoPending({'instId': inst_id, 'ordType': ord_type})
            if len(res.get('data', [])) > 0:
                return True
        except Exception:
            continue
    return False


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
    ⚡ v10.9 快軌核心：
    0. 孤兒單自癒檢查：若發現任何持倉在 OKX 上沒有掛止損單，當場強制補掛！
    1. 硬斷頭止損保護：實時含槓桿 ROI <= -8.2% 立即市價砍單，防止滑價破 -10%！
    2. 階段一 (含槓桿 ROI >= +7.0%)：先平 40% 倉位落袋現金，剩餘 60% 主力倉止損移至 +1.5% ROI (留 5.5% 抗洗盤緩衝)！
    3. 階段二 (含槓桿 ROI >= +12.0%)：剩餘 60% 主力倉止損鎖在 +6.0% ROI！
    4. 階段三 (含槓桿 ROI >= +18.0%)：剩餘 60% 主力倉止損鎖在 +12.0% ROI，止盈外推 1.4 倍吃大波段！
    """
    state_changed = False

    for pos in active_positions:
        sym       = pos['symbol']
        side      = pos['side']
        entry_px  = float(pos['entry_price'])
        contracts = int(float(pos['contracts']))
        roi_pct   = float(pos['roi_pct'])
        lev_num   = max(1.0, float(str(pos.get('leverage', '5x')).replace('x', '')))
        tp_pct    = float(pos.get('tp_pct', 2.8))
        pos_key   = f"{sym}_{side}"
        cur_stage = int(POSITION_STAGE_MAP.get(pos_key, 0))

        if entry_px <= 0 or contracts <= 0:
            continue

        # 🛑 1. 絕對止損鐵律攔截：含槓桿 ROI <= -8.2% 立即市價砍單！
        if roi_pct <= HARD_CUT_SL_ROI_PCT:
            print(f"🛑 [防滑價硬斷頭觸發] {sym} ({side}) 實時含槓桿 ROI 達 {roi_pct:.2f}% (<= {HARD_CUT_SL_ROI_PCT}%)，立即市價平倉！", flush=True)
            cancel_existing_algo_orders_for_symbol(exchange, sym)
            close_partial_or_full_position(exchange, sym, side, contracts, "死守-8.2%硬止損防滑價")
            POSITION_STAGE_MAP.pop(pos_key, None)
            state_changed = True
            continue

        base_tp_px = round(entry_px * (1 + tp_pct / 100.0) if side == 'LONG' else entry_px * (1 - tp_pct / 100.0), 2)

        # 🔥 2. 階段三：含槓桿 ROI >= +18.0% ➔ 剩餘 60% 倉位止損鎖在「含槓桿 ROI +12.0%」，止盈外推 1.4 倍！
        if roi_pct >= STAGE3_TRIGGER_ROI_PCT and cur_stage < 3:
            lock_s3_sl = roi_to_price(entry_px, side, STAGE3_LOCK_ROI_PCT, lev_num)
            ext_tp_px  = round(entry_px * (1 + (tp_pct * 1.4) / 100.0) if side == 'LONG' else entry_px * (1 - (tp_pct * 1.4) / 100.0), 2)
            update_position_tpsl_on_okx(exchange, sym, side, contracts, ext_tp_px, lock_s3_sl)
            POSITION_STAGE_MAP[pos_key] = 3
            pos['sl_trigger_px'] = lock_s3_sl
            pos['tp_trigger_px'] = ext_tp_px
            pos['stage_badge']   = f"🔥 尾倉鎖 +{STAGE3_LOCK_ROI_PCT}% ROI"
            pos['reason']        = f"🔥【含槓桿ROI滿+{STAGE3_TRIGGER_ROI_PCT}%】60%主力尾倉止損已鎖定於含槓桿 ROI +{STAGE3_LOCK_ROI_PCT}% (${lock_s3_sl})！"
            state_changed = True

        # 🚀 3. 階段二：含槓桿 ROI >= +12.0% ➔ 剩餘 60% 倉位止損鎖在「含槓桿 ROI +6.0%」！
        elif roi_pct >= STAGE2_TRIGGER_ROI_PCT and cur_stage < 2:
            lock_s2_sl = roi_to_price(entry_px, side, STAGE2_LOCK_ROI_PCT, lev_num)
            update_position_tpsl_on_okx(exchange, sym, side, contracts, base_tp_px, lock_s2_sl)
            POSITION_STAGE_MAP[pos_key] = 2
            pos['sl_trigger_px'] = lock_s2_sl
            pos['tp_trigger_px'] = base_tp_px
            pos['stage_badge']   = f"🚀 尾倉鎖 +{STAGE2_LOCK_ROI_PCT}% ROI"
            pos['reason']        = f"🚀【含槓桿ROI滿+{STAGE2_TRIGGER_ROI_PCT}%】60%主力尾倉止損已鎖定於含槓桿 ROI +{STAGE2_LOCK_ROI_PCT}% (${lock_s2_sl})！"
            state_changed = True

            send_discord_alert(
                title=f"🚀 階段二主力尾倉推鎖 (+{STAGE2_LOCK_ROI_PCT}% ROI)：{sym}",
                description=f"**{sym} ({side})** 剩餘 60% 主力倉含槓桿收益率達 **+{roi_pct:.2f}%**！\n已將止損單上移鎖定於 **${lock_s2_sl:,.2f} (保證含槓桿獲利 +{STAGE2_LOCK_ROI_PCT}%)**！",
                color=5763719
            )

        # 💰 4. 階段一：含槓桿 ROI >= +7.0% ➔ 先平 40% 倉位落袋現金！剩餘 60% 主力倉止損移至「含槓桿 ROI +1.5%」保底！
        elif roi_pct >= STAGE1_TRIGGER_ROI_PCT and cur_stage < 1:
            close_qty  = max(1, int(round(contracts * STAGE1_CLOSE_RATIO)))
            remain_qty = max(1, contracts - close_qty)
            lock_s1_sl = roi_to_price(entry_px, side, STAGE1_LOCK_ROI_PCT, lev_num)

            print(f"💰 [v10.9 階段一觸發：ROI +{roi_pct:.2f}% >= +7.0%] {sym} ({side}) 平 40% 倉位 ({close_qty}張) 落袋！剩餘 60% ({remain_qty}張) 鎖在 +{STAGE1_LOCK_ROI_PCT}% ROI (${lock_s1_sl})！", flush=True)
            if contracts >= 3:
                close_partial_or_full_position(exchange, sym, side, close_qty, "含槓桿ROI滿+7%平40%倉落袋")
            update_position_tpsl_on_okx(exchange, sym, side, remain_qty, base_tp_px, lock_s1_sl)

            POSITION_STAGE_MAP[pos_key] = 1
            pos['contracts']     = remain_qty
            pos['sl_trigger_px'] = lock_s1_sl
            pos['tp_trigger_px'] = base_tp_px
            pos['stage_badge']   = f"💰 已平40%落袋 & 60%主力鎖+{STAGE1_LOCK_ROI_PCT}%ROI"
            pos['reason']        = f"💰【已落袋40%倉位】剩餘 60% ({remain_qty}張) 止損鎖於 +{STAGE1_LOCK_ROI_PCT}% ROI (${lock_s1_sl})，留 5.5% 空間衝大波段！"
            state_changed = True

            send_discord_alert(
                title=f"💰 滿 +7% ROI 平 40% 落袋 & 60% 主力倉鎖保底 | {sym}",
                description=f"**{sym} ({side} {int(lev_num)}x)** 含槓桿收益率達 **+{roi_pct:.2f}%**！\n1️⃣ 已先市價平倉 **40% ({close_qty}張)** 落袋為安！\n2️⃣ 剩餘 **60% 主力倉 ({remain_qty}張)** 止損移至 **${lock_s1_sl:,.2f} (+{STAGE1_LOCK_ROI_PCT}% ROI 保底)**，留足呼吸空間衝刺 +15% 大波段！",
                color=3066993
            )

        # 🛡️ 5. 孤兒單防裸奔自癒檢查：若該持倉在 OKX 上沒有有效條件單，當場自動補掛！
        else:
            if not has_pending_tpsl_algo_order(exchange, sym):
                cur_sl_px = float(pos.get('sl_trigger_px') or roi_to_price(entry_px, side, -MAX_SL_ROI_TRIGGER_PCT, lev_num))
                print(f"🛡️ [孤兒單自癒補掛] 偵測到 {sym} ({side}) 缺少 OKX 條件單，立即自動補掛止盈 ${base_tp_px} / 止損 ${cur_sl_px}！", flush=True)
                update_position_tpsl_on_okx(exchange, sym, side, contracts, base_tp_px, cur_sl_px)

    return state_changed


def execute_pool_orders(exchange, pool_name, orders_list, pool_budget_usdt, market_data, reason_lookup, held_symbols_map, total_equity, max_allowed_active_positions):
    """
    🌟 v10.9 建倉引擎：
    強制執行『全場同向對齊 BTC』、『止損後 60 分鐘冷卻鎖』、『防地板追空/防山頂追多鎖』與『組合熱度上限 (未保底前限開 2 單)』！
    """
    valid_orders = [o for o in orders_list[:MAX_ORDERS_PER_HALF] if str(o.get("action", "")).upper() in ["BUY", "SELL"]]
    if not valid_orders or pool_budget_usdt < 30:
        return False

    if is_us_market_open_chop_window():
        print("🕒 [華爾街開盤避險時段 21:15~22:00] 暫停開立新倉，避開美股開盤上下插針雙巴洗盤！", flush=True)
        return False

    any_placed = False
    consec_losses = AI_MEMORY_BANK.get("consecutive_losses", {})

    adjusted_weights = []
    for o in valid_orders:
        s_sym = o.get("symbol", "ETH/USDT:USDT")
        w = max(0.5, min(3.0, float(o.get("weight", 1.0))))
        if consec_losses.get(s_sym, 0) >= 2:
            w *= 0.5
        vol_ratio = float(market_data.get(s_sym, {}).get("vol_spike_ratio", 1.0))
        if vol_ratio < 0.65:
            w *= 0.75
        elif vol_ratio >= 1.35:
            w *= 1.20
        adjusted_weights.append(round(w, 2))

    total_weight = sum(adjusted_weights) if sum(adjusted_weights) > 0 else 1.0

    # 全場唯一允許方向：嚴格對齊老大哥 BTC 的 1H 趨勢，杜絕多空精神分裂
    btc_global_act = market_data.get('BTC/USDT:USDT', {}).get("global_allowed_action", "BUY")

    for idx, ord_spec in enumerate(valid_orders, start=1):
        sym = ord_spec.get("symbol", "ETH/USDT:USDT")
        if sym not in WHITELIST_SYMBOLS:
            sym = "ETH/USDT:USDT"

        m_info   = market_data.get(sym, {})
        safe_lev = int(m_info.get("max_safe_leverage", 5))
        trend_1h = m_info.get("trend_1h", "BULLISH")
        rsi_val  = float(m_info.get("rsi_14", 50.0))

        # 🧭 1. 強制全場同向：所有幣種一律與 BTC 1H 大方向一致！
        action = btc_global_act
        weight = adjusted_weights[idx - 1]

        ai_lev   = int(ord_spec.get("leverage", safe_lev))
        leverage = max(4, min(6, min(ai_lev, safe_lev)))

        sl_pct = round(MAX_SL_ROI_TRIGGER_PCT / float(leverage), 2)
        tp_pct = round(max(2.5, min(3.8, DEFAULT_TARGET_TP_ROI / float(leverage))), 2)

        mode_tag = f"🛡️v10.9同向(滿+7%平40%鎖保底/止損鎖-{round(sl_pct*leverage,1)}%ROI)"
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

        # 🔥 2. 投資組合熱度上限檢查 (Portfolio Heat Cap)：若未保底前已達 2 筆上限，暫停加開第 3 筆！
        if len(held_symbols_map) >= max_allowed_active_positions:
            print(f"🛡️ [投資組合熱度鎖] 目前已持有 {len(held_symbols_map)} 筆且尚有倉位未達保底線，暫緩開立第 3 筆 ({sym}) 以控管同向總風險！", flush=True)
            continue

        current_price = float(m_info.get("last_price", 0))
        if current_price <= 0:
            continue

        # 🔒 3. 檢查 60 分鐘止損冷卻鎖、防地板追空/山頂追多鎖、均線乖離率與長影線濾網
        can_enter, filter_msg = check_reentry_cooldown_and_extension(sym, action, current_price, market_data)
        if not can_enter:
            print(f"   {filter_msg}", flush=True)
            continue

        desired_margin = round(pool_budget_usdt * (weight / total_weight), 2)
        contracts, safe_margin = calculate_safe_contracts(exchange, sym, desired_margin, leverage, current_price, total_equity)
        if contracts <= 0:
            continue

        side_upper = 'LONG' if action == 'BUY' else 'SHORT'
        tp_price = roi_to_price(current_price, side_upper, round(tp_pct * leverage, 2), leverage)
        sl_price = roi_to_price(current_price, side_upper, -MAX_SL_ROI_TRIGGER_PCT, leverage)

        order = place_okx_order_with_tpsl(exchange, sym, action, contracts, leverage, tp_price, sl_price, pool_name)
        if order:
            held_symbols_map[sym] = side_upper
            POSITION_STAGE_MAP[f"{sym}_{side_upper}"] = 0
            AI_MEMORY_BANK.setdefault("entry_snapshots", {})[sym] = {
                "side": side_upper,
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
    pending_discord_alerts = []
    seen_pos_ids = set()
    hist_reasons = AI_MEMORY_BANK.setdefault("historical_reasons", {})
    snapshots    = AI_MEMORY_BANK.get("entry_snapshots", {})

    raw_pos_history = []
    try:
        if hasattr(exchange, 'fetch_positions_history'):
            raw_pos_history = exchange.fetch_positions_history(WHITELIST_SYMBOLS, limit=100)
        else:
            res = exchange.privateGetAccountPositionsHistory({'instType': 'SWAP', 'limit': '100'})
            raw_pos_history = res.get('data', [])
    except Exception:
        try:
            res = exchange.privateGetAccountPositionsHistory({'instType': 'SWAP', 'limit': '100'})
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
            locked_reason = snap_reason or f"【{sym.split('/')[0]} 倉位結算】開倉 ${open_avg_px:,.2f} ➔ 平倉 ${close_avg_px:,.2f} ({lev}x 槓桿，結算含槓桿 ROI {pnl_pct:+.2f}%)"
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
            "close_px_num": close_avg_px,
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
                pending_discord_alerts.append({
                    "sym": sym,
                    "side_label": side_label,
                    "realized_pnl": realized_pnl,
                    "pnl_sign": pnl_sign,
                    "pnl_pct": pnl_pct
                })

    for alert_item in reversed(pending_discord_alerts):
        is_win = alert_item["realized_pnl"] >= 0
        send_discord_alert(
            title=f"{'🟢 獲利平倉結算' if is_win else '🔴 止損平倉結算'} | {alert_item['sym']}",
            description=f"**{alert_item['side_label']}** 已於 OKX 完成平倉結算！",
            color=3066993 if is_win else 15158332,
            fields=[
                {"name": "💰 本單已實現損益", "value": f"**{alert_item['pnl_sign']}${alert_item['realized_pnl']:,.2f} USDT ({alert_item['pnl_sign']}{alert_item['pnl_pct']:.2f}%)**", "inline": False},
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

    active_pos_keys = {f"{s}_{sd}" for s, sd in held_symbols_map.items()}
    for old_k in list(POSITION_STAGE_MAP.keys()):
        if old_k not in active_pos_keys:
            POSITION_STAGE_MAP.pop(old_k, None)

    # 🔥 計算目前場上有幾筆倉位還處於 Stage 0 (尚未進入保底狀態)
    unprotected_stage0_count = sum(1 for k in active_pos_keys if int(POSITION_STAGE_MAP.get(k, 0)) == 0)
    # 若場上還有未保底的風險倉位，全場最多同時持有 2 筆；若場上舊單全部已進入 Stage 1+ 保底，才開放第 3 筆！
    max_allowed_active_positions = 2 if unprotected_stage0_count >= 1 else 3

    market_data = fetch_whitelist_market_data(exchange)
    prelim_history, perf_stats = fetch_okx_positions_history_and_analytics(exchange, market_data, {}, "", real_usdt, 0.0)

    mistake_memory = [
        {"asset": h["asset"], "direction": h["direction"], "roi": h["roi"]}
        for h in prelim_history[:5]
    ]

    should_call_ai = (
        LAST_AI_PLAN_CACHE is None
        or len(held_symbols_map) < max_allowed_active_positions
        or (cycle_num % SLOW_AI_EVERY_N_TICKS == 0)
    )

    if should_call_ai:
        news_list = fetch_live_crypto_news()
        LAST_AI_PLAN_CACHE = ask_gemini_quant_brain(
            market_data, news_list, held_symbols_map, mistake_memory
        )
        print(f"🧠 [AI 決策更新]: {LAST_AI_PLAN_CACHE.get('overall_summary')}", flush=True)
    else:
        news_list = ["快軌 20 秒監控中 (v10.9：滿+7%平40%鎖保底 / 止損-7.5%防滑價 / 止損後60分冷卻鎖 / 全場同向)"]
        print(f"⚡ [快軌盯盤] 總資產: ${real_usdt:,.2f} | 今日已實現: ${perf_stats['today_realized_pnl']:+,.2f} | 本期累計: ${perf_stats['all_time_realized_pnl']:+,.2f}", flush=True)

    ai_plan = LAST_AI_PLAN_CACHE or build_fallback_plan(market_data)

    used_margin_now = max(0.0, real_usdt - free_usdt)
    normal_pool_target = real_usdt * POOL_NORMAL_RATIO
    remaining_normal_budget = max(0.0, min(free_usdt * 0.78, normal_pool_target - used_margin_now))

    half_a_budget = round(remaining_normal_budget / 2.0, 2)
    half_b_budget = round(remaining_normal_budget / 2.0, 2)

    reason_lookup = {}
    order_placed = False
    if remaining_normal_budget >= 80 and len(held_symbols_map) < max_allowed_active_positions:
        p1 = execute_pool_orders(exchange, "Half-A 同向順勢池 (40%)", ai_plan.get("half_a_orders", []), half_a_budget, market_data, reason_lookup, held_symbols_map, real_usdt, max_allowed_active_positions)
        p2 = execute_pool_orders(exchange, "Half-B 波段主力池 (40%)", ai_plan.get("half_b_orders", []), half_b_budget, market_data, reason_lookup, held_symbols_map, real_usdt, max_allowed_active_positions)
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
                lev_num      = max(1.0, float(pos.get('leverage') or 5))
                m_info       = market_data.get(sym, {})
                mark_px      = round(float(pos.get('markPrice') or m_info.get('last_price', 0)), 2)

                roi_pct      = compute_exact_position_roi(side_u, entry_px, mark_px, lev_num, upl, margin_used)
                total_upl   += upl

                snap_info = snapshots.get(sym, {})
                r_info    = reason_lookup.get(sym, {})

                max_sl_p = round(MAX_SL_ROI_TRIGGER_PCT / lev_num, 2)
                tp_p_val = float(snap_info.get("tp_pct") or r_info.get("tp_pct") or m_info.get("anchored_tp_pct", 2.8))
                sl_p_val = max_sl_p

                pos_key   = f"{sym}_{side_u}"
                cur_stage = int(POSITION_STAGE_MAP.get(pos_key, 0))
                trend_1h  = m_info.get("trend_1h", "BULLISH")

                if cur_stage == 3:
                    sl_trig = roi_to_price(entry_px, side_u, STAGE3_LOCK_ROI_PCT, lev_num)
                    stage_badge = f"🔥 尾倉鎖 +{STAGE3_LOCK_ROI_PCT}% ROI"
                elif cur_stage == 2:
                    sl_trig = roi_to_price(entry_px, side_u, STAGE2_LOCK_ROI_PCT, lev_num)
                    stage_badge = f"🚀 尾倉鎖 +{STAGE2_LOCK_ROI_PCT}% ROI"
                elif cur_stage == 1:
                    sl_trig = roi_to_price(entry_px, side_u, STAGE1_LOCK_ROI_PCT, lev_num)
                    stage_badge = f"💰 已平40% & 60%主力鎖+{STAGE1_LOCK_ROI_PCT}%ROI"
                else:
                    sl_trig = roi_to_price(entry_px, side_u, -MAX_SL_ROI_TRIGGER_PCT, lev_num)
                    stage_badge = f"⚡ 滿+7%平40%鎖保底 (止損鎖-{MAX_SL_ROI_TRIGGER_PCT}%ROI)"

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
                    "reason": snap_info.get("reason") or r_info.get("reason") or f"1H:{trend_1h} | v10.9：滿+7%平40%鎖+{STAGE1_LOCK_ROI_PCT}%保底"
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
            "normal_80_usdt": round(real_usdt * POOL_NORMAL_RATIO, 2),
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
            "confidence": ai_plan.get("overall_confidence", 93),
            "summary": ai_plan.get("overall_summary", ""),
            "half_a_orders": ai_plan.get("half_a_orders", []),
            "half_b_orders": ai_plan.get("half_b_orders", [])
        },
        "ai_last_decision": {
            "action": f"今日已實現 ${final_perf_stats['today_realized_pnl']:+,.2f} | 本期累計 ${final_perf_stats['all_time_realized_pnl']:+,.2f} (盈虧比 {final_perf_stats['profit_factor']}x)",
            "confidence": ai_plan.get("overall_confidence", 93),
            "reason": ai_plan.get("overall_summary", "")
        },
        "orders": positions_history_formatted
    }

    with open("live_data.json", "w", encoding="utf-8") as f:
        json.dump(live_payload, f, ensure_ascii=False, indent=2)

    if order_placed or fast_changed or cycle_num == 1:
        push_live_data_immediately(force=True)
    else:
        push_live_data_immediately(force=False)


def main():
    print("=========================================================================", flush=True)
    print("🚀 QuantGuard AI v10.9 - 全場同向紀律 × 60分止損冷卻 × 40/60鎖利 × 孤兒單自癒", flush=True)
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
