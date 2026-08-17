"""As cinco tools da Clínica Aurora.

Uma por operação de negócio. Nenhuma aceita SQL, URL, caminho, comando ou nome de
recurso arbitrário (regra 2) — `duracao_min` é domínio fechado e `consulta_id` é
sempre validado contra o tenant da sessão DENTRO da tool, não no prompt.

E nenhuma inventa resultado (regra 7 + escopo §4): se o ERP não gravou, a tool
devolve ToolError e o agente escala. Não existe caminho que produza um código de
confirmação para algo que não aconteceu.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from ..adapters.erp import ConsultaView, ErpIndisponivel, ErpRecusou
from ..policy.engine import Janela
from .registry import AgentContext, ToolError, tool

# A clínica tem UM fuso e é o dela. Interpretar "2026-08-05" no fuso do
# servidor (UTC) faz meia-noite virar 21h do dia anterior — e a busca do dia
# devolve vazio, que o agente reporta como "não há consultas". Bug silencioso
# e caro: o meio-dia local mata o off-by-one sem depender de horário de verão.
BAHIA = ZoneInfo("America/Bahia")


def _dia_local(data: str) -> datetime:
    return datetime.fromisoformat(f"{data}T12:00:00").replace(tzinfo=BAHIA)


INDISPONIVEL = ToolError(
    code="erp_unavailable",
    user_message="Não consegui acessar a agenda agora. Vou chamar alguém da recepção para você.",
)


def _janelas(consultas: list[ConsultaView]) -> list[Janela]:
    return [
        Janela(
            consulta_id=c.id,
            profissional_id=c.profissional_id,
            inicio=c.inicio,
            fim=c.fim,
        )
        for c in consultas
        if c.status == "agendada"
    ]


def _resumo(c: ConsultaView) -> dict[str, str]:
    """Só o que a conversa precisa (regra 7 / G-18)."""
    return {
        "consulta_id": c.id,
        "paciente": c.paciente_nome,
        "profissional": c.profissional_nome,
        "inicio": c.inicio.isoformat(),
    }


# -- 1. listar ----------------------------------------------------------------


class ListarAgendaInput(BaseModel):
    profissional_id: Literal["prof_bruno", "prof_helena"] | None = Field(
        default=None,
        description="Profissional da clínica. Omita para ver a agenda dos dois.",
    )
    data: str | None = Field(
        default=None,
        description="Dia a consultar, YYYY-MM-DD. Omita para ver toda a agenda da clínica.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )


@tool(
    scope="read",
    idempotent=True,
    requires_confirmation=False,
    rate_limit=10,
    pii_level="low",
    description=(
        "Lista consultas agendadas. Sem filtros, devolve a agenda inteira da clínica — "
        "use assim quando não souber o profissional ou o dia da consulta procurada."
    ),
    input_model=ListarAgendaInput,
)
async def listar_agenda(
    ctx: AgentContext, data: ListarAgendaInput
) -> dict[str, Any] | ToolError:
    try:
        consultas = await ctx.erp.listar(
            profissional_id=data.profissional_id,
            data=_dia_local(data.data) if data.data else None,
            at=ctx.at,
        )
    except ErpIndisponivel:
        return INDISPONIVEL
    except ErpRecusou as exc:
        return ToolError(
            code="erp_rejected",
            user_message="Não consegui consultar essa agenda.",
            retry_hint=exc.detail,
        )

    saida = {"consultas": [_resumo(c) for c in consultas]}
    ctx.registrar(
        kind="tool_call",
        tool="listar_agenda",
        input=data.model_dump(),
        output=saida,
        reason_code=None,
    )
    return saida


# -- 2. remarcar --------------------------------------------------------------


class RemarcarConsultaInput(BaseModel):
    consulta_id: str = Field(
        max_length=40,
        pattern=r"^cons_[0-9]+$",
        description="Id da consulta, obtido em listar_agenda.",
    )
    novo_inicio: str = Field(description="Novo início, ISO 8601 com timezone.")


@tool(
    scope="write",
    idempotent=False,
    requires_confirmation=True,
    rate_limit=5,
    pii_level="medium",
    description="Remarca uma consulta existente para um novo horário.",
    input_model=RemarcarConsultaInput,
    confirm_fields=("consulta_id", "novo_inicio"),
)
async def remarcar_consulta(
    ctx: AgentContext, data: RemarcarConsultaInput
) -> dict[str, Any] | ToolError:
    try:
        novo_inicio = datetime.fromisoformat(data.novo_inicio)
    except ValueError:
        return ToolError(
            code="invalid_datetime",
            user_message="Não entendi o horário. Pode repetir com o dia e a hora?",
        )
    if novo_inicio.tzinfo is None:
        novo_inicio = novo_inicio.replace(tzinfo=ctx.at.tzinfo)

    try:
        # O id vem do modelo, então é conteúdo não confiável: resolvê-lo contra o
        # tenant da sessão é o que impede ler ou escrever na agenda de outro.
        consulta = await ctx.erp.obter(data.consulta_id, at=ctx.at)
        if consulta is None:
            return ToolError(
                code="not_found", user_message="Não encontrei essa consulta na agenda."
            )
        agenda = await ctx.erp.listar(
            profissional_id=consulta.profissional_id,
            data=novo_inicio.astimezone(BAHIA),
            at=ctx.at,
        )
    except ErpIndisponivel:
        return INDISPONIVEL
    except ErpRecusou as exc:
        # A LEITURA também pode ser recusada — 429 do rate limit, 400 de filtro.
        # Sem este ramo a exceção escapava da tool inteira: o agente não escalava,
        # o audit não registrava nada, e o visitante via um erro genérico.
        return ToolError(
            code="erp_rejected",
            user_message="Não consegui consultar a agenda para checar o horário.",
            retry_hint=exc.detail,
        )

    decisao = ctx.policy.pode_remarcar(
        consulta_id=consulta.id,
        profissional_id=consulta.profissional_id,
        novo_inicio=novo_inicio,
        duracao=consulta.fim - consulta.inicio,
        agenda=_janelas(agenda),
        at=ctx.at,
    )
    ctx.registrar(
        kind="policy_decision",
        tool="remarcar_consulta",
        input=decisao.audit_payload,
        output={"allowed": decisao.allowed},
        reason_code=decisao.reason_code,
    )
    if not decisao.allowed:
        return ToolError(
            code=decisao.reason_code,
            user_message="Esse horário não está disponível.",
            retry_hint=decisao.explanation_hint,
        )

    # Capturado ANTES da escrita: depois dela, `consulta` pode já refletir o
    # estado novo, e a mensagem ao usuário diria "movi das 16h para as 16h".
    inicio_anterior = consulta.inicio.isoformat()

    try:
        atualizada = await ctx.erp.remarcar(consulta.id, novo_inicio, at=ctx.at)
    except ErpIndisponivel:
        # O ponto inteiro da demo: falhou a escrita, o agente não inventa.
        return INDISPONIVEL
    except ErpRecusou as exc:
        return ToolError(
            code="erp_rejected",
            user_message="A agenda recusou essa alteração.",
            retry_hint=exc.detail,
        )

    ctx.escritas_realizadas += 1
    saida = {**_resumo(atualizada), "inicio_anterior": inicio_anterior}
    ctx.registrar(
        kind="write",
        tool="remarcar_consulta",
        input=data.model_dump(),
        output=saida,
        reason_code="allowed",
    )
    return saida


# -- 3. cancelar --------------------------------------------------------------


class CancelarConsultaInput(BaseModel):
    consulta_id: str = Field(max_length=40, pattern=r"^cons_[0-9]+$")
    # Opcional DE PROPÓSITO. Quando o motivo é exigido é uma regra de negócio
    # (P-1: cancelamento em cima da hora), e regra de negócio mora no motor de
    # política, não no schema. Torná-lo obrigatório aqui duplicava a regra em
    # dois lugares e — pior — escondia a decisão: o modelo pedia o motivo por
    # conta própria e a política nunca chegava a rodar nem a registrar nada.
    motivo: str | None = Field(
        default=None,
        max_length=200,
        description="Motivo do cancelamento, quando a pessoa informar.",
    )


@tool(
    scope="write",
    idempotent=False,
    requires_confirmation=True,
    rate_limit=5,
    pii_level="medium",
    description="Cancela uma consulta agendada, registrando o motivo.",
    input_model=CancelarConsultaInput,
    confirm_fields=("consulta_id",),
)
async def cancelar_consulta(
    ctx: AgentContext, data: CancelarConsultaInput
) -> dict[str, Any] | ToolError:
    try:
        consulta = await ctx.erp.obter(data.consulta_id, at=ctx.at)
    except ErpIndisponivel:
        return INDISPONIVEL
    if consulta is None:
        return ToolError(
            code="not_found", user_message="Não encontrei essa consulta na agenda."
        )

    decisao = ctx.policy.pode_cancelar(
        inicio=consulta.inicio, motivo=data.motivo or "", at=ctx.at
    )
    ctx.registrar(
        kind="policy_decision",
        tool="cancelar_consulta",
        input=decisao.audit_payload,
        output={"allowed": decisao.allowed},
        reason_code=decisao.reason_code,
    )
    if not decisao.allowed:
        return ToolError(
            code=decisao.reason_code,
            user_message="Para cancelar essa consulta eu preciso registrar um motivo.",
            retry_hint=decisao.explanation_hint,
        )

    try:
        cancelada = await ctx.erp.cancelar(
            consulta.id, data.motivo or "cancelado pela recepção", at=ctx.at
        )
    except ErpIndisponivel:
        return INDISPONIVEL
    except ErpRecusou as exc:
        return ToolError(
            code="erp_rejected",
            user_message="A agenda recusou o cancelamento.",
            retry_hint=exc.detail,
        )

    ctx.escritas_realizadas += 1
    saida = _resumo(cancelada)
    ctx.registrar(
        kind="write",
        tool="cancelar_consulta",
        input=data.model_dump(),
        output=saida,
        reason_code="allowed",
    )
    return saida


# -- 4. abrir vaga ------------------------------------------------------------


class AbrirVagaInput(BaseModel):
    profissional_id: Literal["prof_bruno", "prof_helena"]
    inicio: str = Field(description="Início da vaga, ISO 8601 com timezone.")
    duracao_min: Literal[30, 45, 60] = Field(
        description="Domínio fechado: 30, 45 ou 60."
    )


@tool(
    scope="write",
    # NÃO é idempotente, por mais que pareça: a segunda chamada com os mesmos
    # argumentos não é absorvida, ela colide com a vaga que a primeira criou e
    # o ERP devolve 409. Declarar True aqui era descrever um comportamento que
    # o outro lado não tem.
    idempotent=False,
    requires_confirmation=True,
    rate_limit=5,
    pii_level="none",
    description="Abre um horário livre na agenda de um profissional.",
    input_model=AbrirVagaInput,
    confirm_fields=("profissional_id", "inicio", "duracao_min"),
)
async def abrir_vaga(
    ctx: AgentContext, data: AbrirVagaInput
) -> dict[str, Any] | ToolError:
    try:
        inicio = datetime.fromisoformat(data.inicio)
    except ValueError:
        return ToolError(
            code="invalid_datetime",
            user_message="Não entendi o horário. Pode repetir com o dia e a hora?",
        )
    if inicio.tzinfo is None:
        inicio = inicio.replace(tzinfo=ctx.at.tzinfo)

    decisao = ctx.policy.pode_abrir_vaga(
        inicio=inicio, duracao_min=data.duracao_min, at=ctx.at
    )
    ctx.registrar(
        kind="policy_decision",
        tool="abrir_vaga",
        input=decisao.audit_payload,
        output={"allowed": decisao.allowed},
        reason_code=decisao.reason_code,
    )
    if not decisao.allowed:
        return ToolError(
            code=decisao.reason_code,
            user_message="Não posso abrir esse horário.",
            retry_hint=decisao.explanation_hint,
        )

    try:
        vaga = await ctx.erp.abrir_vaga(
            data.profissional_id, inicio, data.duracao_min, at=ctx.at
        )
    except ErpIndisponivel:
        return INDISPONIVEL
    except ErpRecusou as exc:
        return ToolError(
            code="erp_rejected",
            user_message="A agenda recusou esse horário.",
            retry_hint=exc.detail,
        )

    ctx.escritas_realizadas += 1
    saida = {
        "consulta_id": vaga.id,
        "profissional": vaga.profissional_nome,
        "inicio": vaga.inicio.isoformat(),
        "fim": (inicio + timedelta(minutes=data.duracao_min)).isoformat(),
    }
    ctx.registrar(
        kind="write",
        tool="abrir_vaga",
        input=data.model_dump(),
        output=saida,
        reason_code="allowed",
    )
    return saida


# -- 5. marcar consulta -------------------------------------------------------


class MarcarConsultaInput(BaseModel):
    """Domínio fechado nos dois ids (regra 2), pelo mesmo motivo do profissional.

    O paciente é escolhido de uma lista, nunca digitado: se o campo aceitasse
    nome livre, o modelo poderia marcar consulta para alguém que não existe no
    cadastro — e a linha entraria na agenda com um titular inventado, que é
    precisamente o tipo de escrita que esta demo existe para impedir.
    """

    paciente_id: Literal["pac_ana", "pac_joao", "pac_marta"] = Field(
        description="Paciente do cadastro da clínica."
    )
    profissional_id: Literal["prof_bruno", "prof_helena"]
    inicio: str = Field(description="Início da consulta, ISO 8601 com timezone.")
    duracao_min: Literal[30, 45, 60] = Field(
        description="Domínio fechado: 30, 45 ou 60."
    )


@tool(
    scope="write",
    idempotent=False,
    requires_confirmation=True,
    rate_limit=5,
    pii_level="medium",
    description=(
        "Marca uma consulta para um paciente do cadastro com um profissional. "
        "Use esta quando a pessoa disser PARA QUEM é a consulta; use abrir_vaga "
        "quando ela só quiser deixar um horário livre na agenda."
    ),
    input_model=MarcarConsultaInput,
    confirm_fields=("paciente_id", "profissional_id", "inicio", "duracao_min"),
)
async def marcar_consulta(
    ctx: AgentContext, data: MarcarConsultaInput
) -> dict[str, Any] | ToolError:
    """A operação que faltava para a persona fechar.

    A recepção abria vaga e remarcava, mas não conseguia marcar consulta para
    ninguém — o pedido mais óbvio que se faz a uma recepção. Quem tentava
    ouvia que o vínculo com o paciente "é feito em outro lugar do fluxo", e a
    linha entrava na agenda com o paciente em branco.

    Passa pela MESMA política de abrir_vaga: é a mesma ocupação de agenda, e o
    que muda é só ter titular.
    """
    try:
        inicio = datetime.fromisoformat(data.inicio)
    except ValueError:
        return ToolError(
            code="invalid_datetime",
            user_message="Não entendi o horário. Pode repetir com o dia e a hora?",
        )
    if inicio.tzinfo is None:
        inicio = inicio.replace(tzinfo=ctx.at.tzinfo)

    decisao = ctx.policy.pode_abrir_vaga(
        inicio=inicio, duracao_min=data.duracao_min, at=ctx.at
    )
    ctx.registrar(
        kind="policy_decision",
        tool="marcar_consulta",
        input=decisao.audit_payload,
        output={"allowed": decisao.allowed},
        reason_code=decisao.reason_code,
    )
    if not decisao.allowed:
        return ToolError(
            code=decisao.reason_code,
            user_message="Não posso marcar nesse horário.",
            retry_hint=decisao.explanation_hint,
        )

    try:
        consulta = await ctx.erp.abrir_vaga(
            data.profissional_id,
            inicio,
            data.duracao_min,
            at=ctx.at,
            paciente_id=data.paciente_id,
        )
    except ErpIndisponivel:
        return INDISPONIVEL
    except ErpRecusou as exc:
        return ToolError(
            code="erp_rejected",
            user_message="A agenda recusou essa consulta.",
            retry_hint=exc.detail,
        )

    ctx.escritas_realizadas += 1
    saida = {
        **_resumo(consulta),
        "fim": (inicio + timedelta(minutes=data.duracao_min)).isoformat(),
    }
    ctx.registrar(
        kind="write",
        tool="marcar_consulta",
        input=data.model_dump(),
        output=saida,
        reason_code="allowed",
    )
    return saida
