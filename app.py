"""
Bybit/Binance/BingX Perpetual Futures Calculator
Calcula leverage y tamaño de posición. Exchange seleccionable, sin fallback entre exchanges.
"""

from flask import Flask, render_template_string, request, jsonify
import requests
import time
import math
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ============================================================
# CONFIG DE EXCHANGES
# ============================================================
EXCHANGES = ["bybit", "binance", "bingx", "lbank"]

BASE_URLS = {
    "bybit": "https://api.bybit.com",
    "binance": "https://fapi.binance.com",
    "bingx": "https://open-api.bingx.com",
    "lbank": "https://lbkperp.lbank.com",
}

CACHE_TTL = 3600

# Binance no expone el max leverage por símbolo sin API key (endpoint público inexistente).
# Se usa un tope por defecto (125x, el máximo habitual de los perpétuos USDT-M) cuando el
# exchangeInfo no trae el filtro LEVERAGE.
BINANCE_DEFAULT_MAX_LEVERAGE = 125.0

# BingX tampoco expone maxLongLeverage/maxShortLeverage en los endpoints públicos pese a la
# documentación. Default: 125x, el máximo habitual de los perpétuos USDT-M de BingX.
BINGX_DEFAULT_MAX_LEVERAGE = 125.0

_cache = {
    "symbols": {},     # exchange -> {"items": [...], "timestamp": ts}
    "instruments": {}, # (exchange, symbol) -> wrapper
    "last_error": {}
}

