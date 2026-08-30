"""
Bybit Perpetual Futures Calculator
Aplicación web responsiva en un solo archivo.
Ejecutar: python app.py
Abrir: http://localhost:5000

Lógica de cálculo:
1. Calcula leverage teórico = 0.70 / distancia_SL
2. Limita al maxLeverage del símbolo
3. Ajusta qty al qtyStep del símbolo
4. Recalcula el leverage para usar EXACTAMENTE el margen indicado
5. Si el leverage necesario excede el máximo, limita y recalcula qty
"""

from flask import Flask, render_template_string, request, jsonify
import requests
import time
import math

app = Flask(__name__)

# ============================================================
# CONFIGURACIÓN
# ============================================================
BYBIT_API = "https://api.bybit.com"
CACHE_TTL = 3600  # 1 hora

# Caché simple en memoria
_cache = {
    "symbols": None,
    "timestamp": 0,
    "instruments": {}
}


# ============================================================
# API BYBIT
# ============================================================
def get_all_symbols():
    """Obtiene todos los símbolos de futuros lineales (USDT perpetuos)."""
    now = time.time()
    if _cache["symbols"] and (now - _cache["timestamp"]) < CACHE_TTL:
        return _cache["symbols"]

    symbols = []
    cursor = ""
    while True:
        params = {"category": "linear", "limit": "1000"}
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(
            f"{BYBIT_API}/v5/market/instruments-info",
            params=params,
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("retCode") != 0:
            raise RuntimeError(f"Bybit error: {data.get('retMsg')}")

        for item in data["result"]["list"]:
            symbols.append({
                "symbol": item["symbol"],
                "baseCoin": item["baseCoin"],
                "quoteCoin": item["quoteCoin"]
            })

        cursor = data["result"].get("nextPageCursor", "")
        if not cursor:
            break

    symbols.sort(key=lambda x: x["symbol"])
    _cache["symbols"] = symbols
    _cache["timestamp"] = now
    return symbols


def get_instrument(symbol):
    """Obtiene la info completa de un instrumento."""
    if symbol in _cache["instruments"]:
        return _cache["instruments"][symbol]

    resp = requests.get(
        f"{BYBIT_API}/v5/market/instruments-info",
        params={"category": "linear", "symbol": symbol},
        timeout=10
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit error: {data.get('retMsg')}")

    items = data["result"]["list"]
    if not items:
        return None

    _cache["instruments"][symbol] = items[0]
    return items[0]


# ============================================================
# CÁLCULO
# ============================================================
def calcular(entry, sl, margen, instrument):
    """
    Calcula leverage y tamaño de posición garantizando usar EXACTAMENTE
    el margen indicado.
    
    Flujo:
    1. Leverage teórico = 0.70 / distancia (SL = liquidación al -70%)
    2. Limitar al maxLeverage del símbolo
    3. Calcular qty inicial y ajustar al qtyStep
    4. Recalcular leverage = notional_real / margen (para usar margen exacto)
    5. Si leverage recalculado > maxLeverage: limitar y recalcular qty
    """
    # Filtros del instrumento
    lev_filter = instrument["leverageFilter"]
    lot_filter = instrument["lotSizeFilter"]

    max_lev = float(lev_filter["maxLeverage"])
    qty_step = float(lot_filter["qtyStep"])
    min_order_qty = float(lot_filter["minOrderQty"])
    min_notional = float(
        lot_filter.get("minNotionalValue")
        or lot_filter.get("minOrderAmt")
        or 0
    )

    # Leverage step
    lev_step_str = lev_filter.get("leverageStep", "1")
    try:
        lev_step = float(lev_step_str)
    except (ValueError, TypeError):
        lev_step = 1.0

    # Precisión de qty
    qty_decimals = max(0, int(-math.log10(qty_step))) if qty_step < 1 else 0

    # Validaciones
    if entry == sl:
        raise ValueError("El precio de entrada no puede ser igual al SL.")
    if entry <= 0 or sl <= 0 or margen <= 0:
        raise ValueError("Todos los valores deben ser positivos.")

    # 1. Distancia al SL
    distancia = abs(entry - sl) / entry
    if distancia == 0:
        raise ValueError("Distancia al SL inválida.")

    # 2. Leverage teórico (SL = liquidación al -70%)
    leverage_teorico = 0.70 / distancia

    # 3. Limitar al máximo del símbolo
    leverage_capped = min(leverage_teorico, max_lev)

    # 4. Redondear al step (floor conservador)
    leverage_set = math.floor(leverage_capped / lev_step) * lev_step
    leverage_set = round(leverage_set, 10)

    # 5. Tamaño de posición inicial
    notional_inicial = margen * leverage_set
    qty_inicial = notional_inicial / entry

    # 6. Ajustar qty al step (floor para no exceder margen)
    qty_ajustada = math.floor(qty_inicial / qty_step) * qty_step
    qty_ajustada = round(qty_ajustada, qty_decimals)

    # Validar mínimo de cantidad
    if qty_ajustada < min_order_qty:
        raise ValueError(
            f"La cantidad ajustada ({qty_ajustada}) es menor que el mínimo "
            f"permitido ({min_order_qty}). Aumenta el margen."
        )

    notional_real = qty_ajustada * entry

    # Validar mínimo notional
    if min_notional > 0 and notional_real < min_notional:
        raise ValueError(
            f"El notional ({notional_real:.2f} USDT) es menor que el mínimo "
            f"permitido ({min_notional} USDT). Aumenta el margen."
        )

    # 7. RECALCULAR LEVERAGE para usar EXACTAMENTE el margen indicado
    leverage_necesario = notional_real / margen

    # Ajustar al step más cercano (round para minimizar error)
    leverage_final = round(leverage_necesario / lev_step) * lev_step
    leverage_final = round(leverage_final, 10)

    # Flag: ¿el leverage necesario excede el máximo?
    leverage_limitado = False
    margen_usado = margen

    if leverage_final > max_lev:
        # Limitar al máximo y recalcular qty
        leverage_limitado = True
        leverage_final = max_lev
        notional_max = margen * max_lev
        qty_max = notional_max / entry
        qty_ajustada = math.floor(qty_max / qty_step) * qty_step
        qty_ajustada = round(qty_ajustada, qty_decimals)
        notional_real = qty_ajustada * entry

        # Validar nuevamente mínimos
        if qty_ajustada < min_order_qty:
            raise ValueError(
                f"La cantidad ajustada ({qty_ajustada}) es menor que el mínimo "
                f"permitido ({min_order_qty}). Aumenta el margen."
            )
        if min_notional > 0 and notional_real < min_notional:
            raise ValueError(
                f"El notional ({notional_real:.2f} USDT) es menor que el mínimo "
                f"permitido ({min_notional} USDT). Aumenta el margen."
            )

        # El margen usado será menor al indicado (limitado por maxLeverage)
        margen_usado = notional_real / leverage_final

    # 8. Riesgo real
    riesgo_real = notional_real * distancia

    # 9. Dirección
    direccion = "LONG" if sl < entry else "SHORT"

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
        "qty_step": qty_step
    }


# ============================================================
# RUTAS
# ============================================================
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/symbols")
def api_symbols():
    try:
        symbols = get_all_symbols()
        return jsonify({"ok": True, "symbols": symbols})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/instrument/<symbol>")
def api_instrument(symbol):
    try:
        inst = get_instrument(symbol.upper())
        if not inst:
            return jsonify({"ok": False, "error": "Símbolo no encontrado"}), 404

        lev_filter = inst["leverageFilter"]
        lot_filter = inst["lotSizeFilter"]

        return jsonify({
            "ok": True,
            "info": {
                "symbol": inst["symbol"],
                "baseCoin": inst["baseCoin"],
                "quoteCoin": inst["quoteCoin"],
                "maxLeverage": float(lev_filter["maxLeverage"]),
                "leverageStep": lev_filter.get("leverageStep", "1"),
                "qtyStep": float(lot_filter["qtyStep"]),
                "minOrderQty": float(lot_filter["minOrderQty"]),
                "minNotional": float(lot_filter.get("minNotionalValue")
                                     or lot_filter.get("minOrderAmt") or 0)
            }
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/calculate", methods=["POST"])
def api_calculate():
    try:
        data = request.get_json()
        symbol = (data.get("symbol") or "").upper()
        entry = float(data.get("entry", 0))
        sl = float(data.get("sl", 0))
        margen = float(data.get("margen", 0))

        if entry <= 0 or sl <= 0 or margen <= 0:
            return jsonify({"ok": False, "error": "Todos los valores deben ser positivos."}), 400

        inst = get_instrument(symbol)
        if not inst:
            return jsonify({"ok": False, "error": f"Símbolo {symbol} no encontrado."}), 404

        resultado = calcular(entry, sl, margen, inst)
        resultado["symbol"] = symbol
        return jsonify({"ok": True, **resultado})

    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error: {str(e)}"}), 500


# ============================================================
# HTML TEMPLATE RESPONSIVE
# ============================================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <meta name="theme-color" content="#1a1a2e">
    <title>Bybit Futures Calculator</title>

    <!-- Bootstrap 5 -->
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <!-- Select2 -->
    <link href="https://cdn.jsdelivr.net/npm/select2@4.1.0-rc.0/dist/css/select2.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/select2-bootstrap-5-theme@1.3.0/dist/select2-bootstrap-5-theme.min.css" rel="stylesheet">

    <style>
        * {
            box-sizing: border-box;
        }

        body {
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: #e4e4e4;
            min-height: 100vh;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            margin: 0;
            padding: 0;
            -webkit-text-size-adjust: 100%;
        }

        .container {
            padding: 15px;
        }

        @media (min-width: 768px) {
            .container {
                padding: 30px;
            }
        }

        .card-custom {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 16px;
            padding: 20px;
        }

        @media (min-width: 768px) {
            .card-custom {
                padding: 30px;
            }
        }

        .form-control, .select2-container--bootstrap-5 .select2-selection {
            background: rgba(0, 0, 0, 0.3) !important;
            border: 1px solid rgba(255, 255, 255, 0.15) !important;
            color: #fff !important;
            font-size: 16px !important; /* Evita zoom en iOS */
            min-height: 48px; /* Touch-friendly */
        }

        .form-control:focus {
            border-color: #f7a600 !important;
            box-shadow: 0 0 0 0.2rem rgba(247, 166, 0, 0.25) !important;
        }

        .form-label {
            font-size: 14px;
            font-weight: 500;
            margin-bottom: 8px;
            color: #ccc;
        }

        @media (min-width: 768px) {
            .form-label {
                font-size: 15px;
            }
        }

        .btn-calc {
            background: linear-gradient(135deg, #f7a600 0%, #f57600 100%);
            border: none;
            color: #000;
            font-weight: 600;
            padding: 14px;
            border-radius: 10px;
            transition: all 0.2s;
            font-size: 16px;
            min-height: 48px;
            width: 100%;
        }

        .btn-calc:hover {
            transform: translateY(-1px);
            box-shadow: 0 4px 20px rgba(247, 166, 0, 0.4);
            color: #000;
        }

        .btn-calc:active {
            transform: translateY(0);
        }

        .result-box {
            background: rgba(0, 0, 0, 0.3);
            border-radius: 12px;
            padding: 15px;
            margin-top: 20px;
        }

        @media (min-width: 768px) {
            .result-box {
                padding: 20px;
            }
        }

        .result-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            flex-wrap: wrap;
            gap: 8px;
        }

        .result-item:last-child { border-bottom: none; }

        .result-label { 
            color: #aaa; 
            font-size: 13px;
            flex: 1;
            min-width: 120px;
        }

        @media (min-width: 768px) {
            .result-label {
                font-size: 14px;
            }
        }

        .result-value { 
            color: #f7a600; 
            font-weight: 600; 
            font-family: "SF Mono", Monaco, monospace;
            text-align: right;
            font-size: 14px;
            word-break: break-word;
        }

        @media (min-width: 768px) {
            .result-value {
                font-size: 15px;
            }
        }

        .badge-long { background: #00c076; color: #fff; }
        .badge-short { background: #f6465d; color: #fff; }
        .badge-warn { background: #f7a600; color: #000; }
        .badge-ok { background: #00c076; color: #fff; }

        h1 { 
            color: #f7a600; 
            font-size: 24px;
            margin-bottom: 10px;
        }

        @media (min-width: 768px) {
            h1 {
                font-size: 36px;
            }
        }

        .text-muted {
            font-size: 14px;
        }

        .select2-dropdown {
            background: #1a1a2e !important;
            border: 1px solid rgba(255, 255, 255, 0.15) !important;
        }

        .select2-results__option {
            color: #e4e4e4 !important;
            padding: 12px !important;
            font-size: 14px;
        }

        .select2-results__option--highlighted {
            background: #f7a600 !important;
            color: #000 !important;
        }

        .select2-search__field {
            background: #16213e !important;
            color: #fff !important;
            border: 1px solid rgba(255, 255, 255, 0.15) !important;
            font-size: 16px !important;
            padding: 12px !important;
        }

        .info-symbol {
            font-size: 12px;
            color: #aaa;
            background: rgba(0,0,0,0.2);
            padding: 12px;
            border-radius: 8px;
            margin-top: 10px;
            line-height: 1.5;
        }

        @media (min-width: 768px) {
            .info-symbol {
                font-size: 13px;
            }
        }

        .highlight-margen {
            background: rgba(0, 192, 118, 0.1);
            border-left: 3px solid #00c076;
            padding: 12px;
            margin-top: 10px;
            margin-bottom: 15px;
            border-radius: 6px;
        }

        .alert-danger {
            background: rgba(246, 70, 93, 0.2);
            border: 1px solid #f6465d;
            color: #f6465d;
            font-size: 14px;
            padding: 12px;
            border-radius: 8px;
        }

        /* Mobile optimizations */
        @media (max-width: 576px) {
            .row.g-3 > * {
                margin-bottom: 0 !important;
            }

            .col-md-4 {
                margin-bottom: 15px !important;
            }

            .badge {
                font-size: 11px;
                padding: 4px 8px;
            }

            .result-item {
                padding: 10px 0;
            }
        }

        /* Prevent zoom on iOS */
        @supports (-webkit-touch-callout: none) {
            .form-control {
                font-size: 16px !important;
            }
        }

        /* Loading state */
        .loading {
            opacity: 0.5;
            pointer-events: none;
        }

        /* Smooth transitions */
        * {
            -webkit-tap-highlight-color: transparent;
        }

        button, input, select {
            transition: all 0.2s ease;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="row justify-content-center">
            <div class="col-12 col-lg-8 col-xl-6">
                <div class="text-center mb-4">
                    <h1 class="fw-bold">⚡ Bybit Futures Calculator</h1>
                    <p class="text-muted mb-0">Calcula leverage y tamaño de posición para futuros perpetuos</p>
                </div>

                <div class="card-custom">
                    <form id="calcForm">
                        <div class="mb-3">
                            <label class="form-label" for="symbol">Símbolo</label>
                            <select id="symbol" class="form-select" style="width: 100%;" required>
                                <option></option>
                            </select>
                            <div id="symbolInfo" class="info-symbol d-none"></div>
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

                        <button type="submit" class="btn btn-calc mt-4" id="btnCalc">
                            Calcular
                        </button>
                    </form>

                    <div id="result" class="result-box d-none"></div>
                    <div id="error" class="alert ale
