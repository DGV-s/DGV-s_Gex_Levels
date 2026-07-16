"""
gex_calculator.py

Baixa a cadeia de opcoes (delayed, gratuita) da CBOE para um simbolo (ex: SPX, QQQ),
filtra os contratos 0DTE (que vencem hoje) e calcula:
  - GEX por strike (call e put)
  - Call Wall (strike com maior GEX positivo)
  - Put Wall (strike com maior GEX negativo, em modulo)
  - Gamma Flip / Zero Gamma (strike onde o GEX acumulado cruza zero)
  - GEX total (regime positivo ou negativo)

Fonte: CBOE delayed quotes (~15min de atraso), gratuita, sem necessidade de API key.
Endpoint: https://cdn.cboe.com/api/global/delayed_quotes/options/_{SYMBOL}.json

IMPORTANTE: este script precisa ser rodado em um ambiente com acesso de rede ao
dominio cdn.cboe.com (o sandbox de desenvolvimento usado para escrever este script
nao tem esse acesso liberado, entao ele foi escrito mas nao testado ao vivo aqui).
Rode localmente ou dentro do GitHub Actions para validar.
"""

import requests
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional


CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{prefix}{symbol}.json"

# Simbolos de INDICE na CBOE usam underscore na URL (ex: _SPX, _NDX, _VIX, _RUT).
# ETFs e acoes normais (QQQ, SPY, AAPL, etc.) NAO usam underscore.
INDEX_SYMBOLS = {"SPX", "NDX", "VIX", "RUT", "DJX", "OEX", "XSP", "SPXW", "NDXP"}

# Multiplicador de contrato padrao (100 acoes/pontos por contrato)
CONTRACT_MULTIPLIER = 100


@dataclass
class OptionContract:
    ticker: str
    root: str
    expiration: str  # formato YYYY-MM-DD
    option_type: str  # "C" ou "P"
    strike: float
    open_interest: float
    volume: float
    iv: Optional[float]
    delta: Optional[float]
    gamma: Optional[float]


@dataclass
class StrikeGEX:
    strike: float
    call_oi: float = 0.0
    put_oi: float = 0.0
    call_gamma: float = 0.0
    put_gamma: float = 0.0
    call_gex: float = 0.0
    put_gex: float = 0.0

    @property
    def net_gex(self) -> float:
        return self.call_gex + self.put_gex  # put_gex ja vem negativo


def parse_occ_ticker(ticker: str):
    """
    Decodifica um ticker estilo OCC da CBOE, ex: 'SPXW260715C05000000'
    Formato: ROOT + YYMMDD + C/P + STRIKE (8 digitos, 3 casas decimais implicitas)
    Retorna (root, expiration 'YYYY-MM-DD', option_type, strike float) ou None se nao bater o padrao.
    """
    match = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", ticker)
    if not match:
        return None
    root, date_str, opt_type, strike_str = match.groups()
    yy, mm, dd = date_str[0:2], date_str[2:4], date_str[4:6]
    year = 2000 + int(yy)
    expiration = f"{year:04d}-{mm}-{dd}"
    strike = int(strike_str) / 1000.0
    return root, expiration, opt_type, strike


def fetch_option_chain(symbol: str) -> dict:
    """Baixa o JSON completo da cadeia de opcoes delayed da CBOE para o simbolo dado.

    Detecta automaticamente se e' um indice (usa prefixo '_' na URL, ex: _SPX)
    ou um ETF/acao normal (sem prefixo, ex: QQQ).
    """
    symbol_upper = symbol.upper()
    prefix = "_" if symbol_upper in INDEX_SYMBOLS else ""
    url = CBOE_URL.format(prefix=prefix, symbol=symbol_upper)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; GEXResearchBot/1.0)"
    }
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def extract_contracts(raw_json: dict) -> tuple[list[OptionContract], float]:
    """
    Extrai a lista de contratos e o preco a vista (spot) do JSON bruto da CBOE.
    Ajuste os nomes de campo aqui se a CBOE mudar o formato do payload.
    """
    data = raw_json.get("data", raw_json)
    spot_price = data.get("current_price") or data.get("close") or data.get("last_trade_price")
    if spot_price is None:
        raise ValueError("Nao encontrei o preco a vista (spot) no payload da CBOE. Verifique o formato do JSON.")

    options_raw = data.get("options", [])
    contracts = []
    for opt in options_raw:
        ticker = opt.get("option") or opt.get("symbol") or opt.get("option_symbol")
        if not ticker:
            continue
        parsed = parse_occ_ticker(ticker)
        if parsed is None:
            continue
        root, expiration, opt_type, strike = parsed

        contracts.append(OptionContract(
            ticker=ticker,
            root=root,
            expiration=expiration,
            option_type=opt_type,
            strike=strike,
            open_interest=float(opt.get("open_interest", 0) or 0),
            volume=float(opt.get("volume", 0) or 0),
            iv=opt.get("iv"),
            delta=opt.get("delta"),
            gamma=opt.get("gamma"),
        ))

    return contracts, float(spot_price)


