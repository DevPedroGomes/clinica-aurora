"""Avaliação com o modelo de VERDADE: a pessoa confirma uma alteração uma vez só.

O conserto da confirmação dupla tem uma parte que o LLM falso prova (o loop
pede a proposta e o gate retém, `tests/test_agent.py`) e uma que só o modelo
real mostra: se, nas conversas da verificação pós-deploy de 20/10, ele ainda
pergunta em texto. Este teste roda as linhas a e c em português e a linha a em
inglês contra o ERP em processo. Pago (cerca de US$ 0,10 as três conversas), e
por isso fora da suíte padrão:

    cd agent
    ANTHROPIC_API_KEY=... AURORA_EVAL=1 ../.venv/bin/python -m pytest -m modelo_real tests/test_avaliacao_confirmacao.py

A régua (`problemas`) roda sempre, com eventos montados à mão, e o roteiro roda
sempre com um LLM falso: o encanamento não depende de dinheiro para ser testado.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from tests.conftest import texto, uso

ROTEIROS: dict[str, list[tuple[str, list[str]]]] = {
    "pt": [
        ("a", ["Remarque a consulta da Ana para as 16h", "sim"]),
        ("c", ["Cancele a consulta do João"]),
    ],
    "en": [("a", ["Move Ana's appointment to 4pm", "yes"])],
}


async def conversar(idioma: str, falas: list[str], cliente: Any) -> list[list[Any]]:
    """Uma conversa no agente local contra o ERP em processo. Eventos por turno."""
    from erp.app import main as erp_main
    from src.adapters.erp import ErpAdapter
    from src.agent import Agente
    from src.flows.state_machine import Conversation
    from src.policy.engine import PolicyEngine
    from src.tools.registry import AgentContext

    transporte = httpx.ASGITransport(app=erp_main.app)
    turnos: list[list[Any]] = []
    async with httpx.AsyncClient(transport=transporte, base_url="http://erp") as erp:
        tenant = f"demo-eval{uuid.uuid4().hex[:8]}"
        ctx = AgentContext(
            conversation_id=tenant,
            tenant_id=tenant,
            policy=PolicyEngine(),
            erp=ErpAdapter("http://erp", tenant_id=tenant, client=erp),
            at=datetime.now(timezone.utc),
        )
        ctx.definir_idioma(idioma)
        agente, conv = Agente(cliente), Conversation()
        for fala in falas:
            ctx.at = datetime.now(timezone.utc)
            turnos.append([e async for e in agente.turno(conv, fala, ctx)])
    return turnos


def problemas(linha: str, turnos: list[list[Any]]) -> list[str]:
    """O que contraria a linha do plano. Lista vazia é a linha cumprida.

    a: uma confirmação retida no primeiro turno, nenhuma no "sim", e o "sim"
       grava. A confirmação dupla era o gate pedindo de novo no segundo turno.
    c: a regra decide no primeiro turno: ou recusa pedindo o motivo (menos de
       24 h), ou o gate retém o cancelamento. Pergunta em texto sem nenhum dos
       dois é o "É essa que você quer cancelar?" da verificação.
    """
    tipos = [[e.tipo for e in turno] for turno in turnos]
    achados: list[str] = []
    if linha == "a":
        if tipos[0].count("confirmation_required") != 1:
            achados.append("o primeiro turno não reteve exatamente uma proposta")
        if "confirmation_required" in tipos[1]:
            achados.append('o "sim" pediu confirmação de novo')
        if "write" not in tipos[1]:
            achados.append('o "sim" não gravou')
    elif linha == "c":
        regra = [
            e
            for e in turnos[0]
            if e.tipo == "policy" and e.dado.get("regra") == "cancelar_consulta"
        ]
        if not regra and "confirmation_required" not in tipos[0]:
            achados.append("a regra não decidiu no primeiro turno")
    return achados


def _evento(tipo: str, **dado: Any) -> SimpleNamespace:
    return SimpleNamespace(tipo=tipo, dado=dado)


def test_regua_reconhece_a_confirmacao_dupla():
    dupla = [
        [_evento("message", texto="Você quer remarcar para as 16h?")],
        [_evento("confirmation_required"), _evento("message", texto="Confirma?")],
    ]
    assert problemas("a", dupla) == [
        "o primeiro turno não reteve exatamente uma proposta",
        'o "sim" pediu confirmação de novo',
        'o "sim" não gravou',
    ]
    uma = [[_evento("confirmation_required")], [_evento("write")]]
    assert problemas("a", uma) == []


def test_regua_exige_que_a_regra_decida_o_cancelamento():
    so_texto = [[_evento("message", texto="É essa que você quer cancelar?")]]
    assert problemas("c", so_texto) == ["a regra não decidiu no primeiro turno"]
    recusa = [[_evento("policy", regra="cancelar_consulta", allowed=False)]]
    assert problemas("c", recusa) == []


class SoTexto:
    """LLM falso que só responde em texto: exercita o roteiro sem custo."""

    def __init__(self) -> None:
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        return SimpleNamespace(
            content=[texto("Só consigo ajudar com a agenda da clínica.")],
            stop_reason="end_turn",
            usage=uso(),
        )


async def test_roteiro_roda_ponta_a_ponta_sem_custo():
    turnos = await conversar("pt", ROTEIROS["pt"][0][1], SoTexto())
    assert len(turnos) == 2
    assert problemas("a", turnos)  # o falso não chama ferramenta: a régua acusa


@pytest.mark.modelo_real
@pytest.mark.skipif(
    not (os.getenv("ANTHROPIC_API_KEY") and os.getenv("AURORA_EVAL") == "1"),
    reason="avaliação paga: precisa de ANTHROPIC_API_KEY e AURORA_EVAL=1",
)
@pytest.mark.parametrize(
    ("idioma", "linha", "falas"),
    [
        (i, linha, falas)
        for i, roteiros in ROTEIROS.items()
        for linha, falas in roteiros
    ],
)
async def test_modelo_real_confirma_uma_vez(idioma: str, linha: str, falas: list[str]):
    from anthropic import AsyncAnthropic

    turnos = await conversar(idioma, falas, AsyncAnthropic())
    assert problemas(linha, turnos) == [], [
        (e.tipo, e.dado) for turno in turnos for e in turno if e.tipo != "usage"
    ]
