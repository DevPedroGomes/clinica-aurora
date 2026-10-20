"""Registry de tools — o contrato de `core-spec/tools.md` como enforcement.

Os metadados do decorator não são documentação: o runtime lê e aplica antes de
executar. É o que impede o modelo de pular a confirmação ou de exceder o teto de
escritas por conversa, mesmo que o prompt seja manipulado.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from ..adapters.erp import ErpAdapter
from ..i18n import normalizar, traduzir_tool
from ..policy.engine import PolicyEngine
from .tempo import instante_canonico

# Logger dedicado: o audit sai em JSON por linha, separado do access log do
# uvicorn, para poder ser coletado sem grep de texto livre.
audit_log = logging.getLogger("aurora.audit")

Scope = Literal["read", "write"]
PiiLevel = Literal["none", "low", "medium", "high"]


class ToolError(BaseModel):
    """Erro é retorno, não exceção (regra 6).

    O agente precisa poder se recuperar e explicar. Exceção não tratada mata a
    conversa e o usuário final vê silêncio.
    """

    code: str
    user_message: str
    """Texto no idioma da sessão, do catálogo (`src/i18n.py`). Nunca contém erro cru."""
    retry_hint: str | None = None


@dataclass
class AuditEntry:
    kind: str
    tool: str
    input: dict[str, Any]
    output: dict[str, Any]
    reason_code: str | None
    rules_version: str
    at: datetime


@dataclass
class AgentContext:
    """Tudo que uma tool precisa. Nunca variável global, nunca conexão aberta dentro da tool."""

    conversation_id: str
    tenant_id: str
    policy: PolicyEngine
    erp: ErpAdapter
    at: datetime
    escritas_realizadas: int = 0
    idioma: str = "pt"
    """Idioma da SESSÃO ("pt" ou "en"), o `?lang` da página.

    Governa tudo que o código escreve: o prompt do sistema, o bloco de contexto
    de cada turno, os erros das tools, as dicas da política e as frases de
    reserva. Antes ele só escolhia três frases, e o resto ia em português.
    """
    idioma_da_conversa: str | None = None
    """Último idioma que a PESSOA usou de forma clara nesta conversa.

    A resposta segue a mensagem quando ela é clara (quem escreve em espanhol
    recebe espanhol); quando ela não diz nada ("ok", "16h"), segue este, e antes
    de existir um, o da sessão.
    """
    custo_micros: int = 0
    """Quanto esta conversa já custou, em micro-dólares (ver `pricing.py`).

    Mora no contexto, e não numa global, porque é por conversa: o painel mostra
    o acumulado desta sessão, e o `api.py` lê a diferença antes e depois do
    turno para reconciliar a reserva do orçamento com o gasto que aconteceu.
    """
    chamadas: int = 0
    """Iterações do loop que chegaram a virar chamada paga."""
    confirmacoes: set[str] = field(default_factory=set)
    """Assinaturas de escrita que a PRÓXIMA fala do usuário pode confirmar.

    Uma assinatura entra aqui quando o agente PROPÔS a escrita e o turno
    terminou — ou seja, a proposta chegou ao usuário. A execução só acontece
    quando a mesma assinatura volta, o que exige uma fala do usuário no meio.
    Vale só para a mensagem seguinte: o fim de cada turno substitui o conjunto
    pelas propostas daquele turno, e uma proposta velha não sobrevive a uma
    conversa no meio nem a uma mudança de ideia.
    É o gate do G-17, e ele mora no runtime justamente para o modelo não poder
    pular: nenhuma instrução de prompt adiciona nada a este conjunto.
    """
    propostas_do_turno: set[str] = field(default_factory=set)
    """Escritas propostas no turno corrente, ainda não apresentadas ao usuário."""
    audit: list[AuditEntry] = field(default_factory=list)

    def definir_idioma(self, idioma: object) -> None:
        """Idioma da sessão vindo do navegador. Trocar de idioma na tela esquece
        o idioma que a pessoa vinha usando: a troca é o sinal mais recente."""
        novo = normalizar(idioma)
        if novo != self.idioma:
            self.idioma_da_conversa = None
        self.idioma = novo

    def registrar(
        self,
        *,
        kind: str,
        tool: str,
        input: dict[str, Any],
        output: dict[str, Any],
        reason_code: str | None,
    ) -> None:
        entrada = AuditEntry(
            kind=kind,
            tool=tool,
            input=input,
            output=output,
            reason_code=reason_code,
            rules_version=self.policy.version,
            at=self.at,
        )
        self.audit.append(entrada)
        # A lista acima morre com a sessão — ela alimenta a tela e nada mais.
        # O README promete que "a versão da regra vai em cada linha do audit
        # log", e essa promessa exigia uma linha que sobrevivesse à conversa:
        # sem isto não havia como provar, depois do fato, sob qual versão da
        # política uma decisão foi tomada, que é a tese da demo inteira.
        #
        # Vai o payload completo, inclusive nome de paciente: um audit log que
        # esconde o titular do dado não serve para auditar nada. O que protege
        # aqui é o escopo — clínica fictícia, tenant isolado por sessão.
        audit_log.info(
            json.dumps(
                {
                    "conversation_id": self.conversation_id,
                    "tenant_id": self.tenant_id,
                    "kind": kind,
                    "tool": tool,
                    "reason_code": reason_code,
                    "rules_version": entrada.rules_version,
                    "at": entrada.at.isoformat(),
                    "input": input,
                    "output": output,
                },
                ensure_ascii=False,
                default=str,
            )
        )


@dataclass(frozen=True)
class ToolSpec:
    name: str
    scope: Scope
    idempotent: bool
    requires_confirmation: bool
    rate_limit_per_conversation: int
    pii_level: PiiLevel
    audit: bool
    description: str
    input_model: type[BaseModel]
    handler: Callable[..., Awaitable[Any]]
    confirm_fields: tuple[str, ...] = ()
    """Campos que definem a IDENTIDADE da escrita, para o gate de confirmação.

    Não é o input inteiro de propósito. Quem diz "pode cancelar" está
    autorizando cancelar *aquela consulta* — se o agente depois anexar o motivo
    que a pessoa acabou de dar, continua sendo a mesma operação. Já trocar a
    consulta ou o horário muda o efeito, e por isso esses campos entram aqui.
    Comparar o input inteiro travava o fluxo; comparar só a tool autorizaria
    cancelar qualquer coisa. O meio-termo é declarado, tool a tool.
    """


REGISTRY: dict[str, ToolSpec] = {}

_CAMPOS_DE_HORARIO = ("inicio", "novo_inicio")


def tool(
    *,
    scope: Scope,
    idempotent: bool,
    requires_confirmation: bool,
    rate_limit: int,
    pii_level: PiiLevel,
    audit: bool = True,
    description: str,
    input_model: type[BaseModel],
    confirm_fields: tuple[str, ...] = (),
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        if scope == "write" and not confirm_fields:
            raise ValueError(f"{fn.__name__}: scope=write exige confirm_fields")
        if scope == "write" and not requires_confirmation:
            # G-17 no caminho do código, não no prompt: uma tool de escrita sem
            # confirmação nem chega a ser registrada.
            raise ValueError(f"{fn.__name__}: scope=write exige requires_confirmation")
        REGISTRY[fn.__name__] = ToolSpec(
            name=fn.__name__,
            scope=scope,
            idempotent=idempotent,
            requires_confirmation=requires_confirmation,
            rate_limit_per_conversation=rate_limit,
            pii_level=pii_level,
            audit=audit,
            description=description,
            input_model=input_model,
            handler=fn,
            confirm_fields=confirm_fields,
        )
        return fn

    return decorator


def assinatura(nome: str, entrada: dict[str, Any]) -> str:
    """Identidade de uma operação de escrita, para o gate de confirmação.

    Usa só os `confirm_fields` da tool: confirmar "remarca para 16h" não
    autoriza executar "remarca para 17h", mas confirmar "cancela a do João"
    continua valendo depois que o agente anexa o motivo que a pessoa deu.
    Ordenada porque a mesma intenção precisa produzir a mesma assinatura.

    Horário entra na forma canônica (`tempo.instante_canonico`): "16:00-03:00"
    e "19:00Z" são o mesmo instante, e compará-los como texto fazia o gate
    pedir uma segunda confirmação para a operação que a pessoa já tinha aceito.

    Mora aqui, e não no loop do agente, porque é propriedade da TOOL e não de
    quem a chama: o servidor MCP precisa da mesma identidade para aplicar o
    mesmo gate, e importá-la de `agent.py` arrastaria o SDK da Anthropic para
    um processo que não fala com modelo nenhum.
    """
    spec = REGISTRY.get(nome)
    campos = spec.confirm_fields if spec else tuple(entrada)
    relevante = {k: entrada.get(k) for k in campos}
    for campo in _CAMPOS_DE_HORARIO:
        valor = relevante.get(campo)
        if isinstance(valor, str):
            relevante[campo] = instante_canonico(valor)
    return f"{nome}:{json.dumps(relevante, sort_keys=True, ensure_ascii=False)}"


def schema_para_anthropic(idioma: str = "pt") -> list[dict[str, Any]]:
    """Schemas no formato de tool_use da Anthropic, derivados do Pydantic.

    Uma fonte só: se o modelo Pydantic muda, o schema que o LLM vê muda junto —
    não existe caminho onde o modelo enxerga um campo que a tool não valida.
    As descrições seguem o idioma da sessão (`i18n.traduzir_tool`): é o mesmo
    schema, com o texto que o modelo lê na língua da conversa.
    """
    saida: list[dict[str, Any]] = []
    for spec in REGISTRY.values():
        descricao, schema = traduzir_tool(
            spec.name, spec.description, spec.input_model.model_json_schema(), idioma
        )
        saida.append(
            {"name": spec.name, "description": descricao, "input_schema": schema}
        )
    return saida
