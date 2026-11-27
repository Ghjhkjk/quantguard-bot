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
# ⚙️ 2. v11.4 BTC大盤聯動 × 結構反轉 × 務實落袋止盈階梯引擎
# ==========================================================
WHITELIST_SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']

POOL_NORMAL_RATIO          = 0.80  # 80% 常態雙核心池
POOL_HEDGE_RATIO           = 0.10  # 10% 緊急急跌對沖池
POOL_SNIPER_RATIO          = 0.10  # 10% 黑天鵝突發狙擊池

MAX_ORDERS_PER_HALF        = 3
TARGET_SINGLE_MARGIN_RATIO = 0.20  # 單筆目標保證金：總資產 20%
MAX_SINGLE_MARGIN_RATIO    = 0.24  # 單筆訂單保證金硬上限：24%

# 🛑 v11.4 務實落袋止盈階梯：+4.5% 平半鎖利、+7.5% 推鎖、總止盈縮短至 +9.0%
MAX_SL_ROI_TRIGGER_PCT  = 5.0   # 掛單觸發止損設於「含槓桿 ROI -5.0%」
HARD_CUT_SL_ROI_PCT     = -5.6  # 快軌 20 秒硬斷頭保護：實時含槓桿 ROI <= -5.6% 市價砍單防滑價

EST_ROUND_TRIP_FEE_RATE        = 0.0010  # OKX 雙邊吃單預估名目費率 0.10% (開0.05% + 平0.05%)
BREAKEVEN_TRIGGER_ROI_PCT      = 4.5     # 保留變數相容性（已整合進階段一直接平半落袋）
BREAKEVEN_EXTRA_BUFFER_ROI_PCT = 1.1

# 💰 三階段務實鎖利 (全參數為「含槓桿後真實保證金 ROI %」)
STAGE1_CLOSE_RATIO      = 0.50  # 【階段一】含槓桿 ROI 達 +4.5% -> 直接平倉 50% 落袋現金！
STAGE1_TRIGGER_ROI_PCT  = 4.5
STAGE1_LOCK_ROI_PCT     = 1.5   #           剩餘 50% 尾倉止損移至「含槓桿 ROI +1.5%」(覆蓋手續費保底)

STAGE2_TRIGGER_ROI_PCT  = 7.5   # 【階段二】剩餘 50% 尾倉達 +7.5% -> 止損上推鎖死在「+4.5% ROI」！
STAGE2_LOCK_ROI_PCT     = 4.5

STAGE3_TRIGGER_ROI_PCT  = 10.5  # 【階段三】剩餘 50% 尾倉達 +10.5% -> 止損鎖在「+7.5% ROI」，止盈外推！
STAGE3_LOCK_ROI_PCT     = 7.5

DEFAULT_TARGET_TP_ROI   = 9.0   # 預設總止盈目標縮短為「含槓桿 ROI +9.0%」(震盪盤也能真正摸到全數落袋)

# ⏳ 同型態防重複踩坑冷卻時間
LOSS_COOLDOWN_MINUTES   = 45    # 止損後等待 3 根 15m K 線 (45分鐘) 重新評估盤勢體質
WIN_COOLDOWN_MINUTES    = 12    # 止盈或保本鎖出場後等待 12 分鐘加速週轉

# 📰 新聞高波動雷達快取間隔
NEWS_REFRESH_INTERVAL_SEC = 720  # 每 12 分鐘刷新一次新聞快訊與高波動警戒

IS_GITHUB_ACTIONS     = os.environ.get("GITHUB_ACTIONS") == "true"
RUN_CONTINUOUS_LOOP   = True
FAST_TRACK_INTERVAL   = 20    # 快軌：每 20 秒高速巡檢一次 OKX
SLOW_AI_EVERY_N_TICKS = 15    # 慢軌：每 15 個快軌 (約 5 分鐘) 或有空倉槽位時呼叫 Gemini AI
MAX_JOB_RUNTIME_SEC   = int(os.environ.get("MAX_JOB_RUNTIME_SEC", "210"))

LAST_AI_PLAN_CACHE = None
NOTIFIED_POS_IDS   = set()
POSITION_STAGE_MAP = {}
LAST_GIT_PUSH_TS   = 0.0
NEWS_RADAR_CACHE   = {"last_ts": 0.0, "headlines": [], "high_alert": False, "alert_note": ""}

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
    """將『含槓桿後的目標 ROI (%)』精準除以『槓桿倍率』，換算成 OKX 幣價觸發價"""
    lev = max(1.0, float(leverage_num))
    price_move_fraction = (float(target_leveraged_roi_pct) / lev) / 100.0
    if side_upper == 'LONG':
        return round(entry_px * (1.0 + price_move_fraction), 2)
    else:
        return round(entry_px * (1.0 - price_move_fraction), 2)


def calc_breakeven_lock_roi(leverage_num):
    """計算覆蓋 OKX 雙邊手續費 + 1.1% 緩衝後的保本鎖 ROI (%)"""
    lev = max(1.0, float(leverage_num))
    fee_roi_pct = EST_ROUND_TRIP_FEE_RATE * lev * 100.0
    return round(fee_roi_pct + BREAKEVEN_EXTRA_BUFFER_ROI_PCT, 2)


def classify_entry_zone_bucket(bb_pos_pct, range_pos_pct):
    if bb_pos_pct >= 68.0 or range_pos_pct >= 72.0:
        return "HIGH_ZONE"
    elif bb_pos_pct <= 32.0 or range_pos_pct <= 28.0:
        return "LOW_ZONE"
    return "MID_ZONE"


def send_discord_alert(title, description, color=3066993, fields=None):
    if not DISCORD_WEBHOOK_URL or not DISCORD_WEBHOOK_URL.startswith("http"):
        return
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "QuantGuard AI v11.4 | BTC大盤聯動 × 結構反轉 × 務實落袋止盈"}
    }
    if fields:
        embed["fields"] = fields
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=json.dumps({"embeds": [embed]}).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'User-Agent': 'QuantGuardBot/11.4'}
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

        print(f"🧠 [AI 特徵記憶庫載入成功] 已累積學習 {len(AI_MEMORY_BANK.get('processed_pos_keys', []))} 筆實戰型態經驗！", flush=True)
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
            subprocess.run(["git", "commit", "-m", "Real-time sync live_data.json v11.4"], check=False)
            pull_res = subprocess.run(["git", "pull", "--rebase", "-X", "ours", "origin", "main"], check=False)
            if pull_res.returncode != 0:
                subprocess.run(["git", "rebase", "--abort"], check=False)
                subprocess.run(["git", "fetch", "origin", "main"], check=False)
                subprocess.run(["git", "reset", "--hard", "origin/main"], check=False)
                with open("live_data.json", "w", encoding="utf-8") as f:
                    f.write(latest_json_content)
                subprocess.run(["git", "add", "live_data.json"], check=False)
                subprocess.run(["git", "commit", "-m", "Force sync live_data.json v11.4"], check=False)

            subprocess.run(["git", "push", "origin", "main"], check=False)
            LAST_GIT_PUSH_TS = now_ts
            print("🚀 [即時雲端同步] 已成功將最新 live_data.json 推送至網站！", flush=True)
    except Exception as e:
        print(f"ℹ️ 背景 Git 同步提示: {e}", flush=True)


