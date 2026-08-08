"""A agenda semeada tem que obedecer à política que a demo diz aplicar.

Regressão de um bug encontrado testando a demo em produção: o seed ancorava
tudo em "amanhã". Num sábado isso caía em domingo — dia em que a clínica não
abre — e o preset do caminho feliz ("move Ana's appointment to 4pm") era
RECUSADO por `outside_business_hours`. Numa sexta dava no mesmo, porque sábado
fecha ao meio-dia. Dois dias em sete a demo abria provando o contrário do que
se propõe a provar, e a agenda inicial inteira ficava em horário fechado.

O teste varre um ano inteiro de dias de abertura porque o bug só aparecia em
2 dos 7 dias da semana — rodar num único horário fixo era exatamente como ele
passou despercebido.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import count

from erp.app.store import BAHIA, _seed
from src.policy.engine import PolicyEngine

POLITICA = PolicyEngine()


def _agendas_do_ano():
    """Uma agenda semeada por dia do ano, no meio da tarde."""
    inicio = datetime(2026, 1, 1, 14, 30, tzinfo=BAHIA)
    for dia in range(365):
        agora = inicio + timedelta(days=dia)
        yield agora, _seed(agora, count(1))


def test_consultas_com_paciente_nascem_em_dia_de_expediente_cheio():
    for agora, consultas in _agendas_do_ano():
        for c in consultas.values():
            if c.paciente_id == "pac_joao":
                # A do João é ancorada no relógio de propósito: P-1 olha a
                # antecedência (<24h) e num sábado à tarde não existe horário
                # que seja ao mesmo tempo de expediente e a menos de 24h daqui.
                continue
            dia = c.inicio.astimezone(BAHIA)
            assert dia.weekday() <= 4, (
                f"semeado em {dia:%A %d/%m} com agora={agora:%A %d/%m}: "
                "sábado fecha ao meio-dia e domingo não abre"
            )


def test_remarcar_ana_para_as_16h_passa_em_qualquer_dia_da_semana():
    """O preset rotulado 'a write that goes through' tem que ir através."""
    for agora, consultas in _agendas_do_ano():
        ana = next(c for c in consultas.values() if c.paciente_id == "pac_ana")
        as_16h = ana.inicio.astimezone(BAHIA).replace(hour=16, minute=0)

        decisao = POLITICA.pode_remarcar(
            consulta_id=ana.id,
            profissional_id=ana.profissional_id,
            novo_inicio=as_16h,
            duracao=ana.fim - ana.inicio,
            agenda=[],
            at=agora,
        )
        assert decisao.allowed, (
            f"agora={agora:%A %d/%m} → mover para {as_16h:%A %d/%m %H:%M} "
            f"recusado por {decisao.reason_code}"
        )


def test_cancelar_a_do_joao_sempre_exige_motivo():
    """O preset rotulado 'a write a rule refuses' tem que ser recusado."""
    for agora, consultas in _agendas_do_ano():
        joao = next(c for c in consultas.values() if c.paciente_id == "pac_joao")
        decisao = POLITICA.pode_cancelar(inicio=joao.inicio, motivo="", at=agora)
        assert not decisao.allowed
        assert decisao.reason_code == "cancel_window_requires_reason"


def test_marta_ocupa_o_mesmo_dia_da_ana_para_o_conflito_existir():
    for _, consultas in _agendas_do_ano():
        ana = next(c for c in consultas.values() if c.paciente_id == "pac_ana")
        marta = next(c for c in consultas.values() if c.paciente_id == "pac_marta")
        assert ana.inicio.date() == marta.inicio.date()
        assert ana.profissional_id == marta.profissional_id
