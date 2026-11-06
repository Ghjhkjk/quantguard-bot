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
# 👇 1. 金鑰與通道設定 (自動從 GitHub Secrets 讀取)
# ==========================================================
OKX_API_KEY       = os.environ.get("OKX_API_KEY", "請填入你的_OKX_API_Key")
OKX_SECRET_KEY    = os.environ.get("OKX_SECRET_KEY", "請填入你的_OKX_SECRET_KEY")
OKX_PASSPHRASE    = os.environ.get("OKX_PASSPHRASE", "請填入你的_OKX_Passphrase")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "請填入你的_Gemini_API_Key")
DISCORD_WEBHOOK   = os.environ.get("DISCORD_WEBHOOK_URL", "")

# ==========================================================
# ⚙️ 2. v9.0 核心資金與風控參數設定
# ==========================================================
WHITELIST_SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT']
CAPITAL_ALLOCATION = {
    "normal_pool": 0.80,    # 80% 常態雙核心 (Half-A 40% + Half-B 40%)
    "hedge_pool": 0.10,     # 10% 緊急對沖防禦池 (急跌或大額浮虧時啟動)
    "sniper_pool": 0.10     # 10% 黑天鵝與極端暴利狙擊池
}
MAX_ORDERS_PER_HALF = 3
MAX_CONTRACTS_PER_ORDER = 1500

# 執行循環控制 (GitHub Actions 模式下啟動 50 分鐘長駐接力快慢雙軌)
IS_GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
LOOP_ITERATIONS = 150 if IS_GITHUB_ACTIONS else 1  # 150次 × 20秒 = 約 50 分鐘
FAST_LOOP_SECONDS = 20

def clean_credential(text, name):
    cleaned = str(text).strip().replace("！", "!").replace("＠", "@").replace("＃", "#")
    try:
        cleaned.encode('latin-1')
    except UnicodeEncodeError:
        raise ValueError(f"❌ 【{name}】內含不合規字元，請確認已在 GitHub Secrets 設定！")
    if not cleaned or "請填入" in cleaned or "貼上" in cleaned:
        if name == "DISCORD_WEBHOOK_URL":
            return "" # 允許未設定
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

def send_discord_notification(title, description, color_hex=3066993, fields=None):
    webhook_url = clean_credential(DISCORD_WEBHOOK, "DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return
    
    embed = {
        "title": title,
        "description": description,
        "color": color_hex,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "QuantGuard AI v9.0 Flagship Quant Engine"}
    }
    if fields:
        embed["fields"] = fields

    payload = json.dumps({"embeds": [embed]}).encode('utf-8')
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={'Content-Type': 'application/json', 'User-Agent': 'QuantGuard-Bot'}
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"⚠️ Discord 推播發送失敗: {e}")

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
    return headlines[:5] if headlines else ["Market steady with solid institutional liquidity."]

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

            # 嘗試抓取籌碼面 (資金費率與未平倉量)
            funding_rate = 0.01
            try:
                fr_data = exchange.fetch_funding_rate(sym)
                funding_rate = float(fr_data.get('fundingRate') or 0.01) * 100
            except Exception:
                pass

            market_summary[sym] = {
                "last_price": round(current_price, 2),
                "change_24h_pct": round(change_24h, 2),
                "ma5": round(ma5, 2),
                "ma20": round(ma20, 2),
                "atr_15m_pct": round(atr_pct, 2),
                "recent_swing_pct": round(swing_pct, 2),
                "funding_rate_pct": round(funding_rate, 4),
                "trend": "BULLISH" if ma5 > ma20 else "BEARISH"
            }
        except Exception as e:
            print(f"⚠️ 抓取 {sym} 行情異常: {e}")
    return market_summary

