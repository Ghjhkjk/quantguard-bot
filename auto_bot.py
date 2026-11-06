import os
import time
import ccxt
import feedparser
import hashlib
import json
from datetime import datetime, timezone

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

# ==========================================================
# 👇 1. 金鑰設定 (自動從 GitHub Secrets 讀取)
# ==========================================================
OKX_API_KEY    = os.environ.get("OKX_API_KEY", "請填入你的_OKX_API_Key")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "請填入你的_OKX_SECRET_KEY")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "請填入你的_OKX_Passphrase")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "請填入你的_Gemini_API_Key")

# ==========================================================
# ⚙️ 2. 運行模式與兩半資金池參數設定
# ==========================================================
WHITELIST_SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']
MAX_CAPITAL_UTILIZATION = 0.88  # 動用 88% 可用現金拆成兩半 (每半 44%)，保留 12% 緩衝
MAX_ORDERS_PER_HALF = 3         # 每一半資金最多可同時持有/抓 3 張不同原因的倉位
MAX_CONTRACTS_PER_ORDER = 1500  # 單筆最大張數上限

IS_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
RUN_CONTINUOUS_LOOP = not IS_GITHUB_ACTIONS
LOOP_INTERVAL_SECONDS = 180

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
    return headlines[:5] if headlines else ["Market active with steady institutional volume."]

def fetch_whitelist_market_data(exchange):
    market_summary = {}
    for sym in WHITELIST_SYMBOLS:
        try:
            ticker = exchange.fetch_ticker(sym)
            ohlcv = exchange.fetch_ohlcv(sym, timeframe='15m', limit=24)
            closes = [c[4] for c in ohlcv]
            highs = [c[2] for c in ohlcv]
            lows = [c[3] for c in ohlcv]
            current_price = float(ticker['last'])

            ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else current_price
            ma20 = sum(closes) / len(closes) if closes else current_price
            tr_list = [(highs[i] - lows[i]) / closes[i] * 100 for i in range(len(closes))]
            atr_pct = sum(tr_list) / len(tr_list) if tr_list else 1.0
            swing_pct = ((max(highs) - min(lows)) / current_price) * 100 if highs else 3.0
            change_24h = float(ticker.get('percentage') or 0.0)

            market_summary[sym] = {
                "last_price": round(current_price, 2),
                "change_24h_pct": round(change_24h, 2),
                "ma5": round(ma5, 2),
                "ma20": round(ma20, 2),
                "atr_15m_pct": round(atr_pct, 2),
                "recent_swing_pct": round(swing_pct, 2),
                "trend": "BULLISH" if ma5 > ma20 else "BEARISH"
            }
        except Exception as e:
            print(f"⚠️ 抓取 {sym} 行情異常: {e}")
    return market_summary

