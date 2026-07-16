"""
export_json.py

Roda o calculo completo de GEX para SPX, SPY, NDX e QQQ, ajusta os niveis
proporcionalmente para ES=F e NQ=F, e exporta tudo em um unico arquivo JSON
estruturado -- pronto para ser publicado num repositorio GitHub (via Actions)
e consumido por um indicador NinjaScript (ou qualquer outro consumidor HTTP).

Uso:
    python export_json.py
    (gera o arquivo data/gex_levels.json)

Esquema do JSON gerado:
{
  "updated_at_utc": "2026-07-15T20:05:00+00:00",
  "ES": {
    "index_based": { "source": "SPX", "spot": ..., "call_wall": ..., "put_wall": ...,
                      "gamma_flip": ..., "net_gex_total": ..., "regime": ... },
    "etf_based":   { "source": "SPY", ... }
  },
  "NQ": {
    "index_based": { "source": "NDX", ... },
    "etf_based":   { "source": "QQQ", ... }
  }
}

Se algum ativo falhar (erro de rede, sem 0DTE, etc.), o campo correspondente
fica com "error": "<mensagem>" em vez dos niveis, para o indicador poder tratar
graciosamente sem quebrar.
"""

import json
import os
from datetime import datetime, timezone

import es_nq_adjust as adj


OUTPUT_PATH = os.path.join("data", "gex_levels.json")

# Quais fontes alimentam qual futuro, e como rotular ("index_based" vs "etf_based")
SOURCES = {
    "ES": {"index_based": "SPX", "etf_based": "SPY"},
    "NQ": {"index_based": "NDX", "etf_based": "QQQ"},
}


def build_entry(symbol: str) -> dict:
    """Roda o calculo ajustado para um simbolo e retorna um dict pronto pro JSON.
    Em caso de erro, retorna um dict com a chave 'error' em vez de quebrar tudo."""
    try:
        result = adj.run_adjusted(symbol)
        if result is None:
            return {"source": symbol, "error": "Sem contratos 0DTE ou dados insuficientes no momento."}

        gamma_flip = result["gamma_flip"]
        entry = {
            "source": symbol,
            "underlying_spot": round(result["underlying_spot"], 4),
            "futures_price": round(result["futures_price"], 4),
            "call_wall": round(result["call_wall"], 2),
            "put_wall": round(result["put_wall"], 2),
            "gamma_flip": round(gamma_flip, 2) if gamma_flip is not None else None,
            "net_gex_total": round(result["net_gex_total"], 0),
            "regime": result["regime"],
        }
        if gamma_flip is None:
            entry["gamma_flip_note"] = result.get("gamma_flip_note")
        return entry
    except Exception as e:
        return {"source": symbol, "error": str(e)}


def build_json() -> dict:
    output = {
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }

    for futures_symbol, sources in SOURCES.items():
        output[futures_symbol] = {}
        for label, underlying_symbol in sources.items():
            print(f"\n--- Processando {futures_symbol} / {label} ({underlying_symbol}) ---")
            output[futures_symbol][label] = build_entry(underlying_symbol)

    return output


def save_json(data: dict, path: str = OUTPUT_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\nJSON salvo em: {os.path.abspath(path)}")


if __name__ == "__main__":
    data = build_json()
    save_json(data)
    print("\n===== JSON FINAL =====")
    print(json.dumps(data, indent=2, ensure_ascii=False))
