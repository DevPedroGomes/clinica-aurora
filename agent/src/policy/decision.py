"""Contrato de decisão do motor de política.

Regra da casa (core-spec/policy.md): o LLM nunca decide. Ele recebe uma `Decision`
já tomada e apenas redige. Três razões, nesta ordem: jurídica (não existe defesa
para "a IA disse"), auditável (é preciso mostrar QUAL regra produziu a decisão) e
testável (regra determinística tem teste unitário; prompt não tem).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason_code: str
    """Sempre presente, inclusive quando allowed=True. É a chave do audit log."""

    explanation_hint: str
    """Chave da dica de REDAÇÃO para o modelo (`dica.*` em `src/i18n.py`), nunca a regra.

    O texto sai do catálogo no idioma da sessão. O motor devolve a chave porque
    é puro e não sabe em que língua a conversa está; antes ele devolvia o texto
    em português, e uma sessão em inglês recebia a dica em português.

    ✅ "diga que precisa de um motivo para cancelar em cima da hora"
    ❌ "a janela de cancelamento é de 24 horas"

    A diferença importa: o que entra aqui entra no contexto do LLM e pode ser
    extraído numa injeção. A política do cliente não é dado público.
    """

    value: datetime | str | int | None = None
    audit_payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.reason_code:
            raise ValueError("reason_code é obrigatório, mesmo em decisão permitida")
        if self.reason_code != self.reason_code.lower():
            raise ValueError("reason_code é snake_case minúsculo")


ALLOWED = "allowed"
