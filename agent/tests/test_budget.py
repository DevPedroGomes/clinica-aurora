"""Teto diário e kill switch.

A regra que mais importa aqui é a que parece paranoia: estado ilegível
**recusa**, não reseta. Um teto que zera sempre que o arquivo corrompe é um
teto que um abusador desliga de propósito.
"""

from __future__ import annotations

import importlib
import json

import pytest


def carregar(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("BUDGET_PATH", str(tmp_path / "budget.json"))
    monkeypatch.setenv("DAILY_TURN_CAP", env.get("cap", "3"))
    monkeypatch.setenv("DEMO_KILL_SWITCH", env.get("kill", "0"))
    import src.budget as mod

    return importlib.reload(mod)


def test_consome_ate_o_teto_e_recusa(monkeypatch, tmp_path):
    budget = carregar(monkeypatch, tmp_path)
    for _ in range(3):
        budget.consumir()
    with pytest.raises(budget.Bloqueado) as exc:
        budget.consumir()
    assert exc.value.motivo == "teto_diario"


def test_kill_switch_recusa_tudo_na_hora(monkeypatch, tmp_path):
    budget = carregar(monkeypatch, tmp_path, kill="1")
    with pytest.raises(budget.Bloqueado) as exc:
        budget.consumir()
    assert exc.value.motivo == "kill_switch"


def test_refund_devolve_a_unidade(monkeypatch, tmp_path):
    budget = carregar(monkeypatch, tmp_path)
    budget.consumir()
    budget.consumir()
    budget.devolver()
    assert budget.restante() == 2


def test_estado_corrompido_recusa_em_vez_de_resetar(monkeypatch, tmp_path):
    budget = carregar(monkeypatch, tmp_path)
    budget.consumir()
    (tmp_path / "budget.json").write_text("{isto nao e json", "utf-8")

    with pytest.raises(budget.Bloqueado) as exc:
        budget.consumir()
    assert exc.value.motivo == "estado_corrompido"


def test_virada_de_dia_zera(monkeypatch, tmp_path):
    budget = carregar(monkeypatch, tmp_path)
    budget.consumir()
    caminho = tmp_path / "budget.json"
    caminho.write_text(json.dumps({"dia": "2000-01-01", "usado": 99}), "utf-8")
    assert budget.restante() == 3


def test_estado_ingravavel_recusa(monkeypatch, tmp_path):
    """O caso que quase matou voicerag e searcher: /data montado como root."""
    budget = carregar(monkeypatch, tmp_path)
    monkeypatch.setattr(budget.Path, "write_text", _explode)
    with pytest.raises(budget.Bloqueado) as exc:
        budget.consumir()
    assert exc.value.motivo == "estado_ingravavel"


def _explode(*args, **kwargs):
    raise OSError("read-only file system")
