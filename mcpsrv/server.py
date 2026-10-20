"""Servidor MCP da Clínica Aurora.

O pacote se chama `mcpsrv` e não `mcp` por higiene: uma pasta `mcp/` sem
`__init__.py` não chega a sombrear o SDK (pacote regular vence namespace
package), mas basta alguém adicionar um `__init__.py` para que passe a sombrar,
e esse erro só apareceria dentro do container. Nome diferente fecha a classe.

O QUE ESTE PROCESSO EXPÕE, E POR QUE NÃO É O ERP

A tentação óbvia seria publicar o ERP cru como MCP: cinco tools de CRUD e pronto.
Isso demonstraria integração e mais nada, e contradiria a tese da demo, porque
entregaria um sistema onde quem decide o que pode ser gravado é o modelo do outro
lado.

O que este servidor expõe é o ERP **atrás do mesmo motor de política** que o
agente da demo usa. O visitante pluga o Claude, o Cursor ou o agente dele, e as
regras do negócio continuam valendo: cancelamento em cima da hora exige motivo,
nada é agendado fora do expediente, e existe teto de escritas por sessão. Quem
recusa é este processo, com o código da regra no retorno.

A frase que isto prova, e que nenhuma demo de MCP prova: **o seu agente, as
minhas regras, o meu log.**

O QUE ESTE PROCESSO NÃO CONSEGUE GARANTIR

O gate de confirmação (G-17) da demo depende de existir um turno humano entre a
proposta e a execução. No agente da demo isso é garantido, porque o loop é nosso.
Aqui o loop é do cliente, e um agente pode chamar a tool duas vezes seguidas sem
nenhum humano no meio.

Então aqui o gate é um **freio de mão, não uma tranca**: a primeira chamada de
escrita devolve o que seria feito e um token, e nada é gravado; a segunda, com o
token, executa. Isso força a proposta a aparecer no transcrito antes da escrita,
o que é o que dá ao humano a chance de intervir. Está documentado como tal em vez
de ser vendido como garantia, porque a diferença entre as duas coisas é
exatamente o assunto desta demo.

O que continua sendo garantia dura, porque não depende do loop de ninguém: a
política, o teto de escritas e o log.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# O código de domínio (política, tools, adaptador do ERP) mora no pacote do
# agente e é COMPARTILHADO, não copiado: duas cópias divergem, e uma regra que
# vale no chat mas não vale no MCP seria pior do que não ter MCP.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent"))

from mcp.server.fastmcp import Context, FastMCP  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

import src.tools.agenda  # noqa: F401,E402  — o import popula o REGISTRY
from src.adapters.erp import ErpAdapter  # noqa: E402
from src.policy.engine import PolicyEngine  # noqa: E402
from src.tools.agenda import (  # noqa: E402
    abrir_vaga,
    cancelar_consulta,
    listar_agenda,
    marcar_consulta,
    remarcar_consulta,
)
from src.tools.registry import (  # noqa: E402
    REGISTRY,
    AgentContext,
    ToolError,
    assinatura,
)

ERP_URL = os.getenv("ERP_URL", "http://erp:8080")
SESSAO_TTL = 2 * 60 * 60
MAX_SESSOES = 200

HANDLERS = {
    "listar_agenda": listar_agenda,
    "remarcar_consulta": remarcar_consulta,
    "cancelar_consulta": cancelar_consulta,
    "abrir_vaga": abrir_vaga,
    "marcar_consulta": marcar_consulta,
}

# Hosts que este servidor aceita no cabecalho Host. O SDK liga protecao contra
# DNS rebinding por padrao e, com a lista vazia, so aceita localhost — atras do
# Traefik isso vira `421 Invalid Host header` para todo mundo. A protecao e certa
# e fica LIGADA; o que muda e ela saber qual e o nosso host de verdade.
HOSTS_PERMITIDOS = [
    h.strip()
    for h in os.getenv("MCP_ALLOWED_HOSTS", "aurora.pgdev.com.br").split(",")
    if h.strip()
]

servidor = FastMCP(
    name="clinica-aurora",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=HOSTS_PERMITIDOS,
        # Origin so importa para cliente de navegador. Um MCP consumido por
        # Claude ou Cursor nao manda Origin, e exigir um quebraria os dois.
        allowed_origins=[f"https://{h}" for h in HOSTS_PERMITIDOS],
    ),
    instructions=(
        "Agenda de uma clínica fictícia, exposta ATRÁS de um motor de política "
        "determinístico. Você pode ler e escrever, mas as regras do negócio são "
        "decididas aqui, não por você: uma escrita que a política recusar volta "
        "como recusa, com o código da regra. Chame `ler_politica` antes de "
        "prometer qualquer coisa a alguém, e `ler_log_de_decisao` para mostrar o "
        "que foi decidido e por quê. Toda escrita precisa de duas chamadas: a "
        "primeira roda a política sem gravar nada e devolve a recusa da regra ou "
        "a proposta com um token de confirmação; mostre a proposta à pessoa "
        "antes de chamar a segunda com o token."
    ),
)


# -- sessão e isolamento ------------------------------------------------------


@dataclass
class Sessao:
    """Uma conexão MCP, com a agenda isolada dela.

    Mesmo desenho da demo web: cada visitante recebe o próprio tenant e nunca vê
    a agenda de outro. Aqui a chave é a sessão MCP em vez do cookie.
    """

    ctx: AgentContext
    criada_em: float = field(default_factory=time.monotonic)
    confirmacoes: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Token -> operação proposta, aguardando a segunda chamada."""


