"""Teto diário e kill switch.

A regra que mais importa aqui é a que parece paranoia: estado ilegível
**recusa**, não reseta. Um teto que zera sempre que o arquivo corrompe é um
teto que um abusador desliga de propósito.

A unidade é micro-dólar, não turno. Ver o cabeçalho de `src/budget.py` para o
porquê da troca; o resumo é que um turno de seis iterações custa seis vezes um
de uma iteração, e o contador antigo não via diferença entre os dois.
"""

from __future__ import annotations

import importlib
import json

import pytest

from src.pricing import MICROS_POR_USD


def carregar(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("BUDGET_PATH", str(tmp_path / "budget.json"))
    # US$ 0,03 de teto e US$ 0,01 de reserva: três turnos cabem no dia.
    monkeypatch.setenv("DAILY_BUDGET_USD", env.get("teto", "0.03"))
    monkeypatch.setenv("TURN_RESERVE_USD", env.get("reserva", "0.01"))
    monkeypatch.setenv("DEMO_KILL_SWITCH", env.get("kill", "0"))
    import src.budget as mod

    return importlib.reload(mod)


def test_reserva_ate_o_teto_e_recusa(monkeypatch, tmp_path):
    budget = carregar(monkeypatch, tmp_path)
    for _ in range(3):
        budget.reservar()
    with pytest.raises(budget.Bloqueado) as exc:
        budget.reservar()
    assert exc.value.motivo == "teto_diario"


def test_kill_switch_recusa_tudo_na_hora(monkeypatch, tmp_path):
    budget = carregar(monkeypatch, tmp_path, kill="1")
    with pytest.raises(budget.Bloqueado) as exc:
        budget.reservar()
    assert exc.value.motivo == "kill_switch"


def test_refund_devolve_a_reserva(monkeypatch, tmp_path):
    budget = carregar(monkeypatch, tmp_path)
    budget.reservar()
    budget.reservar()
    budget.devolver()
    assert budget.restante() == round(0.02 * MICROS_POR_USD)


def test_reconciliar_devolve_o_que_o_turno_nao_gastou(monkeypatch, tmp_path):
    """O caso comum: a reserva é folgada de propósito, e a folga volta na hora.

    Sem isto o dia seria consumido pela ESTIMATIVA, não pelo gasto — e a demo
    recusaria visitante com o orçamento quase inteiro ainda disponível.
    """
    budget = carregar(monkeypatch, tmp_path)
    reserva = budget.RESERVA_POR_TURNO
    budget.reservar(reserva)
    assert budget.usado() == reserva

    budget.reconciliar(reserva, 1_200)  # o turno custou US$ 0,0012
    assert budget.usado() == 1_200


def test_reconciliar_cobra_o_turno_que_passou_da_estimativa(monkeypatch, tmp_path):
    """Estourar a reserva debita a diferença, não é perdoado.

    É o turno de seis iterações que o contador de turnos cobrava como um.
    """
    budget = carregar(monkeypatch, tmp_path)
    reserva = budget.RESERVA_POR_TURNO
    budget.reservar(reserva)
    budget.reconciliar(reserva, reserva * 3)
    assert budget.usado() == reserva * 3


def test_turno_que_nao_gastou_nada_nao_debita(monkeypatch, tmp_path):
    budget = carregar(monkeypatch, tmp_path)
    reserva = budget.RESERVA_POR_TURNO
    budget.reservar(reserva)
    budget.reconciliar(reserva, 0)
    assert budget.usado() == 0


def test_estado_corrompido_recusa_em_vez_de_resetar(monkeypatch, tmp_path):
    budget = carregar(monkeypatch, tmp_path)
    budget.reservar()
    (tmp_path / "budget.json").write_text("{isto nao e json", "utf-8")

    with pytest.raises(budget.Bloqueado) as exc:
        budget.reservar()
    assert exc.value.motivo == "estado_corrompido"


def test_virada_de_dia_zera(monkeypatch, tmp_path):
    budget = carregar(monkeypatch, tmp_path)
    budget.reservar()
    caminho = tmp_path / "budget.json"
    caminho.write_text(
        json.dumps({"v": budget.FORMATO, "dia": "2000-01-01", "usado": 99_000}), "utf-8"
    )
    assert budget.restante() == budget.TETO_DIARIO


def test_estado_do_formato_antigo_recomeca_o_dia(monkeypatch, tmp_path):
    """Migração declarada, não reset em cima de corrupção.

    O formato v1 guardava CONTAGEM DE TURNOS no mesmo campo `usado`. Lido como
    micro-dólar, um dia inteiro de tráfego viraria menos de um centavo e o teto
    passaria batido — silenciosamente, que é o pior jeito.
    """
    budget = carregar(monkeypatch, tmp_path)
    (tmp_path / "budget.json").write_text(
        json.dumps({"dia": budget._hoje(), "usado": 137}), "utf-8"
    )
    assert budget.restante() == budget.TETO_DIARIO


def test_estado_ingravavel_recusa(monkeypatch, tmp_path):
    """O caso que quase matou voicerag e searcher: /data montado como root."""
    budget = carregar(monkeypatch, tmp_path)
    monkeypatch.setattr(budget.Path, "write_text", _explode)
    with pytest.raises(budget.Bloqueado) as exc:
        budget.reservar()
    assert exc.value.motivo == "estado_ingravavel"


def test_devolucao_com_disco_read_only_nao_derruba_a_resposta(monkeypatch, tmp_path):
    """A resposta já foi entregue; falhar aqui erra para o lado seguro."""
    budget = carregar(monkeypatch, tmp_path)
    budget.reservar()
    monkeypatch.setattr(budget.Path, "write_text", _explode)
    budget.reconciliar(budget.RESERVA_POR_TURNO, 0)  # não levanta


def _explode(*args, **kwargs):
    raise OSError("read-only file system")