def filter_0dte(contracts: list[OptionContract], reference_date: Optional[str] = None) -> list[OptionContract]:
    """Filtra apenas os contratos cujo vencimento e' hoje (0DTE)."""
    if reference_date is None:
        # Usa a data em UTC; ajuste para o fuso do pregao (ET) se precisar de mais precisao
        reference_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [c for c in contracts if c.expiration == reference_date]


def calculate_gex(contracts: list[OptionContract], spot_price: float) -> dict[float, StrikeGEX]:
    """
    Calcula o GEX por strike.

    Convencao usada (a mais comum entre provedores tipo SqueezeMetrics/SpotGamma):
      GEX_call(strike) = OI_call * gamma_call * spot^2 * 0.01 * multiplicador   (positivo)
      GEX_put(strike)  = OI_put  * gamma_put  * spot^2 * 0.01 * multiplicador * -1  (negativo)
      Net GEX(strike)  = GEX_call + GEX_put

    Isso assume que dealers estao short calls (hedge comprando quando o preco sobe -> gamma positivo)
    e long puts na perspectiva do dealer (hedge vendendo quando o preco cai -> contribuicao negativa
    ao GEX agregado). Essa e' a convencao de sinal mais usada no mercado, mas existem variantes -
    ajuste aqui se quiser bater com um provedor especifico.
    """
    strikes: dict[float, StrikeGEX] = {}

    for c in contracts:
        if c.gamma is None:
            continue  # sem gamma calculado pela CBOE, pula (raro, mas acontece em contratos ilíquidos)

        if c.strike not in strikes:
            strikes[c.strike] = StrikeGEX(strike=c.strike)

        entry = strikes[c.strike]
        gex_value = c.open_interest * c.gamma * (spot_price ** 2) * 0.01 * CONTRACT_MULTIPLIER

        if c.option_type == "C":
            entry.call_oi += c.open_interest
            entry.call_gamma += c.gamma
            entry.call_gex += gex_value
        else:  # "P"
            entry.put_oi += c.open_interest
            entry.put_gamma += c.gamma
            entry.put_gex += -gex_value  # put contribui negativamente ao net GEX

    return strikes


def find_walls_and_flip(strikes: dict[float, StrikeGEX]):
    """
    Identifica:
      - call_wall: strike com maior GEX de call (isolado, nao net)
      - put_wall: strike com maior |GEX de put| (isolado, nao net)
      - net_gex_total: soma de todos os net GEX (regime positivo/negativo do dia)
      - gamma_flip: strike onde o GEX acumulado (ordenado por strike) cruza zero
    """
    if not strikes:
        return None

    sorted_strikes_all = sorted(strikes.values(), key=lambda s: s.strike)

    # Para call wall / put wall, usamos todos os strikes (zero OI nunca vence de qualquer jeito)
    call_wall = max(sorted_strikes_all, key=lambda s: s.call_gex)
    put_wall = min(sorted_strikes_all, key=lambda s: s.put_gex)  # mais negativo

    net_gex_total = sum(s.net_gex for s in sorted_strikes_all)

    # Para o gamma flip, ignoramos strikes "vazios" (sem OI de call nem de put) --
    # a CBOE lista strikes de preenchimento bem longe do spot que nunca foram negociados,
    # e isso distorce o calculo de onde comeca/termina a faixa real de dados.
    sorted_strikes = [s for s in sorted_strikes_all if (s.call_oi + s.put_oi) > 0]

    # Gamma flip: acumula o net_gex strike a strike (do menor pro maior preco)
    # e acha onde a soma cruza de negativo pra positivo (ou vice-versa)
    cumulative = 0.0
    gamma_flip = None
    gamma_flip_note = None
    prev_strike = None
    prev_cumulative = None
    cumulative_values = []  # guarda (strike, cumulative) pra diagnostico se nao achar cruzamento
    for s in sorted_strikes:
        cumulative += s.net_gex
        cumulative_values.append((s.strike, cumulative))
        if prev_cumulative is not None and (
            (prev_cumulative < 0 <= cumulative) or (prev_cumulative > 0 >= cumulative)
        ):
            # interpolacao linear simples entre os dois strikes pra achar o ponto de cruzamento
            if cumulative != prev_cumulative:
                ratio = -prev_cumulative / (cumulative - prev_cumulative)
                gamma_flip = prev_strike + ratio * (s.strike - prev_strike)
            else:
                gamma_flip = s.strike
            break
        prev_strike = s.strike
        prev_cumulative = cumulative

    if gamma_flip is None and cumulative_values:
        # Nao houve cruzamento dentro da faixa de strikes 0DTE liquidos.
        # Isso so e' matematicamente possivel se a soma acumulada ficou do MESMO lado
        # do zero do inicio ao fim -- entao usamos o sinal do ULTIMO valor acumulado
        # (que sempre bate com net_gex_total) para decidir a mensagem.
        # (Usar o PRIMEIRO strike seria um bug: strikes bem OTM podem ter gamma tao
        # pequeno que a CBOE arredonda para 0.0000, dando um falso sinal positivo/negativo
        # isolado que nao reflete o regime real do dia.)
        first_strike, first_cum = cumulative_values[0]
        last_strike, last_cum = cumulative_values[-1]

        if last_cum >= 0:
            gamma_flip_note = (
                f"Flip abaixo do menor strike com OI real hoje ({first_strike}). "
                f"O regime ja esta positivo mesmo no strike liquido mais baixo da cadeia."
            )
        else:
            gamma_flip_note = (
                f"Flip acima do maior strike com OI real hoje ({last_strike}). "
                f"O regime segue negativo ate o topo da cadeia liquida."
            )

        # Checagem de sanidade: a soma acumulada final DEVE bater com o net_gex_total.
        # Se nao bater, tem um bug de verdade rolando -- avisa em vez de mostrar resultado errado calado.
        if abs(last_cum - net_gex_total) > 1.0:
            gamma_flip_note += (
                f" [AVISO: inconsistencia interna detectada -- soma acumulada ({last_cum:,.0f}) "
                f"diferente do net_gex_total ({net_gex_total:,.0f}). Reporte isso, ha um bug a investigar.]"
            )

    return {
        "call_wall": call_wall,
        "put_wall": put_wall,
        "net_gex_total": net_gex_total,
        "gamma_flip": gamma_flip,
        "gamma_flip_note": gamma_flip_note,
        "regime": "POSITIVO (dealers amortecem movimento)" if net_gex_total > 0 else "NEGATIVO (dealers amplificam movimento)",
    }


