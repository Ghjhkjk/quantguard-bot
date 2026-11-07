import os
import time
import ccxt
import feedparser
import hashlib
import json
import urllib.request
from datetime import datetime, timezone

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

# ==========================================================
# 👇 1. 金鑰與環境變數設定 (自動從 GitHub Secrets 讀取)
# ==========================================================
OKX_API_KEY         = os.environ.get("OKX_API_KEY", "請填入你的_OKX_API_Key")
OKX_SECRET_KEY      = os.environ.get("OKX_SECRET_KEY", "請填入你的_OKX_SECRET_KEY")
OKX_PASSPHRASE      = os.environ.get("OKX_PASSPHRASE", "請填入你的_OKX_Passphrase")
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY", "請填入你的_Gemini_API_Key")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

# ==========================================================
# ⚙️ 2. 三層資金池 (80/10/10) 與快慢雙軌運行參數
# ==========================================================
WHITELIST_SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']

POOL_NORMAL_RATIO = 0.80  # 80% 常態雙核心池 (Half-A 40% + Half-B 40%)
POOL_HEDGE_RATIO  = 0.10  # 10% 緊急急跌對沖池
POOL_SNIPER_RATIO = 0.10  # 10% 黑天鵝突發狙擊池

MAX_ORDERS_PER_HALF     = 3
MAX_CONTRACTS_PER_ORDER = 1200

# 移動止損參數：當幣價順勢獲利達 +2.2% 時，將止損移至開倉價 +1.0% 鎖利
TRAILING_ACTIVATE_PCT = 2.2
TRAILING_LOCK_PCT     = 1.0

# 執行模式控制 (解決 NameError: RUN_CONTINUOUS_LOOP 問題)
IS_GITHUB_ACTIONS     = os.environ.get("GITHUB_ACTIONS") == "true"
RUN_CONTINUOUS_LOOP   = True  # 啟用長駐循環，在 GitHub Actions 內進行 20 秒級別巡檢
FAST_TRACK_INTERVAL   = 20    # 快軌：每 20 秒巡檢一次 OKX 倉位、+1% 移止損與對沖 (不耗 Gemini 額度)
SLOW_AI_EVERY_N_TICKS = 45    # 慢軌：每 45 個快軌 (約 15 分鐘) 或空倉時才呼叫一次 Gemini AI
MAX_JOB_RUNTIME_SEC   = int(os.environ.get("MAX_JOB_RUNTIME_SEC", "270"))  # 單次排程內循環秒數

# 狀態快取 (避免重複發送 Discord 通知或重複掛單)
LAST_AI_PLAN_CACHE = None
NOTIFIED_POS_IDS   = set()
LOCKED_PROFIT_SYMS = set()
HEDGE_ACTIVE_SYMS  = set()


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
    exchange.set_sandbox_mode(True)  # OKX 模擬盤環境
    exchange.load_markets()
    return exchange


def send_discord_alert(title, description, color=3066993, fields=None):
    """發送 Discord 即時戰報 (平倉結算、+1% 移止損、10% 緊急對沖觸發)"""
    if not DISCORD_WEBHOOK_URL or not DISCORD_WEBHOOK_URL.startswith("http"):
        return
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "QuantGuard AI v9.0 | 80/10/10 快慢雙軌量化系統"}
    }
    if fields:
        embed["fields"] = fields
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=json.dumps({"embeds": [embed]}).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'User-Agent': 'QuantGuardBot/9.0'}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"⚠️ Discord 推播異常: {e}")


