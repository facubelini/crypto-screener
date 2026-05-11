"""
Screener de Criptomonedas - Binance Futures
Detecta señales de SHORT (sobrecompra) y LONG (sobreventa) en tiempo real.
"""
from __future__ import annotations

import os
import threading
import time
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

FUTURES_BASE    = "https://fapi.binance.com"
FUTURES_BACKUP  = "https://fapi1.binance.com"
VALID_INTERVALS = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})

# ── Estado global del screener ─────────────────────────────────────────────────
_state = {
    "status":      "idle",   # idle | running | done | error
    "data":        [],
    "progress":    0,
    "total":       0,
    "interval":    "1h",
    "last_update": None,
    "error":       "",
}
_lock = threading.Lock()


# ── Binance API ────────────────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None, timeout: int = 15):
    """GET con fallback automático al servidor de backup."""
    try:
        r = SESSION.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception:
        backup_url = url.replace(FUTURES_BASE, FUTURES_BACKUP)
        r = SESSION.get(backup_url, params=params, timeout=timeout)
        r.raise_for_status()
        return r


def get_futures_symbols() -> list[str]:
    r = _get(f"{FUTURES_BASE}/fapi/v1/exchangeInfo")
    data = r.json()
    if "symbols" not in data:
        raise RuntimeError(f"Respuesta inesperada de Binance: {str(data)[:200]}")
    return sorted(
        s["symbol"]
        for s in data["symbols"]
        if s["contractType"] == "PERPETUAL"
        and s["quoteAsset"] == "USDT"
        and s["status"] == "TRADING"
    )


def get_klines(symbol: str, interval: str, limit: int = 200) -> pd.DataFrame | None:
    try:
        r = _get(
            f"{FUTURES_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
        )
        cols = ["ts", "open", "high", "low", "close", "volume",
                "close_ts", "qvol", "trades", "tbb", "tbq", "ignore"]
        df = pd.DataFrame(r.json(), columns=cols)
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c])
        return df
    except Exception as e:
        log.debug("Error fetching %s: %s", symbol, e)
        return None


def get_24h_ticker(symbol: str) -> dict:
    try:
        r = requests.get(
            f"{FUTURES_BASE}/fapi/v1/ticker/24hr",
            params={"symbol": symbol},
            timeout=5,
        )
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