def run(symbol: str = "SPX"):
    print(f"Baixando cadeia de opcoes para {symbol}...")
    raw = fetch_option_chain(symbol)

    print("Extraindo contratos e preco a vista...")
    contracts, spot = extract_contracts(raw)
    print(f"Spot price ({symbol}): {spot}")
    print(f"Total de contratos na cadeia: {len(contracts)}")

    dte0 = filter_0dte(contracts)
    print(f"Contratos 0DTE encontrados: {len(dte0)}")

    if not dte0:
        print("Nenhum contrato 0DTE encontrado. Ou o mercado fechou, ou nao ha vencimento hoje para este simbolo.")
        return

    strikes = calculate_gex(dte0, spot)

    liquid_strikes = [s for s in strikes.values() if (s.call_oi + s.put_oi) > 0]
    print(f"Strikes com OI real (nao vazios): {len(liquid_strikes)} de {len(strikes)} strikes totais")

    result = find_walls_and_flip(strikes)

    if result is None:
        print("Nao foi possivel calcular GEX (sem dados de gamma suficientes).")
        return

    # Diagnostico: top 5 strikes por peso de |net GEX|, pra validar visualmente se os
    # numeros fazem sentido (deve concentrar perto do spot, normalmente)
    top5 = sorted(liquid_strikes, key=lambda s: abs(s.net_gex), reverse=True)[:5]
    print("\n--- Top 5 strikes por peso de GEX (diagnostico) ---")
    for s in top5:
        print(f"  Strike {s.strike:>10.1f} | Call OI: {s.call_oi:>8.0f} | Put OI: {s.put_oi:>8.0f} | Net GEX: {s.net_gex:>18,.0f}")

    print("\n===== RESULTADO GEX 0DTE =====")
    print(f"Spot: {spot}")
    print(f"Call Wall: {result['call_wall'].strike}  (GEX: {result['call_wall'].call_gex:,.0f})")
    print(f"Put Wall:  {result['put_wall'].strike}  (GEX: {result['put_wall'].put_gex:,.0f})")
    if result['gamma_flip'] is not None:
        print(f"Gamma Flip (Zero Gamma): {result['gamma_flip']:.2f}")
    else:
        print(f"Gamma Flip (Zero Gamma): fora da faixa de strikes 0DTE disponivel")
        print(f"  -> {result['gamma_flip_note']}")
    print(f"Net GEX Total: {result['net_gex_total']:,.0f}")
    print(f"Regime: {result['regime']}")

    return {
        "symbol": symbol,
        "spot": spot,
        "call_wall": result["call_wall"].strike,
        "put_wall": result["put_wall"].strike,
        "gamma_flip": result["gamma_flip"],
        "gamma_flip_note": result["gamma_flip_note"],
        "net_gex_total": result["net_gex_total"],
        "regime": result["regime"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import sys
    symbol_arg = sys.argv[1] if len(sys.argv) > 1 else "SPX"
    run(symbol_arg)
