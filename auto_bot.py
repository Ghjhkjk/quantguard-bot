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

# 🌟 關鍵修正：設定本份資金的正式起算基準點 (2026/11/05 17:01:00 台灣時間，即 17:01:16 BTC 單起算)
# 早於此時間的 10/29、11/04 等舊測試倉位一律不計入歷史損益與勝率！
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
# ⚙️ 2. v10.1 三層資金池 (80/10/10) 與波動率分批落袋參數
# ==========================================================
WHITELIST_SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']

POOL_NORMAL_RATIO = 0.80  # 80% 常態雙核心池 (Half-A 40% + Half-B 40%)
POOL_HEDGE_RATIO  = 0.10  # 10% 緊急急跌對沖池
POOL_SNIPER_RATIO = 0.10  # 10% 黑天鵝突發狙擊池

MAX_ORDERS_PER_HALF     = 3
MAX_CONTRACTS_PER_ORDER = 1200

# 🎯 含槓桿 ROI 階梯鎖利與 50% 分批止盈門檻
STAGE1_ROI_TRIGGER_PCT  = 5.0   # 含槓桿 ROI 達 +5.0% -> 撤舊單，止損移至開倉價 +0.25% (保本微利)
STAGE1_PRICE_LOCK_PCT   = 0.25
STAGE2_ROI_TRIGGER_PCT  = 9.0   # 含槓桿 ROI 達 +9.0% -> 立即平倉 50% 落袋，剩餘 50% 止損移至開倉價 +1.0%
STAGE2_PRICE_LOCK_PCT   = 1.00
EARLY_HARVEST_ROI_PCT   = 4.5   # 若帳面 ROI >= +4.5% 且短線均線動能反轉衰竭，提前收割全平

IS_GITHUB_ACTIONS     = os.environ.get("GITHUB_ACTIONS") == "true"
RUN_CONTINUOUS_LOOP   = True
FAST_TRACK_INTERVAL   = 20    # 快軌：每 20 秒高速巡檢一次 OKX (0 消耗 Gemini Key)
SLOW_AI_EVERY_N_TICKS = 45    # 慢軌：每 45 個快軌 (約 15 分鐘) 或空倉時才呼叫一次 Gemini AI
MAX_JOB_RUNTIME_SEC   = int(os.environ.get("MAX_JOB_RUNTIME_SEC", "240"))

LAST_AI_PLAN_CACHE = None
NOTIFIED_POS_IDS   = set()
POSITION_STAGE_MAP = {}
HEDGE_ACTIVE_SYMS  = set()
LAST_GIT_PUSH_TS   = 0.0


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
        "footer": {"text": "QuantGuard AI v10.1 | 官方實盤數據與 AI 波動率定錨終端"}
    }
    if fields:
        embed["fields"] = fields
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=json.dumps({"embeds": [embed]}).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'User-Agent': 'QuantGuardBot/10.1'}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"⚠️ Discord 推播異常: {e}", flush=True)


def load_previous_state():
    global NOTIFIED_POS_IDS, POSITION_STAGE_MAP
    if not os.path.exists("live_data.json"):
        return
    try:
        with open("live_data.json", "r", encoding="utf-8") as f:
            old_data = json.load(f)
        for o in old_data.get("orders", []):
            fid = str(o.get("full_id") or o.get("id") or "")
            if fid:
                NOTIFIED_POS_IDS.add(fid)
        saved_stages = old_data.get("position_stage_map", {})
        if isinstance(saved_stages, dict):
            POSITION_STAGE_MAP.update(saved_stages)
    except Exception:
        pass


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
            subprocess.run(["git", "commit", "-m", "Real-time sync live_data.json v10.1"], check=False)
            subprocess.run(["git", "pull", "--rebase", "origin", "main"], check=False)
            subprocess.run(["git", "push", "origin", "main"], check=False)
            LAST_GIT_PUSH_TS = now_ts
            print("🚀 [即時雲端同步] 已將最新 live_data.json 即時推送至網站！", flush=True)
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


