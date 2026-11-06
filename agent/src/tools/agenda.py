"""As seis tools da Clínica Aurora.

Uma por operação de negócio. Nenhuma aceita SQL, URL, caminho, comando ou nome de
recurso arbitrário — `duracao_min` é domínio fechado e `consulta_id` é
sempre validado contra o tenant da sessão DENTRO da tool, não no prompt.

E nenhuma inventa resultado: se o ERP não gravou, a tool
devolve ToolError e o agente escala. Não existe caminho que produza um código de
confirmação para algo que não aconteceu.

As quatro de escrita têm dois modos. `executar=False` é a PRÉVIA: faz as leituras
do ERP e roda a política, sem gravar. Recusa volta como ToolError, com a decisão
no audit; permissão volta como None, sem linha nenhuma, porque a linha que vale
é a da execução e o log não pode ganhar duplicata. É a prévia que deixa a regra
decidir ANTES de o gate pedir o "sim".
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..adapters.erp import ConsultaView, ErpIndisponivel, ErpRecusou
from ..i18n import texto
from ..policy.engine import Janela
from .registry import AgentContext, ToolError, tool
from .tempo import BAHIA, ler_instante

# A clínica tem UM fuso e é o dela (`tempo.BAHIA`). Interpretar "2026-08-05" no
# fuso do servidor (UTC) faz meia-noite virar 21h do dia anterior — e a busca do
# dia devolve vazio, que o agente reporta como "não há consultas". Bug silencioso
# e caro: o meio-dia local mata o off-by-one sem depender de horário de verão.


def _dia_local(data: str) -> datetime:
    return datetime.fromisoformat(f"{data}T12:00:00").replace(tzinfo=BAHIA)


def _indisponivel(ctx: AgentContext) -> ToolError:
    """O ERP não respondeu. Sem promessa de pessoa: a demo não tem ninguém de
    plantão, e "vou chamar alguém da recepção" contradizia a tela, que diz que
    o encaminhamento é simulado."""
    return ToolError(
        code="erp_unavailable",
        user_message=texto("tool.erp_indisponivel", ctx.idioma),
    )


def _janelas(consultas: list[ConsultaView]) -> list[Janela]:
    return [
        Janela(
            consulta_id=c.id,
            profissional_id=c.profissional_id,
            inicio=c.inicio,
            fim=c.fim,
            paciente_id=c.paciente_id,
        )
        for c in consultas
        if c.status == "agendada"
    ]


async def _agenda_do_dia(
    ctx: AgentContext, inicio: datetime
) -> list[Janela] | ToolError:
    """A agenda do dia inteiro, dos dois profissionais, para P-3 e P-6.

    Os dois profissionais porque a P-6 conta as consultas do paciente com
    qualquer um deles. Erro de leitura segue o padrão da remarcação: vira
    ToolError, nunca exceção solta.
    """
    try:
        consultas = await ctx.erp.listar(
            profissional_id=None, data=inicio.astimezone(BAHIA), at=ctx.at
        )
    except ErpIndisponivel:
        return _indisponivel(ctx)
    except ErpRecusou as exc:
        return ToolError(
            code="erp_rejected",
            user_message=texto("tool.checagem_recusada", ctx.idioma),
            retry_hint=exc.detail,
        )
    return _janelas(consultas)


def _resumo(c: ConsultaView) -> dict[str, str]:
    """Só o que a conversa precisa.

    Sem o nome do paciente: quem conversa não está identificado, e a escrita
    foi pedida por alguém que já disse de quem era a consulta. Repetir o nome
    na saída era o caminho para o agente dizê-lo a quem não perguntou.
    """
    return {
        "consulta_id": c.id,
        "profissional": c.profissional_nome,
        "inicio": c.inicio.isoformat(),
    }


def _titular(c: ConsultaView, paciente_id: str | None, idioma: str) -> str:
    """O que a listagem diz de quem é a linha.

    O nome só aparece quando a pessoa já disse de quem quer saber (e o modelo
    passou o id que `buscar_paciente` devolveu). Nas outras linhas, "outro
    paciente": horário ocupado é ocupado, sem dizer por quem.
    """
    if not (c.paciente_id or c.paciente_nome):
        return texto("agenda.vaga_livre", idioma)
    if paciente_id and c.paciente_id == paciente_id:
        return c.paciente_nome
    return texto("agenda.outro_paciente", idioma)


def _sem_acento(texto: str) -> str:
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(ch for ch in decomposto if not unicodedata.combining(ch)).casefold()


# -- 0. buscar paciente -------------------------------------------------------


class BuscarPacienteInput(BaseModel):
    nome: str = Field(
        min_length=2,
        max_length=80,
        description="Nome que a pessoa disse: completo ou só o primeiro nome.",
    )


@tool(
    scope="read",
    idempotent=True,
    requires_confirmation=False,
    rate_limit=10,
    pii_level="low",
    description=(
        "Acha UM paciente do cadastro pelo nome que a pessoa disse (completo ou "
        "primeiro nome, sem diferença de acento ou maiúscula). Devolve o id e o "
        "nome, ou diz que não encontrou ou que há mais de um. Nunca lista o "
        "cadastro."
    ),
    input_model=BuscarPacienteInput,
)
async def buscar_paciente(
    ctx: AgentContext, data: BuscarPacienteInput
) -> dict[str, str] | ToolError:
    """Nome que a pessoa disse → id do cadastro, e nunca uma lista.

    Com o cadastro inteiro no contexto, o agente respondia a quem pedia "Maria
    Teste" que ela não existia, "mas temos Ana, João e Marta". Quem conversa não
    está identificado; nomear um paciente para ele é o mínimo a evitar.
    """
    try:
        pacientes = await ctx.erp.listar_pacientes(at=ctx.at)
    except ErpIndisponivel:
        return _indisponivel(ctx)
    except ErpRecusou as exc:
        return ToolError(
            code="erp_rejected",
            user_message=texto("tool.cadastro_recusado", ctx.idioma),
            retry_hint=exc.detail,
        )

    procurado = _sem_acento(data.nome.strip())
    achados = [
        p
        for p in pacientes
        if _sem_acento(p.nome) == procurado
        or (_sem_acento(p.nome).split() or [""])[0] == procurado
    ]
    ctx.registrar(
        kind="tool_call",
        tool="buscar_paciente",
        input={"nome": data.nome},
        output={"encontrados": len(achados)},
        reason_code=None,
    )
    if not achados:
        return ToolError(
            code="patient_not_found",
            user_message=texto("tool.paciente_nao_encontrado", ctx.idioma),
        )
    if len(achados) > 1:
        return ToolError(
            code="patient_ambiguous",
            user_message=texto("tool.paciente_ambiguo", ctx.idioma),
        )
    return {"paciente_id": achados[0].id, "nome": achados[0].nome}


# -- 1. listar ----------------------------------------------------------------


class ListarAgendaInput(BaseModel):
    paciente_id: Literal["pac_ana", "pac_joao", "pac_marta"] | None = Field(
        default=None,
        description=(
            "Paciente que a pessoa nomeou, com o id de buscar_paciente. Só as "
            "linhas dele trazem o nome; as outras dizem 'outro paciente'."
        ),
    )
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
        return _indisponivel(ctx)
    except ErpRecusou as exc:
        return ToolError(
            code="erp_rejected",
            user_message=texto("tool.listar_recusado", ctx.idioma),
            retry_hint=exc.detail,
        )

    saida = {
        "consultas": [
            {**_resumo(c), "paciente": _titular(c, data.paciente_id, ctx.idioma)}
            for c in consultas
        ]
    }
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
    ctx: AgentContext, data: RemarcarConsultaInput, *, executar: bool = True
) -> dict[str, Any] | ToolError | None:
    novo_inicio = ler_instante(data.novo_inicio)
    if novo_inicio is None:
        return ToolError(
            code="invalid_datetime",
            user_message=texto("tool.horario_invalido", ctx.idioma),
        )

    try:
        # O id vem do modelo, então é conteúdo não confiável: resolvê-lo contra o
        # tenant da sessão é o que impede ler ou escrever na agenda de outro.
        consulta = await ctx.erp.obter(data.consulta_id, at=ctx.at)
        if consulta is None:
            return ToolError(
                code="not_found", user_message=texto("tool.nao_encontrada", ctx.idioma)
            )
        agenda = await ctx.erp.listar(
            profissional_id=consulta.profissional_id,
            data=novo_inicio.astimezone(BAHIA),
            at=ctx.at,
        )
    except ErpIndisponivel:
        return _indisponivel(ctx)
    except ErpRecusou as exc:
        # A LEITURA também pode ser recusada — 429 do rate limit, 400 de filtro.
        # Sem este ramo a exceção escapava da tool inteira: o agente não escalava,
        # o audit não registrava nada, e o visitante via um erro genérico.
        return ToolError(
            code="erp_rejected",
            user_message=texto("tool.checagem_recusada", ctx.idioma),
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
    if executar or not decisao.allowed:
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
            user_message=texto("tool.horario_indisponivel", ctx.idioma),
            retry_hint=texto(decisao.explanation_hint, ctx.idioma),
        )
    if not executar:
        return None

    # Capturado ANTES da escrita: depois dela, `consulta` pode já refletir o
    # estado novo, e a mensagem ao usuário diria "movi das 16h para as 16h".
    inicio_anterior = consulta.inicio.isoformat()

    try:
        atualizada = await ctx.erp.remarcar(consulta.id, novo_inicio, at=ctx.at)
    except ErpIndisponivel:
        # O ponto inteiro da demo: falhou a escrita, o agente não inventa.
        return _indisponivel(ctx)
    except ErpRecusou as exc:
        return ToolError(
            code="erp_rejected",
            user_message=texto("tool.remarcar_recusado", ctx.idioma),
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
    ctx: AgentContext, data: CancelarConsultaInput, *, executar: bool = True
) -> dict[str, Any] | ToolError | None:
    try:
        consulta = await ctx.erp.obter(data.consulta_id, at=ctx.at)
    except ErpIndisponivel:
        return _indisponivel(ctx)
    if consulta is None:
        return ToolError(
            code="not_found", user_message=texto("tool.nao_encontrada", ctx.idioma)
        )

    decisao = ctx.policy.pode_cancelar(
        inicio=consulta.inicio, motivo=data.motivo or "", at=ctx.at
    )
    if executar or not decisao.allowed:
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
            user_message=texto("tool.cancelar_exige_motivo", ctx.idioma),
            retry_hint=texto(decisao.explanation_hint, ctx.idioma),
        )
    if not executar:
        return None

    try:
        cancelada = await ctx.erp.cancelar(
            consulta.id, data.motivo or "cancelado pela recepção", at=ctx.at
        )
    except ErpIndisponivel:
        return _indisponivel(ctx)
    except ErpRecusou as exc:
        return ToolError(
            code="erp_rejected",
            user_message=texto("tool.cancelar_recusado", ctx.idioma),
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
    ctx: AgentContext, data: AbrirVagaInput, *, executar: bool = True
) -> dict[str, Any] | ToolError | None:
    inicio = ler_instante(data.inicio)
    if inicio is None:
        return ToolError(
            code="invalid_datetime",
            user_message=texto("tool.horario_invalido", ctx.idioma),
        )

    agenda = await _agenda_do_dia(ctx, inicio)
    if isinstance(agenda, ToolError):
        return agenda

    decisao = ctx.policy.pode_marcar(
        paciente_id=None,
        profissional_id=data.profissional_id,
        inicio=inicio,
        duracao_min=data.duracao_min,
        agenda=agenda,
        at=ctx.at,
    )
    if executar or not decisao.allowed:
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
            user_message=texto("tool.vaga_nao_permitida", ctx.idioma),
            retry_hint=texto(decisao.explanation_hint, ctx.idioma),
        )
    if not executar:
        return None

    try:
        vaga = await ctx.erp.abrir_vaga(
            data.profissional_id, inicio, data.duracao_min, at=ctx.at
        )
    except ErpIndisponivel:
        return _indisponivel(ctx)
    except ErpRecusou as exc:
        return ToolError(
            code="erp_rejected",
            user_message=texto("tool.vaga_recusada", ctx.idioma),
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
    ctx: AgentContext, data: MarcarConsultaInput, *, executar: bool = True
) -> dict[str, Any] | ToolError | None:
    """A operação que faltava para a persona fechar.

    A recepção abria vaga e remarcava, mas não conseguia marcar consulta para
    ninguém — o pedido mais óbvio que se faz a uma recepção. Quem tentava
    ouvia que o vínculo com o paciente "é feito em outro lugar do fluxo", e a
    linha entrava na agenda com o paciente em branco.

    Passa pela MESMA política de abrir_vaga (`pode_marcar`): é a mesma ocupação
    de agenda, e o que muda é ter titular, que é o que liga a P-6.
    """
    inicio = ler_instante(data.inicio)
    if inicio is None:
        return ToolError(
            code="invalid_datetime",
            user_message=texto("tool.horario_invalido", ctx.idioma),
        )

    agenda = await _agenda_do_dia(ctx, inicio)
    if isinstance(agenda, ToolError):
        return agenda

    decisao = ctx.policy.pode_marcar(
        paciente_id=data.paciente_id,
        profissional_id=data.profissional_id,
        inicio=inicio,
        duracao_min=data.duracao_min,
        agenda=agenda,
        at=ctx.at,
    )
    if executar or not decisao.allowed:
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
            user_message=texto("tool.marcar_nao_permitido", ctx.idioma),
            retry_hint=texto(decisao.explanation_hint, ctx.idioma),
        )
    if not executar:
        return None

    try:
        consulta = await ctx.erp.abrir_vaga(
            data.profissional_id,
            inicio,
            data.duracao_min,
            at=ctx.at,
            paciente_id=data.paciente_id,
        )
    except ErpIndisponivel:
        return _indisponivel(ctx)
    except ErpRecusou as exc:
        return ToolError(
            code="erp_rejected",
            user_message=texto("tool.marcar_recusado", ctx.idioma),
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
