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
# 👇 1. 金鑰設定 (優先讀取 GitHub Secrets，若在本地/Colab 可直接填在雙引號內)
# ==========================================================
OKX_API_KEY    = os.environ.get("OKX_API_KEY", "請填入你的_OKX_API_Key")
OKX_SECRET_KEY = os.environ.get("OKX_SECRET_KEY", "請填入你的_OKX_SECRET_Key")
OKX_PASSPHRASE = os.environ.get("OKX_PASSPHRASE", "請填入你的_OKX_Passphrase!")
GEMINI_API_KEY = os.environ.get("請填入你的_Gemini_API_Key")

# ==========================================================
# ⚙️ 2. 運行模式與兩半資金池參數設定
# ==========================================================
WHITELIST_SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']
MAX_CAPITAL_UTILIZATION = 0.88  # 動用 88% 可用現金拆成兩半 (每半 44%)，保留 12% 防強平緩衝
MAX_ORDERS_PER_HALF = 3         # 每一半資金最多可同時抓 3 張不同原因的訂單
MAX_CONTRACTS_PER_ORDER = 1500  # 單筆最大張數安全上限

# 若在 GitHub Actions 雲端排程中，預設單次執行完畢後交由排程器每5分鐘喚醒；
# 若在 Colab 或本機想無限循環，將下方設為 "true" 即可每 180 秒自動跑一次
IS_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
RUN_CONTINUOUS_LOOP = not IS_GITHUB_ACTIONS
LOOP_INTERVAL_SECONDS = 180

def clean_credential(text, name):
    cleaned = str(text).strip().replace("！", "!").replace("＠", "@").replace("＃", "#")
    try:
        cleaned.encode('latin-1')
    except UnicodeEncodeError:
        raise ValueError(f"❌ 【{name}】內含中文字或全形符號，請替換為真實金鑰或在 GitHub Secrets 中設定！")
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
    exchange.set_sandbox_mode(True)  # OKX 模擬盤實戰環境
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

