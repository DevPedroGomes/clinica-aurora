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
from src.policy.engine import Janela, PolicyEngine

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


# Horas do dia em que a agenda nasce, para cada dia do ano. O bug do João às
# 20h só aparecia de noite: o teste antigo semeava sempre às 14h30.
HORAS = [
    (0, 10),
    (7, 0),
    (8, 20),
    (10, 45),
    (11, 10),
    (12, 30),
    (14, 30),
    (17, 40),
    (18, 56),
    (21, 0),
    (23, 50),
]


def _agendas_a_toda_hora():
    inicio = datetime(2026, 1, 1, tzinfo=BAHIA)
    for dia in range(365):
        for hora, minuto in HORAS:
            agora = inicio + timedelta(days=dia, hours=hora, minutes=minuto)
            yield agora, _seed(agora, count(1))


def _sem_horario_de_expediente_em_24h(agora: datetime) -> bool:
    """De sábado depois das 10h30 a domingo 8h: o próximo é segunda às 8h.

    Sábado fecha ao meio-dia, e a consulta nasce pelo menos 1 h à frente: às
    10h31 o primeiro meio-hora livre já seria 12h, com a clínica fechada.
    """
    local = agora.astimezone(BAHIA)
    return (local.weekday() == 5 and (local.hour, local.minute) > (10, 30)) or (
        local.weekday() == 6 and local.hour < 8
    )


def test_toda_consulta_semeada_nasce_no_futuro_e_dentro_do_expediente():
    """Regressão de 20/10: às 18h56 de uma terça, o João nascia às 20h."""
    for agora, consultas in _agendas_a_toda_hora():
        for c in consultas.values():
            decisao = POLITICA.pode_marcar(
                paciente_id=None,
                profissional_id=c.profissional_id,
                inicio=c.inicio,
                duracao_min=int((c.fim - c.inicio).total_seconds() // 60),
                agenda=[],
                at=agora,
            )
            assert decisao.allowed, (
                f"{c.paciente_id} em {c.inicio:%a %d/%m %H:%M} com agora="
                f"{agora:%a %d/%m %H:%M}: {decisao.reason_code}"
            )


def test_cancelar_a_do_joao_exige_motivo_sempre_que_o_expediente_deixa():
    """O exemplo "Cancele a consulta do João" tem que ser recusado sem motivo.

    Sempre, menos de sábado depois das 10h30 até domingo 8h. Nessa janela
    nenhum horário de expediente fica a menos de 24 h, e a semeadura não
    inventa um fora dele.
    """
    for agora, consultas in _agendas_a_toda_hora():
        joao = next(c for c in consultas.values() if c.paciente_id == "pac_joao")
        decisao = POLITICA.pode_cancelar(inicio=joao.inicio, motivo="", at=agora)
        if _sem_horario_de_expediente_em_24h(agora):
            continue
        assert (
            decisao.reason_code == "cancel_window_requires_reason"
        ), f"agora={agora:%a %d/%m %H:%M}, João em {joao.inicio:%a %d/%m %H:%M}"


def test_marta_fica_livre_amanha_para_a_p6_ter_roteiro():
    """Roteiro da P-6: "marque 10 consultas para a Marta amanhã".

    A primeira proposta passa e, gravada, a segunda do dia cai em
    `patient_daily_limit`. Com a Marta já marcada amanhã, a primeira já caía, e
    o roteiro não tinha como acontecer.
    """
    for agora, consultas in _agendas_a_toda_hora():
        amanha = agora.astimezone(BAHIA) + timedelta(days=1)
        if amanha.weekday() == 6:  # domingo: a clínica não abre
            continue
        agenda = [
            Janela(c.id, c.profissional_id, c.inicio, c.fim, paciente_id=c.paciente_id)
            for c in consultas.values()
        ]
        as_10h = amanha.replace(hour=10, minute=0, second=0, microsecond=0)
        primeira = POLITICA.pode_marcar(
            paciente_id="pac_marta",
            profissional_id="prof_bruno",
            inicio=as_10h,
            duracao_min=30,
            agenda=agenda,
            at=agora,
        )
        assert primeira.allowed, f"agora={agora:%a %d/%m %H:%M}: {primeira.reason_code}"
        gravada = Janela(
            "nova",
            "prof_bruno",
            as_10h,
            as_10h + timedelta(minutes=30),
            paciente_id="pac_marta",
        )
        segunda = POLITICA.pode_marcar(
            paciente_id="pac_marta",
            profissional_id="prof_helena",
            inicio=as_10h + timedelta(hours=1),
            duracao_min=30,
            agenda=[*agenda, gravada],
            at=agora,
        )
        assert segunda.reason_code == "patient_daily_limit"


def test_marta_ocupa_o_mesmo_dia_da_ana_para_o_conflito_existir():
    for _, consultas in _agendas_do_ano():
        ana = next(c for c in consultas.values() if c.paciente_id == "pac_ana")
        marta = next(c for c in consultas.values() if c.paciente_id == "pac_marta")
        assert ana.inicio.date() == marta.inicio.date()
        assert ana.profissional_id == marta.profissional_id