# ============================================================
# FETCHERS POR EXCHANGE
# ============================================================
def _fetch_bybit_symbols():
    resp = requests.get(f"{BASE_URLS['bybit']}/v5/market/instruments-info",
                        params={"category": "linear"}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit error {data.get('retCode')}: {data.get('retMsg')}")
    symbols = []
    for item in data["result"]["list"]:
        if item.get("contractType") == "LinearPerpetual" and item.get("quoteCoin") == "USDT":
            symbols.append({
                "symbol": item["symbol"],
                "baseCoin": item["baseCoin"],
                "quoteCoin": item["quoteCoin"],
            })
    return symbols


def _fetch_binance_symbols():
    resp = requests.get(f"{BASE_URLS['binance']}/fapi/v1/exchangeInfo", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    symbols = []
    for item in data.get("symbols", []):
        if item.get("contractType") == "PERPETUAL" and item.get("quoteAsset") == "USDT":
            symbols.append({
                "symbol": item["symbol"],
                "baseCoin": item["baseAsset"],
                "quoteCoin": item["quoteAsset"],
            })
    return symbols


def _fetch_bingx_symbols():
    resp = requests.get(f"{BASE_URLS['bingx']}/openApi/swap/v2/quote/contracts", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"BingX error {data.get('code')}: {data.get('msg')}")
    symbols = []
    for item in data.get("data", []):
        if item.get("currency") == "USDT" and item.get("status") == 1:
            symbols.append({
                "symbol": item["symbol"],
                "baseCoin": item["asset"],
                "quoteCoin": item["currency"],
            })
    return symbols


# El endpoint /cfd/openApi/v1/pub/instrument de LBank no filtra por `symbol`, así que se
# trae y cachea el listado completo y se filtra en memoria.
_lbank_instruments_cache = {"items": None, "timestamp": 0}


def _fetch_lbank_instruments():
    now = time.time()
    cached = _lbank_instruments_cache
    if cached["items"] and (now - cached["timestamp"]) < CACHE_TTL:
        return cached["items"]
    resp = requests.get(f"{BASE_URLS['lbank']}/cfd/openApi/v1/pub/instrument",
                        params={"productGroup": "SwapU"}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("data", []) or []
    _lbank_instruments_cache["items"] = items
    _lbank_instruments_cache["timestamp"] = now
    return items


def _fetch_lbank_symbols():
    items = _fetch_lbank_instruments()
    symbols = []
    for item in items:
        # needSuspend == 0 → contrato activo; todos los de SwapU son USDT.
        if item.get("needSuspend") == 0 and item.get("clearCurrency") == "USDT":
            symbols.append({
                "symbol": item["symbol"],
                "baseCoin": item.get("baseCurrency", ""),
                "quoteCoin": item.get("clearCurrency", ""),
            })
    return symbols


# ============================================================
# INFO DE INSTRUMENTO POR EXCHANGE (normalizada)
# ============================================================
def _binance_filter(filters, filter_type):
    for f in filters:
        if f.get("filterType") == filter_type:
            return f
    return {}


def _bybit_instrument(symbol):
    resp = requests.get(f"{BASE_URLS['bybit']}/v5/market/instruments-info",
                        params={"category": "linear", "symbol": symbol}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode") != 0 or not data["result"]["list"]:
        raise RuntimeError(f"Bybit no encontró el símbolo {symbol}")
    inst = data["result"]["list"][0]
    lev = inst.get("leverageFilter", {})
    lot = inst.get("lotSizeFilter", {})
    return {
        "symbol": inst["symbol"],
        "baseCoin": inst["baseCoin"],
        "quoteCoin": inst["quoteCoin"],
        "maxLeverage": float(lev.get("maxLeverage", 0) or 0),
        "leverageStep": float(lev.get("leverageStep", "1") or 1),
        "qtyStep": float(lot.get("qtyStep", "0.001") or 0.001),
        "minOrderQty": float(lot.get("minOrderQty", "0.001") or 0.001),
        "minNotional": float(lot.get("minNotionalValue") or lot.get("minOrderAmt") or 5),
    }


def _binance_instrument(symbol):
    resp = requests.get(f"{BASE_URLS['binance']}/fapi/v1/exchangeInfo", params={"symbol": symbol}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("symbols"):
        raise RuntimeError(f"Binance no encontró el símbolo {symbol}")
    item = data["symbols"][0]
    filters = item.get("filters", [])
    lot = _binance_filter(filters, "LOT_SIZE")
    lev = _binance_filter(filters, "LEVERAGE")
    notional = _binance_filter(filters, "MIN_NOTIONAL")
    max_lev = float(lev.get("maxLeverage")) if lev.get("maxLeverage") else BINANCE_DEFAULT_MAX_LEVERAGE
    return {
        "symbol": item["symbol"],
        "baseCoin": item["baseAsset"],
        "quoteCoin": item["quoteAsset"],
        "maxLeverage": max_lev,
        "leverageStep": float(lev.get("leverageStep", "1") or 1),
        "qtyStep": float(lot.get("stepSize", "0.001") or 0.001),
        "minOrderQty": float(lot.get("minQty", "0.001") or 0.001),
        "minNotional": float(notional.get("notional") or notional.get("minNotional") or 5),
    }


def _bingx_instrument(symbol):
    resp = requests.get(f"{BASE_URLS['bingx']}/openApi/swap/v2/quote/contracts",
                        params={"symbol": symbol}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0 or not data.get("data"):
        raise RuntimeError(f"BingX no encontró el símbolo {symbol}")
    item = data["data"][0]
    qty_precision = int(item.get("quantityPrecision", 0) or 0)
    max_long = float(item.get("maxLongLeverage", 0) or 0)
    max_short = float(item.get("maxShortLeverage", 0) or 0)
    max_lev = max_long or max_short or BINGX_DEFAULT_MAX_LEVERAGE
    return {
        "symbol": item["symbol"],
        "baseCoin": item.get("asset", ""),
        "quoteCoin": item.get("currency", ""),
        "maxLeverage": max_lev,
        "maxLongLeverage": max_long if max_long else None,
        "maxShortLeverage": max_short if max_short else None,
        "leverageStep": 1.0,
        "qtyStep": float(10 ** (-qty_precision)) if qty_precision > 0 else 1.0,
        "minOrderQty": float(item.get("tradeMinQuantity", 0) or 0),
        "minNotional": float(item.get("tradeMinUSDT", 0) or 0),
    }


def _lbank_instrument(symbol):
    items = _fetch_lbank_instruments()
    for item in items:
        if item.get("symbol") == symbol:
            return {
                "symbol": item["symbol"],
                "baseCoin": item.get("baseCurrency", ""),
                "quoteCoin": item.get("clearCurrency", item.get("priceCurrency", "")),
                "maxLeverage": float(item.get("maxLeverage", 0) or 0),
                "leverageStep": 1.0,
                "qtyStep": float(item.get("volumeTick", "0.001") or 0.001),
                "minOrderQty": float(item.get("minOrderVolume", 0) or 0),
                "minNotional": float(item.get("minOrderCost", 0) or 0),
            }
    raise RuntimeError(f"LBank no encontró el símbolo {symbol}")


_INSTRUMENT_FETCHERS = {
    "bybit": _bybit_instrument,
    "binance": _binance_instrument,
    "bingx": _bingx_instrument,
    "lbank": _lbank_instrument,
}

_SYMBOL_FETCHERS = {
    "bybit": _fetch_bybit_symbols,
    "binance": _fetch_binance_symbols,
    "bingx": _fetch_bingx_symbols,
    "lbank": _fetch_lbank_symbols,
}


# ============================================================
# API FUNCTIONS
# ============================================================
def _normalize_exchange(exchange):
    exchange = (exchange or "").lower()
    if exchange not in EXCHANGES:
        raise ValueError(f"Exchange no soportado: {exchange}")
    return exchange


def get_all_symbols(exchange):
    """Obtiene símbolos USDT perpetuos del exchange indicado (sin fallback)."""
    exchange = _normalize_exchange(exchange)
    now = time.time()
    cached = _cache["symbols"].get(exchange)
    if cached and (now - cached["timestamp"]) < CACHE_TTL:
        return cached["items"]

    logger.info(f"Consultando símbolos de {exchange}...")
    items = _SYMBOL_FETCHERS[exchange]()
    items.sort(key=lambda x: x["symbol"])

    _cache["symbols"][exchange] = {"items": items, "timestamp": now}
    _cache["last_error"][exchange] = None
    logger.info(f"✅ {exchange}: {len(items)} símbolos")
    return items


def get_instrument(exchange, symbol):
    """Obtiene info normalizada del instrumento desde el exchange indicado (sin fallback)."""
    exchange = _normalize_exchange(exchange)
    symbol = (symbol or "").upper()
    key = (exchange, symbol)
    if key in _cache["instruments"]:
        return _cache["instruments"][key]

    data = _INSTRUMENT_FETCHERS[exchange](symbol)
    wrapper = {"source": exchange, "data": data}
    _cache["instruments"][key] = wrapper
    return wrapper


# ============================================================
# CÁLCULO
# ============================================================
def calcular(entry, sl, margen, instrument_wrapper):
    """Calcula leverage y tamaño de posición."""
    inst = instrument_wrapper["data"]

    if entry == sl:
        raise ValueError("El precio de entrada no puede ser igual al SL.")
    if entry <= 0 or sl <= 0 or margen <= 0:
        raise ValueError("Todos los valores deben ser positivos.")

    direccion = "LONG" if sl < entry else "SHORT"

    max_lev = float(inst["maxLeverage"])
    # Leverage específico por dirección (si el exchange lo expone) — BingX no lo trae, usa el genérico.
    if inst.get("maxLongLeverage") and inst.get("maxShortLeverage"):
        if direccion == "LONG":
            max_lev = float(inst["maxLongLeverage"])
        else:
            max_lev = float(inst["maxShortLeverage"])

    lev_step = float(inst.get("leverageStep", 1) or 1)
    qty_step = float(inst["qtyStep"])
    min_order_qty = float(inst["minOrderQty"])
    min_notional = float(inst["minNotional"])

    qty_decimals = max(0, int(-math.log10(qty_step))) if qty_step < 1 else 0

    distancia = abs(entry - sl) / entry
    if distancia == 0:
        raise ValueError("Distancia al SL inválida.")

    leverage_teorico = 0.70 / distancia
    leverage_capped = min(leverage_teorico, max_lev)
    leverage_set = math.floor(leverage_capped / lev_step) * lev_step
    leverage_set = round(leverage_set, 10)

    notional_inicial = margen * leverage_set
    qty_inicial = notional_inicial / entry

    qty_ajustada = math.floor(qty_inicial / qty_step) * qty_step
    qty_ajustada = round(qty_ajustada, qty_decimals)

    if qty_ajustada < min_order_qty:
        raise ValueError(f"La cantidad ajustada ({qty_ajustada}) es menor que el mínimo permitido ({min_order_qty}). Aumenta el margen.")

    notional_real = qty_ajustada * entry

    if min_notional > 0 and notional_real < min_notional:
        raise ValueError(f"El notional ({notional_real:.2f} USDT) es menor que el mínimo permitido ({min_notional} USDT). Aumenta el margen.")

    leverage_necesario = notional_real / margen
    leverage_final = round(leverage_necesario / lev_step) * lev_step
    leverage_final = round(leverage_final, 10)

    leverage_limitado = False
    margen_usado = margen

    if leverage_final > max_lev:
        leverage_limitado = True
        leverage_final = max_lev
        notional_max = margen * max_lev
        qty_max = notional_max / entry
        qty_ajustada = math.floor(qty_max / qty_step) * qty_step
        qty_ajustada = round(qty_ajustada, qty_decimals)
        notional_real = qty_ajustada * entry

        if qty_ajustada < min_order_qty:
            raise ValueError(f"La cantidad ajustada ({qty_ajustada}) es menor que el mínimo permitido ({min_order_qty}). Aumenta el margen.")
        if min_notional > 0 and notional_real < min_notional:
            raise ValueError(f"El notional ({notional_real:.2f} USDT) es menor que el mínimo permitido ({min_notional} USDT). Aumenta el margen.")

        margen_usado = notional_real / leverage_final

    riesgo_real = notional_real * distancia

    return {
        "direccion": direccion,
        "leverage_teorico": round(leverage_teorico, 2),
        "leverage_final": leverage_final,
        "max_leverage_simbolo": max_lev,
        "leverage_limitado": leverage_limitado,
        "qty": qty_ajustada,
        "notional_usdt": round(notional_real, 2),
        "margen_usado": round(margen_usado, 2),
        "margen_solicitado": margen,
        "riesgo_real_usdt": round(riesgo_real, 2),
        "distancia_sl_pct": round(distancia * 100, 4),
        "min_notional": min_notional,
        "qty_step": qty_step,
        "data_source": instrument_wrapper["source"].upper()
    }


# ============================================================
# RUTAS
# ============================================================
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/symbols")
def api_symbols():
    exchange = request.args.get("exchange", "binance")
    try:
        symbols = get_all_symbols(exchange)
        return jsonify({"ok": True, "exchange": exchange, "symbols": symbols, "count": len(symbols)})
    except Exception as e:
        logger.error(f"Error en /api/symbols ({exchange}): {e}", exc_info=True)
        return jsonify({"ok": False, "exchange": exchange, "error": str(e)}), 500


@app.route("/api/instrument/<symbol>")
def api_instrument(symbol):
    exchange = request.args.get("exchange", "binance")
    try:
        wrapper = get_instrument(exchange, symbol)
        inst = wrapper["data"]
        return jsonify({
            "ok": True,
            "info": {
                "symbol": inst["symbol"],
                "baseCoin": inst["baseCoin"],
                "quoteCoin": inst["quoteCoin"],
                "maxLeverage": float(inst["maxLeverage"]),
                "qtyStep": float(inst["qtyStep"]),
                "minOrderQty": float(inst["minOrderQty"]),
                "minNotional": float(inst["minNotional"]),
                "source": wrapper["source"]
            }
        })
    except Exception as e:
        logger.error(f"Error en /api/instrument ({exchange}): {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/calculate", methods=["POST"])
def api_calculate():
    try:
        data = request.get_json()
        exchange = data.get("exchange", "binance")
        symbol = (data.get("symbol") or "").upper()
        entry = float(data.get("entry", 0))
        sl = float(data.get("sl", 0))
        margen = float(data.get("margen", 0))

        if entry <= 0 or sl <= 0 or margen <= 0:
            return jsonify({"ok": False, "error": "Todos los valores deben ser positivos."}), 400

        wrapper = get_instrument(exchange, symbol)
        resultado = calcular(entry, sl, margen, wrapper)
        resultado["symbol"] = symbol
        resultado["exchange"] = exchange
        return jsonify({"ok": True, **resultado})

    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error en /api/calculate: {e}", exc_info=True)
        return jsonify({"ok": False, "error": f"Error: {str(e)}"}), 500


# ============================================================
# HTML TEMPLATE
# ============================================================
HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#1a1a2e">
    <title>Futures Calculator</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/select2@4.1.0-rc.0/dist/css/select2.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/select2-bootstrap-5-theme@1.3.0/dist/select2-bootstrap-5-theme.min.css" rel="stylesheet">
    <style>
        * { box-sizing: border-box; }
        body { background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%); color: #e4e4e4; min-height: 100vh; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 0; }
        .container { padding: 15px; }
        @media (min-width: 768px) { .container { padding: 30px; } }
        .card-custom { background: rgba(255, 255, 255, 0.05); border: 1px solid rgba(255, 255, 255, 0.1); backdrop-filter: blur(10px); border-radius: 16px; padding: 20px; }
        @media (min-width: 768px) { .card-custom { padding: 30px; } }
        .form-control, .select2-container--bootstrap-5 .select2-selection { background: rgba(0, 0, 0, 0.3) !important; border: 1px solid rgba(255, 255, 255, 0.15) !important; color: #fff !important; font-size: 16px !important; min-height: 48px; }
        .form-control:focus { border-color: #f7a600 !important; box-shadow: 0 0 0 0.2rem rgba(247, 166, 0, 0.25) !important; }
        .form-label { font-size: 14px; font-weight: 500; margin-bottom: 8px; color: #ccc; }
        .btn-calc { background: linear-gradient(135deg, #f7a600 0%, #f57600 100%); border: none; color: #000; font-weight: 600; padding: 14px; border-radius: 10px; font-size: 16px; min-height: 48px; width: 100%; }
        .result-box { background: rgba(0, 0, 0, 0.3); border-radius: 12px; padding: 15px; margin-top: 20px; }
        .result-item { display: flex; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 1px solid rgba(255, 255, 255, 0.05); flex-wrap: wrap; gap: 8px; }
        .result-item:last-child { border-bottom: none; }
        .result-label { color: #aaa; font-size: 13px; flex: 1; min-width: 120px; }
        .result-value { color: #f7a600; font-weight: 600; font-family: "SF Mono", Monaco, monospace; text-align: right; font-size: 14px; }
        .badge-long { background: #00c076; color: #fff; }
        .badge-short { background: #f6465d; color: #fff; }
        .badge-warn { background: #f7a600; color: #000; }
        .badge-ok { background: #00c076; color: #fff; }
        .badge-info { background: #0d6efd; color: #fff; font-size: 10px; }
        h1 { color: #f7a600; font-size: 24px; margin-bottom: 10px; }
        @media (min-width: 768px) { h1 { font-size: 36px; } }
        .select2-dropdown { background: #1a1a2e !important; border: 1px solid rgba(255, 255, 255, 0.15) !important; }
        .select2-results__option { color: #e4e4e4 !important; padding: 12px !important; }
        .select2-results__option--highlighted { background: #f7a600 !important; color: #000 !important; }
        .select2-search__field { background: #16213e !important; color: #fff !important; font-size: 16px !important; }
        .info-symbol { font-size: 12px; color: #aaa; background: rgba(0,0,0,0.2); padding: 12px; border-radius: 8px; margin-top: 10px; }
        .highlight-margen { background: rgba(0, 192, 118, 0.1); border-left: 3px solid #00c076; padding: 12px; margin-bottom: 15px; border-radius: 6px; }
        .alert-danger { background: rgba(246, 70, 93, 0.2); border: 1px solid #f6465d; color: #f6465d; padding: 12px; border-radius: 8px; }
        .status-bar { background: rgba(0,0,0,0.3); padding: 8px 12px; border-radius: 8px; font-size: 12px; margin-bottom: 15px; word-break: break-word; }
        .status-ok { border-left: 3px solid #00c076; }
        .status-error { border-left: 3px solid #f6465d; }
        .status-loading { border-left: 3px solid #f7a600; }
        .exchange-group { display: flex; gap: 8px; background: rgba(0, 0, 0, 0.25); padding: 6px; border-radius: 12px; }
        .exchange-btn { flex: 1; padding: 12px; border-radius: 9px; border: 1px solid rgba(255, 255, 255, 0.1); background: transparent; color: #aaa; font-weight: 600; font-size: 15px; cursor: pointer; transition: all 0.2s; }
        .exchange-btn.active { background: linear-gradient(135deg, #f7a600 0%, #f57600 100%); color: #000; border-color: transparent; }
        .exchange-btn:not(.active):hover { color: #fff; border-color: rgba(247, 166, 0, 0.5); }
        .btn-fav { width: 100%; margin-top: 10px; padding: 10px; border-radius: 8px; border: 1px solid rgba(255, 255, 255, 0.15); background: rgba(0, 0, 0, 0.2); color: #ccc; font-weight: 600; font-size: 14px; cursor: pointer; transition: all 0.2s; }
        .btn-fav:hover { color: #fff; border-color: rgba(247, 166, 0, 0.5); }
        .btn-fav.active { color: #f7a600; border-color: #f7a600; background: rgba(247, 166, 0, 0.12); }
    </style>
</head>
<body>
    <div class="container">
        <div class="row justify-content-center">
            <div class="col-12 col-lg-8 col-xl-6">
                <div class="text-center mb-4">
                    <h1 class="fw-bold">⚡ Futures Calculator</h1>
                    <p class="text-muted mb-0">Calcula leverage y tamaño de posición</p>
                </div>

                <div id="statusBar" class="status-bar status-loading">
                    🔍 Cargando símbolos...
                </div>

                <div class="card-custom">
                    <div class="mb-3">
                        <label class="form-label">Exchange</label>
                        <div class="exchange-group" id="exchangeGroup">
                            <button type="button" class="exchange-btn active" data-exchange="bybit">Bybit</button>
                            <button type="button" class="exchange-btn" data-exchange="binance">Binance</button>
                            <button type="button" class="exchange-btn" data-exchange="bingx">BingX</button>
                            <button type="button" class="exchange-btn" data-exchange="lbank">LBank</button>
                        </div>
                    </div>

                    <form id="calcForm">
                        <div class="mb-3">
                            <label class="form-label" for="symbol">Símbolo</label>
                            <select id="symbol" class="form-select" style="width: 100%;" required>
                                <option></option>
                            </select>
                            <div id="symbolInfo" class="info-symbol d-none"></div>
                            <button type="button" id="btnFav" class="btn-fav" style="display:none;">☆ Marcar como favorito</button>
                        </div>

                        <div class="row g-3">
                            <div class="col-12 col-md-4">
                                <label class="form-label" for="entry">Precio de Entrada</label>
                                <input type="number" id="entry" class="form-control" step="any" required placeholder="Ej: 50000" inputmode="decimal">
                            </div>
                            <div class="col-12 col-md-4">
                                <label class="form-label" for="sl">Stop Loss</label>
                                <input type="number" id="sl" class="form-control" step="any" required placeholder="Ej: 48500" inputmode="decimal">
                            </div>
                            <div class="col-12 col-md-4">
                                <label class="form-label" for="margen">Margen (USDT)</label>
                                <input type="number" id="margen" class="form-control" step="any" required placeholder="Ej: 150" inputmode="decimal">
                            </div>
                        </div>

                        <button type="submit" class="btn btn-calc mt-4" id="btnCalc">Calcular</button>
                    </form>

                    <div id="result" class="result-box d-none"></div>
                    <div id="error" class="alert alert-danger mt-3 d-none"></div>
                </div>
            </div>
        </div>
    </div>

    <script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/select2@4.1.0-rc.0/dist/js/select2.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>

    <script>
        let currentInstrument = null;
        let currentExchange = 'bybit';
        let symbolData = [];

        const EXCHANGE_NAMES = { bybit: 'Bybit', binance: 'Binance', bingx: 'BingX', lbank: 'LBank' };
        const FAV_KEY = 'fut_calc_favs';

        // ---------- Favoritos (localStorage, por exchange) ----------
        function getFavs() {
            try { return JSON.parse(localStorage.getItem(FAV_KEY) || '{}'); } catch (e) { return {}; }
        }
        function saveFavs(f) { localStorage.setItem(FAV_KEY, JSON.stringify(f)); }
        function isFav(symbol) { return (getFavs()[currentExchange] || []).indexOf(symbol) !== -1; }
        function toggleFav(symbol) {
            const f = getFavs();
            let list = f[currentExchange] || [];
            if (list.indexOf(symbol) === -1) list.push(symbol);
            else list = list.filter(s => s !== symbol);
            f[currentExchange] = list;
            saveFavs(f);
        }

        function setStatus(type, message) {
            const bar = $('#statusBar');
            bar.removeClass('status-ok status-error status-loading').addClass('status-' + type).html(message);
        }

        function resetCalc() {
            $('#result').addClass('d-none');
            $('#error').addClass('d-none');
            $('#symbolInfo').addClass('d-none');
        }

        function updateFavButton() {
            const sym = $('#symbol').val();
            const btn = $('#btnFav');
            if (!sym) { btn.hide(); return; }
            btn.show();
            const fav = isFav(sym);
            btn.text(fav ? '★ Favorito — quitar' : '☆ Marcar como favorito');
            btn.toggleClass('active', fav);
        }

        function renderSymbols(restoreVal) {
            const select = $('#symbol');
            const saved = (restoreVal !== undefined) ? restoreVal : select.val();
            const favs = new Set(getFavs()[currentExchange] || []);
            const favList = symbolData.filter(s => favs.has(s.symbol));
            const restList = symbolData.filter(s => !favs.has(s.symbol));

            select.select2('destroy');
            select.empty();
            select.append($('<option value=""></option>'));

            if (favList.length) {
                const g = $('<optgroup label="⭐ Favoritos"></optgroup>');
                favList.forEach(s => g.append(new Option(s.symbol + ' (' + s.baseCoin + '/' + s.quoteCoin + ')', s.symbol)));
                select.append(g);
            }
            if (restList.length) {
                const g = $('<optgroup label="Todos"></optgroup>');
                restList.forEach(s => g.append(new Option(s.symbol + ' (' + s.baseCoin + '/' + s.quoteCoin + ')', s.symbol)));
                select.append(g);
            }

            select.select2({ theme: 'bootstrap-5', placeholder: 'Busca un símbolo', allowClear: true, width: '100%' });

            if (saved && symbolData.some(s => s.symbol === saved)) {
                select.val(saved).trigger('change');
            } else {
                updateFavButton();
            }
        }

        async function loadSymbols() {
            setStatus('loading', '🔍 Cargando símbolos de ' + EXCHANGE_NAMES[currentExchange] + '...');
            symbolData = [];
            currentInstrument = null;
            resetCalc();
            try {
                const resp = await fetch('/api/symbols?exchange=' + currentExchange);
                const data = await resp.json();
                if (!data.ok) throw new Error(data.error || 'Error desconocido');
                symbolData = data.symbols;
                renderSymbols('');
                const favCount = (getFavs()[currentExchange] || []).length;
                setStatus('ok', '✅ ' + EXCHANGE_NAMES[currentExchange] + ' | ' + data.count + ' símbolos disponibles' + (favCount ? ' | ⭐ ' + favCount + ' favoritos' : ''));
            } catch (e) {
                console.error('Error cargando símbolos:', e);
                setStatus('error', '❌ ' + EXCHANGE_NAMES[currentExchange] + ': ' + e.message);
            }
        }

        $(document).ready(function() {
            $('#symbol').select2({
                theme: 'bootstrap-5',
                placeholder: 'Busca un símbolo',
                allowClear: true,
                width: '100%'
            });

            $('#exchangeGroup .exchange-btn').on('click', function() {
                const ex = $(this).data('exchange');
                if (ex === currentExchange) return;
                currentExchange = ex;
                $('#exchangeGroup .exchange-btn').removeClass('active');
                $(this).addClass('active');
                loadSymbols();
            });

            loadSymbols();

            $('#btnFav').on('click', function() {
                const sym = $('#symbol').val();
                if (!sym) return;
                toggleFav(sym);
                renderSymbols(sym);
            });

            $('#symbol').on('change', async function() {
                updateFavButton();
                const symbol = $(this).val();
                if (!symbol) {
                    $('#symbolInfo').addClass('d-none');
                    currentInstrument = null;
                    return;
                }
                try {
                    const resp = await fetch('/api/instrument/' + symbol + '?exchange=' + currentExchange);
                    const data = await resp.json();
                    if (data.ok) {
                        currentInstrument = data.info;
                        const info = data.info;
                        const sourceBadge = '<span class="badge badge-info">' + (EXCHANGE_NAMES[info.source] || info.source) + '</span>';
                        $('#symbolInfo').html(
                            '<strong>' + info.symbol + '</strong> ' + sourceBadge + '<br>' +
                            'Max Leverage: <span class="text-warning">' + info.maxLeverage + 'x</span> | ' +
                            'Qty Step: ' + info.qtyStep + ' | ' +
                            'Min Notional: ' + info.minNotional + ' USDT'
                        ).removeClass('d-none');
                    }
                } catch (e) {
                    console.error(e);
                }
            });

            $('#calcForm').on('submit', async function(e) {
                e.preventDefault();
                const btn = $('#btnCalc');
                const resultBox = $('#result');
                const errorBox = $('#error');

                resultBox.addClass('d-none');
                errorBox.addClass('d-none');
                btn.prop('disabled', true).text('Calculando...');

                try {
                    const payload = {
                        exchange: currentExchange,
                        symbol: $('#symbol').val(),
                        entry: parseFloat($('#entry').val()),
                        sl: parseFloat($('#sl').val()),
                        margen: parseFloat($('#margen').val())
                    };

                    if (!payload.symbol) throw new Error('Selecciona un símbolo');

                    const resp = await fetch('/api/calculate', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(payload)
                    });
                    const data = await resp.json();

                    if (!data.ok) throw new Error(data.error);

                    const dirBadge = data.direccion === 'LONG' ? '<span class="badge badge-long">LONG</span>' : '<span class="badge badge-short">SHORT</span>';
                    const warnLev = data.leverage_teorico > data.max_leverage_simbolo ? '<span class="badge badge-warn ms-2">Limitado</span>' : '';
                    const margenBadge = data.leverage_limitado ? '<span class="badge badge-warn">⚠ Limitado</span>' : '<span class="badge badge-ok">✓ Exacto</span>';
                    const sourceBadge = '<span class="badge badge-info">' + (EXCHANGE_NAMES[data.exchange] || data.exchange) + '</span>';

                    resultBox.html(
                        '<h5 class="mb-3">' + dirBadge + ' ' + data.symbol + ' ' + warnLev + ' ' + sourceBadge + '</h5>' +
                        '<div class="highlight-margen">' +
                            '<div class="result-item"><span class="result-label">Margen solicitado</span><span class="result-value">' + data.margen_solicitado.toFixed(2) + ' USDT</span></div>' +
                            '<div class="result-item"><span class="result-label">Margen usado</span><span class="result-value">' + data.margen_usado.toFixed(2) + ' USDT ' + margenBadge + '</span></div>' +
                        '</div>' +
                        '<div class="result-item"><span class="result-label">Leverage teórico</span><span class="result-value">' + data.leverage_teorico + 'x</span></div>' +
                        '<div class="result-item"><span class="result-label">Leverage aplicado</span><span class="result-value">' + data.leverage_final + 'x</span></div>' +
                        '<div class="result-item"><span class="result-label">Cantidad</span><span class="result-value">' + data.qty + ' ' + (currentInstrument?.baseCoin || '') + '</span></div>' +
                        '<div class="result-item"><span class="result-label">Notional</span><span class="result-value">' + data.notional_usdt + ' USDT</span></div>' +
                        '<div class="result-item"><span class="result-label">Riesgo real (SL)</span><span class="result-value">' + data.riesgo_real_usdt + ' USDT</span></div>' +
                        '<div class="result-item"><span class="result-label">Distancia al SL</span><span class="result-value">' + data.distancia_sl_pct + '%</span></div>'
                    ).removeClass('d-none');

                } catch (err) {
                    errorBox.text(err.message).removeClass('d-none');
                } finally {
                    btn.prop('disabled', false).text('Calcular');
                }
            });
        });
    </script>
</body>
</html>'''

if __name__ == "__main__":
    print("=" * 60)
    print("  Futures Calculator (Bybit / Binance / BingX)")
    print("  http://localhost:8000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=8000, debug=True)