def load_previous_state():
    """從現有 live_data.json 載入已通知過的歷史倉位 ID，防止重複推播"""
    global NOTIFIED_POS_IDS
    if not os.path.exists("live_data.json"):
        return
    try:
        with open("live_data.json", "r", encoding="utf-8") as f:
            old_data = json.load(f)
        for o in old_data.get("orders", []):
            fid = str(o.get("full_id") or o.get("id") or "")
            if fid:
                NOTIFIED_POS_IDS.add(fid)
    except Exception:
        pass


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
    """抓取 K 線、均線、振幅、5分K急跌指標與合約籌碼面 (資金費率 & 未平倉量 OI)"""
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
            ma20 = sum(closes) / len(closes) if closes else current_price
            tr_list   = [(highs[i] - lows[i]) / closes[i] * 100 for i in range(len(closes))]
            atr_pct   = sum(tr_list) / len(tr_list) if tr_list else 1.0
            swing_pct = ((max(highs) - min(lows)) / current_price) * 100 if highs else 3.0
            change_24h = float(ticker.get('percentage') or 0.0)

            # 計算最近一根 15m K 線變化與成交量爆量倍數 (供 10% 對沖與 10% 黑天鵝池判斷)
            bar_change_pct = ((closes[-1] - closes[-2]) / closes[-2] * 100) if len(closes) >= 2 else 0.0
            avg_vol = (sum(vols[:-1]) / max(1, len(vols) - 1)) if len(vols) > 1 else 1.0
            vol_spike_ratio = round(vols[-1] / avg_vol, 2) if avg_vol > 0 else 1.0

            # 籌碼面 1：資金費率 (Funding Rate)
            funding_rate_pct = 0.01
            try:
                fr = exchange.fetch_funding_rate(sym)
                funding_rate_pct = round(float(fr.get('fundingRate') or 0.0001) * 100, 4)
            except Exception:
                pass

            # 籌碼面 2：未平倉合約量 (Open Interest)
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
                "ma20": round(ma20, 2),
                "atr_15m_pct": round(atr_pct, 2),
                "recent_swing_pct": round(swing_pct, 2),
                "trend": "BULLISH" if ma5 > ma20 else "BEARISH"
            }
        except Exception as e:
            print(f"⚠️ 抓取 {sym} 行情異常: {e}")
    return market_summary


def build_fallback_plan(market_data):
    """當 Gemini API 額度達上限或網路瞬斷時的本地端量化備援決策"""
    half_a, half_b = [], []
    for sym in WHITELIST_SYMBOLS:
        info = market_data.get(sym, {})
        act = "BUY" if info.get("trend") == "BULLISH" else "SELL"
        swing = float(info.get("recent_swing_pct", 3.5))
        sl_v = round(max(2.8, min(5.0, swing * 1.05)), 2)
        tp_v = round(sl_v * 2.6, 2)
        half_a.append({
            "symbol": sym, "action": act, "weight": 1.2, "leverage": 5,
            "tp_pct": tp_v, "sl_pct": sl_v,
            "reason": f"【Half-A 技術順勢】{sym.split('/')[0]} 均線呈 {info.get('trend')}，費率 {info.get('funding_rate_pct')}%。"
        })
        half_b.append({
            "symbol": sym, "action": act, "weight": 1.1, "leverage": 5,
            "tp_pct": round(tp_v * 1.15, 2), "sl_pct": sl_v,
            "reason": f"【Half-B 籌碼動能】24h波動 {info.get('change_24h_pct')}%，量能比 {info.get('vol_spike_ratio')}x。"
        })
    return {
        "overall_confidence": 82,
        "overall_summary": "80/10/10 三資金池穩健運行，快軌每 20 秒監控 +1% 移止損與急跌對沖。",
        "black_swan_detected": False,
        "sniper_order": None,
        "half_a_orders": half_a[:2],
        "half_b_orders": half_b[:2]
    }