# WeakKeyDictionary e não dict comum: quando o cliente desconecta, a ServerSession
# é coletada e a entrada some junto. Com dict comum, uma sessão morta ficaria
# viva para sempre segurando o AgentContext dela — vazamento lento, do tipo que
# só aparece em produção semanas depois.
_sessoes: weakref.WeakKeyDictionary[Any, Sessao] = weakref.WeakKeyDictionary()


def _sessao(ctx: Context) -> Sessao:
    chave = ctx.session
    existente = _sessoes.get(chave)
    agora = time.monotonic()

    if existente is not None and agora - existente.criada_em <= SESSAO_TTL:
        # O relógio é parâmetro do contexto, nunca lido de dentro das regras.
        existente.ctx.at = datetime.now(timezone.utc)
        return existente

    if len(_sessoes) >= MAX_SESSOES:
        # Teto duro: demo pública, e cada sessão segura uma agenda semeada.
        mais_antiga = min(_sessoes.items(), key=lambda kv: kv[1].criada_em)[0]
        _sessoes.pop(mais_antiga, None)

    tenant = f"mcp-{uuid.uuid4().hex[:12]}"
    nova = Sessao(
        ctx=AgentContext(
            conversation_id=tenant,
            tenant_id=tenant,
            policy=PolicyEngine(),
            erp=ErpAdapter(ERP_URL, tenant_id=tenant),
            at=datetime.now(timezone.utc),
        )
    )
    _sessoes[chave] = nova
    return nova


# -- execução de tool ---------------------------------------------------------


def _erro(code: str, mensagem: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "erro": code, "mensagem": mensagem, **extra}


async def _executar(
    nome: str,
    entrada: dict[str, Any],
    ctx: Context,
    confirmacao: str | None = None,
) -> dict[str, Any]:
    """O caminho por onde TODA tool passa. É aqui que a política é aplicada."""
    sessao = _sessao(ctx)
    spec = REGISTRY[nome]

    try:
        dados = spec.input_model(**entrada)
    except Exception as exc:  # noqa: BLE001 — input do modelo é dado, não exceção
        return _erro(
            "input_invalido",
            "Os campos não batem com o schema da tool.",
            detalhe=str(exc)[:300],
        )

    if spec.scope == "write":
        if not confirmacao:
            # A prévia roda a política ANTES de emitir o token: uma escrita que
            # a regra barra não tem o que confirmar, e o cliente recebe a recusa
            # com o código da regra já na primeira chamada.
            antes = len(sessao.ctx.audit)
            previa = await HANDLERS[nome](sessao.ctx, dados, executar=False)
            if isinstance(previa, ToolError):
                return _erro(
                    previa.code,
                    previa.user_message,
                    decisoes=_decisoes(sessao, antes),
                )
        recusa = _checar_escrita(nome, dados, sessao, confirmacao)
        if recusa is not None:
            return recusa

    antes = len(sessao.ctx.audit)
    saida = await HANDLERS[nome](sessao.ctx, dados)
    decisoes = _decisoes(sessao, antes)

    if isinstance(saida, ToolError):
        return _erro(saida.code, saida.user_message, decisoes=decisoes)

    # `escritas_realizadas` NÃO é incrementado aqui: quem incrementa é o próprio
    # handler em `agenda.py`, e contar de novo cortaria o teto pela metade sem
    # que nenhum teste do agente percebesse.
    return {"ok": True, "resultado": saida, "decisoes": decisoes}


