"""O loop do agente.

Manual de propósito. O tool runner do SDK resolveria o laço, mas aqui o laço
NÃO é só "chamou tool, executa tool": entre a decisão do modelo e a execução
existem três portões que precisam rodar no caminho do código —

  1. o gate de confirmação (G-17): escrita só executa se a MESMA operação foi
     proposta antes e o usuário falou depois;
  2. o teto de escritas por conversa (P-5);
  3. a política, que decide, e o audit, que registra a decisão.

Nenhum dos três pode depender de o modelo lembrar de respeitá-los.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

from .flows.state_machine import Conversation, State
from .prompts.system import PROMPT_ID, system_blocks
from .tools.agenda import (
    abrir_vaga,
    cancelar_consulta,
    listar_agenda,
    marcar_consulta,
    remarcar_consulta,
)
from .tools.registry import REGISTRY, AgentContext, ToolError, schema_para_anthropic

# Modelo de conversa da arquitetura de referência (docs/03-arquitetura.md).
MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-5")
MAX_TOKENS = 4096
# `low` mantém a demo responsiva. O trabalho aqui é estreito — quatro tools e
# uma agenda pequena — e não é o tipo de tarefa que se beneficia de esforço alto.
EFFORT = os.getenv("AGENT_EFFORT", "low")
BAHIA = ZoneInfo("America/Bahia")
MAX_ITERACOES = 6

# O que dizer quando o modelo devolve um turno inteiro sem texto nenhum. Calar
# é a pior saída: a pessoa fala, some o "pensando…" e nada aparece — parece
# quebrado. Estas frases são as ÚNICAS que este arquivo põe na boca do agente, e
# nenhuma delas afirma que algo foi feito.
#
# Em inglês porque a interface é em inglês: são as duas frases que o CÓDIGO
# emite, então não têm como seguir o idioma da pessoa como o modelo segue. É
# raro chegarem à tela, e o inglês é o idioma padrão da demo.
FALLBACK_CONFIRMACAO = "Shall I go ahead?"
FALLBACK_MUDO = "Sorry, I lost track. Could you say that again?"

HANDLERS = {
    "listar_agenda": listar_agenda,
    "remarcar_consulta": remarcar_consulta,
    "cancelar_consulta": cancelar_consulta,
    "abrir_vaga": abrir_vaga,
    "marcar_consulta": marcar_consulta,
}


def assinatura(nome: str, entrada: dict[str, Any]) -> str:
    """Identidade de uma operação de escrita, para o gate de confirmação.

    Usa só os `confirm_fields` da tool: confirmar "remarca para 16h" não
    autoriza executar "remarca para 17h", mas confirmar "cancela a do João"
    continua valendo depois que o agente anexa o motivo que a pessoa deu.
    Ordenada porque a mesma intenção precisa produzir a mesma assinatura.
    """
    spec = REGISTRY.get(nome)
    campos = spec.confirm_fields if spec else tuple(entrada)
    relevante = {k: entrada.get(k) for k in campos}
    return f"{nome}:{json.dumps(relevante, sort_keys=True, ensure_ascii=False)}"


@dataclass
class Evento:
    """O que a tela mostra. O log de decisão da demo é literalmente isto."""

    tipo: str
    dado: dict[str, Any]


class Agente:
    def __init__(self, client: AsyncAnthropic | None = None) -> None:
        self._client = client or AsyncAnthropic()

    async def turno(
        self, conv: Conversation, texto_do_usuario: str, ctx: AgentContext
    ) -> AsyncIterator[Evento]:
        if conv.state is State.ESCALATED:
            yield Evento(
                "escalated", {"motivo": conv.escalation_reason, "ja_estava": True}
            )
            return

        gatilho = conv.registrar_turno()
        if gatilho:
            conv.escalar(gatilho)
            yield Evento("escalated", {"motivo": gatilho})
            return

        conv.historico.append(
            {"role": "user", "content": _com_contexto(texto_do_usuario, ctx)}
        )
        if conv.state is State.GREETING:
            conv.transicionar(State.QUALIFYING)
        yield Evento("state", {"estado": conv.state.value})

        for _ in range(MAX_ITERACOES):
            # Quando o gate pede confirmação, o turno segue SEM ferramentas: a
            # única coisa que falta é falar com a pessoa. Deixar as tools na mesa
            # convida o modelo a tentar a mesma escrita de novo, e ele tenta —
            # três vezes seguidas, no teste com o modelo real, até concluir
            # sozinho que "o sistema está recusando". Tirar a ferramenta remove
            # a tentação sem precisar pedir bom senso ao modelo.
            ferramentas = [] if ctx.propostas_do_turno else schema_para_anthropic()
            try:
                resposta = await self._client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system_blocks(),
                    messages=conv.historico,
                    tools=ferramentas,
                    output_config={"effort": EFFORT},
                    metadata={"user_id": f"tenant:{ctx.tenant_id}"},
                )
            except Exception as exc:  # noqa: BLE001 — erro de provedor nunca vaza cru
                conv.escalar("tool_failure_repeated")
                yield Evento(
                    "error", {"mensagem": "The assistant is unavailable right now."}
                )
                yield Evento(
                    "escalated",
                    {"motivo": "tool_failure_repeated", "detalhe": type(exc).__name__},
                )
                return

            if resposta.stop_reason == "refusal":
                # O modelo recusou por segurança. Não insista: escale.
                conv.escalar("out_of_scope")
                yield Evento(
                    "message",
                    {"texto": "I can't help with that. Let me get a human for you."},
                )
                yield Evento("escalated", {"motivo": "out_of_scope"})
                return

            # Bloco de texto VAZIO não pode entrar no histórico. Quando o gate de
            # confirmação tira as ferramentas da mesa, o modelo às vezes devolve
            # só raciocínio e um `text` em branco: ele não vira mensagem na tela
            # (turno mudo) mas ia inteiro para o histórico, e a API recusava a
            # chamada SEGUINTE com `text content blocks must be non-empty` — 400
            # que caía no `except` acima e ESCALAVA a conversa para sempre. Uma
            # pessoa confirmava, não via resposta, insistia, e perdia a sessão.
            blocos = [b for b in resposta.content if b.type != "text" or b.text.strip()]
            texto = "".join(b.text for b in blocos if b.type == "text").strip()
            chamadas = [b for b in blocos if b.type == "tool_use"]

            if not texto and not chamadas:
                # Turno sem nada visível e sem nada a executar: em vez de sumir,
                # fala. O mesmo texto vai para o histórico, senão o que o modelo
                # lembra e o que a pessoa leu divergem no turno seguinte.
                texto = (
                    FALLBACK_CONFIRMACAO
                    if conv.state is State.CONFIRMING
                    else FALLBACK_MUDO
                )
                blocos = [*blocos, {"type": "text", "text": texto}]

            conv.historico.append({"role": "assistant", "content": blocos})

            # Texto que vem JUNTO de uma chamada de ferramenta não vai para a
            # tela. É recheio — "um instante, vou verificar" — e some do fluxo
            # sem custo: o log da direita já mostra a chamada, e a resposta de
            # verdade vem na iteração seguinte, sempre. Cortar aqui resolve duas
            # coisas de uma vez: o recheio era o único lugar onde o idioma ainda
            # escapava para português, e era ele que fazia o agente pedir a
            # confirmação DUAS vezes seguidas, uma antes da tool e outra depois.
            if texto and not chamadas:
                yield Evento("message", {"texto": texto})

            if not chamadas:
                # Turno terminou em texto — ou seja, a proposta chegou à pessoa.
                # Só agora ela vira confirmação pendente, e só na próxima fala
                # dela a escrita pode executar. É este passo que garante que
                # existiu um humano entre a intenção e a gravação.
                ctx.confirmacoes.update(ctx.propostas_do_turno)
                ctx.propostas_do_turno.clear()
                return

            resultados = []
            for chamada in chamadas:
                async for evento in self._executar(chamada, conv, ctx, resultados):
                    yield evento

            conv.historico.append({"role": "user", "content": resultados})

            if conv.state is State.ESCALATED:
                yield Evento("escalated", {"motivo": conv.escalation_reason})
                return

        conv.escalar("turn_limit")
        yield Evento("escalated", {"motivo": "turn_limit"})

    async def _executar(
        self,
        chamada: Any,
        conv: Conversation,
        ctx: AgentContext,
        resultados: list[dict],
    ) -> AsyncIterator[Evento]:
        spec = REGISTRY.get(chamada.name)
        if spec is None:
            resultados.append(
                _erro(chamada.id, "tool_desconhecida", "Não consigo fazer isso.")
            )
            return

        try:
            entrada = spec.input_model(**chamada.input)
        except Exception:  # noqa: BLE001 — schema inválido é dado do modelo, não exceção do sistema
            resultados.append(
                _erro(
                    chamada.id,
                    "input_invalido",
                    "Não entendi os detalhes. Pode repetir?",
                )
            )
            return

        yield Evento(
            "tool_call",
            {"tool": spec.name, "scope": spec.scope, "input": chamada.input},
        )

        if spec.scope == "write":
            sig = assinatura(spec.name, entrada.model_dump(mode="json"))

            # Gate de confirmação (G-17). O modelo pode ter sido convencido a
            # pular a etapa; o runtime não.
            if spec.requires_confirmation and sig not in ctx.confirmacoes:
                ctx.propostas_do_turno.add(sig)
                conv.avancar(State.CONFIRMING)
                # O gate PRECISA aparecer no log de decisão. Sem esta linha a
                # tela mostrava duas chamadas idênticas de escrita e nada entre
                # elas — quem lia concluía escrita dupla, quando na verdade a
                # primeira foi barrada aqui e nunca chegou ao ERP. É a garantia
                # central da demo (G-17) ficando invisível justamente no painel
                # construído para prová-la.
                yield Evento(
                    "confirmation_required",
                    {"tool": spec.name, "input": chamada.input},
                )
                yield Evento(
                    "state",
                    {"estado": conv.state.value, "motivo": "aguardando confirmação"},
                )
                resultados.append(
                    _erro(
                        chamada.id,
                        "confirmation_required",
                        "Antes de executar: confirme com a pessoa o que vai ser feito e aguarde a resposta dela.",
                    )
                )
                return

            decisao_quota = ctx.policy.pode_escrever(
                escritas_na_conversa=ctx.escritas_realizadas
            )
            # Esta decisão é do teto de escritas (P-5), não da regra da tool, e
            # precisa ser gravada com nome próprio: registrada sob `spec.name`
            # ela produzia DUAS linhas de audit chamadas "remarcar_consulta"
            # para decisões diferentes, distinguíveis só pelo formato do
            # payload. Qual tool o teto estava barrando entra no payload, que é
            # onde a informação não some.
            ctx.registrar(
                kind="policy_decision",
                tool="write_quota",
                input={**decisao_quota.audit_payload, "tool": spec.name},
                output={"allowed": decisao_quota.allowed},
                reason_code=decisao_quota.reason_code,
            )
            yield Evento(
                "policy",
                {
                    "regra": "write_quota",
                    "allowed": decisao_quota.allowed,
                    "reason_code": decisao_quota.reason_code,
                    # Sem isto a linha do teto era a única do log sem o selo da
                    # versão, do lado de outra que tinha — parecia que a versão
                    # valia para umas decisões e não para outras.
                    "rules_version": ctx.policy.version,
                },
            )
            if not decisao_quota.allowed:
                conv.escalar("policy_denied_twice")
                resultados.append(
                    _erro(
                        chamada.id,
                        decisao_quota.reason_code,
                        "Preciso passar para a recepção para continuar.",
                    )
                )
                return

            conv.avancar(State.EXECUTING)
            yield Evento("state", {"estado": conv.state.value})

        antes = len(ctx.audit)
        saida = await HANDLERS[spec.name](ctx, entrada)
        for entrada_audit in ctx.audit[antes:]:
            if entrada_audit.kind == "policy_decision":
                yield Evento(
                    "policy",
                    {
                        "regra": entrada_audit.tool,
                        "allowed": entrada_audit.output.get("allowed"),
                        "reason_code": entrada_audit.reason_code,
                        "rules_version": entrada_audit.rules_version,
                    },
                )

        if isinstance(saida, ToolError):
            resultados.append(
                _erro(chamada.id, saida.code, saida.user_message, saida.retry_hint)
            )
            gatilho = (
                conv.registrar_falha_de_tool()
                if saida.code in {"erp_unavailable", "erp_rejected"}
                else conv.registrar_negativa()
            )
            yield Evento("tool_error", {"tool": spec.name, "code": saida.code})
            if gatilho:
                conv.escalar(gatilho)
            elif conv.state is State.EXECUTING:
                conv.avancar(State.COLLECTING)
            return

        conv.registrar_sucesso()
        if spec.scope == "write":
            ctx.confirmacoes.discard(
                assinatura(spec.name, entrada.model_dump(mode="json"))
            )
            yield Evento("write", {"tool": spec.name, "resultado": saida})
            conv.avancar(State.RESOLVED)
            yield Evento("state", {"estado": conv.state.value})
        else:
            yield Evento("tool_result", {"tool": spec.name, "resultado": saida})

        resultados.append(
            {
                "type": "tool_result",
                "tool_use_id": chamada.id,
                "content": json.dumps(saida, ensure_ascii=False),
            }
        )


def _com_contexto(texto: str, ctx: AgentContext) -> str:
    """Prende o relógio, o cadastro e o idioma ao turno — depois do cacheado.

    "Amanhã" só significa alguma coisa se o modelo souber que dia é hoje. A
    tentação é escrever isso no system prompt, e é o erro clássico: o prompt
    é o começo do prefixo, então uma data ali muda os bytes a cada turno e
    invalida o cache de TUDO que vem depois. Aqui o contexto entra no turno do
    usuário, onde não há prefixo nenhum para estragar.

    Entra como bloco marcado justamente porque o prompt manda tratar conteúdo
    de dado como dado: é contexto operacional, não uma instrução nova.

    A linha do idioma está AQUI, e não só no system prompt, porque a regra lá
    não segurou: o prompt inteiro é em português, as tools são em português e
    os resultados voltam com chaves em português. Contra todo esse sinal, uma
    frase no começo do prefixo perde — testado, o agente respondia em português
    a uma pergunta em inglês. Repetida a cada turno, colada na mensagem da
    pessoa, ela é a instrução mais recente que o modelo lê.
    """
    local = ctx.at.astimezone(BAHIA)
    return (
        "<contexto_operacional>\n"
        f"agora: {local.strftime('%A, %d/%m/%Y %H:%M')} ({local.tzname()})\n"
        "profissionais: prof_bruno (Dr. Bruno Salles) · prof_helena (Dra. Helena Costa)\n"
        # O cadastro de pacientes entra aqui pelo mesmo motivo dos profissionais:
        # `marcar_consulta` recebe um id de domínio fechado, e sem a lista o
        # modelo não teria como traduzir "marca para a Ana" no id certo.
        "pacientes: pac_ana (Ana Ribeiro) · pac_joao (João Meireles) · pac_marta (Marta Lins)\n"
        "  Só existem estes três. Pedido para outra pessoa: diga que ela não está\n"
        "  no cadastro e que a recepção precisa cadastrá-la antes.\n"
        "idioma da resposta: o MESMO da mensagem abaixo, sempre. Se ela vier em\n"
        "  inglês, responda em inglês; em espanhol, em espanhol; e assim por\n"
        "  diante. Não é o idioma deste bloco nem o das ferramentas.\n"
        "</contexto_operacional>\n"
        f"{texto}"
    )


def _erro(tool_use_id: str, code: str, mensagem: str, hint: str | None = None) -> dict:
    corpo = {"error": code, "message": mensagem}
    if hint:
        corpo["hint"] = hint
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": json.dumps(corpo, ensure_ascii=False),
        "is_error": True,
    }


__all__ = ["Agente", "Evento", "MODEL", "PROMPT_ID", "assinatura"]