def ask_gemini_quant_brain(market_data, news_headlines, current_held_symbols, mistake_memory):
    """
    🧠 慢軌 AI 策略大腦 (含模型自動降級防爆 Key + 歷史錯題本 + 籌碼面 + 黑天鵝判定)
    """
    if not GENAI_AVAILABLE:
        return build_fallback_plan(market_data)

    gemini_key = clean_credential(GEMINI_API_KEY, "GEMINI_API_KEY")
    client = genai.Client(api_key=gemini_key)

    prompt = f"""
    你是 QuantGuard AI v9.0 旗艦量化決策核心。請根據即時行情、合約籌碼、國際新聞與【歷史錯題本】規劃訂單：
    【行情與籌碼 (含資金費率 funding_rate_pct 與量能倍數 vol_spike_ratio)】：{json.dumps(market_data, ensure_ascii=False)}
    【最新國際新聞】：{json.dumps(news_headlines, ensure_ascii=False)}
    【目前持有倉位】：{json.dumps(current_held_symbols, ensure_ascii=False)}
    【歷史平倉錯題本 (最近5筆結算)】：{json.dumps(mistake_memory[:5], ensure_ascii=False)}

    【核心戰略規則】：
    1. 防共伴齊跌與錯題修正：
       - 檢視【歷史平倉錯題本】，若某幣種近期因追高或殺低剛發生虧損，請降低槓桿或改採強弱勢配對（勿三幣無腦同方向重倉）。
       - 參考 `funding_rate_pct`，若資金費率極端偏高 (>0.03%) 避免過度追多；若極端偏負 (<-0.03%) 提防軋空。
    2. 80% 常態雙核心池：
       - `half_a_orders` (40% 技術與籌碼順勢池)：輸出 1~2 張最具把握的訂單。
       - `half_b_orders` (40% 新聞與強弱動能池)：輸出 1~2 張訂單。
    3. 10% 黑天鵝突發狙擊池 (`black_swan_detected` & `sniper_order`)：
       - 僅當新聞出現重大突發事件，或某幣種 `vol_spike_ratio` >= 2.2 / `recent_swing_pct` >= 4.5% 時，才將 `black_swan_detected` 設為 true 並提供 `sniper_order`；否則設為 false。

    請嚴格只輸出 JSON 格式：
    {{
      "overall_confidence": 85,
      "overall_summary": "繁體中文50字內策略與籌碼總結",
      "black_swan_detected": false,
      "sniper_order": {{"symbol": "BTC/USDT:USDT", "action": "BUY", "leverage": 8, "tp_pct": 18.0, "sl_pct": 3.5, "reason": "【10% 黑天鵝狙擊】突發巨量突破"}},
      "half_a_orders": [
        {{"symbol": "BTC/USDT:USDT", "action": "BUY", "weight": 1.5, "leverage": 5, "tp_pct": 12.0, "sl_pct": 3.5, "reason": "【Half-A 技術籌碼】..."}}
      ],
      "half_b_orders": [
        {{"symbol": "ETH/USDT:USDT", "action": "SELL", "weight": 1.2, "leverage": 5, "tp_pct": 12.0, "sl_pct": 3.5, "reason": "【Half-B 強弱對沖】..."}}
      ]
    }}
    """

    # 雙模型防爆保護：優先使用 2.5-flash，若遇 429 限流自動降級至 flash-lite
    for model_name in ["gemini-2.5-flash", "gemini-2.5-flash-lite"]:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2)
            )
            return json.loads(response.text)
        except Exception as e:
            print(f"⚠️ 模型 {model_name} 呼叫提示 ({e})，嘗試切換備援...")
            continue

    return build_fallback_plan(market_data)