# ── Indicadores ────────────────────────────────────────────────────────────────

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    d = series.diff()
    g = d.clip(lower=0).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    l = (-d).clip(lower=0).ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def calc_macd(series: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    fast = series.ewm(span=12, min_periods=12).mean()
    slow = series.ewm(span=26, min_periods=26).mean()
    m    = fast - slow
    s    = m.ewm(span=9, min_periods=9).mean()
    return m, s, m - s


def calc_bollinger(series: pd.Series, period: int = 20, n: float = 2.0):
    mid = series.rolling(period).mean()
    std = series.rolling(period).std()
    return mid + n * std, mid, mid - n * std


def calc_stoch_rsi(rsi_s: pd.Series, period: int = 14, sk: int = 3, sd: int = 3):
    lo    = rsi_s.rolling(period).min()
    hi    = rsi_s.rolling(period).max()
    k_raw = (rsi_s - lo) / (hi - lo + 1e-10) * 100
    k     = k_raw.rolling(sk).mean()
    d     = k.rolling(sd).mean()
    return k, d


def calc_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    pc = close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


# ── Motor de análisis ──────────────────────────────────────────────────────────

def analyze(symbol: str, interval: str) -> dict | None:
    df = get_klines(symbol, interval, limit=200)
    if df is None or len(df) < 60:
        return None

    close = df["close"]
    high  = df["high"]
    low   = df["low"]
    vol   = df["volume"]

    # Indicadores
    rsi_s   = calc_rsi(close)
    rsi_val = float(rsi_s.iloc[-1])
    if np.isnan(rsi_val):
        return None

    sk, sd   = calc_stoch_rsi(rsi_s)
    srsi_k   = float(sk.iloc[-1])
    srsi_d   = float(sd.iloc[-1])

    _, sig_l, hist = calc_macd(close)
    hist_cur = float(hist.iloc[-1])
    hist_prv = float(hist.iloc[-2])

    bb_hi, bb_mid, bb_lo = calc_bollinger(close)
    price    = float(close.iloc[-1])
    bb_range = float(bb_hi.iloc[-1]) - float(bb_lo.iloc[-1])
    bb_pct   = (price - float(bb_lo.iloc[-1])) / (bb_range + 1e-10) * 100

    ema20  = float(close.ewm(span=20,  min_periods=20).mean().iloc[-1])
    ema50  = float(close.ewm(span=50,  min_periods=50).mean().iloc[-1])
    ema200 = float(close.ewm(span=200, min_periods=200).mean().iloc[-1])

    atr_val   = float(calc_atr(high, low, close).iloc[-1])
    atr_pct   = atr_val / price * 100 if price > 0 else 0

    vol_avg   = float(vol.rolling(20).mean().iloc[-1])
    vol_ratio = float(vol.iloc[-1]) / vol_avg if vol_avg > 0 else 1.0

    # Cambio de precio en las últimas N velas (≈24h para 1h chart)
    lookback = min(24, len(close) - 1)
    chg_24h  = (price - float(close.iloc[-lookback])) / float(close.iloc[-lookback]) * 100

    # ── Puntuación ────────────────────────────────────────────────────────────
    # Rango total: -10 a +10
    # Negativo → SHORT, Positivo → LONG
    score = 0.0
    sigs  = []

    # RSI (peso: 2)
    if rsi_val >= 80:
        score -= 2; sigs.append(f"RSI {rsi_val:.1f} — extremo alto")
    elif rsi_val >= 70:
        score -= 1; sigs.append(f"RSI {rsi_val:.1f} — sobrecompra")
    elif rsi_val <= 20:
        score += 2; sigs.append(f"RSI {rsi_val:.1f} — extremo bajo")
    elif rsi_val <= 30:
        score += 1; sigs.append(f"RSI {rsi_val:.1f} — sobreventa")
    else:
        sigs.append(f"RSI {rsi_val:.1f}")

    # Stochastic RSI (peso: 2)
    if not np.isnan(srsi_k):
        if srsi_k >= 90:
            score -= 2; sigs.append(f"StochRSI {srsi_k:.1f} — extremo alto")
        elif srsi_k >= 80:
            score -= 1; sigs.append(f"StochRSI {srsi_k:.1f} — sobrecompra")
        elif srsi_k <= 10:
            score += 2; sigs.append(f"StochRSI {srsi_k:.1f} — extremo bajo")
        elif srsi_k <= 20:
            score += 1; sigs.append(f"StochRSI {srsi_k:.1f} — sobreventa")
        else:
            sigs.append(f"StochRSI {srsi_k:.1f}")

    # MACD (peso: 2 para cruce, 1 para dirección)
    if not np.isnan(hist_cur) and not np.isnan(hist_prv):
        if hist_cur < 0 and hist_prv >= 0:
            score -= 2; sigs.append("MACD — cruce bajista")
        elif hist_cur > 0 and hist_prv <= 0:
            score += 2; sigs.append("MACD — cruce alcista")
        elif hist_cur < 0 and abs(hist_cur) > abs(hist_prv):
            score -= 1; sigs.append("MACD — histograma empeora")
        elif hist_cur > 0 and hist_cur > hist_prv:
            score += 1; sigs.append("MACD — histograma mejora")
        elif hist_cur < 0:
            score -= 0.5
        else:
            score += 0.5

    # Bandas de Bollinger (peso: 1)
    if not np.isnan(bb_pct):
        if bb_pct > 100:
            score -= 1; sigs.append(f"BB {bb_pct:.0f}% — sobre banda superior")
        elif bb_pct > 90:
            score -= 0.5; sigs.append(f"BB {bb_pct:.0f}% — cerca banda superior")
        elif bb_pct < 0:
            score += 1; sigs.append(f"BB {bb_pct:.0f}% — bajo banda inferior")
        elif bb_pct < 10:
            score += 0.5; sigs.append(f"BB {bb_pct:.0f}% — cerca banda inferior")

    # Alineación de EMAs (peso: 2)
    if not any(np.isnan(v) for v in [ema20, ema50, ema200]):
        if price < ema20 < ema50 < ema200:
            score -= 2; sigs.append("EMA bajista completo (precio<20<50<200)")
        elif price > ema20 > ema50 > ema200:
            score += 2; sigs.append("EMA alcista completo (precio>20>50>200)")
        elif price < ema200 and price < ema50:
            score -= 1; sigs.append("Bajo EMA200 y EMA50")
        elif price > ema200 and price > ema50:
            score += 1; sigs.append("Sobre EMA200 y EMA50")
        elif price < ema200:
            score -= 0.5
        else:
            score += 0.5

    # Confirmación por volumen (amplifica señal existente, peso: 1)
    if vol_ratio >= 2.0:
        if score <= -2:
            score -= 1; sigs.append(f"Vol ×{vol_ratio:.1f} — confirma presión vendedora")
        elif score >= 2:
            score += 1; sigs.append(f"Vol ×{vol_ratio:.1f} — confirma presión compradora")

    # ── Clasificación ──────────────────────────────────────────────────────────
    if score <= -7:
        label, cls = "SHORT EXTREMO", "se"
    elif score <= -4:
        label, cls = "SHORT FUERTE", "sf"
    elif score <= -2:
        label, cls = "SHORT", "sh"
    elif score < 0:
        label, cls = "SHORT DÉBIL", "sw"
    elif score >= 7:
        label, cls = "LONG EXTREMO", "le"
    elif score >= 4:
        label, cls = "LONG FUERTE", "lf"
    elif score >= 2:
        label, cls = "LONG", "lo"
    elif score > 0:
        label, cls = "LONG DÉBIL", "lw"
    else:
        label, cls = "NEUTRAL", "n"

    # Enlace a Binance Futures
    base = symbol.replace("USDT", "")
    link = f"https://www.binance.com/es/futures/{base}USDT"

    return {
        "symbol":    symbol.replace("USDT", "/USDT"),
        "link":      link,
        "price":     price,
        "chg24h":    round(chg_24h, 2),
        "rsi":       round(rsi_val, 1),
        "srsi":      round(srsi_k, 1) if not np.isnan(srsi_k) else 50.0,
        "bb_pct":    round(bb_pct, 1) if not np.isnan(bb_pct) else 50.0,
        "ema_trend": "ALCISTA" if price > ema200 else "BAJISTA",
        "vol_ratio": round(vol_ratio, 2),
        "atr_pct":   round(atr_pct, 2),
        "score":     round(score, 1),
        "signal":    label,
        "cls":       cls,
        "details":   " · ".join(sigs),
    }


# ── Screener en segundo plano ──────────────────────────────────────────────────

def _run_screener(interval: str) -> None:
    with _lock:
        _state.update({"status": "running", "progress": 0, "data": [], "error": ""})

    try:
        log.info("Iniciando screener en intervalo %s", interval)
        symbols = get_futures_symbols()
        total   = len(symbols)
        log.info("Analizando %d símbolos", total)

        with _lock:
            _state["total"] = total

        results = []
        done    = 0

        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = {ex.submit(analyze, s, interval): s for s in symbols}
            for f in as_completed(futs):
                done += 1
                with _lock:
                    _state["progress"] = done
                res = f.result()
                if res:
                    results.append(res)

        results.sort(key=lambda x: x["score"])
        with _lock:
            _state.update({
                "status":      "done",
                "data":        results,
                "interval":    interval,
                "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        log.info("Screener completado: %d resultados", len(results))

    except Exception as e:
        msg = repr(e) if str(e) == "" else str(e)
        log.exception("Error en screener: %s", msg)
        with _lock:
            _state.update({"status": "error", "error": msg})


# ── Rutas Flask ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    with _lock:
        return jsonify({
            "status":      _state["status"],
            "progress":    _state["progress"],
            "total":       _state["total"],
            "last_update": _state["last_update"],
            "interval":    _state["interval"],
            "count":       len(_state["data"]),
        })


@app.route("/api/data")
def api_data():
    with _lock:
        return jsonify(_state["data"])


@app.route("/api/test")
def api_test():
    """Diagnóstico: verifica conectividad con Binance."""
    try:
        r = SESSION.get(f"{FUTURES_BASE}/fapi/v1/ping", timeout=10)
        ping_ok = r.status_code == 200
    except Exception as e:
        return jsonify({"ok": False, "error": f"ping falló: {repr(e)}"}), 200

    try:
        r2 = SESSION.get(f"{FUTURES_BASE}/fapi/v1/time", timeout=10)
        time_ok = r2.status_code == 200
    except Exception as e:
        return jsonify({"ok": False, "error": f"time falló: {repr(e)}"}), 200

    return jsonify({"ok": ping_ok and time_ok, "ping": ping_ok, "time": time_ok})


@app.route("/api/start", methods=["POST"])
def api_start():
    payload  = request.get_json(silent=True) or {}
    interval = payload.get("interval", "1h")
    if interval not in VALID_INTERVALS:
        interval = "1h"

    with _lock:
        if _state["status"] == "running":
            return jsonify({"ok": False, "msg": "El screener ya está en ejecución"})

    threading.Thread(target=_run_screener, args=(interval,), daemon=True).start()
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print("\n" + "=" * 60)
    print("  Crypto Screener — Binance Futures")
    print(f"  Abre tu navegador en: http://localhost:{port}")
    print("=" * 60 + "\n")
    app.run(debug=False, host="0.0.0.0", port=port, threaded=True)
