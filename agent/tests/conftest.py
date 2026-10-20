"""Dublês do SDK da Anthropic, divididos entre `test_agent.py` e `test_cenarios.py`.

O LLM é falso de propósito: estes dublês existem para provar o que o runtime
garante INDEPENDENTEMENTE do que o modelo decida fazer. Nenhum teste da suíte faz
chamada paga.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.agent import Agente
from src.flows.state_machine import Conversation
from src.tools.registry import AgentContext


def texto(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


def tool_use(nome: str, entrada: dict, id_: str = "toolu_1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id_, name=nome, input=entrada)


def uso(entrada: int = 40, saida: int = 20, leu: int = 3466, escreveu: int = 0):
    """`usage` plausível: prefixo inteiro vindo do cache, é o caso normal.

    O default tem `cache_read` preenchido de propósito. O turno que NÃO lê cache
    é a anomalia nesta demo, e um teste cujo default fosse zero deixaria passar
    de novo o bug que esta fixture existe para vigiar.
    """
    return SimpleNamespace(
        input_tokens=entrada,
        output_tokens=saida,
        cache_read_input_tokens=leu,
        cache_creation_input_tokens=escreveu,
    )


class FakeLLM:
    """Devolve respostas roteirizadas, uma por chamada."""

    def __init__(self, *respostas: list) -> None:
        self._respostas = list(respostas)
        self.chamadas: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.chamadas.append(kwargs)
        if not self._respostas:
            return SimpleNamespace(
                content=[texto("(fim)")], stop_reason="end_turn", usage=uso()
            )
        blocos = self._respostas.pop(0)
        parou = "tool_use" if any(b.type == "tool_use" for b in blocos) else "end_turn"
        return SimpleNamespace(content=blocos, stop_reason=parou, usage=uso())


async def coletar(
    agente: Agente, conv: Conversation, texto_usuario: str, ctx: AgentContext
) -> list:
    return [e async for e in agente.turno(conv, texto_usuario, ctx)]
