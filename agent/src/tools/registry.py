"""Registry de tools — o contrato de `core-spec/tools.md` como enforcement.

Os metadados do decorator não são documentação: o runtime lê e aplica antes de
executar. É o que impede o modelo de pular a confirmação ou de exceder o teto de
escritas por conversa, mesmo que o prompt seja manipulado.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from ..adapters.erp import ErpAdapter
from ..policy.engine import PolicyEngine

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
        self, *, kind: str, tool: str, input: dict, output: dict, reason_code: str | None
    ) -> None:
        self.audit.append(
            AuditEntry(
                kind=kind,
                tool=tool,
                input=input,
                output=output,
                reason_code=reason_code,
                rules_version=self.policy.version,
                at=self.at,
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
