"""State machine da conversa.

Nunca "agente livre" (G-06). Agente livre parece elegante na demo e vira
imprevisível em produção: entra em loop, pula a confirmação, esquece o que já
perguntou. Estado explícito é o que torna o comportamento testável.

`CONFIRMING` é obrigatório antes de toda escrita (G-17) — e isso é interceptor
no caminho do código, não instrução de prompt. O modelo não tem como pular.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class State(str, Enum):
    GREETING = "greeting"
    QUALIFYING = "qualifying"
    COLLECTING = "collecting"
    CONFIRMING = "confirming"
    EXECUTING = "executing"
    RESOLVED = "resolved"
    ESCALATED = "escalated"


TRANSITIONS: dict[State, list[State]] = {
    State.GREETING: [State.QUALIFYING, State.ESCALATED],
    State.QUALIFYING: [State.COLLECTING, State.RESOLVED, State.ESCALATED],
    State.COLLECTING: [State.CONFIRMING, State.QUALIFYING, State.RESOLVED, State.ESCALATED],
    State.CONFIRMING: [State.EXECUTING, State.COLLECTING, State.ESCALATED],
    State.EXECUTING: [State.RESOLVED, State.COLLECTING, State.ESCALATED],
    State.RESOLVED: [State.QUALIFYING, State.ESCALATED],
    State.ESCALATED: [],  # terminal para a IA: humano decide se devolve
}


class TransitionError(Exception):
    """Transição não declarada é bug, não criatividade."""


# Os cinco gatilhos do core-spec, mais nada. Cada um é verificável no código —
# nenhum depende de o modelo "perceber" que deveria escalar.
ESCALATION_TRIGGERS = (
    "explicit_request",  # o usuário pediu uma pessoa
    "policy_denied_twice",  # duas negativas seguidas de política
    "out_of_scope",  # saiu do escopo três vezes
    "tool_failure_repeated",  # falha de ferramenta depois do retry
    "turn_limit",  # a conversa passou do teto de turnos
)

MAX_TURNS = 20
MAX_OUT_OF_SCOPE = 3
MAX_POLICY_DENIALS = 2


@dataclass
class Conversation:
    """Estado da conversa. Em produção isto vive no banco — o processo pode
    morrer entre turnos. Nesta demo a sessão é efêmera por desenho (escopo §7).
    """

    state: State = State.GREETING
    turns: int = 0
    escritas: int = 0
    negativas_seguidas: int = 0
    fora_de_escopo: int = 0
    falhas_de_tool: int = 0
    escalation_reason: str | None = None
    historico: list[dict] = field(default_factory=list)

    def transicionar(self, destino: State) -> None:
        if destino == self.state:
            return
        if destino not in TRANSITIONS[self.state]:
            raise TransitionError(f"{self.state.value} -> {destino.value} não é uma transição declarada")
        self.state = destino

    def avancar(self, destino: State) -> list[State]:
        """Caminha até `destino` pelo caminho mais curto de transições DECLARADAS.

        O agente costuma pular etapas que aconteceram numa tacada só — quando a
        pessoa manda "remarca a da Ana pras 16h", qualificar e coletar acontecem
        na mesma frase. Deixar o código escrever `avancar(CONFIRMING)` mantém a
        intenção legível sem afrouxar a máquina: o caminho percorrido continua
        sendo só de transições declaradas, e um destino inalcançável ainda é um
        erro, não uma criatividade.
        """
        if destino == self.state:
            return []

        anteriores: dict[State, State] = {}
        fila = [self.state]
        while fila:
            atual = fila.pop(0)
            for proximo in TRANSITIONS[atual]:
                if proximo in anteriores or proximo == self.state:
                    continue
                anteriores[proximo] = atual
                if proximo == destino:
                    fila = []
                    break
                fila.append(proximo)

        if destino not in anteriores:
            raise TransitionError(f"{self.state.value} -> {destino.value} não tem caminho declarado")

        caminho: list[State] = []
        no = destino
        while no != self.state:
            caminho.append(no)
            no = anteriores[no]
        caminho.reverse()

        for passo in caminho:
            self.transicionar(passo)
        return caminho

    def escalar(self, motivo: str) -> None:
        if motivo not in ESCALATION_TRIGGERS:
            raise ValueError(f"gatilho de escalação desconhecido: {motivo}")
        self.escalation_reason = motivo
        self.state = State.ESCALATED

    # -- contadores que decidem escalação --------------------------------------

    def registrar_turno(self) -> str | None:
        self.turns += 1
        if self.turns > MAX_TURNS:
            return "turn_limit"
        return None

    def registrar_negativa(self) -> str | None:
        self.negativas_seguidas += 1
        if self.negativas_seguidas >= MAX_POLICY_DENIALS:
            return "policy_denied_twice"
        return None

    def registrar_sucesso(self) -> None:
        self.negativas_seguidas = 0
        self.falhas_de_tool = 0

    def registrar_fora_de_escopo(self) -> str | None:
        self.fora_de_escopo += 1
        if self.fora_de_escopo >= MAX_OUT_OF_SCOPE:
            return "out_of_scope"
        return None

    def registrar_falha_de_tool(self) -> str | None:
        self.falhas_de_tool += 1
        if self.falhas_de_tool >= 2:
            return "tool_failure_repeated"
        return None
