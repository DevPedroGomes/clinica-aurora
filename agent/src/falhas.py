"""Falha na chamada ao provedor de LLM: classificar, registrar, nunca escalar.

Até 2026-09 toda exceção de `messages.create` virava `tool_failure_repeated` e
a conversa escalava para "um humano". Três erros num só: não era falha de
ferramenta, não havia humano nenhum, e o log guardava só o nome da classe
(`BadRequestError`), o que não distingue saldo acabado de parâmetro recusado.
A demo ficou fora do ar sem que o log dissesse por quê.

Aqui a exceção vira um código estável, que é o que a tela e o log usam, e uma
linha de diagnóstico com status, tipo do erro e request id do provedor. A
mensagem do provedor vai para o log do servidor, sanitizada e truncada, e
nunca para o visitante.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import anthropic

# Mensagem de erro do provedor não carrega a chave, mas o custo de conferir é
# uma regex, e o de errar é uma credencial no log.
_SEGREDO = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
_DETALHE_MAX = 300


@dataclass(frozen=True)
class FalhaDoProvedor:
    codigo: str
    """Estável e sem segredo: é o que a tela recebe e o que o log agrupa."""
    status: int | None
    tipo_erro: str | None
    request_id: str | None
    classe: str
    detalhe: str
    """Mensagem do provedor, sanitizada. Só para o log do servidor."""

    def log_payload(self) -> dict[str, Any]:
        return {
            "codigo": self.codigo,
            "status": self.status,
            "tipo_erro": self.tipo_erro,
            "request_id": self.request_id,
            "classe": self.classe,
            "detalhe": self.detalhe,
        }


def sanitizar(texto: str) -> str:
    return _SEGREDO.sub("[redacted]", texto)[:_DETALHE_MAX]


def classificar(exc: BaseException) -> FalhaDoProvedor:
    status: int | None = None
    tipo_erro: str | None = None
    request_id: str | None = None
    mensagem = str(exc)

    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
        request_id = exc.request_id
        corpo = exc.body if isinstance(exc.body, dict) else {}
        erro = corpo.get("error") if isinstance(corpo.get("error"), dict) else {}
        tipo_erro = erro.get("type") if isinstance(erro.get("type"), str) else None
        if isinstance(erro.get("message"), str):
            mensagem = erro["message"]

    return FalhaDoProvedor(
        codigo=_codigo(exc, status, mensagem),
        status=status,
        tipo_erro=tipo_erro,
        request_id=request_id,
        classe=type(exc).__name__,
        detalhe=sanitizar(mensagem),
    )


def _codigo(exc: BaseException, status: int | None, mensagem: str) -> str:
    # Timeout antes de conexão: `APITimeoutError` é subclasse de
    # `APIConnectionError`, e a ordem inversa nunca chegaria aqui.
    if isinstance(exc, anthropic.APITimeoutError):
        return "provider_timeout"
    if isinstance(exc, anthropic.APIConnectionError):
        return "provider_unreachable"
    if status is None:
        # Não veio do provedor: exceção levantada no nosso lado antes ou depois
        # da requisição (argumento que o SDK não aceita, por exemplo). É bug do
        # agente, e chamar de "provedor fora" esconderia isso.
        return "agent_internal_error"
    if status == 400:
        # Saldo insuficiente chega como 400 `invalid_request_error`, igual a um
        # parâmetro recusado. Só a mensagem separa os dois, e a correção de um
        # é financeira, a do outro é código.
        if "credit balance" in mensagem.lower():
            return "provider_billing"
        return "provider_bad_request"
    if status == 401:
        return "provider_auth"
    if status == 403:
        return "provider_permission"
    if status == 404:
        return "provider_not_found"
    if status == 413:
        return "provider_bad_request"
    if status == 429:
        return "provider_rate_limited"
    if status == 529:
        return "provider_overloaded"
    if status >= 500:
        return "provider_server_error"
    return "provider_error"