def ask_gemini_two_halves_brain(market_data, news_headlines):
    """
    🌟 AI 雙資金池決策核心：
    將可用資金拆為 Half-A 與 Half-B 兩半，每一半各自最多抓 3 張不同原因的訂單同時下單！
    """
    gemini_key = clean_credential(GEMINI_API_KEY, "GEMINI_API_KEY")
    client = genai.Client(api_key=gemini_key)
    prompt = f"""
    你是 QuantGuard AI 雙資金池多策略決策核心。請根據以下即時行情與國際新聞進行規劃：
    【三大幣種行情與振幅】：{json.dumps(market_data, ensure_ascii=False)}
    【最新國際新聞】：{json.dumps(news_headlines, ensure_ascii=False)}

    【核心規則：資金拆兩半 × 每一半最多 3 張不同原因訂單】：
    1. 我們將可用資金拆成兩半：
       - `half_a_orders` (第一半資金池)：以「技術面均線突破、動能順勢、支撐壓力」為主要進場原因，請給出 1 ~ 3 張訂單。
       - `half_b_orders` (第二半資金池)：以「國際新聞事件催化、巨鯨籌碼動能、24h強弱勢表現」為主要進場原因，請給出 1 ~ 3 張訂單。
    2. 每一張訂單必須包含：
       - `symbol`: "BTC/USDT:USDT" 或 "ETH/USDT:USDT" 或 "SOL/USDT:USDT"
       - `action`: "BUY" 或 "SELL"
       - `weight`: 浮點數 (0.8 ~ 2.5，獲利機會越大權重越高，決定在該半邊資金池分到多少錢)
       - `leverage`: 整數 (3 ~ 10 倍槓桿)
       - `tp_pct`: 浮點數 (6.0 ~ 25.0 %，依該筆下單原因決定動態止盈目標)
       - `sl_pct`: 浮點數 (2.8 ~ 5.5 %，參考 recent_swing_pct 設定較寬的防洗盤止損)
       - `reason`: 繁體中文 45 字內，明確寫出該筆訂單的獨立進場原因與止盈止損設定依據。

    請嚴格只輸出 JSON 格式：
    {{
      "overall_confidence": 整數(0-100),
      "overall_summary": "繁體中文50字內：兩半資金池的多訂單配置總結",
      "half_a_orders": [
        {{"symbol": "BTC/USDT:USDT", "action": "BUY", "weight": 1.5, "leverage": 6, "tp_pct": 11.0, "sl_pct": 3.5, "reason": "【Half-A 技術突破】..."}}
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
            "overall_summary": "雙資金池 (Half-A 技術面 3 單 + Half-B 動能面 3 單) 同時併發運行中。",
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

def execute_half_pool_orders(exchange, pool_name, orders_list, pool_budget_usdt, market_data, reason_lookup):
    """執行單一半邊資金池 (最多 3 張不同原因訂單) 的自動配資與同時下單"""
    valid_orders = [o for o in orders_list[:MAX_ORDERS_PER_HALF] if str(o.get("action", "")).upper() in ["BUY", "SELL"]]
    if not valid_orders or pool_budget_usdt < 25:
        print(f"ℹ️ [{pool_name}] 目前無新增訊號或該半邊資金已全數投入持倉中。")
        return

    total_weight = sum(float(o.get("weight", 1.0)) for o in valid_orders)
    print(f"\n🔥 [{pool_name} 啟動] 該半邊總預算: ${pool_budget_usdt:,.2f} USDT | 同時執行 {len(valid_orders)} 張不同原因訂單：")

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

        current_price = float(market_data.get(sym, {}).get("last_price", 0))
        if current_price <= 0:
            continue

        order_margin = round(pool_budget_usdt * (weight / total_weight), 2) if total_weight > 0 else round(pool_budget_usdt / len(valid_orders), 2)
        contracts = calculate_contracts(exchange, sym, order_margin, leverage, current_price)

        side = 'buy' if action == 'BUY' else 'sell'
        pos_side = 'long' if side == 'buy' else 'short'
        tp_price = round(current_price * (1 + tp_pct / 100.0) if side == 'buy' else current_price * (1 - tp_pct / 100.0), 2)
        sl_price = round(current_price * (1 - sl_pct / 100.0) if side == 'buy' else current_price * (1 + sl_pct / 100.0), 2)

        reason_lookup[sym] = {
            "tp_pct": tp_pct, "sl_pct": sl_pct, "leverage": leverage,
            "pool": pool_name, "reason": reason
        }

        print(f"   ⚡ [{pool_name} 訂單 #{idx} | {sym}] 方向: {pos_side.upper()} | 槓桿: {leverage}x | 機會權重: {weight}x")
        print(f"      ├─ 投入保證金: ${order_margin:,.2f} USDT ➔ 換算買進: 【{contracts} 張合約】")
        print(f"      ├─ 原因導向止盈: ${tp_price:,.2f} (+{tp_pct}%) | 防洗盤寬止損: ${sl_price:,.2f} (-{sl_pct}%)")
        print(f"      └─ 下單原因: {reason}")

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

def run_single_cycle(exchange, cycle_num=1):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n==================== [第 {cycle_num} 次自動巡檢 | {now_str}] ====================")

    balance = exchange.fetch_balance()
    real_usdt = round(float(balance.get('USDT', {}).get('total', 0.0)), 2)
    free_usdt = round(float(balance.get('USDT', {}).get('free', 0.0)), 2)
    print(f"💰 OKX 帳戶實時總資產: ${real_usdt:,.2f} USDT | 可用現金: ${free_usdt:,.2f} USDT")

    market_data = fetch_whitelist_market_data(exchange)
    news_list = fetch_live_crypto_news()
    for sym, info in market_data.items():
        print(f"   📊 {sym}: 現價 ${info['last_price']:,.2f} | 24h: {info['change_24h_pct']}% | 振幅: {info['recent_swing_pct']}% | 趨勢: {info['trend']}")

    ai_plan = ask_gemini_two_halves_brain(market_data, news_list)
    print(f"🧠 Gemini 雙資金池決策總結: {ai_plan.get('overall_summary')}")

    # 將可用資金 (88%) 拆成兩半：Half-A (44%) 與 Half-B (44%)
    deployable_cash = free_usdt * MAX_CAPITAL_UTILIZATION
    half_a_budget = round(deployable_cash / 2.0, 2)
    half_b_budget = round(deployable_cash / 2.0, 2)

    reason_lookup = {}
    if deployable_cash >= 60:
        execute_half_pool_orders(exchange, "第一半資金池 (Half-A)", ai_plan.get("half_a_orders", []), half_a_budget, market_data, reason_lookup)
        execute_half_pool_orders(exchange, "第二半資金池 (Half-B)", ai_plan.get("half_b_orders", []), half_b_budget, market_data, reason_lookup)
    else:
        print("🛡️ 當前兩半資金池皆已在場內滿載運作中，靜待止盈/止損觸發後釋放資金自動投入下一輪！")

    # 重新讀取下單後的最新 OKX 總資產、目前持有訂單 (含訂單金額與收益率) 與歷史訂單
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
                    "margin_usdt": margin_used,      # 訂單投入保證金金額 (USDT)
                    "notional_usdt": notional_val,   # 訂單合約總名目金額 (USDT)
                    "unrealized_pnl": upl,           # 實時未實現損益金額 (USDT)
                    "roi_pct": roi_pct,              # 實時收益率 (%)
                    "tp_pct": r_info.get("tp_pct", 12.0),
                    "sl_pct": r_info.get("sl_pct", 3.8),
                    "reason": r_info.get("reason", "兩半資金池實盤持倉追蹤中")
                }
                active_positions.append(pos_obj)
                positions_map[sym] = pos_obj
    except Exception as e:
        print(f"⚠️ 讀取目前持倉提示: {e}")

    # 抓取 OKX 歷史與當前委託單
    real_orders_formatted = []
    for sym in WHITELIST_SYMBOLS:
        r_info = reason_lookup.get(sym, {})
        tp_pct = r_info.get('tp_pct', 12.0)
        sl_pct = r_info.get('sl_pct', 3.8)
        lev = r_info.get('leverage', 5)
        pool_label = r_info.get('pool', '兩半動態資金池')

        try:
            closed_orders = exchange.fetch_closed_orders(sym, limit=10)
            open_orders = exchange.fetch_open_orders(sym)
            for o in (closed_orders + open_orders):
                oid = str(o.get('id', ''))
                side_raw = o.get('side', 'buy')
                info_pos_side = str(o.get('info', {}).get('posSide', '')).upper()
                side_label = f"{side_raw.upper()} ({info_pos_side})" if info_pos_side else side_raw.upper()
                avg_p = float(o.get('average') or o.get('price') or market_data.get(sym, {}).get('last_price', 0))
                amt = float(o.get('amount') or 1)
                cost_val = float(o.get('cost') or (avg_p * amt))
                fee_info = o.get('fee') or {}
                fee_cost = float(fee_info.get('cost') or 0.0)
                fee_curr = fee_info.get('currency') or 'USDT'
                status_str = o.get('status', 'closed')
                dt_raw = o.get('datetime') or datetime.now(timezone.utc).isoformat()
                dt_clean = dt_raw[:19].replace('T', ' ')

                live_pos = positions_map.get(sym)
                if live_pos:
                    live_roi_str = f"實時收益率 {live_pos['roi_pct']:+.2f}% (${live_pos['unrealized_pnl']:+,.2f})"
                else:
                    live_roi_str = f"已執行 ({status_str})"

                raw_audit = f"{oid}_{sym}_{side_raw}_{avg_p}_{amt}_{dt_clean}"
                sha_hash = "0x" + hashlib.sha256(raw_audit.encode('utf-8')).hexdigest()[:16]

                real_orders_formatted.append({
                    "id": f"OKX-{oid[-6:]}" if len(oid) >= 6 else f"OKX-{oid}",
                    "full_id": oid,
                    "timestamp_ms": o.get('timestamp') or 0,
                    "time": dt_clean,
                    "market": "crypto",
                    "marketBadge": f"🟢 {pool_label} ({amt}張)",
                    "asset": sym,
                    "direction": side_label,
                    "leverage": f"{lev}.0x 動態槓桿",
                    "pool": f"訂單金額 ${cost_val:,.2f} (共 {amt} 張)",
                    "entryExit": f"成交均價 ${avg_p:,.2f} ➔ 現價 ${market_data.get(sym, {}).get('last_price', 0):,.2f}",
                    "roi": live_roi_str,
                    "pnl": f"訂單名目金額: ${cost_val:,.2f} USDT",
                    "fee": f"手續費 {fee_cost:.4f} {fee_curr}",
                    "rr": f"動態止盈 +{tp_pct}% / 寬止損 -{sl_pct}%",
                    "reason": r_info.get('reason', ai_plan.get('overall_summary', '由兩半資金池自動執行之真實訂單')),
                    "hash": sha_hash
                })
        except Exception:
            pass

    real_orders_formatted.sort(key=lambda x: x.get('timestamp_ms', 0), reverse=True)

    # 10 等分卡片真實映射 (台股誠實顯示 0.00%)
    tranches_payload = []
    slot_configs = [
        (1, 'BTC/USDT:USDT', 'crypto_high', 'Half-A 技術倉 #1'),
        (2, 'ETH/USDT:USDT', 'crypto_high', 'Half-A 技術倉 #2'),
        (3, 'SOL/USDT:USDT', 'crypto_high', 'Half-A 技術倉 #3'),
        (4, 'BTC/USDT:USDT', 'crypto_high', 'Half-B 新聞倉 #4'),
        (5, 'ETH/USDT:USDT', 'crypto_high', 'Half-B 新聞倉 #5'),
        (6, 'SOL/USDT:USDT', 'crypto_low',  'Half-B 動能倉 #6'),
        (7, 'ETH/USDT:USDT', 'crypto_low',  '複利滾存備用 #7'),
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
                "sub": f"{sym.split('/')[0]} 空倉待命", "lev": "0x", "roi": 0.0, "real_pnl_usd": 0.0
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
            "action": "Half-A & Half-B 兩半資金多單併發",
            "confidence": ai_plan.get("overall_confidence", 88),
            "reason": ai_plan.get("overall_summary", "")
        },
        "tranches": tranches_payload,
        "orders": real_orders_formatted
    }

    with open("live_data.json", "w", encoding="utf-8") as f:
        json.dump(live_payload, f, ensure_ascii=False, indent=2)

    print(f"📁 live_data.json 已更新！OKX 總資產: ${real_usdt:,.2f} | 目前持有訂單: {len(active_positions)} 筆 (實時浮盈: ${total_upl:+,.2f}) | 歷史交易: {len(real_orders_formatted)} 筆")

def main():
    print("=========================================================================")
    print("🚀 QuantGuard AI v7.8 - 24h 自動喚醒 × 兩半資金池多原因併發引擎")
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
            print("✅ GitHub Actions 單次排程巡檢完成，5 分鐘後將由雲端排程自動再次喚醒！")
            break

        print(f"⏳ 將於 {LOOP_INTERVAL_SECONDS} 秒後進行下一輪自動巡檢...")
        time.sleep(LOOP_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()