def _decisoes(sessao: Sessao, antes: int) -> list[dict[str, Any]]:
    """As decisões de política gravadas desde `antes`, no formato da resposta.

    Sem isto o cliente recebe "não deu" e não sabe qual regra decidiu, que é
    justamente o que esta demo existe para mostrar.
    """
    return [
        {
            "regra": e.tool,
            "permitido": e.output.get("allowed"),
            "codigo": e.reason_code,
            "versao_da_politica": e.rules_version,
        }
        for e in sessao.ctx.audit[antes:]
        if e.kind == "policy_decision"
    ]


def _checar_escrita(
    nome: str, dados: Any, sessao: Sessao, confirmacao: str | None
) -> dict[str, Any] | None:
    """Teto de escritas e gate de confirmação. Devolve None quando pode seguir."""
    # A ORDEM aqui importa para o log ser legivel. O gate vem ANTES do teto de
    # escritas porque uma proposta nao grava nada: checar o teto primeiro fazia
    # cada proposta, cada token invalido e cada tentativa recusada deixarem uma
    # linha `write_quota` no log. Uma escrita gerava cinco linhas de politica, e
    # quem lesse nao conseguia dizer qual delas foi a que decidiu de verdade —
    # justamente numa demo cujo argumento e o log ser legivel.
    sig = assinatura(nome, dados.model_dump(mode="json"))

    if confirmacao:
        pendente = sessao.confirmacoes.get(confirmacao)
        if pendente is None:
            return _erro(
                "confirmacao_invalida",
                "Esse token não existe nesta sessão, ou já foi usado. Chame a "
                "tool sem `confirmacao` para receber uma proposta nova.",
            )
        if pendente["assinatura"] != sig:
            # O token vale para a operação que foi PROPOSTA, não para a tool.
            # Sem esta checagem, confirmar "remarca para 16h" autorizaria
            # executar "remarca para 17h" com o mesmo token — que é exatamente o
            # buraco que o gate existe para fechar.
            return _erro(
                "confirmacao_nao_corresponde",
                "Esse token foi emitido para outra operação. Os dados mudaram "
                "desde a proposta, então ela precisa ser reapresentada.",
                proposta_original=pendente["proposta"],
            )
        del sessao.confirmacoes[confirmacao]

        # So agora, com a escrita de fato a caminho, o teto e consultado e a
        # decisao entra no log. Uma linha por escrita real, nunca por tentativa.
        quota = sessao.ctx.policy.pode_escrever(
            escritas_na_conversa=sessao.ctx.escritas_realizadas
        )
        sessao.ctx.registrar(
            kind="policy_decision",
            tool="write_quota",
            input={**quota.audit_payload, "tool": nome},
            output={"allowed": quota.allowed},
            reason_code=quota.reason_code,
        )
        if not quota.allowed:
            return _erro(
                quota.reason_code,
                "Teto de escritas desta sessão atingido. Comece uma sessão nova.",
                versao_da_politica=sessao.ctx.policy.version,
            )
        return None  # confirmado, dentro do teto: pode executar

    novo = uuid.uuid4().hex[:16]
    proposta = dados.model_dump(mode="json")
    sessao.confirmacoes[novo] = {"assinatura": sig, "tool": nome, "proposta": proposta}
    return {
        "ok": False,
        "erro": "confirmation_required",
        "mensagem": (
            "Nada foi gravado. Mostre esta proposta à pessoa, e só depois da "
            "resposta dela chame a MESMA tool com os MESMOS dados mais o campo "
            "`confirmacao`."
        ),
        "proposta": {"tool": nome, "dados": proposta},
        "confirmacao": novo,
    }


# -- as tools -----------------------------------------------------------------
#
# As assinaturas sao escritas a mao, e nao geradas do REGISTRY, porque o FastMCP
# deriva o schema que o cliente ve da assinatura da funcao. Gerar dinamicamente
# daria um schema opaco (**kwargs) justamente na superficie que o modelo do outro
# lado precisa entender. `tests/test_schema.py` trava as duas fontes juntas: se
# um input model ganhar campo e a assinatura aqui nao, o teste quebra.