def ask_gemini_two_halves_brain(market_data, news_headlines, current_held_symbols):
    gemini_key = clean_credential(GEMINI_API_KEY, "GEMINI_API_KEY")
    client = genai.Client(api_key=gemini_key)
    prompt = f"""
    你是 QuantGuard AI 雙資金池多策略決策核心。請根據以下即時行情、新聞與目前持倉狀態進行規劃：
    【三大幣種行情與振幅】：{json.dumps(market_data, ensure_ascii=False)}
    【最新國際新聞】：{json.dumps(news_headlines, ensure_ascii=False)}
    【目前已持有倉位之幣種】：{json.dumps(current_held_symbols, ensure_ascii=False)}

    【核心規則：資金拆兩半 × 每一半最多 3 張不同原因訂單】：
    1. 資金分為兩半：
       - `half_a_orders` (第一半資金池)：以「技術面均線突破、支撐壓力、趨勢順勢」為進場原因，輸出 1 ~ 3 張訂單。
       - `half_b_orders` (第二半資金池)：以「國際新聞事件催化、巨鯨動能、24h強弱勢表現」為進場原因，輸出 1 ~ 3 張訂單。
    2. 每一張訂單必須包含：
       - `symbol`: "BTC/USDT:USDT" 或 "ETH/USDT:USDT" 或 "SOL/USDT:USDT"
       - `action`: "BUY" 或 "SELL"
       - `weight`: 浮點數 (0.8 ~ 2.5，獲利機會越大權重越高)
       - `leverage`: 整數 (3 ~ 10 倍槓桿)
       - `tp_pct`: 浮點數 (6.0 ~ 25.0 %，依下單原因設定動態止盈)
       - `sl_pct`: 浮點數 (2.8 ~ 5.5 %，參考 recent_swing_pct 設定防洗盤寬止損)
       - `reason`: 繁體中文 45 字內，明確寫出該筆訂單的獨立進場原因與止盈止損設定依據。

    請嚴格只輸出 JSON 格式：
    {{
      "overall_confidence": 整數(0-100),
      "overall_summary": "繁體中文50字內：當前持倉監控與兩半資金池配置總結",
      "half_a_orders": [
        {{"symbol": "BTC/USDT:USDT", "action": "BUY", "weight": 1.5, "leverage": 6, "tp_pct": 11.0, "sl_pct": 3.5, "reason": "【Half-A 技術順勢】..."}}
      ],
      "half_b_orders": [
        {{"symbol": "ETH/USDT:USDT", "action": "BUY", "weight": 1.8, "leverage": 7, "tp_pct": 14.0, "sl_pct": 3.8, "reason": "【Half-B 新聞催化】..."}}
      ]
    }}
    """
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.25)
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"⚠️ AI 呼叫異常，啟動雙資金池多訂單備援: {e}")
        half_a, half_b = [], []
        for sym in WHITELIST_SYMBOLS:
            info = market_data.get(sym, {})
            act = "BUY" if info.get("trend") == "BULLISH" else "SELL"
            swing = float(info.get("recent_swing_pct", 3.5))
            sl_v = round(max(2.8, min(5.2, swing * 1.1)), 2)
            tp_v = round(sl_v * 2.5, 2)
            half_a.append({
                "symbol": sym, "action": act, "weight": 1.2, "leverage": 5,
                "tp_pct": tp_v, "sl_pct": sl_v,
                "reason": f"【Half-A 均線順勢】{sym.split('/')[0]} 15分K呈現 {info.get('trend')}，依振幅設寬止損 -{sl_v}%。"
            })
            half_b.append({
                "symbol": sym, "action": act, "weight": 1.2, "leverage": 6,
                "tp_pct": round(tp_v * 1.2, 2), "sl_pct": sl_v,
                "reason": f"【Half-B 動能爆發】{sym.split('/')[0]} 24h變動 {info.get('change_24h_pct')}%，捕捉波段動能。"
            })
        return {
            "overall_confidence": 82,
            "overall_summary": "依當前倉位與資產動態監控，雙資金池順勢運作中。",
            "half_a_orders": half_a[:3],
            "half_b_orders": half_b[:3]
        }