def calculate_safe_contracts(exchange, symbol, desired_margin_usdt, leverage, price):
    """
    🌟 防 51008 保證金不足核心函數：
    每次計算張數前，即時向 OKX 查詢最新可用餘額 (free_usdt)，並強制控制在當下可用餘額的 80% 內！
    """
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

    # 二次驗算所需保證金是否超過可用餘額
    required_margin = (contracts * notional_per_contract) / leverage
    if required_margin > current_free * 0.85:
        contracts = int((current_free * 0.80 * leverage) // notional_per_contract)

    return max(0, contracts), round(safe_margin, 2)


def place_okx_order_with_tpsl(exchange, sym, action, contracts, leverage, tp_price, sl_price, tag_label):
    """統一下單封裝 (支援雙向持倉與單向持倉自動切換)"""
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
        print(f"      🎉 [{tag_label} 下單成功] {sym} {pos_side.upper()} {contracts}張 | ID: {order.get('id')}")
        return order
    except Exception as first_err:
        if "51000" in str(first_err) or "posSide" in str(first_err):
            try:
                order = exchange.create_order(
                    symbol=sym, type='market', side=side, amount=contracts,
                    params={'tdMode': 'cross', 'attachAlgoOrds': algo_params}
                )
                print(f"      🎉 [{tag_label} 單向模式成功] {sym} {contracts}張 | ID: {order.get('id')}")
                return order
            except Exception as second_err:
                print(f"      ⚠️ [{tag_label}] 下單異常: {second_err}")
        else:
            print(f"      ⚠️ [{tag_label}] 下單異常: {first_err}")
    return None


def fast_track_trailing_stop_and_hedge(exchange, active_positions, market_data, total_equity, total_upl):
    """
    ⚡ 快軌核心 (每 20 秒執行)：
    1. 檢查持倉幣價獲利是否達 +2.2% ➔ 自動將止損推進至【開倉均價 +1.0%】保本鎖利！
    2. 檢查是否發生急跌/嚴重逆勢虧損 ➔ 自動動用【10% 緊急對沖池】開立反向對沖單鎖倉！
    """
    # --- 功能 A：+1% 自動保本鎖利 (Trailing Stop to +1%) ---
    for pos in active_positions:
        sym        = pos['symbol']
        side       = pos['side']  # 'LONG' or 'SHORT'
        entry_px   = float(pos['entry_price'])
        mark_px    = float(pos['mark_price'])
        contracts  = int(float(pos['contracts']))
        pos_key    = f"{sym}_{side}_{entry_px}"

        if entry_px <= 0 or contracts <= 0 or pos_key in LOCKED_PROFIT_SYMS:
            continue

        price_gain_pct = ((mark_px - entry_px) / entry_px * 100.0) if side == 'LONG' else ((entry_px - mark_px) / entry_px * 100.0)

        if price_gain_pct >= TRAILING_ACTIVATE_PCT:
            # 計算開倉價 +1% 的保本鎖利止損價
            new_sl_px = round(entry_px * (1 + TRAILING_LOCK_PCT / 100.0), 2) if side == 'LONG' else round(entry_px * (1 - TRAILING_LOCK_PCT / 100.0), 2)
            close_side = 'sell' if side == 'LONG' else 'buy'
            pos_side   = 'long' if side == 'LONG' else 'short'

            print(f"🛡️ [+1% 自動移止損觸發] {sym} ({side}) 目前幣價獲利 +{price_gain_pct:.2f}%，將止損上移至開倉價 +1% (${new_sl_px})！")
            try:
                exchange.privatePostTradeOrderAlgo({
                    'instId': sym.replace('/USDT:USDT', '-USDT-SWAP'),
                    'tdMode': 'cross',
                    'side': close_side,
                    'posSide': pos_side,
                    'ordType': 'conditional',
                    'sz': str(contracts),
                    'slTriggerPx': str(new_sl_px),
                    'slOrdPx': '-1',
                    'reduceOnly': True
                })
            except Exception as e:
                print(f"ℹ️ 附加 +1% 保本條件單提示: {e}")

            LOCKED_PROFIT_SYMS.add(pos_key)
            pos['reason'] = f"🛡️【已啟動 +1% 鎖利】止損已移至 ${new_sl_px} (保底鎖住 +1% 幣價利潤)"
            send_discord_alert(
                title=f"🛡️ 已啟動 +1% 自動移止損鎖利：{sym}",
                description=f"**{sym} ({side})** 幣價順勢獲利已達 **+{price_gain_pct:.2f}%**！\n系統已自動將止損推進至開倉均價 **+{TRAILING_LOCK_PCT}% (${new_sl_px:,.2f})**，確保本單立於不敗之地。",
                color=5763719
            )

    # --- 功能 B：10% 緊急對沖防禦池 (偵測急跌或嚴重逆勢自動對沖) ---
    if total_equity <= 0 or not active_positions:
        return

    portfolio_drawdown_pct = (total_upl / total_equity) * 100.0
    btc_bar_drop = float(market_data.get('BTC/USDT:USDT', {}).get('bar_15m_change_pct', 0.0))

    # 若整體未實現虧損超過總資產 -3.5%，或單一持倉逆勢暴跌超過 -2.2%
    worst_pos = min(active_positions, key=lambda x: x.get('unrealized_pnl', 0.0))
    worst_sym = worst_pos['symbol']
    worst_upl = float(worst_pos.get('unrealized_pnl', 0.0))
    worst_margin = max(1.0, float(worst_pos.get('margin_usdt', 1.0)))
    worst_price_move = (worst_upl / float(worst_pos.get('notional_usdt') or worst_margin)) * 100.0

    need_emergency_hedge = (portfolio_drawdown_pct <= -3.5) or (worst_price_move <= -2.2) or (abs(btc_bar_drop) >= 2.5 and worst_upl < 0)

    if need_emergency_hedge and worst_sym not in HEDGE_ACTIVE_SYMS:
        hedge_budget = round(total_equity * POOL_HEDGE_RATIO, 2)
        hedge_action = 'SELL' if worst_pos['side'] == 'LONG' else 'BUY'
        cur_px = float(market_data.get(worst_sym, {}).get('last_price', 0.0))
        hedge_lev = 8

        contracts, actual_margin = calculate_safe_contracts(exchange, worst_sym, hedge_budget, hedge_lev, cur_px)
        if contracts > 0:
            tp_px = round(cur_px * (1 + 0.06) if hedge_action == 'BUY' else cur_px * (1 - 0.06), 2)
            sl_px = round(cur_px * (1 - 0.025) if hedge_action == 'BUY' else cur_px * (1 + 0.025), 2)
            print(f"🚨 [10% 緊急對沖啟動] 偵測到逆勢急跌 (浮虧 {portfolio_drawdown_pct:.2f}%)！立即動用對沖池 ${actual_margin} 開立 {worst_sym} 反向 {hedge_action} 單！")
            order = place_okx_order_with_tpsl(exchange, worst_sym, hedge_action, contracts, hedge_lev, tp_px, sl_px, "10% 緊急對沖池")
            if order:
                HEDGE_ACTIVE_SYMS.add(worst_sym)
                send_discord_alert(
                    title=f"🚨 10% 緊急對沖防禦啟動：{worst_sym}",
                    description=f"偵測到帳面逆勢波動 (**{portfolio_drawdown_pct:.2f}%**)，已自動動用 **10% 對沖池 (${actual_margin:,.2f} USDT)** 開立反向 **{hedge_action} ({hedge_lev}x, {contracts}張)** 保護常態倉位！",
                    color=15548997
                )


def execute_pool_orders(exchange, pool_name, orders_list, pool_budget_usdt, market_data, reason_lookup, held_symbols):
    """依預算分配執行下單 (嚴格檢查已持有倉位與即時可用餘額，防止重複加碼與 51008 錯誤)"""
    valid_orders = [o for o in orders_list[:MAX_ORDERS_PER_HALF] if str(o.get("action", "")).upper() in ["BUY", "SELL"]]
    if not valid_orders or pool_budget_usdt < 30:
        return

    total_weight = sum(float(o.get("weight", 1.0)) for o in valid_orders)
    for idx, ord_spec in enumerate(valid_orders, start=1):
        sym = ord_spec.get("symbol", "ETH/USDT:USDT")
        if sym not in WHITELIST_SYMBOLS:
            sym = "ETH/USDT:USDT"

        action   = str(ord_spec.get("action", "BUY")).upper()
        weight   = max(0.5, min(3.0, float(ord_spec.get("weight", 1.0))))
        leverage = max(2, min(10, int(ord_spec.get("leverage", 5))))
        sl_pct   = round(max(2.5, min(5.5, float(ord_spec.get("sl_pct", 3.5)))), 2)
        tp_pct   = round(max(sl_pct * 1.8, min(28.0, float(ord_spec.get("tp_pct", 11.0)))), 2)
        reason   = ord_spec.get("reason", f"{pool_name} 自動策略")

        reason_lookup[sym] = {
            "tp_pct": tp_pct, "sl_pct": sl_pct, "leverage": leverage,
            "pool": pool_name, "reason": reason
        }

        # 若該幣種已在當前倉位中，直接跳過不重複疊加，交由快軌監控
        if sym in held_symbols:
            continue

        current_price = float(market_data.get(sym, {}).get("last_price", 0))
        if current_price <= 0:
            continue

        desired_margin = round(pool_budget_usdt * (weight / total_weight), 2) if total_weight > 0 else round(pool_budget_usdt / len(valid_orders), 2)
        contracts, safe_margin = calculate_safe_contracts(exchange, sym, desired_margin, leverage, current_price)
        if contracts <= 0:
            continue

        side = 'buy' if action == 'BUY' else 'sell'
        tp_price = round(current_price * (1 + tp_pct / 100.0) if side == 'buy' else current_price * (1 - tp_pct / 100.0), 2)
        sl_price = round(current_price * (1 - sl_pct / 100.0) if side == 'buy' else current_price * (1 + sl_pct / 100.0), 2)

        print(f"   ⚡ [{pool_name} #{idx} | {sym}] 方向: {action} | 槓桿: {leverage}x | 保證金: ${safe_margin:,.2f} ({contracts}張)")
        order = place_okx_order_with_tpsl(exchange, sym, action, contracts, leverage, tp_price, sl_price, pool_name)
        if order:
            held_symbols.add(sym)


def fetch_okx_positions_history(exchange, market_data, reason_lookup, ai_summary, total_equity):
    """
    📦 從 OKX 『歷史倉位區 (Positions History)』抓取已平倉結算數據，
    若發現有新平倉結算的倉位，立即發送 Discord 結算戰報！
    """
    history_records = []
    seen_pos_ids = set()

    raw_pos_history = []
    try:
        if hasattr(exchange, 'fetch_positions_history'):
            raw_pos_history = exchange.fetch_positions_history(WHITELIST_SYMBOLS, limit=30)
        else:
            res = exchange.privateGetAccountPositionsHistory({'instType': 'SWAP', 'limit': '30'})
            raw_pos_history = res.get('data', [])
    except Exception:
        try:
            res = exchange.privateGetAccountPositionsHistory({'instType': 'SWAP', 'limit': '30'})
            raw_pos_history = res.get('data', [])
        except Exception:
            raw_pos_history = []

    for item in raw_pos_history:
        info = item.get('info', item) if isinstance(item, dict) else {}
        inst_id = info.get('instId', '')
        sym = item.get('symbol') or (inst_id.replace('-SWAP', '').replace('-', '/') + ':USDT' if inst_id else 'ETH/USDT:USDT')

        pos_id = str(info.get('posId') or item.get('id') or '')
        u_time = str(info.get('uTime') or info.get('cTime') or item.get('timestamp') or '')
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

        ts_ms    = int(u_time) if u_time.isdigit() else int(time.time() * 1000)
        dt_clean = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        r_info   = reason_lookup.get(sym, {})
        pnl_sign = "+" if realized_pnl >= 0 else ""

        raw_audit = f"{pos_id}_{sym}_{open_avg_px}_{close_avg_px}_{realized_pnl}_{dt_clean}"
        sha_hash  = "0x" + hashlib.sha256(raw_audit.encode('utf-8')).hexdigest()[:16]

        record_obj = {
            "id": f"POS-{pos_id[-6:]}" if len(pos_id) >= 6 else f"POS-{ts_ms % 1000000}",
            "full_id": unique_key,
            "timestamp_ms": ts_ms,
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
            "realized_pnl_num": realized_pnl,
            "fee": f"倉位累計手續費 {fee_val:.4f} USDT",
            "rr": f"開倉 ${open_avg_px:,.2f} / 平倉 ${close_avg_px:,.2f}",
            "reason": r_info.get('reason', ai_summary or 'OKX 歷史倉位區已平倉結算紀錄'),
            "hash": sha_hash
        }
        history_records.append(record_obj)

        # 🔔 若為全新平倉結算紀錄，發送 Discord 通知！
        if unique_key not in NOTIFIED_POS_IDS:
            NOTIFIED_POS_IDS.add(unique_key)
            is_win = realized_pnl >= 0
            send_discord_alert(
                title=f"{'🟢 獲利平倉結算' if is_win else '🔴 止損平倉結算'} | {sym}",
                description=f"**{side_label}** 已於 OKX 完成平倉結算！",
                color=3066993 if is_win else 15158332,
                fields=[
                    {"name": "💰 已實現損益 (Realized PnL)", "value": f"**{pnl_sign}${realized_pnl:,.2f} USDT ({pnl_sign}{pnl_pct:.2f}%)**", "inline": False},
                    {"name": "📊 開倉 ➔ 平倉均價", "value": f"`${open_avg_px:,.2f}` ➔ `${close_avg_px:,.2f}`", "inline": True},
                    {"name": "⚡ 槓桿 / 張數", "value": f"`{lev}x` / `{close_qty} 張`", "inline": True},
                    {"name": "🏦 當前 OKX 總資產", "value": f"`${total_equity:,.2f} USDT`", "inline": False}
                ]
            )

    history_records.sort(key=lambda x: x.get('timestamp_ms', 0), reverse=True)
    return history_records


def run_single_cycle(exchange, cycle_num=1):
    global LAST_AI_PLAN_CACHE
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n==================== [巡檢 #{cycle_num} | {now_str}] ====================")

    # 1. 讀取 OKX 實時總資產與「當前倉位 (Current Positions)」
    balance   = exchange.fetch_balance()
    real_usdt = round(float(balance.get('USDT', {}).get('total', 0.0)), 2)
    free_usdt = round(float(balance.get('USDT', {}).get('free', 0.0)), 2)

    held_symbols = set()
    try:
        cur_positions = exchange.fetch_positions(WHITELIST_SYMBOLS)
        for pos in cur_positions:
            if float(pos.get('contracts') or 0) > 0:
                held_symbols.add(pos.get('symbol'))
    except Exception as e:
        print(f"⚠️ 初始倉位讀取提示: {e}")

    market_data = fetch_whitelist_market_data(exchange)

    # 先抓一次歷史倉位作為「AI 歷史錯題本」與平倉推播檢測
    prelim_history = fetch_okx_positions_history(exchange, market_data, {}, "", real_usdt)
    mistake_memory = [
        {"asset": h["asset"], "direction": h["direction"], "roi": h["roi"]}
        for h in prelim_history[:5]
    ]

    # 2. 快慢雙軌判斷：只有第 1 次循環、或目前空倉、或每隔 SLOW_AI_EVERY_N_TICKS 次才呼叫 Gemini AI
    should_call_ai = (
        LAST_AI_PLAN_CACHE is None
        or len(held_symbols) == 0
        or (cycle_num % SLOW_AI_EVERY_N_TICKS == 0)
    )

    if should_call_ai:
        news_list = fetch_live_crypto_news()
        LAST_AI_PLAN_CACHE = ask_gemini_quant_brain(market_data, news_list, list(held_symbols), mistake_memory)
        print(f"🧠 [慢軌 AI 決策更新]: {LAST_AI_PLAN_CACHE.get('overall_summary')}")
    else:
        news_list = ["快軌 20 秒級別高速盯盤中 (節省 Gemini API 額度)"]
        print(f"⚡ [快軌高速盯盤] 總資產: ${real_usdt:,.2f} | 可用: ${free_usdt:,.2f} | 持倉數: {len(held_symbols)} 筆")

    ai_plan = LAST_AI_PLAN_CACHE or build_fallback_plan(market_data)

    # 3. 依照 80% / 10% / 10% 資金池規則配置預算
    # 總資產的 80% 為常態池上限，扣除目前已用保證金後計算可新開倉額度
    used_margin_now = max(0.0, real_usdt - free_usdt)
    normal_pool_target = real_usdt * POOL_NORMAL_RATIO
    remaining_normal_budget = max(0.0, min(free_usdt * 0.78, normal_pool_target - used_margin_now))

    half_a_budget = round(remaining_normal_budget / 2.0, 2)
    half_b_budget = round(remaining_normal_budget / 2.0, 2)

    reason_lookup = {}
    for o in (ai_plan.get("half_a_orders", []) + ai_plan.get("half_b_orders", [])):
        s_sym = o.get("symbol")
        if s_sym:
            reason_lookup[s_sym] = {
                "tp_pct": o.get("tp_pct", 12.0),
                "sl_pct": o.get("sl_pct", 3.5),
                "leverage": o.get("leverage", 5),
                "pool": "80% 常態雙核心池",
                "reason": o.get("reason", "")
            }

    # 若 80% 常態池還有空間且尚有未持倉標的，執行常態雙核心建倉
    if remaining_normal_budget >= 80 and len(held_symbols) < 3:
        execute_pool_orders(exchange, "Half-A 技術籌碼池 (40%)", ai_plan.get("half_a_orders", []), half_a_budget, market_data, reason_lookup, held_symbols)
        execute_pool_orders(exchange, "Half-B 新聞動能池 (40%)", ai_plan.get("half_b_orders", []), half_b_budget, market_data, reason_lookup, held_symbols)

    # 若 AI 偵測到黑天鵝或極端爆量突破，動用 10% 黑天鵝狙擊池
    if ai_plan.get("black_swan_detected") and isinstance(ai_plan.get("sniper_order"), dict):
        sniper_ord = ai_plan["sniper_order"]
        sniper_budget = round(real_usdt * POOL_SNIPER_RATIO, 2)
        execute_pool_orders(exchange, "10% 黑天鵝狙擊池", [sniper_ord], sniper_budget, market_data, reason_lookup, held_symbols)

    # 4. 重新抓取最新「當前倉位」與「即時資產」，執行快軌 +1% 移動止損與 10% 急跌對沖
    balance   = exchange.fetch_balance()
    real_usdt = round(float(balance.get('USDT', {}).get('total', 0.0)), 2)
    free_usdt = round(float(balance.get('USDT', {}).get('free', 0.0)), 2)
    used_usdt = round(real_usdt - free_usdt, 2)

    active_positions = []
    positions_map    = {}
    total_upl        = 0.0

    try:
        positions = exchange.fetch_positions(WHITELIST_SYMBOLS)
        for pos in positions:
            contracts = float(pos.get('contracts') or 0)
            if contracts > 0:
                sym          = pos.get('symbol')
                upl          = round(float(pos.get('unrealizedPnl') or 0.0), 2)
                roi_pct      = round(float(pos.get('percentage') or 0.0), 2)
                margin_used  = round(float(pos.get('initialMargin') or pos.get('collateral') or 0.0), 2)
                notional_val = round(float(pos.get('notional') or 0.0), 2)
                total_upl   += upl
                r_info       = reason_lookup.get(sym, {})

                pos_obj = {
                    "symbol": sym,
                    "side": pos.get('side', 'long').upper(),
                    "contracts": contracts,
                    "leverage": f"{pos.get('leverage', 5)}x",
                    "entry_price": round(float(pos.get('entryPrice') or 0.0), 2),
                    "mark_price": round(float(pos.get('markPrice') or market_data.get(sym, {}).get('last_price', 0)), 2),
                    "margin_usdt": margin_used,
                    "notional_usdt": notional_val,
                    "unrealized_pnl": upl,
                    "roi_pct": roi_pct,
                    "tp_pct": r_info.get("tp_pct", 12.0),
                    "sl_pct": r_info.get("sl_pct", 3.5),
                    "reason": r_info.get("reason", "80/10/10 資金池實時監控中 (達+2.2%自動移止損至+1%)")
                }
                active_positions.append(pos_obj)
                positions_map[sym] = pos_obj
    except Exception as e:
        print(f"⚠️ 讀取當前倉位提示: {e}")

    # ⚡ 執行快軌：+1% 移動止損 & 10% 急跌鎖倉對沖檢查
    fast_track_trailing_stop_and_hedge(exchange, active_positions, market_data, real_usdt, total_upl)

    # 5. 更新歷史倉位與寫出 live_data.json
    positions_history_formatted = fetch_okx_positions_history(
        exchange, market_data, reason_lookup, ai_plan.get('overall_summary', ''), real_usdt
    )

    live_payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "real_usdt": real_usdt,
        "free_usdt": free_usdt,
        "used_usdt": used_usdt,
        "allocations": {
            "normal_80_usdt": round(real_usdt * POOL_NORMAL_RATIO, 2),
            "hedge_10_usdt": round(real_usdt * POOL_HEDGE_RATIO, 2),
            "sniper_10_usdt": round(real_usdt * POOL_SNIPER_RATIO, 2)
        },
        "total_unrealized_pnl": round(total_upl, 2),
        "total_crypto_roi_pct": round((total_upl / real_usdt) * 100, 2) if real_usdt > 0 else 0.0,
        "active_positions": active_positions,
        "market_prices": market_data,
        "latest_news": news_list,
        "ai_last_decision": {
            "action": f"80/10/10 架構 | 當前持倉 {len(active_positions)} 筆 | 歷史結算 {len(positions_history_formatted)} 筆",
            "confidence": ai_plan.get("overall_confidence", 86),
            "reason": ai_plan.get("overall_summary", "")
        },
        "orders": positions_history_formatted
    }

    with open("live_data.json", "w", encoding="utf-8") as f:
        json.dump(live_payload, f, ensure_ascii=False, indent=2)

    print(f"📁 live_data.json 更新完成！總資產: ${real_usdt:,.2f} | 持倉: {len(active_positions)} 筆 (浮盈: ${total_upl:+,.2f}) | 歷史倉位: {len(positions_history_formatted)} 筆")


def main():
    print("=========================================================================")
    print("🚀 QuantGuard AI v9.0 - 80/10/10 三資金池 × +1% 移止損 × 快慢雙軌防爆引擎")
    print("=========================================================================")
    load_previous_state()
    exchange = init_okx_exchange()
    start_ts = time.time()
    cycle = 0

    while True:
        cycle += 1
        try:
            run_single_cycle(exchange, cycle)
        except Exception as e:
            print(f"❌ 巡檢發生異常: {e}")

        # 若在 GitHub Actions 內運行，持續每 20 秒快軌巡檢直到接近排程時間上限才安全退出交棒
        elapsed = time.time() - start_ts
        if IS_GITHUB_ACTIONS and elapsed >= MAX_JOB_RUNTIME_SEC:
            print(f"✅ 本輪雲端長駐巡檢已滿 {int(elapsed)} 秒，安全寫入 live_data.json 並交棒給下一輪排程！")
            break

        if not RUN_CONTINUOUS_LOOP:
            break

        time.sleep(FAST_TRACK_INTERVAL)


if __name__ == "__main__":
    main()
