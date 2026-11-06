"""Teto diário e kill switch — é demo pública com chave paga atrás.

O desenho: consumo
ANTES da chamada paga, estado em disco, fronteira de dia em UTC, kill switch por
env var, e **recusa** (nunca reset) se o estado estiver corrompido ou ingravável.
Falhar aberto aqui significa uma fatura aberta.

🔄 A unidade passou de TURNO para DINHEIRO (micro-dólares).

Contar turnos media a coisa errada. Um turno que roda seis iterações custa seis
vezes um que roda uma, e o contador não enxergava a diferença: o teto dizia
"400 turnos" e o que ele realmente protegia era um valor entre alguns centavos e
alguns dólares, dependendo de como as conversas do dia caíram. Um teto que não
sabe quanto está segurando não é um teto, é uma esperança.

O desenho é **reserva e reconcilia**: reserva-se uma
estimativa antes da chamada (porque o custo real só existe depois dela) e
ajusta-se pela diferença quando o `usage` volta. A reserva precisa ser maior que
a mediana, senão o teto vaza; e não pode ser o pior caso absoluto, senão a demo
recusa visitante com o orçamento quase inteiro sobrando. O excedente possível é
limitado pelo número de turnos em voo, que o lock de sessão já mantém em um por
visitante.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .pricing import MICROS_POR_USD

CAMINHO = Path(os.getenv("BUDGET_PATH", "/data/budget.json"))

# Versão do formato do arquivo de estado. O formato anterior guardava contagem
# de turnos no mesmo campo `usado`; lido como micro-dólares daria um saldo
# absurdamente baixo e o teto do dia passaria batido. Formato desconhecido
# recomeça o dia — que é migração declarada, e não o "reset em cima de estado
# corrompido" que o resto deste módulo existe para impedir.
FORMATO = 2


def _micros_do_env(nome: str, padrao: str) -> int:
    return round(float(os.getenv(nome, padrao)) * MICROS_POR_USD)


TETO_DIARIO = _micros_do_env("DAILY_BUDGET_USD", "3.00")
"""Quanto esta demo pode gastar por dia UTC, no total, somando todo mundo."""

RESERVA_POR_TURNO = _micros_do_env("TURN_RESERVE_USD", "0.05")
"""Estimativa retida antes de cada turno, devolvida na reconciliação."""

KILL_SWITCH = os.getenv("DEMO_KILL_SWITCH", "") == "1"

_lock = threading.Lock()


class Bloqueado(Exception):
    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo


def _hoje() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _novo() -> dict:
    return {"v": FORMATO, "dia": _hoje(), "usado": 0}


def _ler() -> dict:
    if not CAMINHO.exists():
        return _novo()
    try:
        dados = json.loads(CAMINHO.read_text("utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # Estado ilegível não vira "começar do zero": isso zeraria o teto a
        # cada corrupção, que é exatamente o que um abusador provocaria.
        raise Bloqueado("estado_corrompido") from exc
    if dados.get("v") != FORMATO or dados.get("dia") != _hoje():
        return _novo()
    return dados


def _gravar(estado: dict) -> None:
    try:
        CAMINHO.parent.mkdir(parents=True, exist_ok=True)
        tmp = CAMINHO.with_suffix(".tmp")
        tmp.write_text(json.dumps(estado), "utf-8")
        os.replace(tmp, CAMINHO)
    except OSError as exc:
        raise Bloqueado("estado_ingravavel") from exc


def reservar(micros: int | None = None) -> int:
    """Retém `micros` do orçamento do dia. Levanta Bloqueado se não couber.

    Devolve quanto sobrou depois da reserva.
    """
    micros = RESERVA_POR_TURNO if micros is None else micros
    if KILL_SWITCH:
        raise Bloqueado("kill_switch")

    with _lock:
        estado = _ler()
        if estado["usado"] + micros > TETO_DIARIO:
            raise Bloqueado("teto_diario")
        estado["usado"] += micros
        _gravar(estado)
        return TETO_DIARIO - estado["usado"]


def devolver(micros: int | None = None) -> None:
    """Refund quando a falha é nossa — o visitante não paga pelo nosso erro."""
    micros = RESERVA_POR_TURNO if micros is None else micros
    _ajustar(-micros)


def reconciliar(reservado: int, real: int) -> None:
    """Troca a estimativa pelo custo que aconteceu de verdade.

    Chamada depois do turno, com o `usage` já somado. Se o turno saiu mais
    barato que a reserva — o caso comum, porque a reserva é folgada — a
    diferença volta para o orçamento do dia na hora, e não no fim do dia.
    """
    _ajustar(real - reservado)


def _ajustar(delta: int) -> None:
    if delta == 0:
        return
    with _lock:
        try:
            estado = _ler()
        except Bloqueado:
            # Estado ilegível já bloqueia toda reserva nova. Não há o que
            # ajustar, e reescrever aqui apagaria a evidência da corrupção.
            return
        estado["usado"] = max(0, estado["usado"] + delta)
        try:
            _gravar(estado)
        except Bloqueado:
            # Disco read-only na devolução não pode derrubar a resposta que já
            # foi entregue ao visitante. A reserva a mais fica no dia e o teto
            # erra para o lado seguro.
            pass


def restante() -> int | None:
    """Micro-dólares que ainda cabem hoje. None se o estado está bloqueado."""
    try:
        return TETO_DIARIO - _ler()["usado"]
    except Bloqueado:
        return None


def usado() -> int | None:
    try:
        return _ler()["usado"]
    except Bloqueado:
        return None


__all__ = [
    "Bloqueado",
    "KILL_SWITCH",
    "RESERVA_POR_TURNO",
    "TETO_DIARIO",
    "devolver",
    "reconciliar",
    "reservar",
    "restante",
    "usado",
]