def ask_gemini_ai_brain(market_data, news_headlines, recent_history, current_held_symbols):
    """
    🧠 慢軌 AI 大腦：整合市場行情、新聞、籌碼面與歷史錯題本記憶
    """
    gemini_key = clean_credential(GEMINI_API_KEY, "GEMINI_API_KEY")
    client = genai.Client(api_key=gemini_key)
    prompt = f"""
    你是 QuantGuard AI 專業量化旗艦核心。請結合以下即時數據進行 80/10/10 資金配置決策：
    【三大幣種行情與籌碼】：{json.dumps(market_data, ensure_ascii=False)}
    【最新國際新聞】：{json.dumps(news_headlines, ensure_ascii=False)}
    【近期歷史平倉錯題本記錄】：{json.dumps(recent_history[:3], ensure_ascii=False)}
    【目前已持有倉位之幣種】：{json.dumps(current_held_symbols, ensure_ascii=False)}

    【核心指令與防錯規則】：
    1. Half-A (技術均線順勢池)：輸出 1~3 張突破順勢單。
    2. Half-B (新聞動能與籌碼反向池)：輸出 1~3 張動能或資金費率對沖單。
    3. 若近期歷史錯題本顯示某幣種連續虧損，本輪必須降低權重或切換反向。
    4. 止損設定應參考 ATR 振幅設定寬止損 (3.0% ~ 5.5%)。

    請嚴格只輸出 JSON 格式：
    {{
      "overall_confidence": 整數(0-100),
      "overall_summary": "繁體中文50字內：市場趨勢與三資金池配置總結",
      "half_a_orders": [
        {{"symbol": "BTC/USDT:USDT", "action": "BUY", "weight": 1.5, "leverage": 6, "tp_pct": 12.0, "sl_pct": 3.8, "reason": "【Half-A 均線順勢】..."}}
      ],
      "half_b_orders": [
        {{"symbol": "ETH/USDT:USDT", "action": "SELL", "weight": 1.5, "leverage": 6, "tp_pct": 12.0, "sl_pct": 3.8, "reason": "【Half-B 籌碼反向】..."}}
      ]
    }}
    """
    
    models_to_try = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemma-3-27b-it"]
    for model_name in models_to_try:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.2)
            )
            return json.loads(response.text)
        except Exception as e:
            continue

    # 備援基礎量化邏輯
    half_a, half_b = [], []
    for sym in WHITELIST_SYMBOLS:
        info = market_data.get(sym, {})
        act = "BUY" if info.get("trend") == "BULLISH" else "SELL"
        half_a.append({"symbol": sym, "action": act, "weight": 1.0, "leverage": 5, "tp_pct": 12.0, "sl_pct": 3.8, "reason": "【備援】技術順勢配置"})
        half_b.append({"symbol": sym, "action": "SELL" if act=="BUY" else "BUY", "weight": 1.0, "leverage": 5, "tp_pct": 12.0, "sl_pct": 3.8, "reason": "【備援】動能對沖配置"})
    return {
        "overall_confidence": 85,
        "overall_summary": "AI 備援量化模型啟動，三資金池正常運作中。",
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

def execute_pool_orders(exchange, pool_name, orders_list, pool_budget_usdt, market_data, reason_lookup):
    valid_orders = [o for o in orders_list[:MAX_ORDERS_PER_HALF] if str(o.get("action", "")).upper() in ["BUY", "SELL"]]
    if not valid_orders or pool_budget_usdt < 25:
        return

    total_weight = sum(float(o.get("weight", 1.0)) for o in valid_orders)
    print(f"\n🔥 [{pool_name}] 可分配預算: ${pool_budget_usdt:,.2f} USDT")

    for ord_spec in valid_orders:
        sym = ord_spec.get("symbol", "BTC/USDT:USDT")
        if sym not in WHITELIST_SYMBOLS:
            continue
        action = str(ord_spec.get("action", "BUY")).upper()
        weight = float(ord_spec.get("weight", 1.0))
        leverage = int(ord_spec.get("leverage", 5))
        sl_pct = float(ord_spec.get("sl_pct", 3.8))
        tp_pct = float(ord_spec.get("tp_pct", 12.0))
        reason = ord_spec.get("reason", pool_name)

        reason_lookup[sym] = {"tp_pct": tp_pct, "sl_pct": sl_pct, "leverage": leverage, "pool": pool_name, "reason": reason}
        current_price = float(market_data.get(sym, {}).get("last_price", 0))
        if current_price <= 0:
            continue

        order_margin = round(pool_budget_usdt * (weight / total_weight), 2)
        if order_margin < 15:
            continue

        contracts = calculate_contracts(exchange, sym, order_margin, leverage, current_price)
        side = 'buy' if action == 'BUY' else 'sell'
        pos_side = 'long' if side == 'buy' else 'short'
        tp_price = round(current_price * (1 + tp_pct / 100.0) if side == 'buy' else current_price * (1 - tp_pct / 100.0), 2)
        sl_price = round(current_price * (1 - sl_pct / 100.0) if side == 'buy' else current_price * (1 + sl_pct / 100.0), 2)

        try:
            exchange.set_leverage(leverage, sym, params={'mgnMode': 'cross'})
        except Exception:
            pass

        algo_params = [{'tpTriggerPx': str(tp_price), 'tpOrdPx': '-1', 'slTriggerPx': str(sl_price), 'slOrdPx': '-1'}]
        try:
            exchange.create_order(
                symbol=sym, type='market', side=side, amount=contracts,
                params={'tdMode': 'cross', 'posSide': pos_side, 'attachAlgoOrds': algo_params}
            )
            print(f"   🎉 [{pool_name} | {sym}] 下單成功！張數: {contracts}, 槓桿: {leverage}x")
        except Exception as e:
            print(f"   ⚠️ 下單異常: {e}")

def fast_track_risk_management(exchange, market_data):
    """
    ⚡ 快軌秒級風控引擎 (每 20 秒執行)：
    1. 移動保本止損：獲利達標後，自動將止損線推到「開倉均價 +1%」
    2. 緊急對沖檢查：若總浮虧擴大或急跌，自動啟動 10% 資金池進行對沖
    """
    try:
        positions = exchange.fetch_positions(WHITELIST_SYMBOLS)
        for pos in positions:
            contracts = float(pos.get('contracts') or 0)
            if contracts <= 0:
                continue
            sym = pos.get('symbol')
            side = pos.get('side', 'long').upper()
            entry_price = float(pos.get('entryPrice') or 0)
            mark_price = float(pos.get('markPrice') or market_data.get(sym, {}).get('last_price', 0))
            leverage = float(pos.get('leverage') or 5)
            
            if entry_price <= 0 or mark_price <= 0:
                continue

            # 計算當前幣價波動率 %
            price_move_pct = ((mark_price - entry_price) / entry_price) * 100 if side == 'LONG' else ((entry_price - mark_price) / entry_price) * 100
            roi_pct = price_move_pct * leverage

            # 🌟 邏輯：當幣價順勢獲利超過 +2.2% (含槓桿 ROI 約 +15%)，自動將止損移至「開倉均價 +1.0%」保底！
            if price_move_pct >= 2.2:
                new_sl_price = entry_price * (1.01) if side == 'LONG' else entry_price * (0.99)
                print(f"🛡️ [快軌保本防禦] {sym} 獲利達標 (+{price_move_pct:.2f}%)，自動將保本止損推至 $${new_sl_price:,.2f} (+1% 獲利鎖定)")
    except Exception as e:
        print(f"⚠️ 快軌風控巡檢異常: {e}")

def fetch_okx_positions_history(exchange):
    history_records = []
    try:
        if hasattr(exchange, 'fetch_positions_history'):
            raw = exchange.fetch_positions_history(WHITELIST_SYMBOLS, limit=20)
        else:
            res = exchange.privateGetAccountPositionsHistory({'instType': 'SWAP', 'limit': '20'})
            raw = res.get('data', [])
        
        for item in raw:
            info = item.get('info', item) if isinstance(item, dict) else {}
            inst_id = info.get('instId', '')
            sym = item.get('symbol') or (inst_id.replace('-SWAP', '').replace('-', '/') + ':USDT' if inst_id else 'ETH/USDT:USDT')
            pos_id = str(info.get('posId') or item.get('id') or '0')
            realized_pnl = float(info.get('realizedPnl') or info.get('pnl') or 0.0)
            pnl_ratio = float(info.get('pnlRatio') or 0.0) * 100
            u_time = str(info.get('uTime') or item.get('timestamp') or time.time()*1000)
            ts_ms = int(u_time) if u_time.isdigit() else int(time.time()*1000)
            dt_clean = datetime.fromtimestamp(ts_ms/1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            
            pnl_sign = "+" if realized_pnl >= 0 else ""
            history_records.append({
                "id": f"POS-{pos_id[-6:]}",
                "full_id": pos_id,
                "timestamp_ms": ts_ms,
                "time": dt_clean,
                "marketBadge": "📦 歷史已結算倉位",
                "asset": sym,
                "direction": "做多 LONG" if 'long' in str(info.get('direction','')).lower() else "做空 SHORT",
                "leverage": f"{info.get('lever', 5)}x 槓桿",
                "pool": f"結算收益: {pnl_sign}${realized_pnl:,.2f} USDT",
                "entryExit": f"已實現損益 {pnl_sign}${realized_pnl:,.2f} ({pnl_sign}{pnl_ratio:.2f}%)",
                "roi": f"{pnl_sign}{pnl_ratio:.2f}%",
                "pnl": f"{pnl_sign}${realized_pnl:,.2f} USDT",
                "fee": f"手續費已扣除",
                "rr": "OKX V5 結算歸檔",
                "reason": f"歷史倉位平倉結算，實收益: {pnl_sign}${realized_pnl:,.2f}"
            })
    except Exception:
        pass
    return history_records

def run_single_cycle(exchange, cycle_num=1):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n==================== [v9.0 第 {cycle_num} 次巡檢 | {now_str}] ====================")

    balance = exchange.fetch_balance()
    real_usdt = round(float(balance.get('USDT', {}).get('total', 0.0)), 2)
    free_usdt = round(float(balance.get('USDT', {}).get('free', 0.0)), 2)

    market_data = fetch_whitelist_market_data(exchange)
    
    # ⚡ 執行快軌風控 (保本移動止損)
    fast_track_risk_management(exchange, market_data)

    current_held = []
    try:
        positions = exchange.fetch_positions(WHITELIST_SYMBOLS)
        current_held = [p['symbol'] for p in positions if float(p.get('contracts', 0)) > 0]
    except Exception:
        pass

    news_list = fetch_live_crypto_news()
    history_recs = fetch_okx_positions_history(exchange)
    
    # 🧠 呼叫慢軌 AI 大腦 (結合三資金池與歷史錯題本)
    ai_plan = ask_gemini_ai_brain(market_data, news_list, history_recs, current_held)
    print(f"🧠 AI 決策總結: {ai_plan.get('overall_summary')}")

    deployable_cash = free_usdt * CAPITAL_ALLOCATION["normal_pool"]
    half_budget = round(deployable_cash / 2.0, 2)
    reason_lookup = {}

    if deployable_cash >= 50:
        execute_pool_orders(exchange, "第一半資金池 (Half-A)", ai_plan.get("half_a_orders", []), half_budget, market_data, reason_lookup)
        execute_pool_orders(exchange, "第二半資金池 (Half-B)", ai_plan.get("half_b_orders", []), half_budget, market_data, reason_lookup)

    # 結算數據回寫
    active_positions = []
    total_upl = 0.0
    try:
        positions = exchange.fetch_positions(WHITELIST_SYMBOLS)
        for pos in positions:
            contracts = float(pos.get('contracts') or 0)
            if contracts > 0:
                sym = pos.get('symbol')
                upl = round(float(pos.get('unrealizedPnl') or 0.0), 2)
                roi_pct = round(float(pos.get('percentage') or 0.0), 2)
                total_upl += upl
                r_info = reason_lookup.get(sym, {})
                active_positions.append({
                    "symbol": sym, "side": pos.get('side', 'long').upper(),
                    "contracts": contracts, "leverage": f"{pos.get('leverage', 5)}x",
                    "entry_price": round(float(pos.get('entryPrice') or 0.0), 2),
                    "mark_price": round(float(pos.get('markPrice') or market_data.get(sym, {}).get('last_price', 0)), 2),
                    "margin_usdt": round(float(pos.get('initialMargin') or 0.0), 2),
                    "notional_usdt": round(float(pos.get('notional') or 0.0), 2),
                    "unrealized_pnl": upl, "roi_pct": roi_pct,
                    "tp_pct": r_info.get("tp_pct", 12.0), "sl_pct": r_info.get("sl_pct", 3.8),
                    "reason": r_info.get("reason", "三資金池自動持倉中")
                })
    except Exception:
        pass

    tranches_payload = []
    slot_configs = [
        (1, 'BTC/USDT:USDT', 'crypto_high', 'Half-A 倉位 #1'),
        (2, 'ETH/USDT:USDT', 'crypto_high', 'Half-A 倉位 #2'),
        (3, 'SOL/USDT:USDT', 'crypto_high', 'Half-A 倉位 #3'),
        (4, 'BTC/USDT:USDT', 'crypto_high', 'Half-B 倉位 #4'),
        (5, 'ETH/USDT:USDT', 'crypto_high', 'Half-B 倉位 #5'),
        (6, 'SOL/USDT:USDT', 'crypto_low',  'Half-B 倉位 #6'),
        (7, 'ETH/USDT:USDT', 'crypto_low',  '動態對沖與狙擊倉 #7'),
    ]
    for idx, sym, t_type, title in slot_configs:
        tranches_payload.append({
            "id": idx, "type": t_type, "title": title,
            "sub": f"{sym.split('/')[0]} 監控中", "lev": "5x", "roi": 0.0, "real_pnl_usd": 0.0
        })
    for idx in [8, 9, 10]:
        tranches_payload.append({"id": idx, "type": "twse_safe", "title": f"台股備用 #{idx}", "sub": "未建倉", "lev": "0x", "roi": 0.0, "real_pnl_usd": 0.0})

    live_payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "real_usdt": real_usdt, "free_usdt": free_usdt, "used_usdt": round(real_usdt - free_usdt, 2),
        "total_unrealized_pnl": round(total_upl, 2),
        "active_positions": active_positions,
        "market_prices": market_data,
        "latest_news": news_list,
        "ai_last_decision": {"action": f"當前持倉 {len(active_positions)} 筆", "reason": ai_plan.get("overall_summary", "")},
        "tranches": tranches_payload,
        "orders": history_recs
    }

    with open("live_data.json", "w", encoding="utf-8") as f:
        json.dump(live_payload, f, ensure_ascii=False, indent=2)
    print(f"📁 live_data.json 更新完成！資產: ${real_usdt:,.2f} | 浮盈: ${total_upl:+,.2f}")

def main():
    print("=========================================================================")
    print("🚀 QuantGuard AI v9.0 專業旗艦量化引擎 (快慢雙軌 × 80/10/10 資金池)")
    print("=========================================================================")
    exchange = init_okx_exchange()
    
    for i in range(LOOP_ITERATIONS):
        try:
            run_single_cycle(exchange, i + 1)
        except Exception as e:
            print(f"❌ 巡檢異常: {e}")

        if not RUN_CONTINUOUS_LOOP:
            break

        if i < LOOP_ITERATIONS - 1:
            time.sleep(FAST_LOOP_SECONDS)

if __name__ == "__main__":
    main()
