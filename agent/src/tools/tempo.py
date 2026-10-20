"""Horário como a clínica entende, e como o gate de confirmação compara.

Dois bugs com a mesma raiz: o horário vinha do modelo como TEXTO e era tratado
como texto.

- A assinatura do gate comparava `novo_inicio` byte a byte. "16:00:00-03:00" e
  "19:00Z" são o mesmo instante, mas viravam operações diferentes, e o "sim" da
  pessoa não liberava a escrita: o gate pedia confirmação de novo.
- Horário sem offset ganhava o fuso de `ctx.at`, que na API é UTC. "16:00" sem
  fuso virava 13:00 em Salvador.

A clínica tem um fuso só, e é o dela.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BAHIA = ZoneInfo("America/Bahia")


def ler_instante(texto: str) -> datetime | None:
    """ISO 8601 do modelo como instante no fuso da clínica.

    Sem offset, o horário é o da clínica. Ilegível devolve None, e quem chama
    responde `invalid_datetime` em vez de adivinhar.
    """
    try:
        instante = datetime.fromisoformat(texto)
    except (TypeError, ValueError):
        return None
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=BAHIA)
    return instante.astimezone(BAHIA)


def instante_canonico(texto: str) -> str:
    """A forma única de um horário, para comparar propostas.

    UTC com precisão de minuto: segundos e formato do offset deixam de criar
    operações "diferentes". Ilegível volta como veio, e aí só bate com ele mesmo.
    """
    instante = ler_instante(texto)
    if instante is None:
        return texto
    return instante.astimezone(timezone.utc).isoformat(timespec="minutes")