@servidor.tool(description=REGISTRY["listar_agenda"].description)
async def listar_agenda_tool(
    ctx: Context,
    profissional_id: str | None = None,
    data: str | None = None,
) -> dict[str, Any]:
    return await _executar(
        "listar_agenda", {"profissional_id": profissional_id, "data": data}, ctx
    )


@servidor.tool(description=REGISTRY["remarcar_consulta"].description)
async def remarcar_consulta_tool(
    ctx: Context,
    consulta_id: str,
    novo_inicio: str,
    confirmacao: str | None = None,
) -> dict[str, Any]:
    return await _executar(
        "remarcar_consulta",
        {"consulta_id": consulta_id, "novo_inicio": novo_inicio},
        ctx,
        confirmacao,
    )


@servidor.tool(description=REGISTRY["cancelar_consulta"].description)
async def cancelar_consulta_tool(
    ctx: Context,
    consulta_id: str,
    motivo: str | None = None,
    confirmacao: str | None = None,
) -> dict[str, Any]:
    return await _executar(
        "cancelar_consulta",
        {"consulta_id": consulta_id, "motivo": motivo},
        ctx,
        confirmacao,
    )


@servidor.tool(description=REGISTRY["abrir_vaga"].description)
async def abrir_vaga_tool(
    ctx: Context,
    profissional_id: str,
    inicio: str,
    duracao_min: int,
    confirmacao: str | None = None,
) -> dict[str, Any]:
    return await _executar(
        "abrir_vaga",
        {
            "profissional_id": profissional_id,
            "inicio": inicio,
            "duracao_min": duracao_min,
        },
        ctx,
        confirmacao,
    )


@servidor.tool(description=REGISTRY["marcar_consulta"].description)
async def marcar_consulta_tool(
    ctx: Context,
    paciente_id: str,
    profissional_id: str,
    inicio: str,
    duracao_min: int,
    confirmacao: str | None = None,
) -> dict[str, Any]:
    return await _executar(
        "marcar_consulta",
        {
            "paciente_id": paciente_id,
            "profissional_id": profissional_id,
            "inicio": inicio,
            "duracao_min": duracao_min,
        },
        ctx,
        confirmacao,
    )


@servidor.tool(
    description=(
        "As regras que este servidor aplica, com a versão delas. Leia antes de "
        "prometer qualquer coisa: são elas que decidem o que pode ser gravado, "
        "não você."
    )
)
async def ler_politica(ctx: Context) -> dict[str, Any]:
    """Publica a política. Um sistema que recusa sem dizer a regra é opaco."""
    politica = _sessao(ctx).ctx.policy
    return {
        "versao": politica.version,
        "regras": {
            "cancelamento": (
                "Cancelar uma consulta que começa dentro da janela exige motivo "
                "registrado. Código: cancel_window_requires_reason."
            ),
            "expediente": (
                "Nada é agendado ou remarcado fora do horário de funcionamento, "
                "nem para o passado, nem para um horário já ocupado."
            ),
            "teto_de_escritas": (
                "Há um limite de escritas por sessão. Não é regra da clínica: é "
                "freio de segurança contra agente em laço."
            ),
            "confirmacao": (
                "Toda escrita exige duas chamadas. A primeira roda a política sem "
                "gravar nada: devolve a recusa com o código da regra, ou a "
                "proposta e um token."
            ),
        },
        "arquivo_de_origem": "agent/src/policy/rules.yaml",
    }


@servidor.tool(
    description=(
        "O log de decisão desta sessão: cada chamada, cada decisão de política e "
        "cada escrita, com o código da regra que a produziu. Use para mostrar à "
        "pessoa o que foi feito e por quê."
    )
)
async def ler_log_de_decisao(ctx: Context) -> dict[str, Any]:
    """O que sustenta a afirmação de log auditável, do lado do MCP."""
    sessao = _sessao(ctx)
    return {
        "tenant": sessao.ctx.tenant_id,
        "escritas_realizadas": sessao.ctx.escritas_realizadas,
        "linhas": [
            {
                "tipo": e.kind,
                "tool": e.tool,
                "codigo": e.reason_code,
                "versao_da_politica": e.rules_version,
                "quando": e.at.isoformat(),
            }
            for e in sessao.ctx.audit
        ],
    }


# O Starlette que o Traefik serve. `stateless_http` fica DESLIGADO de proposito:
# a sessao MCP e o que isola a agenda de um visitante da do outro, e sem ela
# todo mundo dividiria o mesmo tenant.
app = servidor.streamable_http_app()
