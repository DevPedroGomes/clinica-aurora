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
from ..policy.engine import PolicyEngine

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
    """Texto em pt-BR já pronto para o usuário final. Nunca contém erro cru."""
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
    confirmacoes: set[str] = field(default_factory=set)
    """Assinaturas de escrita já confirmadas pelo usuário nesta conversa.

    Uma assinatura entra aqui quando o agente PROPÔS a escrita e o turno
    terminou — ou seja, a proposta chegou ao usuário. A execução só acontece
    quando a mesma assinatura volta, o que exige uma fala do usuário no meio.
    É o gate do G-17, e ele mora no runtime justamente para o modelo não poder
    pular: nenhuma instrução de prompt adiciona nada a este conjunto.
    """
    propostas_do_turno: set[str] = field(default_factory=set)
    """Escritas propostas no turno corrente, ainda não apresentadas ao usuário."""
    audit: list[AuditEntry] = field(default_factory=list)

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


def schema_para_anthropic() -> list[dict[str, Any]]:
    """Schemas no formato de tool_use da Anthropic, derivados do Pydantic.

    Uma fonte só: se o modelo Pydantic muda, o schema que o LLM vê muda junto —
    não existe caminho onde o modelo enxerga um campo que a tool não valida.
    """
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.input_model.model_json_schema(),
        }
        for spec in REGISTRY.values()
    ]
