"""As quatro tools da Clínica Aurora.

Uma por operação de negócio. Nenhuma aceita SQL, URL, caminho, comando ou nome de
recurso arbitrário (regra 2) — `duracao_min` é domínio fechado e `consulta_id` é
sempre validado contra o tenant da sessão DENTRO da tool, não no prompt.

E nenhuma inventa resultado (regra 7 + escopo §4): se o ERP não gravou, a tool
devolve ToolError e o agente escala. Não existe caminho que produza um código de
confirmação para algo que não aconteceu.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from ..adapters.erp import ConsultaView, ErpIndisponivel, ErpRecusou
from ..policy.engine import Janela
from .registry import AgentContext, ToolError, tool

INDISPONIVEL = ToolError(
    code="erp_unavailable",
    user_message="Não consegui acessar a agenda agora. Vou chamar alguém da recepção para você.",
)


def _janelas(consultas: list[ConsultaView]) -> list[Janela]:
    return [
        Janela(consulta_id=c.id, profissional_id=c.profissional_id, inicio=c.inicio, fim=c.fim)
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
    profissional_id: Literal["prof_bruno", "prof_helena"] = Field(
        description="Profissional da clínica. Obtido em profissionais."
    )
    data: str = Field(description="Dia a consultar, no formato YYYY-MM-DD.", pattern=r"^\d{4}-\d{2}-\d{2}$")


@tool(
    scope="read",
    idempotent=True,
    requires_confirmation=False,
    rate_limit=10,
    pii_level="low",
    description="Lista as consultas agendadas de um profissional em um dia.",
    input_model=ListarAgendaInput,
)
async def listar_agenda(ctx: AgentContext, data: ListarAgendaInput) -> dict | ToolError:
    try:
        dia = datetime.fromisoformat(data.data).replace(tzinfo=ctx.at.tzinfo)
        consultas = await ctx.erp.listar(profissional_id=data.profissional_id, data=dia, at=ctx.at)
    except ErpIndisponivel:
        return INDISPONIVEL
    except ErpRecusou as exc:
        return ToolError(code="erp_rejected", user_message="Não consegui consultar essa agenda.", retry_hint=exc.detail)

    saida = {"consultas": [_resumo(c) for c in consultas]}
    ctx.registrar(kind="tool_call", tool="listar_agenda", input=data.model_dump(), output=saida, reason_code=None)
    return saida


# -- 2. remarcar --------------------------------------------------------------


class RemarcarConsultaInput(BaseModel):
    consulta_id: str = Field(max_length=40, pattern=r"^cons_[0-9]+$", description="Id da consulta, obtido em listar_agenda.")
    novo_inicio: str = Field(description="Novo início, ISO 8601 com timezone.")


@tool(
    scope="write",
    idempotent=False,
    requires_confirmation=True,
    rate_limit=5,
    pii_level="medium",
    description="Remarca uma consulta existente para um novo horário.",
    input_model=RemarcarConsultaInput,
)
async def remarcar_consulta(ctx: AgentContext, data: RemarcarConsultaInput) -> dict | ToolError:
    try:
        novo_inicio = datetime.fromisoformat(data.novo_inicio)
    except ValueError:
        return ToolError(code="invalid_datetime", user_message="Não entendi o horário. Pode repetir com o dia e a hora?")
    if novo_inicio.tzinfo is None:
        novo_inicio = novo_inicio.replace(tzinfo=ctx.at.tzinfo)

    try:
        # O id vem do modelo, então é conteúdo não confiável: resolvê-lo contra o
        # tenant da sessão é o que impede ler ou escrever na agenda de outro.
        consulta = await ctx.erp.obter(data.consulta_id, at=ctx.at)
        if consulta is None:
            return ToolError(code="not_found", user_message="Não encontrei essa consulta na agenda.")
        agenda = await ctx.erp.listar(profissional_id=consulta.profissional_id, data=novo_inicio, at=ctx.at)
    except ErpIndisponivel:
        return INDISPONIVEL

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
        return ToolError(code="erp_rejected", user_message="A agenda recusou essa alteração.", retry_hint=exc.detail)

    ctx.escritas_realizadas += 1
    saida = {**_resumo(atualizada), "inicio_anterior": inicio_anterior}
    ctx.registrar(kind="write", tool="remarcar_consulta", input=data.model_dump(), output=saida, reason_code="allowed")
    return saida


# -- 3. cancelar --------------------------------------------------------------


class CancelarConsultaInput(BaseModel):
    consulta_id: str = Field(max_length=40, pattern=r"^cons_[0-9]+$")
    motivo: str = Field(min_length=1, max_length=200, description="Motivo informado pelo paciente ou pela recepção.")


@tool(
    scope="write",
    idempotent=False,
    requires_confirmation=True,
    rate_limit=5,
    pii_level="medium",
    description="Cancela uma consulta agendada, registrando o motivo.",
    input_model=CancelarConsultaInput,
)
async def cancelar_consulta(ctx: AgentContext, data: CancelarConsultaInput) -> dict | ToolError:
    try:
        consulta = await ctx.erp.obter(data.consulta_id, at=ctx.at)
    except ErpIndisponivel:
        return INDISPONIVEL
    if consulta is None:
        return ToolError(code="not_found", user_message="Não encontrei essa consulta na agenda.")

    decisao = ctx.policy.pode_cancelar(inicio=consulta.inicio, motivo=data.motivo, at=ctx.at)
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
        cancelada = await ctx.erp.cancelar(consulta.id, data.motivo, at=ctx.at)
    except ErpIndisponivel:
        return INDISPONIVEL
    except ErpRecusou as exc:
        return ToolError(code="erp_rejected", user_message="A agenda recusou o cancelamento.", retry_hint=exc.detail)

    ctx.escritas_realizadas += 1
    saida = _resumo(cancelada)
    ctx.registrar(kind="write", tool="cancelar_consulta", input=data.model_dump(), output=saida, reason_code="allowed")
    return saida


# -- 4. abrir vaga ------------------------------------------------------------


class AbrirVagaInput(BaseModel):
    profissional_id: Literal["prof_bruno", "prof_helena"]
    inicio: str = Field(description="Início da vaga, ISO 8601 com timezone.")
    duracao_min: Literal[30, 45, 60] = Field(description="Domínio fechado: 30, 45 ou 60.")


@tool(
    scope="write",
    idempotent=True,
    requires_confirmation=True,
    rate_limit=5,
    pii_level="none",
    description="Abre um horário livre na agenda de um profissional.",
    input_model=AbrirVagaInput,
)
async def abrir_vaga(ctx: AgentContext, data: AbrirVagaInput) -> dict | ToolError:
    try:
        inicio = datetime.fromisoformat(data.inicio)
    except ValueError:
        return ToolError(code="invalid_datetime", user_message="Não entendi o horário. Pode repetir com o dia e a hora?")
    if inicio.tzinfo is None:
        inicio = inicio.replace(tzinfo=ctx.at.tzinfo)

    decisao = ctx.policy.pode_abrir_vaga(inicio=inicio, duracao_min=data.duracao_min, at=ctx.at)
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
        vaga = await ctx.erp.abrir_vaga(data.profissional_id, inicio, data.duracao_min, at=ctx.at)
    except ErpIndisponivel:
        return INDISPONIVEL
    except ErpRecusou as exc:
        return ToolError(code="erp_rejected", user_message="A agenda recusou esse horário.", retry_hint=exc.detail)

    ctx.escritas_realizadas += 1
    saida = {
        "consulta_id": vaga.id,
        "profissional": vaga.profissional_nome,
        "inicio": vaga.inicio.isoformat(),
        "fim": (inicio + timedelta(minutes=data.duracao_min)).isoformat(),
    }
    ctx.registrar(kind="write", tool="abrir_vaga", input=data.model_dump(), output=saida, reason_code="allowed")
    return saida