def fetch_live_crypto_news():
    """每 12 分鐘自動快取更新新聞快訊，並偵測重大宏觀／幣圈高波動警戒關鍵字"""
    global NEWS_RADAR_CACHE
    now_ts = time.time()
    if NEWS_RADAR_CACHE["headlines"] and (now_ts - NEWS_RADAR_CACHE["last_ts"]) < NEWS_REFRESH_INTERVAL_SEC:
        return NEWS_RADAR_CACHE["headlines"]

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

    final_headlines = headlines[:6] if headlines else ["Institutional crypto volume steady amid macro watch."]
    joined_lower = " | ".join(final_headlines).lower()
    high_impact_kws = [
        "fed", "powell", "cpi", "pce", "rate", "sec", "etf", "liquidation",
        "surge", "plunge", "crash", "rally", "nvidia", "trump", "tariff", "war"
    ]
    matched_kws = [kw for kw in high_impact_kws if kw in joined_lower]
    high_alert = len(matched_kws) > 0
    alert_note = f"高波動新聞警戒({','.join(matched_kws[:3])})" if high_alert else "新聞情緒平穩"

    NEWS_RADAR_CACHE = {
        "last_ts": now_ts,
        "headlines": final_headlines,
        "high_alert": high_alert,
        "alert_note": alert_note
    }
    return final_headlines


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
    if len(ohlcv_15m) < 2:
        return "NONE"
    for candle in ohlcv_15m[-2:]:
        o_px, h_px, l_px, c_px = float(candle[1]), float(candle[2]), float(candle[3]), float(candle[4])
        total_range = h_px - l_px
        if total_range <= 0 or c_px <= 0:
            continue
        if (total_range / c_px * 100.0) < 0.30:
            continue
        body       = max(abs(c_px - o_px), total_range * 0.12)
        lower_wick = min(o_px, c_px) - l_px
        upper_wick = h_px - max(o_px, c_px)

        if lower_wick >= body * 1.40 and lower_wick >= total_range * 0.50:
            return "BULL_PINBAR_REVERSAL"
        if upper_wick >= body * 1.40 and upper_wick >= total_range * 0.50:
            return "BEAR_PINBAR_REVERSAL"
    return "NONE"