def fetch_whitelist_market_data(exchange):
    market_summary = {}
    for sym in WHITELIST_SYMBOLS:
        try:
            ticker = exchange.fetch_ticker(sym)
            ohlcv = exchange.fetch_ohlcv(sym, timeframe='15m', limit=24)
            closes = [c[4] for c in ohlcv]
            highs  = [c[2] for c in ohlcv]
            lows   = [c[3] for c in ohlcv]
            vols   = [c[5] for c in ohlcv]
            current_price = float(ticker['last'])

            ma5  = sum(closes[-5:]) / 5 if len(closes) >= 5 else current_price
            ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else current_price
            ma20 = sum(closes) / len(closes) if closes else current_price
            tr_list   = [(highs[i] - lows[i]) / closes[i] * 100 for i in range(len(closes))]
            atr_pct   = sum(tr_list) / len(tr_list) if tr_list else 0.8
            swing_pct = ((max(highs[-12:]) - min(lows[-12:])) / current_price) * 100 if len(highs) >= 12 else 2.2
            change_24h = float(ticker.get('percentage') or 0.0)

            bar_change_pct = ((closes[-1] - closes[-2]) / closes[-2] * 100) if len(closes) >= 2 else 0.0
            avg_vol = (sum(vols[:-1]) / max(1, len(vols) - 1)) if len(vols) > 1 else 1.0
            vol_spike_ratio = round(vols[-1] / avg_vol, 2) if avg_vol > 0 else 1.0

            anchored_tp_pct = round(max(1.5, min(3.2, swing_pct * 0.75)), 2)
            anchored_sl_pct = round(max(1.2, min(2.8, swing_pct * 0.55)), 2)

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
                "atr_15m_pct": round(atr_pct, 2),
                "recent_swing_pct": round(swing_pct, 2),
                "anchored_tp_pct": anchored_tp_pct,
                "anchored_sl_pct": anchored_sl_pct,
                "trend": "BULLISH" if ma5 > ma20 else "BEARISH",
                "momentum_weakening_long": (current_price < ma5 and ma5 < ma10),
                "momentum_weakening_short": (current_price > ma5 and ma5 > ma10)
            }
        except Exception as e:
            print(f"⚠️ 抓取 {sym} 行情異常: {e}", flush=True)
    return market_summary


def build_fallback_plan(market_data):
    half_a, half_b = [], []
    for sym in WHITELIST_SYMBOLS:
        info = market_data.get(sym, {})
        act = "BUY" if info.get("trend") == "BULLISH" else "SELL"
        tp_v = float(info.get("anchored_tp_pct", 2.2))
        sl_v = float(info.get("anchored_sl_pct", 1.8))
        half_a.append({
            "symbol": sym, "action": act, "weight": 1.3, "leverage": 6,
            "tp_pct": tp_v, "sl_pct": sl_v,
            "reason": f"【Half-A 波動定錨】15m 呈 {info.get('trend')}，依振幅設精準止盈 +{tp_v}% (ROI滿9%先平半倉)。"
        })
        half_b.append({
            "symbol": sym, "action": act, "weight": 1.1, "leverage": 6,
            "tp_pct": round(tp_v * 1.15, 2), "sl_pct": sl_v,
            "reason": f"【Half-B 籌碼動能】費率 {info.get('funding_rate_pct')}%，量能比 {info.get('vol_spike_ratio')}x。"
        })
    return {
        "overall_confidence": 85,
        "overall_summary": "採用 ATR 波動率定錨止盈 (1.5%~3.2%) 搭配 ROI +5% 保本、+9% 半倉落袋機制。",
        "black_swan_detected": False,
        "sniper_order": None,
        "half_a_orders": half_a[:2],
        "half_b_orders": half_b[:2]
    }


