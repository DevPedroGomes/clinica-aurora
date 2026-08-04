"""Teto diário e kill switch — é demo pública com chave paga atrás.

Mesmo desenho já em produção no scraper, no voicerag e no searcher:
consumo ANTES da chamada paga, estado em disco, fronteira de dia em UTC,
kill switch por env var, e **recusa** (nunca reset) se o estado estiver
corrompido ou ingravável. Falhar aberto aqui significa uma fatura aberta.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

CAMINHO = Path(os.getenv("BUDGET_PATH", "/data/budget.json"))
TETO_DIARIO = int(os.getenv("DAILY_TURN_CAP", "400"))
KILL_SWITCH = os.getenv("DEMO_KILL_SWITCH", "") == "1"

_lock = threading.Lock()


class Bloqueado(Exception):
    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo


def _hoje() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ler() -> dict:
    if not CAMINHO.exists():
        return {"dia": _hoje(), "usado": 0}
    try:
        dados = json.loads(CAMINHO.read_text("utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        # Estado ilegível não vira "começar do zero": isso zeraria o teto a
        # cada corrupção, que é exatamente o que um abusador provocaria.
        raise Bloqueado("estado_corrompido") from exc
    if dados.get("dia") != _hoje():
        return {"dia": _hoje(), "usado": 0}
    return dados


def consumir(n: int = 1) -> int:
    """Reserva `n` turnos. Levanta Bloqueado se não puder."""
    if KILL_SWITCH:
        raise Bloqueado("kill_switch")

    with _lock:
        estado = _ler()
        if estado["usado"] + n > TETO_DIARIO:
            raise Bloqueado("teto_diario")
        estado["usado"] += n
        try:
            CAMINHO.parent.mkdir(parents=True, exist_ok=True)
            tmp = CAMINHO.with_suffix(".tmp")
            tmp.write_text(json.dumps(estado), "utf-8")
            os.replace(tmp, CAMINHO)
        except OSError as exc:
            raise Bloqueado("estado_ingravavel") from exc
        return TETO_DIARIO - estado["usado"]


def devolver(n: int = 1) -> None:
    """Refund quando a falha é nossa — o visitante não paga pelo nosso erro."""
    with _lock:
        try:
            estado = _ler()
        except Bloqueado:
            return
        estado["usado"] = max(0, estado["usado"] - n)
        try:
            CAMINHO.parent.mkdir(parents=True, exist_ok=True)
            tmp = CAMINHO.with_suffix(".tmp")
            tmp.write_text(json.dumps(estado), "utf-8")
            os.replace(tmp, CAMINHO)
        except OSError:
            pass


def restante() -> int | None:
    try:
        return TETO_DIARIO - _ler()["usado"]
    except Bloqueado:
        return None