def calculate_contracts(exchange, symbol, margin_usdt, leverage, price):
    market = exchange.market(symbol)
    contract_size = float(market.get('contractSize') or 1.0)
    notional = contract_size * price
    if notional <= 0:
        return 1
    return max(1, min(int((margin_usdt * leverage) // notional), MAX_CONTRACTS_PER_ORDER))

def execute_half_pool_orders(exchange, pool_name, orders_list, pool_budget_usdt, market_data, reason_lookup, existing_positions_map):
    valid_orders = [o for o in orders_list[:MAX_ORDERS_PER_HALF] if str(o.get("action", "")).upper() in ["BUY", "SELL"]]
    if not valid_orders or pool_budget_usdt < 25:
        print(f"ℹ️ [{pool_name}] 可用預算 (${pool_budget_usdt:,.2f}) 已全數在當前倉位中運作，不重複下單。")
        return

    total_weight = sum(float(o.get("weight", 1.0)) for o in valid_orders)
    print(f"\n🔥 [{pool_name} 巡檢] 可分配預算: ${pool_budget_usdt:,.2f} USDT | 評估 {len(valid_orders)} 筆策略指令：")

    for idx, ord_spec in enumerate(valid_orders, start=1):
        sym = ord_spec.get("symbol", "ETH/USDT:USDT")
        if sym not in WHITELIST_SYMBOLS:
            sym = "ETH/USDT:USDT"
        action = str(ord_spec.get("action", "BUY")).upper()
        weight = max(0.5, min(3.0, float(ord_spec.get("weight", 1.0))))
        leverage = max(1, min(10, int(ord_spec.get("leverage", 5))))
        sl_pct = round(max(2.5, min(6.0, float(ord_spec.get("sl_pct", 3.5)))), 2)
        tp_pct = round(max(sl_pct * 1.8, min(30.0, float(ord_spec.get("tp_pct", 10.0)))), 2)
        reason = ord_spec.get("reason", f"{pool_name} 自動策略")

        reason_lookup[sym] = {
            "tp_pct": tp_pct, "sl_pct": sl_pct, "leverage": leverage,
            "pool": pool_name, "reason": reason
        }

        current_price = float(market_data.get(sym, {}).get("last_price", 0))
        if current_price <= 0:
            continue

        order_margin = round(pool_budget_usdt * (weight / total_weight), 2) if total_weight > 0 else round(pool_budget_usdt / len(valid_orders), 2)
        if order_margin < 15:
            continue

        contracts = calculate_contracts(exchange, sym, order_margin, leverage, current_price)
        side = 'buy' if action == 'BUY' else 'sell'
        pos_side = 'long' if side == 'buy' else 'short'
        tp_price = round(current_price * (1 + tp_pct / 100.0) if side == 'buy' else current_price * (1 - tp_pct / 100.0), 2)
        sl_price = round(current_price * (1 - sl_pct / 100.0) if side == 'buy' else current_price * (1 + sl_pct / 100.0), 2)

        print(f"   ⚡ [{pool_name} #{idx} | {sym}] 方向: {pos_side.upper()} | 槓桿: {leverage}x | 權重: {weight}x")
        print(f"      ├─ 投入保證金: ${order_margin:,.2f} USDT ➔ 換算買進: 【{contracts} 張合約】")
        print(f"      ├─ 動態止盈: ${tp_price:,.2f} (+{tp_pct}%) | 寬幅止損: ${sl_price:,.2f} (-{sl_pct}%)")
        print(f"      └─ 原因: {reason}")

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
            print(f"      🎉 [下單成功！] 訂單 ID: {order.get('id')}")
        except Exception as first_err:
            if "51000" in str(first_err) or "posSide" in str(first_err):
                try:
                    order = exchange.create_order(
                        symbol=sym, type='market', side=side, amount=contracts,
                        params={'tdMode': 'cross', 'attachAlgoOrds': algo_params}
                    )
                    print(f"      🎉 [下單成功 (單向模式)] 訂單 ID: {order.get('id')}")
                except Exception as second_err:
                    print(f"      ⚠️ 下單異常: {second_err}")
            else:
                print(f"      ⚠️ 下單異常: {first_err}")

def fetch_okx_positions_history(exchange, market_data, reason_lookup, ai_summary):
    history_records = []
    seen_pos_ids = set()

    raw_pos_history = []
    try:
        if hasattr(exchange, 'fetch_positions_history'):
            raw_pos_history = exchange.fetch_positions_history(WHITELIST_SYMBOLS, limit=30)
        else:
            res = exchange.privateGetAccountPositionsHistory({'instType': 'SWAP', 'limit': '30'})
            raw_pos_history = res.get('data', [])
    except Exception as e:
        print(f"ℹ️ 讀取歷史倉位 API 備援切換: {e}")
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

        open_avg_px = float(info.get('openAvgPx') or item.get('entryPrice') or 0.0)
        close_avg_px = float(info.get('closeAvgPx') or item.get('closePrice') or 0.0)
        close_qty = float(info.get('closeTotalPos') or item.get('contracts') or 1.0)
        lev = info.get('lever') or item.get('leverage') or 5

        realized_pnl = float(info.get('realizedPnl') or info.get('pnl') or item.get('realizedPnl') or 0.0)
        pnl_ratio_raw = float(info.get('pnlRatio') or item.get('percentage') or 0.0)
        pnl_pct = round(pnl_ratio_raw * 100, 2) if abs(pnl_ratio_raw) < 5 else round(pnl_ratio_raw, 2)
        fee_val = abs(float(info.get('fee') or 0.0))

        ts_ms = int(u_time) if u_time.isdigit() else int(time.time() * 1000)
        dt_clean = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        r_info = reason_lookup.get(sym, {})
        pnl_sign = "+" if realized_pnl >= 0 else ""

        raw_audit = f"{pos_id}_{sym}_{open_avg_px}_{close_avg_px}_{realized_pnl}_{dt_clean}"
        sha_hash = "0x" + hashlib.sha256(raw_audit.encode('utf-8')).hexdigest()[:16]

        history_records.append({
            "id": f"POS-{pos_id[-6:]}" if len(pos_id) >= 6 else f"POS-{ts_ms % 1000000}",
            "full_id": pos_id or str(ts_ms),
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
            "fee": f"倉位累計手續費 {fee_val:.4f} USDT",
            "rr": f"開倉 ${open_avg_px:,.2f} / 平倉 ${close_avg_px:,.2f}",
            "reason": r_info.get('reason', ai_summary or 'OKX 歷史倉位區已平倉結算紀錄'),
            "hash": sha_hash
        })

    if len(history_records) == 0:
        for sym in WHITELIST_SYMBOLS:
            r_info = reason_lookup.get(sym, {})
            try:
                closed_orders = exchange.fetch_closed_orders(sym, limit=6)
                for o in closed_orders:
                    oid = str(o.get('id', ''))
                    side_raw = o.get('side', 'buy')
                    info_pos_side = str(o.get('info', {}).get('posSide', '')).upper()
                    side_label = f"開倉成交 {side_raw.upper()} ({info_pos_side})"
                    avg_p = float(o.get('average') or o.get('price') or market_data.get(sym, {}).get('last_price', 0))
                    amt = float(o.get('amount') or 1)
                    cost_val = float(o.get('cost') or (avg_p * amt))
                    dt_raw = o.get('datetime') or datetime.now(timezone.utc).isoformat()
                    dt_clean = dt_raw[:19].replace('T', ' ')

                    raw_audit = f"{oid}_{sym}_{side_raw}_{avg_p}_{amt}_{dt_clean}"
                    sha_hash = "0x" + hashlib.sha256(raw_audit.encode('utf-8')).hexdigest()[:16]

                    history_records.append({
                        "id": f"ORD-{oid[-6:]}" if len(oid) >= 6 else f"ORD-{oid}",
                        "full_id": oid,
                        "timestamp_ms": o.get('timestamp') or 0,
                        "time": dt_clean,
                        "market": "crypto",
                        "marketBadge": f"🟢 建倉成交 ({amt}張)",
                        "asset": sym,
                        "direction": side_label,
                        "leverage": f"{r_info.get('leverage', 5)}x 動態槓桿",
                        "pool": f"建倉名目金額 ${cost_val:,.2f} ({amt}張)",
                        "entryExit": f"開倉均價 ${avg_p:,.2f} ➔ 現價 ${market_data.get(sym, {}).get('last_price', 0):,.2f}",
                        "roi": "持倉運行中 (平倉後將轉入歷史倉位結算損益)",
                        "pnl": f"合約價值: ${cost_val:,.2f} USDT",
                        "fee": "依 OKX 實盤標準計收",
                        "rr": f"動態止盈 +{r_info.get('tp_pct', 12.0)}% / 寬止損 -{r_info.get('sl_pct', 3.8)}%",
                        "reason": r_info.get('reason', ai_summary or '兩半資金池自動建倉紀錄'),
                        "hash": sha_hash
                    })
            except Exception:
                pass

    history_records.sort(key=lambda x: x.get('timestamp_ms', 0), reverse=True)
    return history_records

def run_single_cycle(exchange, cycle_num=1):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n==================== [第 {cycle_num} 次自動巡檢 | {now_str}] ====================")

    balance = exchange.fetch_balance()
    real_usdt = round(float(balance.get('USDT', {}).get('total', 0.0)), 2)
    free_usdt = round(float(balance.get('USDT', {}).get('free', 0.0)), 2)

    initial_positions_map = {}
    try:
        cur_positions = exchange.fetch_positions(WHITELIST_SYMBOLS)
        for pos in cur_positions:
            contracts = float(pos.get('contracts') or 0)
            if contracts > 0:
                sym = pos.get('symbol')
                initial_positions_map[sym] = {
                    "side": pos.get('side', 'long').upper(),
                    "contracts": contracts,
                    "margin": float(pos.get('initialMargin') or pos.get('collateral') or 0.0)
                }
    except Exception as e:
        print(f"⚠️ 初始倉位讀取提示: {e}")

    print(f"💰 OKX 實時總資產: ${real_usdt:,.2f} USDT | 可用現金: ${free_usdt:,.2f} USDT | 當前持有倉位數: {len(initial_positions_map)} 筆")

    market_data = fetch_whitelist_market_data(exchange)
    news_list = fetch_live_crypto_news()
    ai_plan = ask_gemini_two_halves_brain(market_data, news_list, list(initial_positions_map.keys()))
    print(f"🧠 Gemini 決策總結: {ai_plan.get('overall_summary')}")

    deployable_cash = free_usdt * MAX_CAPITAL_UTILIZATION
    half_a_budget = round(deployable_cash / 2.0, 2)
    half_b_budget = round(deployable_cash / 2.0, 2)

    reason_lookup = {}
    for o in (ai_plan.get("half_a_orders", []) + ai_plan.get("half_b_orders", [])):
        s_sym = o.get("symbol")
        if s_sym:
            reason_lookup[s_sym] = {
                "tp_pct": o.get("tp_pct", 12.0),
                "sl_pct": o.get("sl_pct", 3.8),
                "leverage": o.get("leverage", 5),
                "pool": "兩半資金池",
                "reason": o.get("reason", "")
            }

    if deployable_cash >= 60:
        execute_half_pool_orders(exchange, "第一半資金池 (Half-A)", ai_plan.get("half_a_orders", []), half_a_budget, market_data, reason_lookup, initial_positions_map)
        execute_half_pool_orders(exchange, "第二半資金池 (Half-B)", ai_plan.get("half_b_orders", []), half_b_budget, market_data, reason_lookup, initial_positions_map)
    else:
        print("🛡️ 當前倉位已充分運用兩半資金池，持續依當前倉位追蹤實時損益！")

    balance = exchange.fetch_balance()
    real_usdt = round(float(balance.get('USDT', {}).get('total', 0.0)), 2)
    free_usdt = round(float(balance.get('USDT', {}).get('free', 0.0)), 2)
    used_usdt = round(real_usdt - free_usdt, 2)

    active_positions = []
    positions_map = {}
    total_upl = 0.0

    try:
        positions = exchange.fetch_positions(WHITELIST_SYMBOLS)
        for pos in positions:
            contracts = float(pos.get('contracts') or 0)
            if contracts > 0:
                sym = pos.get('symbol')
                upl = round(float(pos.get('unrealizedPnl') or 0.0), 2)
                roi_pct = round(float(pos.get('percentage') or 0.0), 2)
                margin_used = round(float(pos.get('initialMargin') or pos.get('collateral') or 0.0), 2)
                notional_val = round(float(pos.get('notional') or 0.0), 2)
                total_upl += upl
                r_info = reason_lookup.get(sym, {})

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
                    "sl_pct": r_info.get("sl_pct", 3.8),
                    "reason": r_info.get("reason", "OKX 當前持有倉位實時追蹤中")
                }
                active_positions.append(pos_obj)
                positions_map[sym] = pos_obj
    except Exception as e:
        print(f"⚠️ 讀取當前倉位提示: {e}")

    positions_history_formatted = fetch_okx_positions_history(
        exchange, market_data, reason_lookup, ai_plan.get('overall_summary', '')
    )

    tranches_payload = []
    slot_configs = [
        (1, 'BTC/USDT:USDT', 'crypto_high', 'Half-A 倉位 #1'),
        (2, 'ETH/USDT:USDT', 'crypto_high', 'Half-A 倉位 #2'),
        (3, 'SOL/USDT:USDT', 'crypto_high', 'Half-A 倉位 #3'),
        (4, 'BTC/USDT:USDT', 'crypto_high', 'Half-B 倉位 #4'),
        (5, 'ETH/USDT:USDT', 'crypto_high', 'Half-B 倉位 #5'),
        (6, 'SOL/USDT:USDT', 'crypto_low',  'Half-B 倉位 #6'),
        (7, 'ETH/USDT:USDT', 'crypto_low',  '動態備用倉 #7'),
    ]
    for idx, sym, t_type, title in slot_configs:
        pos_data = positions_map.get(sym)
        if pos_data:
            tranches_payload.append({
                "id": idx, "type": t_type, "title": title,
                "sub": f"{sym.split('/')[0]} {pos_data['side']} ({pos_data['contracts']}張)",
                "lev": pos_data['leverage'],
                "roi": pos_data['roi_pct'],
                "real_pnl_usd": round(pos_data['unrealized_pnl'] / 2.0, 2)
            })
        else:
            tranches_payload.append({
                "id": idx, "type": t_type, "title": title,
                "sub": f"{sym.split('/')[0]} 目前無倉位", "lev": "0x", "roi": 0.0, "real_pnl_usd": 0.0
            })

    for idx in [8, 9, 10]:
        tranches_payload.append({
            "id": idx, "type": "twse_safe", "title": f"台股倉位 #{idx}",
            "sub": "未建倉 (0%)", "lev": "0x", "roi": 0.0, "real_pnl_usd": 0.0
        })

    live_payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "real_usdt": real_usdt,
        "free_usdt": free_usdt,
        "used_usdt": used_usdt,
        "tranche_unit": round(real_usdt / 10.0, 2),
        "total_unrealized_pnl": round(total_upl, 2),
        "total_crypto_roi_pct": round((total_upl / real_usdt) * 100, 2) if real_usdt > 0 else 0.0,
        "twse_real_roi_pct": 0.0,
        "active_positions": active_positions,
        "market_prices": market_data,
        "latest_news": news_list,
        "ai_last_decision": {
            "action": f"當前持有 {len(active_positions)} 筆倉位 | 歷史已結算 {len(positions_history_formatted)} 筆",
            "confidence": ai_plan.get("overall_confidence", 88),
            "reason": ai_plan.get("overall_summary", "")
        },
        "tranches": tranches_payload,
        "orders": positions_history_formatted
    }

    with open("live_data.json", "w", encoding="utf-8") as f:
        json.dump(live_payload, f, ensure_ascii=False, indent=2)

    print(f"📁 live_data.json 已更新！OKX 總資產: ${real_usdt:,.2f} | 當前持有倉位: {len(active_positions)} 筆 (浮盈: ${total_upl:+,.2f}) | 歷史倉位紀錄: {len(positions_history_formatted)} 筆")

def main():
    print("=========================================================================")
    print("🚀 QuantGuard AI v7.9 - 當前倉位實時綁定 × 歷史倉位 (Positions History) 追蹤引擎")
    print("=========================================================================")
    exchange = init_okx_exchange()
    cycle = 0
    while True:
        cycle += 1
        try:
            run_single_cycle(exchange, cycle)
        except Exception as e:
            print(f"❌ 巡檢發生異常: {e}")

        if not RUN_CONTINUOUS_LOOP:
            print("✅ GitHub Actions 巡檢完成，已將最新『當前倉位』與『歷史倉位』寫入 live_data.json！")
            break

        print(f"⏳ 將於 {LOOP_INTERVAL_SECONDS} 秒後進行下一輪自動巡檢...")
        time.sleep(LOOP_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
