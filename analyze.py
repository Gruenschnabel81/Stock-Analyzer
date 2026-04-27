"""
Aktien-Analyse Tool
-------------------
Liest Ticker aus tickers.txt, berechnet RSI/MACD/SMA90
und schreibt das Ergebnis nach public/data.json.

Verbesserungen gegenüber Excel-Logik:
  T = RSI-3-Perioden-Durchschnitt < 40  (war: 2 Perioden → erzeugte 0.5-Werte)
  V = MACD kreuzte Signal von unten     (war: MACD < 0 → träges Signal)
  W = MACD-Histogramm steigt            (gleiches Konzept, aber Histogramm ist präziser)
  X = Kurs < SMA90                      (unverändert)
  Score = T + V + W + X  →  ganzzahlig 0–4
"""

import yfinance as yf
import pandas as pd
import json
import os
from datetime import datetime, timezone

TICKERS_FILE = "tickers.txt"
OUTPUT_FILE  = "public/data.json"

# ---------------------------------------------------------------------------
# Technische Indikatoren
# ---------------------------------------------------------------------------

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calc_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))

def calc_macd(series):
    ema12     = calc_ema(series, 12)
    ema26     = calc_ema(series, 26)
    macd      = ema12 - ema26
    signal    = calc_ema(macd, 9)
    histogram = macd - signal
    return macd, signal, histogram

def calc_sma(series, period):
    return series.rolling(window=period).mean()

# ---------------------------------------------------------------------------
# Verbesserte Scoring-Logik
# ---------------------------------------------------------------------------

def score_row(df, idx):
    """Berechnet Score für eine einzelne Zeile idx (>=4)."""
    if idx < 4:
        return None, {}

    r0 = df.iloc[idx]
    r1 = df.iloc[idx - 1]
    r2 = df.iloc[idx - 2]

    # T — RSI 3-Perioden-Durchschnitt < 40
    rsi_avg3 = (r0["rsi"] + r1["rsi"] + r2["rsi"]) / 3
    T = 1 if rsi_avg3 < 40 else 0

    # V — MACD kreuzte Signal-Linie von unten (in letzten 2 Bars)
    cross_now  = (r0["macd"] > r0["signal"]) and (r1["macd"] <= r1["signal"])
    cross_prev = (r1["macd"] > r1["signal"]) and (r2["macd"] <= r2["signal"])
    V = 1 if (cross_now or cross_prev) else 0

    # W — MACD-Histogramm steigt (Momentum verbessert sich)
    W = 1 if r0["histogram"] > r1["histogram"] else 0

    # X — Kurs unterhalb SMA90 (möglicher Einstiegsbereich)
    X = 1 if (pd.notna(r0["sma90"]) and r0["close"] < r0["sma90"]) else 0

    # Z — Volumen über 20-Tage-Durchschnitt (Kaufinteresse bestätigt)
    Z = 1 if (pd.notna(r0["vol_sma20"]) and r0["volume"] > r0["vol_sma20"]) else 0

    score   = T + V + W + X + Z
    signals = {
        "rsi_oversold":        T,
        "macd_crossover":      V,
        "histogram_improving": W,
        "below_sma90":         X,
        "volume_above_avg":    Z,
    }
    return score, signals

def calc_all_scores(df):
    """Berechnet Scores für alle Zeilen (für History-Sparkline)."""
    scores = []
    for i in range(len(df)):
        s, _ = score_row(df, i)
        scores.append(s)
    return scores

# ---------------------------------------------------------------------------
# Einzelnen Ticker analysieren
# ---------------------------------------------------------------------------

def analyze_ticker(ticker):
    try:
        raw = yf.download(ticker, period="9mo", interval="1d",
                          progress=False, auto_adjust=True)
        if raw.empty or len(raw) < 35:
            print("zu wenig Daten")
            return None

        df = raw[["Close", "Volume"]].copy()
        df.columns = ["close", "volume"]
        df["rsi"]       = calc_rsi(df["close"])
        macd, sig, hist = calc_macd(df["close"])
        df["macd"]      = macd
        df["signal"]    = sig
        df["histogram"] = hist
        df["sma90"]     = calc_sma(df["close"], 90)
        df["vol_sma20"] = calc_sma(df["volume"], 20)

        df = df.dropna(subset=["rsi", "macd", "signal"]).copy()
        if len(df) < 5:
            print("nach dropna zu wenig Daten")
            return None

        # Aktueller Score + Signale
        last_idx        = len(df) - 1
        score, signals  = score_row(df, last_idx)

        r0          = df.iloc[-1]
        r1          = df.iloc[-2]
        change_pct  = (r0["close"] - r1["close"]) / r1["close"] * 100

        # Letzte 60 Tage als History (für Sparkline)
        all_scores  = calc_all_scores(df)
        history_raw = all_scores[-60:]
        history     = [s for s in history_raw if s is not None]

        return {
            "ticker":     ticker,
            "price":      round(float(r0["close"]), 2),
            "change_pct": round(float(change_pct), 3),
            "score":      score,  # 0-5
            "rsi":        round(float(r0["rsi"]), 1),
            "rsi_avg3":   round(float((df["rsi"].iloc[-1] + df["rsi"].iloc[-2] + df["rsi"].iloc[-3]) / 3), 1),
            "macd":       round(float(r0["macd"]), 4),
            "signal":     round(float(r0["signal"]), 4),
            "histogram":  round(float(r0["histogram"]), 4),
            "sma90":      round(float(r0["sma90"]), 2) if pd.notna(r0["sma90"]) else None,
            "signals":    signals,
            "history":    history,
        }

    except Exception as e:
        print(f"Fehler: {e}")
        return None

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_tickers():
    tickers = []
    with open(TICKERS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                symbol, name = line.split("|", 1)
                tickers.append((symbol.strip(), name.strip()))
            else:
                tickers.append((line, line))
    return tickers

def main():
    tickers = load_tickers()
    print(f"Analysiere {len(tickers)} Titel...\n")

    results = []
    for symbol, name in tickers:
        print(f"  {symbol:20s}", end=" ", flush=True)
        data = analyze_ticker(symbol)
        if data:
            data["name"] = name
            results.append(data)
            bar  = "█" * data["score"] + "░" * (4 - data["score"])
            print(f"[{bar}] {data['score']}/4  RSI={data['rsi']}  {data['price']}")
        else:
            print("— übersprungen")

    # Nach Score absteigend sortieren
    results.sort(key=lambda x: x["score"], reverse=True)

    output = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count":   len(results),
        "stocks":  results,
    }

    os.makedirs("public", exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nFertig. {len(results)} Titel gespeichert → {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
