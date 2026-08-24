"""Quanto custou o turno, em dinheiro, com a tarifa que produziu o número.

Por que isto existe separado de `budget.py`: o budget é o *freio*, e responde
"posso gastar mais?". Este módulo é a *régua*, e responde "quanto isto custou?".
Um teto que conta turnos, e não dinheiro, mede a coisa errada — uma conversa que
roda seis iterações custa seis vezes uma que roda uma, e um contador de turnos
não enxerga a diferença.

Tudo em micro-dólares inteiros (1 USD = 1_000_000). Custo de turno vive na casa
de 10^3 a 10^5 micros, então inteiro cobre a faixa com folga e sem o arredonda-
mento de float que estraga um saldo somado milhares de vezes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

MICROS_POR_USD = 1_000_000
_TABELA = Path(__file__).resolve().parent / "pricing.yaml"


def _carregar() -> dict[str, Any]:
    with _TABELA.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@dataclass(frozen=True)
class Custo:
    """O custo de uma chamada, e a tarifa sob a qual ele foi calculado."""

    micros: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    rates_version: str
    promocao_aplicada: bool

    @property
    def usd(self) -> float:
        """Só para exibição. A conta nunca passa por float."""
        return self.micros / MICROS_POR_USD

    @property
    def cache_hit(self) -> bool:
        """Houve leitura de cache neste request.

        É o sinal que denuncia invalidador silencioso: um prefixo que deveria
        cachear e nunca lê é um prefixo que está mudando de bytes sem ninguém
        perceber.
        """
        return self.cache_read_tokens > 0

    def audit_payload(self) -> dict[str, Any]:
        return {
            "usd": round(self.usd, 6),
            "micros": self.micros,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_hit": self.cache_hit,
            "rates_version": self.rates_version,
            "promocao_aplicada": self.promocao_aplicada,
        }


class Tabela:
    """A tabela carregada, com a versão que vai junto em cada custo."""

    def __init__(self, dados: dict[str, Any] | None = None) -> None:
        self._d = dados if dados is not None else _carregar()
        self.version: str = str(self._d["version"])
        self.fonte: str = str(self._d.get("fonte", ""))

    def _tarifa(self, modelo: str, hoje: date) -> tuple[float, float, bool]:
        """Preço de input e output por milhão, e se a promoção pegou.

        Modelo desconhecido é erro, não zero. Um custo que silenciosamente vira
        zero porque o nome do modelo mudou é pior do que não medir: o painel
        continua mostrando um número, e o número está errado para baixo.
        """
        try:
            m = self._d["modelos"][modelo]
        except KeyError as exc:
            raise KeyError(
                f"modelo {modelo!r} não está em pricing.yaml (v{self.version})"
            ) from exc

        promo = m.get("promocao")
        if promo and hoje <= date.fromisoformat(str(promo["ate"])):
            return float(promo["input"]), float(promo["output"]), True
        return float(m["input"]), float(m["output"]), False

    def custo(self, modelo: str, usage: Any, *, hoje: date | None = None) -> Custo:
        """Converte o `usage` de uma resposta da API em dinheiro.

        `input_tokens` já vem líquido do que foi lido ou escrito no cache — as
        três contagens são disjuntas, então somá-las com os multiplicadores
        certos dá o total sem contar nada duas vezes.
        """
        hoje = hoje or date.today()
        p_in, p_out, promo = self._tarifa(modelo, hoje)
        c = self._d["cache"]

        entrada = int(getattr(usage, "input_tokens", 0) or 0)
        saida = int(getattr(usage, "output_tokens", 0) or 0)
        # Ausente quando não há cache na conversa; `or 0` cobre None e 0.
        leu = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        escreveu = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)

        usd = (
            entrada * p_in
            + leu * p_in * float(c["leitura"])
            + escreveu * p_in * float(c["escrita"])
            + saida * p_out
        ) / 1_000_000

        return Custo(
            # Arredonda uma vez só, no fim, já em inteiro.
            micros=round(usd * MICROS_POR_USD),
            input_tokens=entrada,
            output_tokens=saida,
            cache_read_tokens=leu,
            cache_write_tokens=escreveu,
            rates_version=self.version,
            promocao_aplicada=promo,
        )


TABELA = Tabela()


def custo(modelo: str, usage: Any, *, hoje: date | None = None) -> Custo:
    return TABELA.custo(modelo, usage, hoje=hoje)


__all__ = ["Custo", "Tabela", "TABELA", "custo", "MICROS_POR_USD"]
