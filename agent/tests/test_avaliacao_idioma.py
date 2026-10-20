"""Avaliação com o modelo de VERDADE: N conversas em inglês, zero respostas em português.

O teste pago fica fora da suíte padrão de propósito. Ele gasta dinheiro (de 2 a
8 chamadas pagas por conversa) e depende do provedor, e o resto da suíte prova o
que o código garante com o LLM falso. Roda só com as duas variáveis:

    cd agent
    ANTHROPIC_API_KEY=... AURORA_EVAL=1 ../.venv/bin/python -m pytest -m modelo_real

`AURORA_EVAL_CONVERSAS` muda o N (padrão 10, o roteiro inteiro). Os outros dois
testes daqui rodam sempre: a régua da avaliação e o encanamento do script, com
o LLM falso e o ERP de verdade em processo.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from scripts.avaliar_idioma import avaliar, parece_portugues
from tests.conftest import texto, uso


def test_classificador_da_avaliacao_e_independente_do_detector():
    """A régua da avaliação não é a da correção: outra lista, e o acento conta."""
    assert parece_portugues("Pronto, remarquei a consulta para as 16h.")
    assert parece_portugues("Não consegui.")
    assert not parece_portugues("Done, João Meireles's appointment is now at 4 PM.")
    assert not parece_portugues("Clínica Aurora: Dra. Helena Costa is free at 3 PM.")


class SempreResponde:
    """LLM falso que responde sempre o mesmo texto, sem ferramenta."""

    def __init__(self, resposta: str) -> None:
        self.resposta = resposta
        self.chamadas = 0
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.chamadas += 1
        return SimpleNamespace(
            content=[texto(self.resposta)], stop_reason="end_turn", usage=uso()
        )


async def test_avaliacao_roda_ponta_a_ponta_sem_custo():
    """O roteiro inteiro, com ERP em processo e caos, mas com o LLM falso."""
    ingles = await avaliar("en", 10, cliente=SempreResponde("Done, nothing changed."))
    assert ingles.conversas == 10
    assert ingles.respostas >= 10
    assert ingles.em_portugues == 0
    assert ingles.reescritas == 0

    # Resposta em português numa sessão em inglês: a checagem pede UMA reescrita,
    # o falso devolve o mesmo texto, e a avaliação conta a resposta como português.
    errado = await avaliar(
        "en", 2, cliente=SempreResponde("Pronto, não mudei nada na agenda.")
    )
    assert errado.em_portugues == errado.respostas >= 2
    assert errado.reescritas == errado.reescritas_sem_sucesso == errado.respostas


@pytest.mark.modelo_real
@pytest.mark.skipif(
    not (os.getenv("ANTHROPIC_API_KEY") and os.getenv("AURORA_EVAL") == "1"),
    reason="avaliação paga: precisa de ANTHROPIC_API_KEY e AURORA_EVAL=1",
)
async def test_conversas_em_ingles_nao_saem_em_portugues():
    n = int(os.getenv("AURORA_EVAL_CONVERSAS", "10"))
    resultado = await avaliar("en", n)
    assert resultado.conversas == n
    assert resultado.respostas >= n
    assert resultado.em_portugues == 0, resultado.resumo()
