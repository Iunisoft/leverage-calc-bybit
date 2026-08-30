"""
Bybit/Binance Perpetual Futures Calculator
Resiliente a bloqueos de IP en Render.
"""

from flask import Flask, render_template_string, request, jsonify
import requests
import time
import math
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BYBIT_API = "https://api.bybit.com"
BINANCE_API = "https://fapi.binance.com"
CACHE_TTL = 3600

_cache = {
    "symbols": None,
    "timestamp": 0,
    "instruments": {},
    "last_error": None
}

# ============================================================
# API FUNCTIONS
# ============================================================
def get_all_symbols():
    """Obtiene símbolos de futuros USDT perpetuos desde Binance (más permisivo con Render)."""
    now = time.time()
    if _cache["symbols"] and (now - _cache["timestamp"]) < CACHE_TTL:
        return _cache["symbols"]

    logger.info("Consultando API de Binance para obtener símbolos...")
    try:
        resp = requests.get(f"{BINANCE_API}/fapi/v1/exchangeInfo", timeout=15)
        resp.raise_for_status()
        data = resp.json()

        symbols = []
        for item in data.get("symbols", []):
            if item.get("contractType") == "PERPETUAL" and item.get("quoteAsset") == "USDT":
                symbols.append({
                    "symbol": item["symbol"],
                    "baseCoin": item["baseAsset"],
                    "quoteCoin": item["quoteAsset"]
                })

        symbols.sort(key=lambda x: x["symbol"])
        _cache["symbols"] = symbols
        _cache["timestamp"] = now
        _cache["last_error"] = None
        logger.info(f"✅ Total símbolos cargados desde Binance: {len(symbols)}")
        return symbols

    except Exception as e:
        logger.error(f"Error obteniendo símbolos: {e}")
        _cache["last_error"] = str(e)
        raise