def fetch_whitelist_market_data(exchange):
    """
    🌟 v11.4 智慧盤勢體質與嚴格結構反轉探測器：
    結合 MA5/MA10 結構反轉確認與務實止盈目標 (+9.0% ROI)！
    """
    market_summary = {}
    for sym in WHITELIST_SYMBOLS:
        try:
            ticker    = exchange.fetch_ticker(sym)
            ohlcv_15m = exchange.fetch_ohlcv(sym, timeframe='15m', limit=48)
            ohlcv_1h  = exchange.fetch_ohlcv(sym, timeframe='1h', limit=60)

            opens  = [c[1] for c in ohlcv_15m]
            highs  = [c[2] for c in ohlcv_15m]
            lows   = [c[3] for c in ohlcv_15m]
            closes = [c[4] for c in ohlcv_15m]
            vols   = [c[5] for c in ohlcv_15m]
            current_price = float(ticker['last'])

            ma5  = sum(closes[-5:]) / 5 if len(closes) >= 5 else current_price
            ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else current_price
            ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else current_price
            std20 = (sum((x - ma20) ** 2 for x in closes[-20:]) / 20) ** 0.5 if len(closes) >= 20 else current_price * 0.01
            bb_upper = round(ma20 + 2.0 * std20, 2)
            bb_lower = round(ma20 - 2.0 * std20, 2)
            bb_pos_pct   = round(((current_price - bb_lower) / max(0.0001, (bb_upper - bb_lower))) * 100, 1)
            bb_width_pct = round(((bb_upper - bb_lower) / max(0.0001, ma20)) * 100, 2)

            ma20_dist_pct = round(((current_price - ma20) / max(0.0001, ma20)) * 100.0, 2)
            wick_signal   = detect_pinbar_wick_signal(ohlcv_15m)
            rsi_14        = compute_rsi(closes, 14)

            resistance_12h = round(max(highs), 2) if highs else round(current_price * 1.02, 2)
            support_12h    = round(min(lows), 2) if lows else round(current_price * 0.98, 2)
            range_span     = max(0.0001, resistance_12h - support_12h)
            range_pos_pct  = round(((current_price - support_12h) / range_span) * 100.0, 1)

            upside_room_pct   = round(max(0.05, ((resistance_12h - current_price) / current_price) * 100.0), 2)
            downside_room_pct = round(max(0.05, ((current_price - support_12h) / current_price) * 100.0), 2)

            # 1H 大結構
            closes_1h = [c[4] for c in ohlcv_1h]
            ma20_1h = sum(closes_1h[-20:]) / 20 if len(closes_1h) >= 20 else current_price
            ma60_1h = sum(closes_1h[-60:]) / len(closes_1h) if len(closes_1h) >= 25 else ma20_1h

            if current_price > ma20_1h and ma20_1h >= ma60_1h:
                macro_structure_1h = "TRUE_BULL_TREND"
                trend_1h = "BULLISH"
            elif current_price < ma20_1h and ma20_1h <= ma60_1h:
                macro_structure_1h = "TRUE_BEAR_TREND"
                trend_1h = "BEARISH"
            elif current_price > ma20_1h and current_price < ma60_1h:
                macro_structure_1h = "BEAR_MARKET_REBOUND"
                trend_1h = "BEARISH"
            else:
                macro_structure_1h = "BULL_MARKET_PULLBACK"
                trend_1h = "BULLISH"

            trend_15m = "BULLISH" if ma5 > ma20 else "BEARISH"

            # 🛡️ 嚴格結構反轉確認（須有短均線結構或實體反轉確認）
            recent_3_green = len(closes) >= 3 and all(closes[-i] > opens[-i] for i in (1, 2, 3))
            recent_3_red   = len(closes) >= 3 and all(closes[-i] < opens[-i] for i in (1, 2, 3))

            bear_reversal_confirmed = (
                (ma5 < ma10 and len(closes) >= 2 and closes[-1] < opens[-1] and closes[-1] < closes[-2] and rsi_14 <= 64.0)
                or (wick_signal == "BEAR_PINBAR_REVERSAL" and closes[-1] < opens[-1])
            )
            bull_reversal_confirmed = (
                (ma5 > ma10 and len(closes) >= 2 and closes[-1] > opens[-1] and closes[-1] > closes[-2] and rsi_14 >= 36.0)
                or (wick_signal == "BULL_PINBAR_REVERSAL" and closes[-1] > opens[-1])
            )

            anti_squeeze_block_short = (
                macro_structure_1h == "TRUE_BULL_TREND"
                or recent_3_green
                or (bb_pos_pct >= 90.0 and not bear_reversal_confirmed)
                or (ma5 > ma20 and current_price > ma5 and not bear_reversal_confirmed)
            )
            anti_knife_block_long = (
                macro_structure_1h == "TRUE_BEAR_TREND"
                or recent_3_red
                or (bb_pos_pct <= 10.0 and not bull_reversal_confirmed)
                or (ma5 < ma20 and current_price < ma5 and not bull_reversal_confirmed)
            )

            tr_list   = [(highs[i] - lows[i]) / closes[i] * 100 for i in range(len(closes))]
            atr_pct   = sum(tr_list) / len(tr_list) if tr_list else 0.8
            swing_pct = ((max(highs[-12:]) - min(lows[-12:])) / current_price) * 100 if len(highs) >= 12 else 2.2
            change_24h = float(ticker.get('percentage') or 0.0)

            bar_change_pct = ((closes[-1] - closes[-2]) / closes[-2] * 100) if len(closes) >= 2 else 0.0
            avg_vol = (sum(vols[:-1]) / max(1, len(vols) - 1)) if len(vols) > 1 else 1.0
            vol_spike_ratio = round(vols[-1] / avg_vol, 2) if avg_vol > 0 else 1.0

            is_volume_breakout = (
                (vol_spike_ratio >= 1.35 and abs(bar_change_pct) >= 0.45 and bb_width_pct >= 2.2)
                or recent_3_green
                or recent_3_red
            )
            market_regime_type = "TREND_BREAKOUT" if is_volume_breakout else "RANGE_MEAN_REVERSION"
            entry_zone_bucket = classify_entry_zone_bucket(bb_pos_pct, range_pos_pct)

            natural_sl_pct = round(max(1.2, min(1.8, swing_pct * 0.50)), 2)
            max_safe_lev   = max(3, min(4, int(MAX_SL_ROI_TRIGGER_PCT // natural_sl_pct)))

            enforced_sl_pct = round(MAX_SL_ROI_TRIGGER_PCT / float(max_safe_lev), 2)
            anchored_tp_pct = round(max(2.0, min(3.2, DEFAULT_TARGET_TP_ROI / float(max_safe_lev))), 2)

            if market_regime_type == "RANGE_MEAN_REVERSION":
                if (entry_zone_bucket == "HIGH_ZONE" or rsi_14 >= 61.0 or wick_signal == "BEAR_PINBAR_REVERSAL") and bear_reversal_confirmed and not anti_squeeze_block_short:
                    tactical_signal = "SELL"
                    tactical_desc   = f"【震盪盤結構轉弱做空】壓力區(BB:{bb_pos_pct}%)且確認均線/K線轉弱，向下空間 {downside_room_pct}% > 向上 {upside_room_pct}%！"
                elif (entry_zone_bucket == "LOW_ZONE" or rsi_14 <= 39.0 or wick_signal == "BULL_PINBAR_REVERSAL") and bull_reversal_confirmed and not anti_knife_block_long:
                    tactical_signal = "BUY"
                    tactical_desc   = f"【震盪盤結構止跌做多】支撐區(BB:{bb_pos_pct}%)且確認站回短均止跌，向上空間 {upside_room_pct}% > 向下 {downside_room_pct}%！"
                else:
                    if trend_1h == "BULLISH" and bull_reversal_confirmed and not anti_knife_block_long:
                        tactical_signal = "BUY"
                        tactical_desc   = f"【順勢偏多結構確認】1H偏多且15m結構止跌確認，順勢偏多。"
                    elif trend_1h == "BEARISH" and bear_reversal_confirmed and not anti_squeeze_block_short:
                        tactical_signal = "SELL"
                        tactical_desc   = f"【順勢偏空結構確認】1H偏空且15m結構轉弱確認，順勢偏空。"
                    else:
                        tactical_signal = "HOLD"
                        tactical_desc   = f"【等待結構確認】尚未出現明確 MA5/MA10 結構反轉訊號，不盲目進場。"
            else:
                tactical_signal = "BUY" if (bar_change_pct > 0 or recent_3_green) else "SELL"
                tactical_desc   = f"【強勢動能突破順勢追擊】量能/連K動能強({vol_spike_ratio}x, {bar_change_pct:+.2f}%)，順勢追擊大波段！"

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
                "ma20_1h": round(ma20_1h, 2),
                "ma60_1h": round(ma60_1h, 2),
                "ma20_dist_pct": ma20_dist_pct,
                "wick_signal": wick_signal,
                "rsi_14": rsi_14,
                "bb_upper": bb_upper,
                "bb_lower": bb_lower,
                "bb_pos_pct": bb_pos_pct,
                "bb_width_pct": bb_width_pct,
                "resistance_12h": resistance_12h,
                "support_12h": support_12h,
                "range_pos_pct": range_pos_pct,
                "upside_room_pct": upside_room_pct,
                "downside_room_pct": downside_room_pct,
                "macro_structure_1h": macro_structure_1h,
                "market_regime_type": market_regime_type,
                "entry_zone_bucket": entry_zone_bucket,
                "anti_squeeze_block_short": anti_squeeze_block_short,
                "anti_knife_block_long": anti_knife_block_long,
                "bear_reversal_confirmed": bear_reversal_confirmed,
                "bull_reversal_confirmed": bull_reversal_confirmed,
                "tactical_signal": tactical_signal,
                "tactical_desc": tactical_desc,
                "trend_1h": trend_1h,
                "trend": trend_15m,
                "atr_15m_pct": round(atr_pct, 2),
                "recent_swing_pct": round(swing_pct, 2),
                "anchored_tp_pct": anchored_tp_pct,
                "anchored_sl_pct": enforced_sl_pct,
                "max_safe_leverage": max_safe_lev
            }
        except Exception as e:
            print(f"⚠️ 抓取 {sym} 行情異常: {e}", flush=True)

    return market_summary


def update_ai_memory_bank_from_history(history_records):
    processed_set  = set(AI_MEMORY_BANK.get("processed_pos_keys", []))
    sym_stats      = AI_MEMORY_BANK.get("symbol_direction_stats", {})
    pat_stats      = AI_MEMORY_BANK.get("pattern_stats", {})
    snapshots      = AI_MEMORY_BANK.get("entry_snapshots", {})
    consec_losses  = AI_MEMORY_BANK.get("consecutive_losses", {s: 0 for s in WHITELIST_SYMBOLS})
    last_exit_info = AI_MEMORY_BANK.setdefault("last_exit_info", {})

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

        snap        = snapshots.get(sym, {})
        regime_type = snap.get("market_regime_type", "RANGE_MEAN_REVERSION")
        zone_bucket = snap.get("entry_zone_bucket")
        if not zone_bucket:
            bb_p = float(snap.get("bb_pos_pct", 50.0))
            zone_bucket = "HIGH_ZONE" if bb_p >= 65.0 else ("LOW_ZONE" if bb_p <= 35.0 else "MID_ZONE")

        pat_key = f"{side}_in_{regime_type}_at_{zone_bucket}"
        if pat_key not in pat_stats:
            pat_stats[pat_key] = {"wins": 0, "losses": 0, "net_pnl": 0.0}
        if pnl > 0:
            pat_stats[pat_key]["wins"] += 1
        elif pnl < 0:
            pat_stats[pat_key]["losses"] += 1
        pat_stats[pat_key]["net_pnl"] = round(pat_stats[pat_key]["net_pnl"] + pnl, 2)

    lessons = [
        "👑【v11.4 BTC 大盤聯動總開關】BTC 空頭結構時嚴禁 ETH/SOL 逆勢抄底接刀，順勢同步出擊！",
        "📐【嚴格結構反轉確認】須確認 15m MA5/MA10 結構反轉才進場，杜絕下跌途中單根假紅K誘多！",
        "💰【務實落袋止盈階梯】滿 +4.5% ROI 直接平半落袋、尾倉鎖 +1.5%，總止盈縮短至 +9.0%！"
    ]

    for p_k, pst in pat_stats.items():
        ptot = pst["wins"] + pst["losses"]
        if ptot >= 2:
            pwr = round((pst["wins"] / ptot) * 100, 1)
            if pwr <= 35.0 and pst["net_pnl"] < -500:
                lessons.append(f"🚫【高敗率型態攔截】特徵『{p_k}』歷史 {pst['wins']}勝{pst['losses']}敗 (淨虧${pst['net_pnl']})，遇到此位置禁止追單！")
            elif pwr >= 55.0 and pst["net_pnl"] > 1000:
                lessons.append(f"🏆【印鈔機型態確認】特徵『{p_k}』歷史 {pst['wins']}勝{pst['losses']}敗 (勝率{pwr}%，淨賺+${pst['net_pnl']})，出現時優先出擊！")

    AI_MEMORY_BANK["processed_pos_keys"]     = list(processed_set)[-300:]
    AI_MEMORY_BANK["symbol_direction_stats"] = sym_stats
    AI_MEMORY_BANK["pattern_stats"]          = pat_stats
    AI_MEMORY_BANK["consecutive_losses"]     = consec_losses
    AI_MEMORY_BANK["last_exit_info"]         = last_exit_info
    AI_MEMORY_BANK["lessons_learned"]        = lessons[:12]


def evaluate_room_to_run_and_pattern(sym, action, current_price, market_data):
    """
    🧠 v11.4 智慧交易員進場評估器：
    最高優先權加入『BTC 大盤聯動總開關』＋『防軋空/防接刀硬鎖』＋『嚴格結構反轉確認』！
    """
    m_info       = market_data.get(sym, {})
    regime_type  = m_info.get("market_regime_type", "RANGE_MEAN_REVERSION")
    zone_bucket  = m_info.get("entry_zone_bucket", "MID_ZONE")
    bb_pos       = float(m_info.get("bb_pos_pct", 50.0))
    range_pos    = float(m_info.get("range_pos_pct", 50.0))
    up_room      = float(m_info.get("upside_room_pct", 1.5))
    dn_room      = float(m_info.get("downside_room_pct", 1.5))
    rsi_val      = float(m_info.get("rsi_14", 50.0))

    anti_squeeze = bool(m_info.get("anti_squeeze_block_short", False))
    anti_knife   = bool(m_info.get("anti_knife_block_long", False))
    bear_rev     = bool(m_info.get("bear_reversal_confirmed", False))
    bull_rev     = bool(m_info.get("bull_reversal_confirmed", False))
    news_alert   = bool(NEWS_RADAR_CACHE.get("high_alert", False))

    if action not in ["BUY", "SELL"]:
        return False, action, f"⏸️ {sym} 當前訊號為觀望 ({action})"

    # 1. 短線冷卻檢查
    exit_info = AI_MEMORY_BANK.get("last_exit_info", {}).get(sym)
    if exit_info:
        now_ms = int(time.time() * 1000)
        elapsed_mins = (now_ms - int(exit_info.get("timestamp_ms", 0))) / 60000.0
        last_pnl     = float(exit_info.get("pnl", 0.0))
        if last_pnl < -50 and elapsed_mins < LOSS_COOLDOWN_MINUTES:
            return False, action, f"⏳ {sym} 剛止損 ({int(elapsed_mins)}分前)，冷卻滿 {LOSS_COOLDOWN_MINUTES} 分鐘待盤勢穩定再判讀"
        if last_pnl >= 0 and elapsed_mins < WIN_COOLDOWN_MINUTES:
            return False, action, f"⏳ {sym} 剛止盈 ({int(elapsed_mins)}分前)，等待 {WIN_COOLDOWN_MINUTES} 分鐘換線觀察"

    # 👑 2. 最高優先權：BTC 大盤聯動總開關 (ETH / SOL 嚴禁逆著 BTC 1H 趨勢操作)
    btc_info     = market_data.get("BTC/USDT:USDT", {})
    btc_macro    = btc_info.get("macro_structure_1h", "NEUTRAL")
    btc_trend_1h = btc_info.get("trend_1h", "NEUTRAL")
    if sym != "BTC/USDT:USDT":
        if action == "BUY" and (btc_macro == "TRUE_BEAR_TREND" or btc_trend_1h == "BEARISH"):
            return False, action, f"👑 [BTC 大盤聯動濾網攔截] BTC 處於空頭結構({btc_macro})，嚴禁 {sym} 逆勢抄底做多！"
        if action == "SELL" and (btc_macro == "TRUE_BULL_TREND" or btc_trend_1h == "BULLISH"):
            return False, action, f"👑 [BTC 大盤聯動濾網攔截] BTC 處於多頭結構({btc_macro})，嚴禁 {sym} 逆勢摸頂做空！"

    # 🛡️ 3. 防軋空硬鎖 & 防接刀硬鎖
    if action == "SELL" and anti_squeeze:
        return False, action, f"🛡️ [防軋空盾攔截] {sym} 處於強勢多頭/連紅推升或 BB:{bb_pos}% 未轉弱，嚴禁逆勢摸頂做空！"
    if action == "BUY" and anti_knife:
        return False, action, f"🛡️ [防接刀盾攔截] {sym} 處於強勢空頭/連黑急殺或 BB:{bb_pos}% 未止跌，嚴禁盲目抄底接刀！"

    # 4. 震盪盤空間盈虧比與嚴格結構反轉確認
    if regime_type == "RANGE_MEAN_REVERSION":
        if action == "BUY" and not bull_rev:
            return False, action, f"⏸️ [等待結構止跌] {sym} 尚未確認 MA5>MA10 止跌結構，不盲目抄底！"
        if action == "SELL" and not bear_rev:
            return False, action, f"⏸️ [等待結構轉弱] {sym} 尚未確認 MA5<MA10 轉弱結構，不盲目摸頂！"

        if action == "BUY" and (zone_bucket == "HIGH_ZONE" or bb_pos >= 68.0 or range_pos >= 72.0 or rsi_val >= 64.0):
            if not anti_squeeze and bear_rev and not news_alert and dn_room >= 1.2 and (sym == "BTC/USDT:USDT" or btc_trend_1h != "BULLISH"):
                return True, "SELL", f"🔄 [智慧變通：受阻轉弱➔逢高做空] {sym} 處於12h壓力區(BB:{bb_pos}%)且已確認結構轉弱！"
            return False, action, f"⏸️ [高檔防追高觀望] {sym} 處於高檔區(BB:{bb_pos}%)，上方僅剩 {up_room}%，先觀望！"

        if action == "SELL" and (zone_bucket == "LOW_ZONE" or bb_pos <= 32.0 or range_pos <= 28.0 or rsi_val <= 36.0):
            if not anti_knife and bull_rev and not news_alert and up_room >= 1.2 and (sym == "BTC/USDT:USDT" or btc_trend_1h != "BEARISH"):
                return True, "BUY", f"🔄 [智慧變通：止跌反轉➔逢低抄底] {sym} 處於12h支撐區(BB:{bb_pos}%)且已確認結構止跌！"
            return False, action, f"⏸️ [低檔防殺低觀望] {sym} 處於低檔區(BB:{bb_pos}%)，下方僅剩 {dn_room}%，先觀望！"

    # 5. 檢查特徵指紋記憶庫
    side_str = "LONG" if action == "BUY" else "SHORT"
    pat_key  = f"{side_str}_in_{regime_type}_at_{zone_bucket}"
    pat_stat = AI_MEMORY_BANK.get("pattern_stats", {}).get(pat_key, {})
    ptot     = pat_stat.get("wins", 0) + pat_stat.get("losses", 0)
    if ptot >= 3:
        pwr = (pat_stat.get("wins", 0) / ptot) * 100.0
        if pwr < 30.0 and pat_stat.get("net_pnl", 0.0) < -1000:
            return False, action, f"🧠 [記憶庫型態攔截] 特徵指紋『{pat_key}』歷史勝率僅 {pwr:.0f}%，拒絕在同一陷阱重複跌倒！"

    return True, action, f"✅ [v11.4 BTC聯動與結構核驗通過] 體質:{regime_type} | 位置:{zone_bucket}(BB:{bb_pos}%) | 向上:{up_room}% / 向下:{dn_room}%"


def build_fallback_plan(market_data):
    half_a, half_b = [], []
    for sym in WHITELIST_SYMBOLS:
        info        = market_data.get(sym, {})
        act         = info.get("tactical_signal", "HOLD")
        if act not in ["BUY", "SELL"]:
            continue
        desc        = info.get("tactical_desc", "依 BTC 大盤聯動與結構確認擇優出擊")
        safe_lev    = int(info.get("max_safe_leverage", 3))
        tp_v        = float(info.get("anchored_tp_pct", 2.5))
        sl_v        = float(info.get("anchored_sl_pct", 1.5))
        regime_type = info.get("market_regime_type", "RANGE_MEAN_REVERSION")

        half_a.append({
            "symbol": sym, "action": act, "weight": 1.5, "leverage": safe_lev,
            "tp_pct": tp_v, "sl_pct": sl_v,
            "reason": f"【{regime_type}】{desc}"
        })
        half_b.append({
            "symbol": sym, "action": act, "weight": 1.2, "leverage": safe_lev,
            "tp_pct": round(tp_v * 1.1, 2), "sl_pct": sl_v,
            "reason": f"【結構確認優選】{desc}"
        })
    return {
        "overall_confidence": 94,
        "overall_summary": f"v11.4 BTC大盤聯動＋務實落袋(+4.5%平半/+9%總止盈/-5%止損)運行中（{NEWS_RADAR_CACHE.get('alert_note', '新聞監控中')}）。",
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

    btc_trend = market_data.get("BTC/USDT:USDT", {}).get("macro_structure_1h", "UNKNOWN")

    prompt = f"""
    你是 QuantGuard AI v11.4 頂級對沖基金交易大腦。你的任務是「順應 BTC 大盤總方向、嚴格等待結構反轉、務實落袋為安」！

    核心鐵律：
1. 【BTC 大盤聯動總開關】：目前 BTC 1H 結構為 `{btc_trend}`。若 BTC 偏空，**嚴禁對 ETH / SOL 輸出 `"BUY"`**；若 BTC 偏多，**嚴禁對 ETH / SOL 輸出 `"SELL"`**！
2. 【防軋空與防接刀】：`anti_squeeze_block_short == true` 禁空；`anti_knife_block_long == true` 禁多。
3. 【嚴格結構確認】：震盪盤必須 `bull_reversal_confirmed == true` 才可做多，`bear_reversal_confirmed == true` 才可做空，否則輸出 `"HOLD"`！
4. 【新聞警戒】：當前狀態 `{NEWS_RADAR_CACHE.get('alert_note', '平穩')}`。

    【1. 三大幣種即時多維度體質、結構確認與 12h 空間比】：
    {json.dumps(market_data, ensure_ascii=False)}

    【2. 🧠 AI 特徵指紋記憶庫】：
- 型態勝敗統計：{json.dumps(AI_MEMORY_BANK.get("pattern_stats", {}), ensure_ascii=False)}
- 提煉法則：{json.dumps(AI_MEMORY_BANK.get("lessons_learned", []), ensure_ascii=False)}

    【3. 最新國際新聞】：{json.dumps(news_headlines, ensure_ascii=False)}
    【4. 目前持有倉位】：{json.dumps(current_held_symbols, ensure_ascii=False)}

    請嚴格只輸出 JSON 格式（`leverage` 請設為 `3` ~ `4` 倍）：
    {{
      "overall_confidence": 94,
      "overall_summary": "繁體中文65字內：說明 BTC 大盤方向、結構確認狀態與順勢策略方向",
      "black_swan_detected": false,
      "sniper_order": null,
      "half_a_orders": [
        {{"symbol": "BTC/USDT:USDT", "action": "HOLD", "weight": 1.6, "leverage": 3, "tp_pct": 2.5, "sl_pct": 1.6, "reason": "【BTC聯動與結構判斷】..."}}
      ],
      "half_b_orders": []
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
    safe_margin = min(desired_margin_usdt, max_allowed_margin, current_free * 0.85)
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
    if required_margin > current_free * 0.88:
        contracts = int((current_free * 0.82 * leverage) // notional_per_contract)

    actual_margin = round((contracts * notional_per_contract) / leverage, 2)
    return max(0, contracts), actual_margin


def has_pending_tpsl_algo_order(exchange, sym):
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
    state_changed = False

    for pos in active_positions:
        sym       = pos['symbol']
        side      = pos['side']
        entry_px  = float(pos['entry_price'])
        contracts = int(float(pos['contracts']))
        roi_pct   = float(pos['roi_pct'])
        lev_num   = max(1.0, float(str(pos.get('leverage', '3x')).replace('x', '')))
        tp_pct    = float(pos.get('tp_pct', 2.5))
        pos_key   = f"{sym}_{side}"
        cur_stage = float(POSITION_STAGE_MAP.get(pos_key, 0.0))

        if entry_px <= 0 or contracts <= 0:
            continue

        # 🛑 1. 絕對止損鐵律攔截：含槓桿 ROI <= -5.6% 立即市價砍單！
        if roi_pct <= HARD_CUT_SL_ROI_PCT:
            print(f"🛑 [防滑價硬斷頭觸發] {sym} ({side}) 實時含槓桿 ROI 達 {roi_pct:.2f}% (<= {HARD_CUT_SL_ROI_PCT}%)，立即市價平倉！", flush=True)
            cancel_existing_algo_orders_for_symbol(exchange, sym)
            close_partial_or_full_position(exchange, sym, side, contracts, f"死守{HARD_CUT_SL_ROI_PCT}%硬止損防滑價")
            POSITION_STAGE_MAP.pop(pos_key, None)
            state_changed = True
            continue

        base_tp_px = round(entry_px * (1 + tp_pct / 100.0) if side == 'LONG' else entry_px * (1 - tp_pct / 100.0), 2)

        # 🔥 2. 階段三：含槓桿 ROI >= +10.5% ➔ 尾倉止損鎖 +7.5%，止盈外推 1.4 倍
        if roi_pct >= STAGE3_TRIGGER_ROI_PCT and cur_stage < 3.0:
            lock_s3_sl = roi_to_price(entry_px, side, STAGE3_LOCK_ROI_PCT, lev_num)
            ext_tp_px  = round(entry_px * (1 + (tp_pct * 1.4) / 100.0) if side == 'LONG' else entry_px * (1 - (tp_pct * 1.4) / 100.0), 2)
            update_position_tpsl_on_okx(exchange, sym, side, contracts, ext_tp_px, lock_s3_sl)
            POSITION_STAGE_MAP[pos_key] = 3
            pos['sl_trigger_px'] = lock_s3_sl
            pos['tp_trigger_px'] = ext_tp_px
            pos['stage_badge']   = f"🔥 尾倉鎖 +{STAGE3_LOCK_ROI_PCT}% ROI"
            pos['reason']        = f"🔥【含槓桿ROI滿+{STAGE3_TRIGGER_ROI_PCT}%】50%主力尾倉止損已鎖定於 +{STAGE3_LOCK_ROI_PCT}% (${lock_s3_sl})！"
            state_changed = True

        # 🚀 3. 階段二：含槓桿 ROI >= +7.5% ➔ 尾倉止損上推鎖 +4.5%
        elif roi_pct >= STAGE2_TRIGGER_ROI_PCT and cur_stage < 2.0:
            lock_s2_sl = roi_to_price(entry_px, side, STAGE2_LOCK_ROI_PCT, lev_num)
            update_position_tpsl_on_okx(exchange, sym, side, contracts, base_tp_px, lock_s2_sl)
            POSITION_STAGE_MAP[pos_key] = 2
            pos['sl_trigger_px'] = lock_s2_sl
            pos['tp_trigger_px'] = base_tp_px
            pos['stage_badge']   = f"🚀 尾倉鎖 +{STAGE2_LOCK_ROI_PCT}% ROI"
            pos['reason']        = f"🚀【含槓桿ROI滿+{STAGE2_TRIGGER_ROI_PCT}%】50%主力尾倉止損已鎖定於 +{STAGE2_LOCK_ROI_PCT}% (${lock_s2_sl})！"
            state_changed = True

            send_discord_alert(
                title=f"🚀 階段二主力尾倉推鎖 (+{STAGE2_LOCK_ROI_PCT}% ROI)：{sym}",
                description=f"**{sym} ({side})** 剩餘 50% 主力倉含槓桿收益率達 **+{roi_pct:.2f}%**！\n已將止損單上移鎖定於 **${lock_s2_sl:,.2f} (保證含槓桿獲利 +{STAGE2_LOCK_ROI_PCT}%)**！",
                color=5763719
            )

        # 💰 4. 階段一：含槓桿 ROI >= +4.5% ➔ 直接平倉 50% 落袋！剩餘 50% 止損鎖在 +1.5%（覆蓋手續費保底）
        elif roi_pct >= STAGE1_TRIGGER_ROI_PCT and cur_stage < 1.0:
            close_qty  = max(1, int(round(contracts * STAGE1_CLOSE_RATIO)))
            remain_qty = max(1, contracts - close_qty)
            lock_s1_sl = roi_to_price(entry_px, side, STAGE1_LOCK_ROI_PCT, lev_num)

            print(f"💰 [v11.4 務實落袋觸發：ROI +{roi_pct:.2f}% >= +{STAGE1_TRIGGER_ROI_PCT}%] {sym} ({side}) 平 50% ({close_qty}張) 落袋！剩餘 50% ({remain_qty}張) 鎖 +{STAGE1_LOCK_ROI_PCT}% ROI (${lock_s1_sl})！", flush=True)
            if contracts >= 2:
                close_partial_or_full_position(exchange, sym, side, close_qty, "含槓桿ROI滿+4.5%平50%倉落袋")
            update_position_tpsl_on_okx(exchange, sym, side, remain_qty, base_tp_px, lock_s1_sl)

            POSITION_STAGE_MAP[pos_key] = 1
            pos['contracts']     = remain_qty
            pos['sl_trigger_px'] = lock_s1_sl
            pos['tp_trigger_px'] = base_tp_px
            pos['stage_badge']   = f"💰 已平50%落袋 & 50%尾倉鎖+{STAGE1_LOCK_ROI_PCT}%ROI"
            pos['reason']        = f"💰【已落袋50%倉位】剩餘 50% ({remain_qty}張) 止損鎖於 +{STAGE1_LOCK_ROI_PCT}% ROI (${lock_s1_sl})，目標 +{DEFAULT_TARGET_TP_ROI}%！"
            state_changed = True

            send_discord_alert(
                title=f"💰 滿 +{STAGE1_TRIGGER_ROI_PCT}% ROI 平 50% 落袋 & 50% 尾倉鎖保底 | {sym}",
                description=f"**{sym} ({side} {int(lev_num)}x)** 含槓桿收益率達 **+{roi_pct:.2f}%**！\n1️⃣ 已先市價平倉 **50% ({close_qty}張)** 落袋為安！\n2️⃣ 剩餘 **50% 尾倉 ({remain_qty}張)** 止損移至 **${lock_s1_sl:,.2f} (+{STAGE1_LOCK_ROI_PCT}% ROI 保底)**！",
                color=3066993
            )

        # 🛡️ 5. 孤兒單防裸奔自癒檢查
        else:
            if not has_pending_tpsl_algo_order(exchange, sym):
                cur_sl_px = float(pos.get('sl_trigger_px') or roi_to_price(entry_px, side, -MAX_SL_ROI_TRIGGER_PCT, lev_num))
                print(f"🛡️ [孤兒單自癒補掛] 偵測到 {sym} ({side}) 缺少 OKX 條件單，立即自動補掛止盈 ${base_tp_px} / 止損 ${cur_sl_px}！", flush=True)
                update_position_tpsl_on_okx(exchange, sym, side, contracts, base_tp_px, cur_sl_px)

    return state_changed


def execute_pool_orders(exchange, pool_name, orders_list, pool_budget_usdt, market_data, reason_lookup, held_symbols_map, total_equity, max_allowed_active_positions):
    valid_orders = [o for o in orders_list[:MAX_ORDERS_PER_HALF] if str(o.get("action", "")).upper() in ["BUY", "SELL"]]
    if not valid_orders or pool_budget_usdt < 30:
        return False

    any_placed = False
    consec_losses = AI_MEMORY_BANK.get("consecutive_losses", {})

    for ord_spec in valid_orders:
        sym = ord_spec.get("symbol", "ETH/USDT:USDT")
        if sym not in WHITELIST_SYMBOLS:
            sym = "ETH/USDT:USDT"

        m_info      = market_data.get(sym, {})
        safe_lev    = int(m_info.get("max_safe_leverage", 3))
        regime_type = m_info.get("market_regime_type", "RANGE_MEAN_REVERSION")
        zone_bucket = m_info.get("entry_zone_bucket", "MID_ZONE")
        rsi_val     = float(m_info.get("rsi_14", 50.0))

        raw_action    = str(ord_spec.get("action", m_info.get("tactical_signal", "HOLD"))).upper()
        current_price = float(m_info.get("last_price", 0))
        if current_price <= 0:
            continue

        can_enter, final_action, eval_msg = evaluate_room_to_run_and_pattern(sym, raw_action, current_price, market_data)
        print(f"   [{sym}] {eval_msg}", flush=True)
        if not can_enter:
            continue

        ai_lev   = int(ord_spec.get("leverage", safe_lev))
        leverage = max(3, min(4, min(ai_lev, safe_lev)))

        sl_pct = round(MAX_SL_ROI_TRIGGER_PCT / float(leverage), 2)
        tp_pct = round(max(2.0, min(3.2, DEFAULT_TARGET_TP_ROI / float(leverage))), 2)

        mode_tag = f"👑v11.4({regime_type[:5]}|{zone_bucket}|止損-{MAX_SL_ROI_TRIGGER_PCT}%ROI)"
        clean_ai_reason = str(ord_spec.get("reason", m_info.get("tactical_desc", "")))
        reason = f"[{mode_tag}] {eval_msg} | {clean_ai_reason}"

        reason_lookup[sym] = {
            "tp_pct": tp_pct, "sl_pct": sl_pct, "leverage": leverage,
            "pool": pool_name, "reason": reason
        }

        if sym in held_symbols_map:
            continue

        if len(held_symbols_map) >= max_allowed_active_positions:
            print(f"🛡️ [投資組合熱度鎖] 目前已持有 {len(held_symbols_map)} 筆且尚有倉位未達保底線，暫緩開立第 3 筆 ({sym})！", flush=True)
            continue

        target_ratio = TARGET_SINGLE_MARGIN_RATIO * (0.75 if consec_losses.get(sym, 0) >= 1 else 1.0)
        desired_margin = round(min(total_equity * target_ratio, pool_budget_usdt), 2)

        contracts, safe_margin = calculate_safe_contracts(exchange, sym, desired_margin, leverage, current_price, total_equity)
        if contracts <= 0:
            continue

        side_upper = 'LONG' if final_action == 'BUY' else 'SHORT'
        tp_price = roi_to_price(current_price, side_upper, round(tp_pct * leverage, 2), leverage)
        sl_price = roi_to_price(current_price, side_upper, -MAX_SL_ROI_TRIGGER_PCT, leverage)

        order = place_okx_order_with_tpsl(exchange, sym, final_action, contracts, leverage, tp_price, sl_price, pool_name)
        if order:
            held_symbols_map[sym] = side_upper
            POSITION_STAGE_MAP[f"{sym}_{side_upper}"] = 0
            AI_MEMORY_BANK.setdefault("entry_snapshots", {})[sym] = {
                "side": side_upper,
                "reason": reason,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "leverage": leverage,
                "market_regime_type": regime_type,
                "entry_zone_bucket": zone_bucket,
                "macro_structure_1h": m_info.get("macro_structure_1h", "GENERAL"),
                "rsi_14": rsi_val,
                "bb_pos_pct": m_info.get("bb_pos_pct", 50.0),
                "range_pos_pct": m_info.get("range_pos_pct", 50.0),
                "upside_room_pct": m_info.get("upside_room_pct", 1.5),
                "downside_room_pct": m_info.get("downside_room_pct", 1.5),
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
        lev          = info.get('lever') or item.get('leverage') or 3

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
                {"name": "🏆 本期累計總實現盈虧 (近100筆)", "value": f"`${all_time_realized_pnl:+,.2f} USDT`", "inline": True},
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

    unprotected_stage0_count = sum(1 for k in active_pos_keys if float(POSITION_STAGE_MAP.get(k, 0.0)) < 1.0)
    max_allowed_active_positions = 2 if unprotected_stage0_count >= 1 else 3

    market_data = fetch_whitelist_market_data(exchange)
    prelim_history, perf_stats = fetch_okx_positions_history_and_analytics(exchange, market_data, {}, "", real_usdt, 0.0)

    mistake_memory = [
        {"asset": h["asset"], "direction": h["direction"], "roi": h["roi"]}
        for h in prelim_history[:5]
    ]

    news_list = fetch_live_crypto_news()

    should_call_ai = (
        LAST_AI_PLAN_CACHE is None
        or len(held_symbols_map) < max_allowed_active_positions
        or (cycle_num % SLOW_AI_EVERY_N_TICKS == 0)
    )

    if should_call_ai:
        LAST_AI_PLAN_CACHE = ask_gemini_quant_brain(
            market_data, news_list, held_symbols_map, mistake_memory
        )
        print(f"🧠 [AI 智慧決策更新]: {LAST_AI_PLAN_CACHE.get('overall_summary')}", flush=True)
    else:
        print(f"⚡ [快軌盯盤] 總資產: ${real_usdt:,.2f} | 今日已實現: ${perf_stats['today_realized_pnl']:+,.2f} | 本期累計: ${perf_stats['all_time_realized_pnl']:+,.2f}", flush=True)

    ai_plan = LAST_AI_PLAN_CACHE or build_fallback_plan(market_data)

    used_margin_now = max(0.0, real_usdt - free_usdt)
    normal_pool_target = real_usdt * POOL_NORMAL_RATIO
    remaining_normal_budget = max(0.0, min(free_usdt * 0.85, normal_pool_target - used_margin_now))

    reason_lookup = {}
    order_placed = False
    if remaining_normal_budget >= 80 and len(held_symbols_map) < max_allowed_active_positions:
        p1 = execute_pool_orders(exchange, "Half-A BTC聯動主力池 (20%倉)", ai_plan.get("half_a_orders", []), remaining_normal_budget, market_data, reason_lookup, held_symbols_map, real_usdt, max_allowed_active_positions)
        p2 = execute_pool_orders(exchange, "Half-B 結構優選池 (20%倉)", ai_plan.get("half_b_orders", []), remaining_normal_budget, market_data, reason_lookup, held_symbols_map, real_usdt, max_allowed_active_positions)
        order_placed = p1 or p2

        if not order_placed and len(held_symbols_map) == 0:
            fb_plan = build_fallback_plan(market_data)
            p3 = execute_pool_orders(exchange, "Half-A 結構優選補倉池 (20%倉)", fb_plan.get("half_a_orders", []), remaining_normal_budget, market_data, reason_lookup, held_symbols_map, real_usdt, max_allowed_active_positions)
            order_placed = order_placed or p3

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
                lev_num      = max(1.0, float(pos.get('leverage') or 3))
                m_info       = market_data.get(sym, {})
                mark_px      = round(float(pos.get('markPrice') or m_info.get('last_price', 0)), 2)

                roi_pct      = compute_exact_position_roi(side_u, entry_px, mark_px, lev_num, upl, margin_used)
                total_upl   += upl

                snap_info = snapshots.get(sym, {})
                r_info    = reason_lookup.get(sym, {})

                max_sl_p = round(MAX_SL_ROI_TRIGGER_PCT / lev_num, 2)
                tp_p_val = float(snap_info.get("tp_pct") or r_info.get("tp_pct") or m_info.get("anchored_tp_pct", 2.5))
                sl_p_val = max_sl_p

                pos_key   = f"{sym}_{side_u}"
                cur_stage = float(POSITION_STAGE_MAP.get(pos_key, 0.0))
                regime_t  = m_info.get("market_regime_type", "RANGE")

                if cur_stage >= 3.0:
                    sl_trig = roi_to_price(entry_px, side_u, STAGE3_LOCK_ROI_PCT, lev_num)
                    stage_badge = f"🔥 尾倉鎖 +{STAGE3_LOCK_ROI_PCT}% ROI"
                elif cur_stage >= 2.0:
                    sl_trig = roi_to_price(entry_px, side_u, STAGE2_LOCK_ROI_PCT, lev_num)
                    stage_badge = f"🚀 尾倉鎖 +{STAGE2_LOCK_ROI_PCT}% ROI"
                elif cur_stage >= 1.0:
                    sl_trig = roi_to_price(entry_px, side_u, STAGE1_LOCK_ROI_PCT, lev_num)
                    stage_badge = f"💰 已平50%落袋 & 50%尾倉鎖+{STAGE1_LOCK_ROI_PCT}%ROI"
                else:
                    sl_trig = roi_to_price(entry_px, side_u, -MAX_SL_ROI_TRIGGER_PCT, lev_num)
                    stage_badge = f"👑 滿+{STAGE1_TRIGGER_ROI_PCT}%平半鎖+{STAGE1_LOCK_ROI_PCT}% / 止損-{MAX_SL_ROI_TRIGGER_PCT}%"

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
                    "reason": snap_info.get("reason") or r_info.get("reason") or f"體質:{regime_t} | v11.4 務實落袋 | 止損-{MAX_SL_ROI_TRIGGER_PCT}%"
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
            "confidence": ai_plan.get("overall_confidence", 94),
            "summary": f"[{NEWS_RADAR_CACHE.get('alert_note', '新聞監控')}] {ai_plan.get('overall_summary', '')}",
            "half_a_orders": ai_plan.get("half_a_orders", []),
            "half_b_orders": ai_plan.get("half_b_orders", [])
        },
        "ai_last_decision": {
            "action": f"今日已實現 ${final_perf_stats['today_realized_pnl']:+,.2f} | 本期累計 ${final_perf_stats['all_time_realized_pnl']:+,.2f} (盈虧比 {final_perf_stats['profit_factor']}x)",
            "confidence": ai_plan.get("overall_confidence", 94),
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
    print("🚀 QuantGuard AI v11.4 - BTC大盤聯動 × 結構反轉 × 務實落袋止盈引擎", flush=True)
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
