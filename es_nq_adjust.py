"""
es_nq_adjust.py

Pega os niveis de GEX calculados em cima de um ativo "proxy" (SPX, SPY, NDX, QQQ)
e ajusta proporcionalmente para a escala de preco do futuro CME correspondente (ES ou NQ).

Logica do ajuste:
    nivel_no_futuro = nivel_calculado * (preco_atual_do_futuro / preco_do_ativo_usado_no_calculo)

Isso preserva a distancia proporcional entre o nivel e o preco a vista, funcionando tanto
para indices (SPX/NDX, proporcao ~1:1 com o futuro) quanto para ETFs (SPY/QQQ, proporcao ~10:1),
sem precisar fixar essa proporcao manualmente -- ela e' calculada com o preco real do momento.

Fonte do preco do futuro: Yahoo Finance (gratuita, sem necessidade de API key).

IMPORTANTE: assim como o gex_calculator.py, este script precisa rodar em um ambiente
com acesso de rede a query1.finance.yahoo.com -- nao testado ao vivo no sandbox de
desenvolvimento (sem acesso a esse dominio). Rode localmente para validar.
"""

import requests
from datetime import datetime, timezone

import gex_calculator as gc


YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Mapeamento de qual futuro cada ativo proxy deve ser ajustado para
FUTURES_MAP = {
    "SPX": "ES=F",
    "SPY": "ES=F",
    "NDX": "NQ=F",
    "QQQ": "NQ=F",
}


def fetch_futures_price(futures_symbol: str) -> float:
    """Busca o preco atual (regularMarketPrice) de um futuro via Yahoo Finance."""
    url = YAHOO_CHART_URL.format(symbol=futures_symbol)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; GEXResearchBot/1.0)"
    }
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    result = data.get("chart", {}).get("result")
    if not result:
        raise ValueError(f"Nao encontrei dados de preco para {futures_symbol}. Payload: {data}")

    meta = result[0].get("meta", {})
    price = meta.get("regularMarketPrice")
    if price is None:
        raise ValueError(f"Campo 'regularMarketPrice' nao encontrado para {futures_symbol}.")

    return float(price)


def adjust_level(level: float, ratio: float) -> float:
    """Aplica a razao de ajuste a um nivel de preco."""
    return level * ratio


def run_adjusted(underlying_symbol: str):
    """
    Roda o calculo de GEX completo para o ativo proxy (ex: SPX) e ajusta os
    niveis resultantes (call wall, put wall, gamma flip) para a escala do
    futuro correspondente (ES ou NQ), usando a razao de precos do momento.
    """
    futures_symbol = FUTURES_MAP.get(underlying_symbol.upper())
    if futures_symbol is None:
        raise ValueError(f"Nao sei para qual futuro ajustar o simbolo {underlying_symbol}.")

    print(f"\n=== Calculando GEX para {underlying_symbol}, ajustando para {futures_symbol} ===")

    gex_result = gc.run(underlying_symbol)
    if gex_result is None:
        print(f"Nao foi possivel calcular GEX para {underlying_symbol} (sem contratos 0DTE ou dados insuficientes).")
        return None

    underlying_spot = gex_result["spot"]
    futures_price = fetch_futures_price(futures_symbol)
    ratio = futures_price / underlying_spot

    print(f"\nPreco {underlying_symbol}: {underlying_spot}")
    print(f"Preco {futures_symbol}: {futures_price}")
    print(f"Razao de ajuste: {ratio:.6f}")

    call_wall_adj = adjust_level(gex_result["call_wall"], ratio)
    put_wall_adj = adjust_level(gex_result["put_wall"], ratio)
    gamma_flip_adj = (
        adjust_level(gex_result["gamma_flip"], ratio)
        if gex_result["gamma_flip"] is not None
        else None
    )

    print(f"\n--- Niveis ajustados para {futures_symbol} ---")
    print(f"Call Wall: {call_wall_adj:.2f}")
    print(f"Put Wall:  {put_wall_adj:.2f}")
    if gamma_flip_adj is not None:
        print(f"Gamma Flip: {gamma_flip_adj:.2f}")
    else:
        print(f"Gamma Flip: {gex_result['gamma_flip_note']}")
    print(f"Regime: {gex_result['regime']}")

    return {
        "underlying_symbol": underlying_symbol,
        "futures_symbol": futures_symbol,
        "underlying_spot": underlying_spot,
        "futures_price": futures_price,
        "ratio": ratio,
        "call_wall": call_wall_adj,
        "put_wall": put_wall_adj,
        "gamma_flip": gamma_flip_adj,
        "gamma_flip_note": gex_result.get("gamma_flip_note"),
        "net_gex_total": gex_result["net_gex_total"],
        "regime": gex_result["regime"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        symbols_to_run = [sys.argv[1].upper()]
    else:
        # Roda os 4 de uma vez por padrao, para comparar indice vs ETF
        symbols_to_run = ["SPX", "SPY", "NDX", "QQQ"]

    results = []
    for sym in symbols_to_run:
        try:
            r = run_adjusted(sym)
            if r:
                results.append(r)
        except Exception as e:
            print(f"\nERRO ao processar {sym}: {e}")

    if len(results) > 1:
        print("\n\n===== COMPARACAO FINAL =====")
        for r in results:
            gf = f"{r['gamma_flip']:.2f}" if r['gamma_flip'] is not None else "fora da faixa"
            print(
                f"{r['underlying_symbol']:>4} -> {r['futures_symbol']:<5} | "
                f"Call Wall: {r['call_wall']:>10.2f} | "
                f"Put Wall: {r['put_wall']:>10.2f} | "
                f"Gamma Flip: {gf:>10} | "
                f"Regime: {r['regime']}"
            )