def get_instrument(symbol):
    """Obtiene la info del instrumento. Intenta Bybit, si falla (403), usa Binance."""
    if symbol in _cache["instruments"]:
        return _cache["instruments"][symbol]

    # 1. Intentar Bybit primero
    try:
        resp = requests.get(
            f"{BYBIT_API}/v5/market/instruments-info",
            params={"category": "linear", "symbol": symbol},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("retCode") == 0 and data["result"]["list"]:
                _cache["instruments"][symbol] = {"source": "bybit", "data": data["result"]["list"][0]}
                return _cache["instruments"][symbol]
    except Exception as e:
        logger.warning(f"Bybit falló para {symbol}, intentando Binance: {e}")

    # 2. Fallback a Binance
    try:
        logger.info(f"Obteniendo datos de {symbol} desde Binance...")
        resp = requests.get(f"{BINANCE_API}/fapi/v1/exchangeInfo?symbol={symbol}", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        
        if "symbols" in data and len(data["symbols"]) > 0:
            item = data["symbols"][0]
            # Adaptar formato de Binance al de Bybit para que el cálculo funcione
            adapted_data = {
                "symbol": item["symbol"],
                "baseCoin": item["baseAsset"],
                "quoteCoin": item["quoteAsset"],
                "leverageFilter": {"maxLeverage": "125", "leverageStep": "1"}, # Binance permite hasta 125x
                "lotSizeFilter": {
                    "qtyStep": str(item["filters"][0].get("stepSize", "0.001")),
                    "minOrderQty": str(item["filters"][0].get("minQty", "0.001")),
                    "minNotionalValue": str(item["filters"][3].get("notional", "5"))
                }
            }
            _cache["instruments"][symbol] = {"source": "binance", "data": adapted_data}
            return _cache["instruments"][symbol]
    except Exception as e:
        logger.error(f"Error obteniendo de Binance: {e}")
        raise

    raise RuntimeError(f"No se pudo obtener información para {symbol}")


# ============================================================
# CÁLCULO
# ============================================================
def calcular(entry, sl, margen, instrument_wrapper):
    """Calcula leverage y tamaño de posición."""
    source = instrument_wrapper["source"]
    inst = instrument_wrapper["data"]

    if source == "bybit":
        lev_filter = inst["leverageFilter"]
        lot_filter = inst["lotSizeFilter"]
        max_lev = float(lev_filter["maxLeverage"])
        qty_step = float(lot_filter["qtyStep"])
        min_order_qty = float(lot_filter["minOrderQty"])
        min_notional = float(lot_filter.get("minNotionalValue") or lot_filter.get("minOrderAmt") or 5)
        lev_step_str = lev_filter.get("leverageStep", "1")
    else: # binance
        max_lev = float(inst["leverageFilter"]["maxLeverage"])
        qty_step = float(inst["lotSizeFilter"]["qtyStep"])
        min_order_qty = float(inst["lotSizeFilter"]["minOrderQty"])
        min_notional = float(inst["lotSizeFilter"]["minNotionalValue"])
        lev_step_str = "1"

    try:
        lev_step = float(lev_step_str)
    except (ValueError, TypeError):
        lev_step = 1.0

    qty_decimals = max(0, int(-math.log10(qty_step))) if qty_step < 1 else 0

    if entry == sl:
        raise ValueError("El precio de entrada no puede ser igual al SL.")
    if entry <= 0 or sl <= 0 or margen <= 0:
        raise ValueError("Todos los valores deben ser positivos.")

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
        "qty_step": qty_step,
        "data_source": source.upper()
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
        return jsonify({"ok": True, "symbols": symbols, "count": len(symbols)})
    except Exception as e:
        logger.error(f"Error en /api/symbols: {e}", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/instrument/<symbol>")
def api_instrument(symbol):
    try:
        wrapper = get_instrument(symbol.upper())
        inst = wrapper["data"]
        return jsonify({
            "ok": True,
            "info": {
                "symbol": inst["symbol"],
                "baseCoin": inst["baseCoin"],
                "quoteCoin": inst["quoteCoin"],
                "maxLeverage": float(inst["leverageFilter"]["maxLeverage"]),
                "qtyStep": float(inst["lotSizeFilter"]["qtyStep"]),
                "minOrderQty": float(inst["lotSizeFilter"]["minOrderQty"]),
                "minNotional": float(inst["lotSizeFilter"]["minNotionalValue"]),
                "source": wrapper["source"]
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

        wrapper = get_instrument(symbol)
        resultado = calcular(entry, sl, margen, wrapper)
        resultado["symbol"] = symbol
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
    <title>Bybit/Binance Futures Calculator</title>
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
    </style>
</head>
<body>
    <div class="container">
        <div class="row justify-content-center">
            <div class="col-12 col-lg-8 col-xl-6">
                <div class="text-center mb-4">
                    <h1 class="fw-bold">⚡ Futures Calculator</h1>
                    <p class="text-muted mb-0">Calcula leverage y tamaño de posición (Resiliente a bloqueos)</p>
                </div>

                <div id="statusBar" class="status-bar status-loading">
                    🔍 Cargando símbolos...
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

        function setStatus(type, message) {
            const bar = $('#statusBar');
            bar.removeClass('status-ok status-error status-loading').addClass('status-' + type).html(message);
        }

        $(document).ready(function() {
            $('#symbol').select2({
                theme: 'bootstrap-5',
                placeholder: 'Busca un símbolo (ej: BTCUSDT)',
                allowClear: true,
                width: '100%'
            });

            loadSymbols();

            $('#symbol').on('change', async function() {
                const symbol = $(this).val();
                if (!symbol) {
                    $('#symbolInfo').addClass('d-none');
                    currentInstrument = null;
                    return;
                }
                try {
                    const resp = await fetch('/api/instrument/' + symbol);
                    const data = await resp.json();
                    if (data.ok) {
                        currentInstrument = data.info;
                        const info = data.info;
                        const sourceBadge = info.source === 'binance' ? '<span class="badge badge-info">Datos de Binance (Bybit bloqueado)</span>' : '<span class="badge badge-ok">Bybit</span>';
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
                    const sourceBadge = data.data_source === 'BINANCE' ? '<span class="badge badge-info">Usando datos de Binance</span>' : '';

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

        async function loadSymbols() {
            try {
                setStatus('loading', '🔍 Cargando símbolos...');
                const resp = await fetch('/api/symbols');
                const data = await resp.json();

                if (!data.ok) throw new Error(data.error || 'Error desconocido');

                const select = $('#symbol');
                data.symbols.forEach(s => {
                    select.append(new Option(s.symbol + ' (' + s.baseCoin + '/' + s.quoteCoin + ')', s.symbol));
                });

                setStatus('ok', '✅ Conectado | ' + data.count + ' símbolos disponibles');
            } catch (e) {
                console.error('Error cargando símbolos:', e);
                setStatus('error', '❌ Error: ' + e.message);
            }
        }
    </script>
</body>
</html>'''

if __name__ == "__main__":
    print("=" * 60)
    print("  Futures Calculator (Render-Resilient)")
    print("  http://localhost:5000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=True)