def ask_gemini_quant_brain(market_data, news_headlines, current_held_symbols, mistake_memory, crowd_win_stats):
    if not GENAI_AVAILABLE:
        return build_fallback_plan(market_data)

    gemini_key = clean_credential(GEMINI_API_KEY, "GEMINI_API_KEY")
    client = genai.Client(api_key=gemini_key)

    prompt = f"""
    你是 QuantGuard AI v10.1 旗艦量化決策核心。請根據即時行情、波動率定錨指標、新聞與【AI 實戰錯題本】規劃訂單：
    【行情與波動定錨目標 (請優先採用 anchored_tp_pct 與 anchored_sl_pct)】：{json.dumps(market_data, ensure_ascii=False)}
    【最新國際新聞】：{json.dumps(news_headlines, ensure_ascii=False)}
    【目前持有倉位 (嚴禁對已持有幣種開立反向常態單)】：{json.dumps(current_held_symbols, ensure_ascii=False)}
    【近期歷史平倉錯題本 (自 11/05 17:01 新資金起算)】：{json.dumps(mistake_memory[:5], ensure_ascii=False)}
    【AI 思考策略勝率統計】：{json.dumps(crowd_win_stats, ensure_ascii=False)}

    【核心鐵律】：
    1. 止盈 (`tp_pct`) 必須嚴格貼合當下 15m 波動率：設定在 `1.5% ~ 3.5%` 之間（參考 `anchored_tp_pct`），不可設超過 4.5%！
    2. 止損 (`sl_pct`) 設定在 `1.2% ~ 2.8%` 之間（參考 `anchored_sl_pct`）。
    3. 槓桿 (`leverage`) 設定為 `5 ~ 8` 倍。
    4. `half_a_orders` 與 `half_b_orders` 中，同一幣種的方向 (`action`) 必須一致，禁止同幣種多空雙開互鎖！

    請嚴格只輸出 JSON 格式：
    {{
      "overall_confidence": 88,
      "overall_summary": "繁體中文50字內策略與波動率定錨總結",
      "black_swan_detected": false,
      "sniper_order": null,
      "half_a_orders": [
        {{"symbol": "BTC/USDT:USDT", "action": "BUY", "weight": 1.5, "leverage": 6, "tp_pct": 2.2, "sl_pct": 1.6, "reason": "【Half-A 波動定錨】..."}}
      ],
      "half_b_orders": [
        {{"symbol": "ETH/USDT:USDT", "action": "SELL", "weight": 1.2, "leverage": 6, "tp_pct": 2.4, "sl_pct": 1.8, "reason": "【Half-B 強弱配對】..."}}
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


def calculate_safe_contracts(exchange, symbol, desired_margin_usdt, leverage, price):
    try:
        bal = exchange.fetch_balance()
        current_free = float(bal.get('USDT', {}).get('free', 0.0))
    except Exception:
        current_free = desired_margin_usdt

    safe_margin = min(desired_margin_usdt, current_free * 0.80)
    if safe_margin < 15.0 or price <= 0:
        return 0, 0.0

    market = exchange.market(symbol)
    contract_size = float(market.get('contractSize') or 1.0)
    notional_per_contract = contract_size * price
    if notional_per_contract <= 0:
        return 0, 0.0

    raw_contracts = int((safe_margin * leverage) // notional_per_contract)
    contracts = max(1, min(raw_contracts, MAX_CONTRACTS_PER_ORDER))

    required_margin = (contracts * notional_per_contract) / leverage
    if required_margin > current_free * 0.85:
        contracts = int((current_free * 0.80 * leverage) // notional_per_contract)

    return max(0, contracts), round(safe_margin, 2)


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
        print(f"   💰 [{tag_reason}] 成功市價平倉 {sym} ({side_upper}) 共 {int(close_contracts_qty)} 張合約落袋！", flush=True)
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

    try:
        exchange.set_leverage(leverage, sym, params={'mgnMode': 'cross'})
    except Exception:
        pass

    algo_params = [{
        'tpTriggerPx': str(tp_price), 'tpOrdPx': '-1',
        'slTriggerPx': str(sl_price), 'slOrdPx': '-1'
    }]

    try:
        order = exchange.create_order(
            symbol=sym, type='market', side=side, amount=contracts,
            params={'tdMode': 'cross', 'posSide': pos_side, 'attachAlgoOrds': algo_params}
        )
        print(f"      🎉 [{tag_label} 下單成功] {sym} {pos_side.upper()} {contracts}張 | 止盈:${tp_price} 止損:${sl_price}", flush=True)
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
        tp_pct    = float(pos.get('tp_pct', 2.2))
        pos_key   = f"{sym}_{side}"
        cur_stage = int(POSITION_STAGE_MAP.get(pos_key, 0))

        if entry_px <= 0 or contracts <= 0:
            continue

        m_info = market_data.get(sym, {})
        tp_px = round(entry_px * (1 + tp_pct / 100.0) if side == 'LONG' else entry_px * (1 - tp_pct / 100.0), 2)

        if roi_pct >= STAGE2_ROI_TRIGGER_PCT and cur_stage < 2:
            half_qty = max(1, contracts // 2)
            remain_qty = max(1, contracts - half_qty)
            lock_1pct_sl = round(entry_px * (1 + STAGE2_PRICE_LOCK_PCT / 100.0) if side == 'LONG' else entry_px * (1 - STAGE2_PRICE_LOCK_PCT / 100.0), 2)

            if contracts >= 2:
                close_partial_or_full_position(exchange, sym, side, half_qty, "ROI達+9%半倉落袋")
            update_position_tpsl_on_okx(exchange, sym, side, remain_qty, tp_px, lock_1pct_sl)

            POSITION_STAGE_MAP[pos_key] = 2
            pos['sl_trigger_px'] = lock_1pct_sl
            pos['tp_trigger_px'] = tp_px
            pos['stage_badge']   = "💰 已平半倉落袋 & 止損鎖 +1%"
            pos['reason']        = f"💰【已落袋50%倉位】剩餘 {remain_qty} 張止損已鎖定於開倉價 +1% (${lock_1pct_sl})"
            state_changed = True

            send_discord_alert(
                title=f"💰 階段二半倉落袋 & +1%鎖利：{sym}",
                description=f"**{sym} ({side})** 含槓桿收益率達 **+{roi_pct:.2f}%**！\n已自動市價平倉 **50% ({half_qty}張)** 落袋為安，並將剩餘 **{remain_qty}張** 的止損單更新至 **${lock_1pct_sl:,.2f} (開倉價+1%)**！",
                color=3066993
            )

        elif roi_pct >= STAGE1_ROI_TRIGGER_PCT and cur_stage < 1:
            lock_be_sl = round(entry_px * (1 + STAGE1_PRICE_LOCK_PCT / 100.0) if side == 'LONG' else entry_px * (1 - STAGE1_PRICE_LOCK_PCT / 100.0), 2)
            update_position_tpsl_on_okx(exchange, sym, side, contracts, tp_px, lock_be_sl)
            POSITION_STAGE_MAP[pos_key] = 1
            pos['sl_trigger_px'] = lock_be_sl
            pos['tp_trigger_px'] = tp_px
            pos['stage_badge']   = "🛡️ 已鎖保本 (+0.25%)"
            pos['reason']        = f"🛡️【ROI滿5%已鎖保本】舊止損已撤除，新止損鎖定於 ${lock_be_sl} (保證不虧損)"
            state_changed = True

            send_discord_alert(
                title=f"🛡️ 階段一保本鎖利啟動：{sym}",
                description=f"**{sym} ({side})** 含槓桿收益率達 **+{roi_pct:.2f}%**！\n已撤銷原初始止損單，並將 OKX 止損價同步上移至 **${lock_be_sl:,.2f} (開倉價 +0.25%)**！",
                color=5763719
            )

        elif roi_pct >= EARLY_HARVEST_ROI_PCT:
            is_weak = m_info.get("momentum_weakening_long") if side == "LONG" else m_info.get("momentum_weakening_short")
            if is_weak:
                cancel_existing_algo_orders_for_symbol(exchange, sym)
                close_partial_or_full_position(exchange, sym, side, contracts, "動能轉弱提前收割")
                POSITION_STAGE_MAP.pop(pos_key, None)
                state_changed = True

    if total_equity > 0 and active_positions:
        portfolio_drawdown_pct = (total_upl / total_equity) * 100.0
        worst_pos = min(active_positions, key=lambda x: x.get('unrealized_pnl', 0.0))
        worst_sym = worst_pos['symbol']
        worst_roi = float(worst_pos.get('roi_pct', 0.0))

        if (portfolio_drawdown_pct <= -4.0 or worst_roi <= -16.0) and worst_sym not in HEDGE_ACTIVE_SYMS:
            hedge_budget = round(total_equity * POOL_HEDGE_RATIO, 2)
            hedge_action = 'SELL' if worst_pos['side'] == 'LONG' else 'BUY'
            cur_px = float(market_data.get(worst_sym, {}).get('last_price', 0.0))
            hedge_lev = 7
            contracts, actual_margin = calculate_safe_contracts(exchange, worst_sym, hedge_budget, hedge_lev, cur_px)
            if contracts > 0:
                tp_px = round(cur_px * (1 + 0.018) if hedge_action == 'BUY' else cur_px * (1 - 0.018), 2)
                sl_px = round(cur_px * (1 - 0.012) if hedge_action == 'BUY' else cur_px * (1 + 0.012), 2)
                order = place_okx_order_with_tpsl(exchange, worst_sym, hedge_action, contracts, hedge_lev, tp_px, sl_px, "10% 緊急對沖池")
                if order:
                    HEDGE_ACTIVE_SYMS.add(worst_sym)
                    state_changed = True

    return state_changed


def execute_pool_orders(exchange, pool_name, orders_list, pool_budget_usdt, market_data, reason_lookup, held_symbols_map):
    valid_orders = [o for o in orders_list[:MAX_ORDERS_PER_HALF] if str(o.get("action", "")).upper() in ["BUY", "SELL"]]
    if not valid_orders or pool_budget_usdt < 30:
        return False

    any_placed = False
    total_weight = sum(float(o.get("weight", 1.0)) for o in valid_orders)

    for idx, ord_spec in enumerate(valid_orders, start=1):
        sym = ord_spec.get("symbol", "ETH/USDT:USDT")
        if sym not in WHITELIST_SYMBOLS:
            sym = "ETH/USDT:USDT"

        m_info      = market_data.get(sym, {})
        anchored_tp = float(m_info.get("anchored_tp_pct", 2.2))
        anchored_sl = float(m_info.get("anchored_sl_pct", 1.6))

        action   = str(ord_spec.get("action", "BUY")).upper()
        weight   = max(0.5, min(3.0, float(ord_spec.get("weight", 1.0))))
        leverage = max(3, min(8, int(ord_spec.get("leverage", 6))))

        raw_tp = float(ord_spec.get("tp_pct", anchored_tp))
        raw_sl = float(ord_spec.get("sl_pct", anchored_sl))
        tp_pct = round(max(1.4, min(3.5, min(raw_tp, anchored_tp * 1.2))), 2)
        sl_pct = round(max(1.2, min(2.8, min(raw_sl, anchored_sl * 1.2))), 2)
        reason = ord_spec.get("reason", f"{pool_name} 波動定錨策略")

        reason_lookup[sym] = {
            "tp_pct": tp_pct, "sl_pct": sl_pct, "leverage": leverage,
            "pool": pool_name, "reason": reason
        }

        if sym in held_symbols_map:
            continue

        current_price = float(m_info.get("last_price", 0))
        if current_price <= 0:
            continue

        desired_margin = round(pool_budget_usdt * (weight / total_weight), 2) if total_weight > 0 else round(pool_budget_usdt / len(valid_orders), 2)
        contracts, safe_margin = calculate_safe_contracts(exchange, sym, desired_margin, leverage, current_price)
        if contracts <= 0:
            continue

        side = 'buy' if action == 'BUY' else 'sell'
        tp_price = round(current_price * (1 + tp_pct / 100.0) if side == 'buy' else current_price * (1 - tp_pct / 100.0), 2)
        sl_price = round(current_price * (1 - sl_pct / 100.0) if side == 'buy' else current_price * (1 + sl_pct / 100.0), 2)

        order = place_okx_order_with_tpsl(exchange, sym, action, contracts, leverage, tp_price, sl_price, pool_name)
        if order:
            held_symbols_map[sym] = 'LONG' if side == 'buy' else 'SHORT'
            any_placed = True

    return any_placed


def fetch_okx_positions_history_and_analytics(exchange, market_data, reason_lookup, ai_summary, total_equity):
    """
    🌟 關鍵修正：
    1. 全面採用台灣時間 (UTC+8) 解析開倉與平倉時間。
    2. 嚴格過濾：只保留開倉/結算時間 >= 2026-11-05 17:01:00 (BTC 4.87張那筆單) 的紀錄！
       自動剃除 10/29、11/04 等舊資金時期的測試單，確保累計盈虧與勝率 100% 精準！
    3. Discord 通知增加 20 分鐘時效鎖，絕不重發歷史舊單通知！
    """
    history_records = []
    seen_pos_ids = set()

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

        # 🌟 鐵律過濾：若該筆倉位的開倉時間或結算時間早於 2026-11-05 17:01:00 (台灣時間)，直接跳過不計入！
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
        lev          = info.get('lever') or item.get('leverage') or 6

        realized_pnl  = float(info.get('realizedPnl') or info.get('pnl') or item.get('realizedPnl') or 0.0)
        pnl_ratio_raw = float(info.get('pnlRatio') or item.get('percentage') or 0.0)
        pnl_pct       = round(pnl_ratio_raw * 100, 2) if abs(pnl_ratio_raw) < 5 else round(pnl_ratio_raw, 2)
        fee_val       = abs(float(info.get('fee') or 0.0))

        dt_obj   = datetime.fromtimestamp(u_ts_ms / 1000.0, tz=TW_TZ)
        dt_clean = dt_obj.strftime("%Y-%m-%d %H:%M:%S")
        day_key  = dt_obj.strftime("%Y-%m-%d")

        all_time_realized_pnl += realized_pnl
        if realized_pnl > 0:
            win_count += 1
        elif realized_pnl < 0:
            loss_count += 1

        if day_key == today_str:
            today_realized_pnl += realized_pnl
            today_trades_count += 1

        daily_pnl_map[day_key] = round(daily_pnl_map.get(day_key, 0.0) + realized_pnl, 2)
        if sym in asset_win_map:
            asset_win_map[sym]["total"] += 1
            asset_win_map[sym]["pnl"] = round(asset_win_map[sym]["pnl"] + realized_pnl, 2)
            if realized_pnl > 0:
                asset_win_map[sym]["wins"] += 1

        r_info   = reason_lookup.get(sym, {})
        pnl_sign = "+" if realized_pnl >= 0 else ""
        raw_audit = f"{pos_id}_{sym}_{open_avg_px}_{close_avg_px}_{realized_pnl}_{dt_clean}"
        sha_hash  = "0x" + hashlib.sha256(raw_audit.encode('utf-8')).hexdigest()[:16]

        history_records.append({
            "id": f"POS-{pos_id[-6:]}" if len(pos_id) >= 6 else f"POS-{u_ts_ms % 1000000}",
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
            "fee": f"倉位累計手續費 {fee_val:.4f} USDT",
            "rr": f"開倉 ${open_avg_px:,.2f} / 平倉 ${close_avg_px:,.2f}",
            "reason": r_info.get('reason', ai_summary or 'AI 波動率定錨與分批止盈結算紀錄'),
            "hash": sha_hash
        })

        # 🔔 只有當該筆平倉是發生在最近 20 分鐘內 (1,200,000 ms) 的新結算單，才發送 Discord 戰報！
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
    total_settled = win_count + loss_count
    win_rate_pct  = round((win_count / total_settled) * 100, 1) if total_settled > 0 else 0.0

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
        "total_settled_count": total_settled,
        "win_count": win_count,
        "loss_count": loss_count,
        "win_rate_pct": win_rate_pct,
        "daily_pnl_history": daily_series,
        "asset_performance": asset_win_map
    }

    return history_records, performance_stats


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
    prelim_history, perf_stats = fetch_okx_positions_history_and_analytics(exchange, market_data, {}, "", real_usdt)

    mistake_memory = [
        {"asset": h["asset"], "direction": h["direction"], "roi": h["roi"]}
        for h in prelim_history[:5]
    ]

    should_call_ai = (
        LAST_AI_PLAN_CACHE is None
        or len(held_symbols_map) == 0
        or (cycle_num % SLOW_AI_EVERY_N_TICKS == 0)
    )

    if should_call_ai:
        news_list = fetch_live_crypto_news()
        LAST_AI_PLAN_CACHE = ask_gemini_quant_brain(
            market_data, news_list, held_symbols_map, mistake_memory, perf_stats.get("asset_performance", {})
        )
        print(f"🧠 [慢軌 AI 廣播更新]: {LAST_AI_PLAN_CACHE.get('overall_summary')}", flush=True)
    else:
        news_list = ["快軌 20 秒級別高速監控中 (自動執行 ROI +5% 保本 / +9% 半倉落袋與 +1% 鎖利)"]
        print(f"⚡ [快軌盯盤] 總資產: ${real_usdt:,.2f} | 今日已實現: ${perf_stats['today_realized_pnl']:+,.2f} | 本期累計(11/05起): ${perf_stats['all_time_realized_pnl']:+,.2f}", flush=True)

    ai_plan = LAST_AI_PLAN_CACHE or build_fallback_plan(market_data)

    used_margin_now = max(0.0, real_usdt - free_usdt)
    normal_pool_target = real_usdt * POOL_NORMAL_RATIO
    remaining_normal_budget = max(0.0, min(free_usdt * 0.78, normal_pool_target - used_margin_now))

    half_a_budget = round(remaining_normal_budget / 2.0, 2)
    half_b_budget = round(remaining_normal_budget / 2.0, 2)

    reason_lookup = {}
    for o in (ai_plan.get("half_a_orders", []) + ai_plan.get("half_b_orders", [])):
        s_sym = o.get("symbol")
        if s_sym:
            m_info = market_data.get(s_sym, {})
            anch_tp = float(m_info.get("anchored_tp_pct", 2.2))
            anch_sl = float(m_info.get("anchored_sl_pct", 1.6))
            reason_lookup[s_sym] = {
                "tp_pct": round(min(3.5, float(o.get("tp_pct", anch_tp))), 2),
                "sl_pct": round(min(2.8, float(o.get("sl_pct", anch_sl))), 2),
                "leverage": o.get("leverage", 6),
                "pool": "80% 常態雙核心池",
                "reason": o.get("reason", "")
            }

    order_placed = False
    if remaining_normal_budget >= 80 and len(held_symbols_map) < 3:
        p1 = execute_pool_orders(exchange, "Half-A 波動定錨池 (40%)", ai_plan.get("half_a_orders", []), half_a_budget, market_data, reason_lookup, held_symbols_map)
        p2 = execute_pool_orders(exchange, "Half-B 籌碼動能池 (40%)", ai_plan.get("half_b_orders", []), half_b_budget, market_data, reason_lookup, held_symbols_map)
        order_placed = p1 or p2

    balance   = exchange.fetch_balance()
    real_usdt = round(float(balance.get('USDT', {}).get('total', 0.0)), 2)
    free_usdt = round(float(balance.get('USDT', {}).get('free', 0.0)), 2)
    used_usdt = round(real_usdt - free_usdt, 2)

    active_positions = []
    total_upl        = 0.0

    try:
        positions = exchange.fetch_positions(WHITELIST_SYMBOLS)
        for pos in positions:
            contracts = float(pos.get('contracts') or 0)
            if contracts > 0:
                sym          = pos.get('symbol')
                side_u       = pos.get('side', 'long').upper()
                upl          = round(float(pos.get('unrealizedPnl') or 0.0), 2)
                roi_pct      = round(float(pos.get('percentage') or 0.0), 2)
                margin_used  = round(float(pos.get('initialMargin') or pos.get('collateral') or 0.0), 2)
                notional_val = round(float(pos.get('notional') or 0.0), 2)
                entry_px     = round(float(pos.get('entryPrice') or 0.0), 2)
                total_upl   += upl

                r_info   = reason_lookup.get(sym, {})
                m_info   = market_data.get(sym, {})
                tp_p_val = float(r_info.get("tp_pct") or m_info.get("anchored_tp_pct", 2.2))
                sl_p_val = float(r_info.get("sl_pct") or m_info.get("anchored_sl_pct", 1.6))

                pos_key   = f"{sym}_{side_u}"
                cur_stage = int(POSITION_STAGE_MAP.get(pos_key, 0))

                if cur_stage == 2:
                    sl_trig = round(entry_px * (1 + STAGE2_PRICE_LOCK_PCT / 100.0) if side_u == 'LONG' else entry_px * (1 - STAGE2_PRICE_LOCK_PCT / 100.0), 2)
                    stage_badge = "💰 已平半倉 & 止損鎖 +1%"
                elif cur_stage == 1:
                    sl_trig = round(entry_px * (1 + STAGE1_PRICE_LOCK_PCT / 100.0) if side_u == 'LONG' else entry_px * (1 - STAGE1_PRICE_LOCK_PCT / 100.0), 2)
                    stage_badge = "🛡️ 已鎖保本 (+0.25%)"
                else:
                    sl_trig = round(entry_px * (1 - sl_p_val / 100.0) if side_u == 'LONG' else entry_px * (1 + sl_p_val / 100.0), 2)
                    stage_badge = "⚡ 監控中 (滿5%保本/滿9%平半倉)"

                tp_trig = round(entry_px * (1 + tp_p_val / 100.0) if side_u == 'LONG' else entry_px * (1 - tp_p_val / 100.0), 2)

                pos_obj = {
                    "symbol": sym,
                    "side": side_u,
                    "contracts": contracts,
                    "leverage": f"{pos.get('leverage', 6)}x",
                    "entry_price": entry_px,
                    "mark_price": round(float(pos.get('markPrice') or m_info.get('last_price', 0)), 2),
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
                    "reason": r_info.get("reason", f"波動率定錨止盈 +{tp_p_val}% | ROI滿5%自動移止損保本、滿9%平半倉鎖+1%")
                }
                active_positions.append(pos_obj)
    except Exception as e:
        print(f"⚠️ 讀取當前倉位提示: {e}", flush=True)

    fast_changed = fast_track_smart_harvest_and_hedge(exchange, active_positions, market_data, real_usdt, total_upl)

    positions_history_formatted, final_perf_stats = fetch_okx_positions_history_and_analytics(
        exchange, market_data, reason_lookup, ai_plan.get('overall_summary', ''), real_usdt
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
        "position_stage_map": POSITION_STAGE_MAP,
        "total_unrealized_pnl": round(total_upl, 2),
        "total_crypto_roi_pct": round((total_upl / real_usdt) * 100, 2) if real_usdt > 0 else 0.0,
        "active_positions": active_positions,
        "market_prices": market_data,
        "latest_news": news_list,
        "ai_broadcast_signals": {
            "updated_at": datetime.now(TW_TZ).strftime("%H:%M:%S"),
            "confidence": ai_plan.get("overall_confidence", 88),
            "summary": ai_plan.get("overall_summary", ""),
            "half_a_orders": ai_plan.get("half_a_orders", []),
            "half_b_orders": ai_plan.get("half_b_orders", [])
        },
        "ai_last_decision": {
            "action": f"今日已實現 ${final_perf_stats['today_realized_pnl']:+,.2f} | 本期累計(11/05 17:01起) ${final_perf_stats['all_time_realized_pnl']:+,.2f} (勝率 {final_perf_stats['win_rate_pct']}%)",
            "confidence": ai_plan.get("overall_confidence", 88),
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
    print("🚀 QuantGuard AI v10.1 - 11/05 新資金基準鎖定 × 三大獨立模式量化平台", flush=True)
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
