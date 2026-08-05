"""Cobertura 100% do motor de política — é o portão do passo 3 do procedimento.

É pouco código e é o que protege juridicamente: cada teste aqui é a prova de que
uma decisão saiu de uma regra escrita, e não de um modelo de linguagem.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.policy.decision import Decision
from src.policy.engine import Janela, PolicyEngine

BAHIA = ZoneInfo("America/Bahia")


@pytest.fixture
def engine() -> PolicyEngine:
    return PolicyEngine()


def em(ano: int, mes: int, dia: int, hora: int, minuto: int = 0) -> datetime:
    return datetime(ano, mes, dia, hora, minuto, tzinfo=BAHIA)


# -- contrato da Decision -----------------------------------------------------


def test_decision_exige_reason_code():
    with pytest.raises(ValueError, match="reason_code é obrigatório"):
        Decision(allowed=True, reason_code="", explanation_hint="x")


def test_decision_exige_snake_case():
    with pytest.raises(ValueError, match="snake_case"):
        Decision(allowed=True, reason_code="SLOT_CONFLICT", explanation_hint="x")


def test_decision_permitida_tambem_carrega_reason_code(engine: PolicyEngine):
    d = engine.pode_escrever(escritas_na_conversa=0)
    assert d.allowed is True
    assert d.reason_code == "allowed"


# -- P-1 · janela de cancelamento ---------------------------------------------


def test_p1_cancelar_em_cima_da_hora_sem_motivo_recusa(engine: PolicyEngine):
    at = em(2026, 8, 10, 9)
    d = engine.pode_cancelar(inicio=em(2026, 8, 10, 11), motivo="", at=at)
    assert d.allowed is False
    assert d.reason_code == "cancel_window_requires_reason"
    assert d.audit_payload["horas_de_antecedencia"] == 2.0


def test_p1_motivo_curto_conta_como_ausente(engine: PolicyEngine):
    at = em(2026, 8, 10, 9)
    d = engine.pode_cancelar(inicio=em(2026, 8, 10, 11), motivo="  ok ", at=at)
    assert d.allowed is False


def test_p1_cancelar_em_cima_da_hora_com_motivo_passa(engine: PolicyEngine):
    at = em(2026, 8, 10, 9)
    d = engine.pode_cancelar(
        inicio=em(2026, 8, 10, 11), motivo="paciente adoeceu", at=at
    )
    assert d.allowed is True


def test_p1_fora_da_janela_nao_exige_motivo(engine: PolicyEngine):
    at = em(2026, 8, 10, 9)
    d = engine.pode_cancelar(inicio=em(2026, 8, 15, 11), motivo="", at=at)
    assert d.allowed is True
    assert d.reason_code == "allowed"


def test_p1_a_dica_nao_vaza_a_regra(engine: PolicyEngine):
    d = engine.pode_cancelar(
        inicio=em(2026, 8, 10, 11), motivo="", at=em(2026, 8, 10, 9)
    )
    assert "24" not in d.explanation_hint
    assert "hora" not in d.explanation_hint.replace("em cima da hora", "")


# -- P-2 · passado ------------------------------------------------------------


@pytest.mark.parametrize("delta_horas", [-1, 0])
def test_p2_remarcar_para_o_passado_ou_agora_recusa(
    engine: PolicyEngine, delta_horas: int
):
    at = em(2026, 8, 10, 9)
    d = engine.pode_remarcar(
        consulta_id="c1",
        profissional_id="p1",
        novo_inicio=at + timedelta(hours=delta_horas),
        duracao=timedelta(minutes=30),
        agenda=[],
        at=at,
    )
    assert d.allowed is False
    assert d.reason_code == "slot_in_the_past"


# -- P-3 · conflito -----------------------------------------------------------


def test_p3_horario_ocupado_do_mesmo_profissional_recusa(engine: PolicyEngine):
    at = em(2026, 8, 10, 9)
    agenda = [Janela("c2", "p1", em(2026, 8, 11, 14), em(2026, 8, 11, 15))]
    d = engine.pode_remarcar(
        consulta_id="c1",
        profissional_id="p1",
        novo_inicio=em(2026, 8, 11, 14, 30),
        duracao=timedelta(minutes=30),
        agenda=agenda,
        at=at,
    )
    assert d.allowed is False
    assert d.reason_code == "slot_conflict"
    assert d.audit_payload["conflita_com"] == "c2"


def test_p3_mesmo_horario_de_outro_profissional_passa(engine: PolicyEngine):
    at = em(2026, 8, 10, 9)
    agenda = [Janela("c2", "p2", em(2026, 8, 11, 14), em(2026, 8, 11, 15))]
    d = engine.pode_remarcar(
        consulta_id="c1",
        profissional_id="p1",
        novo_inicio=em(2026, 8, 11, 14),
        duracao=timedelta(minutes=30),
        agenda=agenda,
        at=at,
    )
    assert d.allowed is True


def test_p3_a_propria_consulta_nao_conflita_consigo(engine: PolicyEngine):
    at = em(2026, 8, 10, 9)
    agenda = [Janela("c1", "p1", em(2026, 8, 11, 14), em(2026, 8, 11, 15))]
    d = engine.pode_remarcar(
        consulta_id="c1",
        profissional_id="p1",
        novo_inicio=em(2026, 8, 11, 14, 30),
        duracao=timedelta(minutes=30),
        agenda=agenda,
        at=at,
    )
    assert d.allowed is True


def test_p3_encostar_no_fim_da_outra_nao_e_conflito(engine: PolicyEngine):
    at = em(2026, 8, 10, 9)
    agenda = [Janela("c2", "p1", em(2026, 8, 11, 14), em(2026, 8, 11, 15))]
    d = engine.pode_remarcar(
        consulta_id="c1",
        profissional_id="p1",
        novo_inicio=em(2026, 8, 11, 15),
        duracao=timedelta(minutes=30),
        agenda=agenda,
        at=at,
    )
    assert d.allowed is True
    assert d.value == em(2026, 8, 11, 15)


# -- P-4 · expediente ---------------------------------------------------------


def test_p4_dentro_do_expediente_passa(engine: PolicyEngine):
    at = em(2026, 8, 10, 9)
    d = engine.pode_abrir_vaga(
        inicio=em(2026, 8, 13, 10), duracao_min=60, at=at
    )  # quinta
    assert d.allowed is True


def test_p4_domingo_recusa(engine: PolicyEngine):
    at = em(2026, 8, 10, 9)
    d = engine.pode_abrir_vaga(
        inicio=em(2026, 8, 16, 10), duracao_min=30, at=at
    )  # domingo
    assert d.allowed is False
    assert d.reason_code == "outside_business_hours"


def test_p4_sabado_a_tarde_recusa(engine: PolicyEngine):
    at = em(2026, 8, 10, 9)
    d = engine.pode_abrir_vaga(
        inicio=em(2026, 8, 15, 13), duracao_min=30, at=at
    )  # sábado 13h
    assert d.allowed is False
    assert d.reason_code == "outside_business_hours"


def test_p4_antes_de_abrir_recusa(engine: PolicyEngine):
    at = em(2026, 8, 10, 6)
    d = engine.pode_abrir_vaga(inicio=em(2026, 8, 13, 7, 30), duracao_min=30, at=at)
    assert d.allowed is False


def test_p4_atendimento_que_passa_do_fechamento_recusa(engine: PolicyEngine):
    """Começa 17h50 e termina 18h50: o começo cabe, o atendimento não."""
    at = em(2026, 8, 10, 9)
    d = engine.pode_abrir_vaga(inicio=em(2026, 8, 13, 17, 50), duracao_min=60, at=at)
    assert d.allowed is False
    assert d.reason_code == "outside_business_hours"


def test_p4_terminar_exatamente_no_fechamento_passa(engine: PolicyEngine):
    at = em(2026, 8, 10, 9)
    d = engine.pode_abrir_vaga(inicio=em(2026, 8, 13, 17), duracao_min=60, at=at)
    assert d.allowed is True


def test_p4_vaga_no_passado_recusa_antes_de_olhar_o_expediente(engine: PolicyEngine):
    at = em(2026, 8, 13, 12)
    d = engine.pode_abrir_vaga(inicio=em(2026, 8, 13, 10), duracao_min=30, at=at)
    assert d.allowed is False
    assert d.reason_code == "slot_in_the_past"


def test_p4_atravessar_a_meia_noite_recusa(engine: PolicyEngine):
    """Defesa de borda: fim no dia seguinte nunca é 'dentro do expediente'."""
    at = em(2026, 8, 10, 9)
    d = engine.pode_abrir_vaga(inicio=em(2026, 8, 13, 23, 45), duracao_min=60, at=at)
    assert d.allowed is False
    assert d.reason_code == "outside_business_hours"


# -- P-4 vale também para remarcação ------------------------------------------
#
# Este bloco existe por causa de um buraco encontrado usando a demo em produção:
# a regra do expediente só estava ligada ao abrir_vaga. Dava para mover uma
# consulta para domingo às 3h da manhã, e o log de decisão — que é o que a demo
# mostra na tela — carimbava "permitido". Uma agenda não fica correta só porque
# o horário está livre: ela precisa estar dentro do expediente também.


def remarcar_para(engine: PolicyEngine, quando: datetime, minutos: int = 30):
    return engine.pode_remarcar(
        consulta_id="c1",
        profissional_id="p1",
        novo_inicio=quando,
        duracao=timedelta(minutes=minutos),
        agenda=[],
        at=em(2026, 8, 10, 9),
    )


def test_p4_nao_remarca_para_dia_em_que_a_clinica_nao_abre(engine: PolicyEngine):
    d = remarcar_para(engine, em(2026, 8, 16, 3))  # domingo, 3h da manhã
    assert d.allowed is False
    assert d.reason_code == "outside_business_hours"


def test_p4_nao_remarca_para_depois_do_fechamento(engine: PolicyEngine):
    d = remarcar_para(engine, em(2026, 8, 13, 17, 45), minutos=45)  # termina 18h30
    assert d.allowed is False
    assert d.reason_code == "outside_business_hours"


def test_p4_nao_remarca_para_antes_da_abertura(engine: PolicyEngine):
    d = remarcar_para(engine, em(2026, 8, 13, 7, 30))
    assert d.allowed is False
    assert d.reason_code == "outside_business_hours"


def test_p4_remarcar_dentro_do_expediente_continua_passando(engine: PolicyEngine):
    d = remarcar_para(engine, em(2026, 8, 13, 10), minutos=60)  # quinta, 10h
    assert d.allowed is True


def test_p4_remarcacao_no_passado_recusa_antes_de_olhar_o_expediente(
    engine: PolicyEngine,
):
    """A ordem importa: 'já passou' é mais útil para a pessoa que 'fora do horário'."""
    d = engine.pode_remarcar(
        consulta_id="c1",
        profissional_id="p1",
        novo_inicio=em(2026, 8, 9, 3),  # domingo, e no passado
        duracao=timedelta(minutes=30),
        agenda=[],
        at=em(2026, 8, 10, 9),
    )
    assert d.allowed is False
    assert d.reason_code == "slot_in_the_past"


# -- P-5 · teto de escritas ---------------------------------------------------


@pytest.mark.parametrize(
    "feitas,esperado", [(0, True), (2, True), (3, False), (9, False)]
)
def test_p5_teto_de_escritas(engine: PolicyEngine, feitas: int, esperado: bool):
    d = engine.pode_escrever(escritas_na_conversa=feitas)
    assert d.allowed is esperado
    if not esperado:
        assert d.reason_code == "write_quota_exceeded"


# -- configuração -------------------------------------------------------------


def test_regras_vem_de_arquivo_versionado(engine: PolicyEngine):
    assert engine.version == "2026.08.2"


def test_engine_aceita_regras_injetadas():
    """Trocar a política não exige tocar em código — é o ponto do rules.yaml."""
    custom = {
        "version": "teste.1",
        "cancelamento": {"janela_horas": 1, "motivo_min_chars": 5},
        "expediente": {
            "timezone": "America/Bahia",
            "dias": {"0": {"abre": "09:00", "fecha": "10:00"}},
        },
        "conversa": {"max_escritas": 1},
    }
    e = PolicyEngine(rules=custom)
    at = em(2026, 8, 10, 9)
    assert e.pode_cancelar(inicio=em(2026, 8, 10, 11), motivo="", at=at).allowed is True
    assert e.pode_escrever(escritas_na_conversa=1).allowed is False
