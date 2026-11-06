"""O loop do agente.

Manual de propósito. O tool runner do SDK resolveria o laço, mas aqui o laço
NÃO é só "chamou tool, executa tool": entre a decisão do modelo e a execução
existem três portões que precisam rodar no caminho do código —

  1. o gate de confirmação: escrita só executa se a MESMA operação foi
     proposta antes e o usuário falou depois;
  2. o teto de escritas por conversa (P-5);
  3. a política, que decide, e o audit, que registra a decisão.

Nenhum dos três pode depender de o modelo lembrar de respeitá-los.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

from . import detector_idioma, i18n
from .confirmacao_em_texto import pede_confirmacao
from .falhas import FalhaDoProvedor, classificar
from .flows.state_machine import Conversation, State
from .pricing import custo as calcular_custo
from .prompts.system import PROMPT_ID, system_blocks
from .tools.agenda import (
    abrir_vaga,
    buscar_paciente,
    cancelar_consulta,
    listar_agenda,
    marcar_consulta,
    remarcar_consulta,
)
from .tools.registry import (
    REGISTRY,
    AgentContext,
    ToolError,
    assinatura,
    schema_para_anthropic,
)

# Modelo de conversa. Modelo sem tarifa em `pricing.yaml` é erro no cálculo de custo.
MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-5")
MAX_TOKENS = 4096
# `low` mantém a demo responsiva. O trabalho aqui é estreito — seis tools e
# uma agenda pequena — e não é o tipo de tarefa que se beneficia de esforço alto.
EFFORT = os.getenv("AGENT_EFFORT", "low")
BAHIA = ZoneInfo("America/Bahia")
MAX_ITERACOES = 6
# Quantas vezes o turno pede ao modelo para reescrever uma resposta que saiu no
# idioma errado. Uma: a reescrita com a instrução explícita resolve, e uma
# segunda chamada paga por turno já é o teto que vale a pena gastar nisso.
MAX_REESCRITAS_DE_IDIOMA = 1

# Modelos que recusam `tool_choice` forçado (`any`/`tool`) com 400. Neles o
# pedido da proposta vai com `auto` e a instrução, e o loop confere se a chamada
# veio. O padrão (`claude-sonnet-5`) aceita `any`, e aí a chamada é garantida no
# nível da amostragem, não pela boa vontade do modelo.
MODELOS_SEM_TOOL_CHOICE_FORCADO = (
    "claude-sonnet-5-5",
    "claude-opus-5-5",
    "claude-fable-5-1",
    "claude-mythos-5-1",
)

# O que dizer quando o modelo devolve um turno inteiro sem texto nenhum. Calar
# é a pior saída: a pessoa fala, some o "pensando…" e nada aparece — parece
# quebrado. Estas frases são as ÚNICAS que este arquivo põe na boca do agente, e
# nenhuma delas afirma que algo foi feito.
#
# Vêm do catálogo (`i18n.py`, chaves `fala.*`) no idioma da SESSÃO: são as
# frases que o CÓDIGO emite. É raro chegarem à tela.
FALLBACK_CONFIRMACAO = i18n.texto("fala.confirmacao", "pt")
FALLBACK_MUDO = i18n.texto("fala.mudo", "pt")

# O que entra no histórico quando uma escrita JÁ foi gravada e a chamada
# seguinte ao provedor falhou (`nota.escrita_sem_resposta`). Sem isto o modelo,
# no próximo turno, não saberia se a pessoa leu a confirmação, e a pessoa não
# leu: a tela avisa por código.

log = logging.getLogger("aurora.agent")


def _frase(ctx: AgentContext, chave: str) -> str:
    return i18n.texto(f"fala.{chave}", ctx.idioma)


HANDLERS = {
    "buscar_paciente": buscar_paciente,
    "listar_agenda": listar_agenda,
    "remarcar_consulta": remarcar_consulta,
    "cancelar_consulta": cancelar_consulta,
    "abrir_vaga": abrir_vaga,
    "marcar_consulta": marcar_consulta,
}


@dataclass
class Evento:
    """O que a tela mostra. O log de decisão da demo é literalmente isto."""

    tipo: str
    dado: dict[str, Any]


@dataclass
class Saude:
    """Resultado da última chamada ao provedor, para o /health e para a tela.

    O /health respondia "ok" com o agente parado havia horas: ele provava que
    o processo estava de pé, e a tela mostrava o ERP "online". Nenhum dos dois
    dizia se o assistente conseguia responder. Isto não chama o provedor (cada
    checagem custaria dinheiro); só guarda como terminou a última tentativa real.
    """

    ultimo_ok_em: float | None = None
    ultima_falha_em: float | None = None
    ultima_falha_codigo: str | None = None

    def registrar_ok(self) -> None:
        self.ultimo_ok_em = time.time()

    def registrar_falha(self, codigo: str) -> None:
        self.ultima_falha_em = time.time()
        self.ultima_falha_codigo = codigo

    def resumo(self) -> dict[str, str | None]:
        if self.ultima_falha_em is None and self.ultimo_ok_em is None:
            return {"agente": "sem_dados", "motivo": None}
        if self.ultima_falha_em is not None and (
            self.ultimo_ok_em is None or self.ultima_falha_em > self.ultimo_ok_em
        ):
            return {"agente": "indisponivel", "motivo": self.ultima_falha_codigo}
        return {"agente": "ok", "motivo": None}


class Agente:
    def __init__(self, client: AsyncAnthropic | None = None) -> None:
        self._client = client or AsyncAnthropic()
        self.saude = Saude()

    async def turno(
        self, conv: Conversation, texto_do_usuario: str, ctx: AgentContext
    ) -> AsyncIterator[Evento]:
        if conv.state is State.ESCALATED:
            yield Evento(
                "escalated", {"motivo": conv.escalation_reason, "ja_estava": True}
            )
            return

        # Foto do começo do turno. Se o provedor falhar antes de qualquer escrita,
        # o turno é desfeito inteiro e a pessoa pode simplesmente mandar de novo:
        # nada no ERP mudou, então repetir não duplica nada.
        estado_inicial = conv.state
        turnos_iniciais = conv.turns
        tamanho_do_historico = len(conv.historico)
        escritas_iniciais = ctx.escritas_realizadas
        idioma_da_conversa_inicial = ctx.idioma_da_conversa

        # Em que idioma este turno responde. Decidido pelo CÓDIGO e dito ao
        # modelo, em vez de pedir a ele que adivinhe pela mensagem: um "ok" ou
        # um "yes" curto perdia para o português do resto do contexto, e a
        # confirmação voltava em português numa sessão em inglês.
        alvo = detector_idioma.idioma_da_resposta(
            texto_do_usuario, ctx.idioma, ctx.idioma_da_conversa
        )
        if detector_idioma.detectar(texto_do_usuario, minimo=1):
            ctx.idioma_da_conversa = alvo

        gatilho = conv.registrar_turno()
        if gatilho:
            conv.escalar(gatilho)
            yield Evento("escalated", {"motivo": gatilho})
            return

        conv.historico.append(
            {"role": "user", "content": _com_contexto(texto_do_usuario, ctx, alvo)}
        )
        if conv.state is State.GREETING:
            conv.transicionar(State.QUALIFYING)
        yield Evento("state", {"estado": conv.state.value})

        # Uma vez por turno: se a resposta pedir confirmação em texto sem
        # proposta retida no código, o loop pede ao modelo a chamada da
        # ferramenta (ver `_pedir_a_proposta`). Duas vezes seria insistir.
        pediu_a_proposta = False

        for _ in range(MAX_ITERACOES):
            # Quando o gate pede confirmação, o turno segue sem PODER chamar
            # ferramenta: a única coisa que falta é falar com a pessoa. Deixar
            # as tools disponíveis convida o modelo a tentar a mesma escrita de
            # novo, e ele tenta — três vezes seguidas, no teste com o modelo
            # real, até concluir sozinho que "o sistema está recusando".
            #
            # A trava é `tool_choice: none`, e não uma lista vazia, por causa do
            # cache. O prefixo é renderizado na ordem tools → system → messages,
            # e o breakpoint de cache está no system, ou seja, atrás das tools.
            # Esvaziar a lista tirava 1.964 tokens da FRENTE do prefixo, o que
            # invalidava tudo que vinha depois — inclusive o system. O gate é o
            # mecanismo central desta demo e dispara em toda escrita, então o
            # efeito era o cache morrer justamente nas conversas que importam.
            # Com `tool_choice` os bytes do prefixo não mudam (medido: 3.466 dos
            # dois lados) e a garantia é a mesma, porque o modelo fica impedido
            # de emitir tool_use no nível da amostragem, não por bom senso.
            escolha = {"type": "none"} if ctx.propostas_do_turno else {"type": "auto"}
            try:
                resposta, uso = await self._chamar_modelo(ctx, conv.historico, escolha)
            except Exception as exc:  # noqa: BLE001 — erro de provedor nunca vaza cru
                falha = classificar(exc)
                self.saude.registrar_falha(falha.codigo)
                self._registrar_falha(falha, ctx)

                # Falha do provedor não é falha de ferramenta, e escalar aqui
                # mandava a conversa para um "humano" inexistente e a travava
                # para sempre. Duas saídas, e nenhuma reexecuta escrita:
                #  - nada foi gravado neste turno: desfaz o turno, a pessoa manda
                #    de novo;
                #  - algo foi gravado: mantém o histórico (a escrita aconteceu) e
                #    diz isso na tela, para ninguém repetir o pedido às cegas.
                escrita_feita = ctx.escritas_realizadas > escritas_iniciais
                ctx.propostas_do_turno.clear()
                if escrita_feita:
                    nota = i18n.texto("nota.escrita_sem_resposta", ctx.idioma)
                    conv.historico.append(
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": nota}],
                        }
                    )
                else:
                    del conv.historico[tamanho_do_historico:]
                    # Restauração, não transição: o turno não aconteceu.
                    conv.state = estado_inicial
                    conv.turns = turnos_iniciais
                    ctx.idioma_da_conversa = idioma_da_conversa_inicial
                yield Evento(
                    "provider_error",
                    {
                        "codigo": falha.codigo,
                        "escrita_feita": escrita_feita,
                        "pode_repetir": not escrita_feita,
                    },
                )
                yield Evento("state", {"estado": conv.state.value})
                return

            yield uso

            if resposta.stop_reason == "refusal":
                # O modelo recusou por segurança. Não insista, e não prometa
                # pessoa nenhuma: conta como fora de escopo, que só escala na
                # terceira vez, como qualquer outro pedido fora do escopo.
                recusa = _frase(ctx, "recusa")
                conv.historico.append(
                    {"role": "assistant", "content": [{"type": "text", "text": recusa}]}
                )
                yield Evento("message", {"texto": recusa})
                # O turno terminou sem proposta nova: nada fica confirmável.
                for evento in _fechar_turno(conv, ctx):
                    yield evento
                gatilho = conv.registrar_fora_de_escopo()
                if gatilho:
                    conv.escalar(gatilho)
                    yield Evento("escalated", {"motivo": gatilho})
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
            # Chamadas vindas do pedido de proposta só podem PROPOR (ver
            # `_executar`): nunca executam uma escrita, mesmo que ela já esteja
            # confirmável.
            so_propor = False
            # A frase de reserva é do código, no idioma da sessão: não passa
            # pela checagem de idioma, que é para o que o MODELO escreveu.
            frase_do_codigo = False

            if not texto and not chamadas:
                # Turno sem nada visível e sem nada a executar: em vez de sumir,
                # fala. O mesmo texto vai para o histórico, senão o que o modelo
                # lembra e o que a pessoa leu divergem no turno seguinte.
                texto = _frase(
                    ctx,
                    "confirmacao" if conv.state is State.CONFIRMING else "mudo",
                )
                blocos = [*blocos, {"type": "text", "text": texto}]
                frase_do_codigo = True
            elif texto and not chamadas:
                # Confirmação pedida em TEXTO, sem proposta retida no código: a
                # pergunta não registra nada, e o "sim" da pessoa só abriria o
                # gate, que pediria o "sim" de novo. Em vez de mostrar a
                # pergunta, o loop pede ao modelo a chamada da ferramenta: a
                # regra decide já, e se ela permitir o gate retém e a pessoa
                # confirma uma vez só. Depois de uma recusa de regra no mesmo
                # turno não pede: uma segunda recusa seguida encerraria a
                # conversa por `policy_denied_twice`.
                if (
                    not pediu_a_proposta
                    and not ctx.propostas_do_turno
                    and conv.negativas_seguidas == 0
                    and pede_confirmacao(texto)
                ):
                    pediu_a_proposta = True
                    eventos, forcados = await self._pedir_a_proposta(conv, ctx)
                    for evento in eventos:
                        yield evento
                    if forcados:
                        blocos = forcados
                        texto = "".join(
                            b.text for b in blocos if b.type == "text"
                        ).strip()
                        chamadas = [b for b in blocos if b.type == "tool_use"]
                        so_propor = True

            if texto and not chamadas and not frase_do_codigo:
                # A instrução explícita não basta sozinha: conferir é barato
                # (contar palavras, sem chamada paga) e só a resposta errada
                # paga uma reescrita. O texto errado nunca entra no histórico.
                detectado = detector_idioma.detectar(texto)
                if detectado is not None and detectado != alvo:
                    eventos, refeitos, refeito = await self._reescrever_no_idioma(
                        conv, ctx, blocos, alvo
                    )
                    for evento in eventos:
                        yield evento
                    corrigido = bool(refeito) and detector_idioma.detectar(refeito) in (
                        None,
                        alvo,
                    )
                    if corrigido:
                        blocos, texto = refeitos, refeito
                    ctx.registrar(
                        kind="language_check",
                        tool=MODEL,
                        input={"alvo": alvo, "detectado": detectado},
                        output={"corrigido": corrigido},
                        reason_code="reply_language_mismatch",
                    )
                    yield Evento(
                        "language_check",
                        {"alvo": alvo, "detectado": detectado, "corrigido": corrigido},
                    )

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
                for evento in _fechar_turno(conv, ctx):
                    yield evento
                return

            resultados: list[dict] = []
            escritas_nesta_resposta = 0
            for chamada in chamadas:
                spec = REGISTRY.get(chamada.name)
                if spec is not None and spec.scope == "write":
                    escritas_nesta_resposta += 1
                    if escritas_nesta_resposta > 1:
                        # Uma escrita por vez. Dez `tool_use` de escrita na mesma
                        # resposta (a injeção "marque 10 consultas") passavam
                        # todos pelo laço; agora só a primeira segue para a
                        # prévia e o gate. Não é recusa de regra, então não conta
                        # como negativa para a escalação.
                        resultados.append(
                            _erro(
                                chamada.id,
                                "one_write_at_a_time",
                                i18n.texto("erro.uma_por_vez", ctx.idioma),
                            )
                        )
                        yield Evento(
                            "tool_error",
                            {"tool": spec.name, "code": "one_write_at_a_time"},
                        )
                        continue
                async for evento in self._executar(
                    chamada, conv, ctx, resultados, so_propor=so_propor
                ):
                    yield evento

            conv.historico.append({"role": "user", "content": resultados})

            if conv.state is State.ESCALATED:
                yield Evento("escalated", {"motivo": conv.escalation_reason})
                return

        conv.escalar("turn_limit")
        yield Evento("escalated", {"motivo": "turn_limit"})

    async def _chamar_modelo(
        self, ctx: AgentContext, mensagens: list[dict], escolha: dict[str, Any]
    ) -> tuple[Any, Evento]:
        """Uma chamada paga, com o custo no contexto, no audit e na tela.

        Prompt e schema no idioma da SESSÃO: são dois prefixos estáveis, um por
        idioma, cada um com o seu cache.
        """
        comecou = time.perf_counter()
        resposta = await self._client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_blocks(ctx.idioma),
            messages=mensagens,
            tools=schema_para_anthropic(ctx.idioma),
            tool_choice=escolha,
            output_config={"effort": EFFORT},
            metadata={"user_id": f"tenant:{ctx.tenant_id}"},
        )
        self.saude.registrar_ok()

        # O custo entra no log de decisão pelo mesmo motivo que a regra
        # entra: sem ele, "teto de gasto" é uma afirmação que ninguém pode
        # conferir. E `cache_hit` fica visível de propósito — é o número que
        # denuncia invalidador silencioso, que foi como o bug do gate
        # apareceu. Um prefixo que deveria cachear e nunca lê está mudando
        # de bytes sem ninguém perceber.
        latencia_ms = round((time.perf_counter() - comecou) * 1000)
        c = calcular_custo(MODEL, resposta.usage)
        ctx.custo_micros += c.micros
        ctx.chamadas += 1
        ctx.registrar(
            kind="llm_call",
            tool=MODEL,
            input={"latencia_ms": latencia_ms, "tool_choice": escolha["type"]},
            output=c.audit_payload(),
            reason_code=None,
        )
        uso = Evento(
            "usage",
            {
                "modelo": MODEL,
                "latencia_ms": latencia_ms,
                "usd_turno": round(c.usd, 6),
                "usd_conversa": round(ctx.custo_micros / 1_000_000, 6),
                "input_tokens": c.input_tokens,
                "output_tokens": c.output_tokens,
                "cache_read_tokens": c.cache_read_tokens,
                "cache_hit": c.cache_hit,
                "rates_version": c.rates_version,
            },
        )
        return resposta, uso

    async def _reescrever_no_idioma(
        self, conv: Conversation, ctx: AgentContext, blocos: list, alvo: str
    ) -> tuple[list[Evento], list, str]:
        """Pede ao modelo a MESMA resposta no idioma do turno. Uma chamada só.

        O rascunho errado vai junto do pedido, para o conteúdo não mudar (a
        proposta que o gate reteve é a mesma), mas não entra no histórico: o
        que fica é a versão reescrita. `tool_choice: none` porque é reescrita,
        não um passo novo. Se o provedor falhar aqui, fica o rascunho: melhor a
        resposta no idioma errado do que turno nenhum.
        """
        pedido = [
            *conv.historico,
            {"role": "assistant", "content": blocos},
            {
                "role": "user",
                "content": i18n.texto(
                    "idioma.reescrever",
                    ctx.idioma,
                    idioma=i18n.nome_do_idioma(alvo, ctx.idioma),
                ),
            },
        ]
        try:
            resposta, uso = await self._chamar_modelo(ctx, pedido, {"type": "none"})
        except Exception as exc:  # noqa: BLE001 — erro de provedor nunca vaza cru
            falha = classificar(exc)
            self.saude.registrar_falha(falha.codigo)
            self._registrar_falha(falha, ctx)
            return [], blocos, ""
        refeitos = [b for b in resposta.content if b.type == "text" and b.text.strip()]
        refeito = "".join(b.text for b in refeitos).strip()
        return [uso], refeitos, refeito

    async def _pedir_a_proposta(
        self, conv: Conversation, ctx: AgentContext
    ) -> tuple[list[Evento], list]:
        """Pede ao modelo a chamada da ferramenta que ele ia confirmar em texto.

        Uma chamada a mais, só quando a resposta pede confirmação de alteração
        sem proposta retida (`confirmacao_em_texto.pede_confirmacao`). O
        rascunho com a pergunta não vai para a tela nem para o histórico. A
        nota entra no FIM da última mensagem da pessoa (depois dos resultados
        de ferramenta), e fica no histórico quando a chamada vem: o modelo
        precisa lembrar por que chamou a ferramenta, e o histórico continua só
        crescendo, sem editar o que já foi enviado antes.

        Devolve os eventos e os blocos da resposta quando ela traz chamada de
        ferramenta; lista vazia quando não traz, quando o modelo recusa ou
        quando o provedor falha. Nesses casos fica o rascunho: a pergunta em
        texto é o comportamento de antes, não um turno perdido.
        """
        # A última mensagem é da pessoa: o rascunho ainda não entrou no histórico.
        nota = i18n.texto("nota.proposta_em_texto", ctx.idioma)
        com_nota = _com_nota(conv.historico[-1], nota)
        escolha = _escolha_para_propor(MODEL)
        try:
            resposta, uso = await self._chamar_modelo(
                ctx, [*conv.historico[:-1], com_nota], escolha
            )
        except Exception as exc:  # noqa: BLE001 — erro de provedor nunca vaza cru
            falha = classificar(exc)
            self.saude.registrar_falha(falha.codigo)
            self._registrar_falha(falha, ctx)
            return [], []

        blocos = [b for b in resposta.content if b.type != "text" or b.text.strip()]
        chamadas = (
            [b.name for b in blocos if b.type == "tool_use"]
            if resposta.stop_reason != "refusal"
            else []
        )
        ctx.registrar(
            kind="proposal_check",
            tool=MODEL,
            input={"tool_choice": escolha["type"]},
            output={"chamou": chamadas},
            reason_code="confirmation_asked_in_text",
        )
        eventos = [
            uso,
            Evento(
                "proposal_check",
                {"motivo": "confirmation_asked_in_text", "chamou": chamadas},
            ),
        ]
        if not chamadas:
            return eventos, []
        conv.historico[-1] = com_nota
        return eventos, blocos

    @staticmethod
    def _registrar_falha(falha: FalhaDoProvedor, ctx: AgentContext) -> None:
        # Linha de diagnóstico no log do servidor: status, tipo do erro, request
        # id e a mensagem do provedor sanitizada. É o que faltou quando a demo
        # caiu, e o log só dizia "400 Bad Request".
        log.warning(
            "provider_failure %s",
            json.dumps(
                {"tenant_id": ctx.tenant_id, "modelo": MODEL, **falha.log_payload()},
                ensure_ascii=False,
            ),
        )
        ctx.registrar(
            kind="provider_failure",
            tool=MODEL,
            input={"classe": falha.classe},
            output={"status": falha.status, "tipo_erro": falha.tipo_erro},
            reason_code=falha.codigo,
        )

    @staticmethod
    async def _tratar_erro_de_tool(
        saida: ToolError,
        chamada: Any,
        spec: Any,
        conv: Conversation,
        resultados: list[dict],
    ) -> AsyncIterator[Evento]:
        """Recusa ou falha de tool, venha da prévia ou da execução.

        Falha do ERP conta para a escalação por falha repetida; o resto é
        negativa de regra, que escala na segunda seguida.
        """
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

    async def _executar(
        self,
        chamada: Any,
        conv: Conversation,
        ctx: AgentContext,
        resultados: list[dict],
        *,
        so_propor: bool = False,
    ) -> AsyncIterator[Evento]:
        spec = REGISTRY.get(chamada.name)
        if spec is None:
            resultados.append(
                _erro(
                    chamada.id,
                    "tool_desconhecida",
                    i18n.texto("erro.tool_desconhecida", ctx.idioma),
                )
            )
            return

        try:
            entrada = spec.input_model(**chamada.input)
        except Exception:  # noqa: BLE001 — schema inválido é dado do modelo, não exceção do sistema
            resultados.append(
                _erro(
                    chamada.id,
                    "input_invalido",
                    i18n.texto("erro.input_invalido", ctx.idioma),
                )
            )
            return

        yield Evento(
            "tool_call",
            {"tool": spec.name, "scope": spec.scope, "input": chamada.input},
        )

        if spec.scope == "write":
            sig = assinatura(spec.name, entrada.model_dump(mode="json"))

            # Gate de confirmação. O modelo pode ter sido convencido a
            # pular a etapa; o runtime não.
            #
            # `so_propor`: a chamada veio do pedido de proposta, feito porque o
            # modelo estava PERGUNTANDO à pessoa. Ela ainda não respondeu a essa
            # pergunta, então a chamada vira proposta mesmo que a mesma escrita
            # esteja confirmável desde o turno anterior. Sem isto, o pedido de
            # proposta podia executar uma escrita depois de um "não, espera".
            if spec.requires_confirmation and (
                so_propor or sig not in ctx.confirmacoes
            ):
                # Antes do gate, a PRÉVIA: leituras e política, sem gravar. Uma
                # escrita que a regra barra não tem o que confirmar, e pedir o
                # "sim" antes fazia "domingo às 10h" recusar só na mensagem
                # seguinte. Na execução a política roda de novo, porque a agenda
                # pode ter mudado entre a proposta e o "sim".
                antes = len(ctx.audit)
                previa = await HANDLERS[spec.name](ctx, entrada, executar=False)
                for evento in _eventos_de_politica(ctx, antes):
                    yield evento
                if isinstance(previa, ToolError):
                    async for evento in self._tratar_erro_de_tool(
                        previa, chamada, spec, conv, resultados
                    ):
                        yield evento
                    return

                ctx.propostas_do_turno.add(sig)
                conv.avancar(State.CONFIRMING)
                # O gate PRECISA aparecer no log de decisão. Sem esta linha a
                # tela mostrava duas chamadas idênticas de escrita e nada entre
                # elas — quem lia concluía escrita dupla, quando na verdade a
                # primeira foi barrada aqui e nunca chegou ao ERP. É a garantia
                # central da demo ficando invisível justamente no painel
                # construído para prová-la.
                yield Evento(
                    "confirmation_required",
                    {"tool": spec.name, "input": chamada.input},
                )
                yield Evento(
                    "state",
                    {
                        "estado": conv.state.value,
                        "motivo": i18n.texto(
                            "estado.aguardando_confirmacao", ctx.idioma
                        ),
                    },
                )
                resultados.append(
                    _erro(
                        chamada.id,
                        "confirmation_required",
                        i18n.texto("gate.confirmar", ctx.idioma),
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
                        i18n.texto("erro.teto_de_escritas", ctx.idioma),
                    )
                )
                return

            conv.avancar(State.EXECUTING)
            yield Evento("state", {"estado": conv.state.value})

        antes = len(ctx.audit)
        saida = await HANDLERS[spec.name](ctx, entrada)
        for evento in _eventos_de_politica(ctx, antes):
            yield evento

        if isinstance(saida, ToolError):
            async for evento in self._tratar_erro_de_tool(
                saida, chamada, spec, conv, resultados
            ):
                yield evento
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


def _fechar_turno(conv: Conversation, ctx: AgentContext) -> list[Evento]:
    """Fim do turno: a proposta que chegou à pessoa vira a confirmação pendente.

    Só agora ela fica confirmável, e só na próxima fala da pessoa a escrita
    pode executar. É este passo que garante que existiu um humano entre a
    intenção e a gravação. Atribuição, não acúmulo: a confirmação vale só para
    a mensagem SEGUINTE; acumulando, um "sim" cinco mensagens depois, ou depois
    de a pessoa mudar de ideia, ainda executava a proposta velha.

    E o estado passa a dizer o que o código sabe. Antes ele ficava em
    `confirming` desde a primeira proposta até a próxima escrita: depois de uma
    recusa de regra, ou de um turno só em texto, a tela continuava "esperando a
    sua confirmação" sem proposta nenhuma pendente.
    """
    ctx.confirmacoes = set(ctx.propostas_do_turno)
    ctx.propostas_do_turno.clear()
    if ctx.confirmacoes or conv.state is not State.CONFIRMING:
        return []
    conv.avancar(State.COLLECTING)
    return [Evento("state", {"estado": conv.state.value})]


def _escolha_para_propor(modelo: str) -> dict[str, Any]:
    """`tool_choice` do pedido de proposta: forçado onde o modelo aceita.

    Uma escrita por resposta em qualquer caso (`disable_parallel_tool_use`).
    """
    tipo = "auto" if modelo.startswith(MODELOS_SEM_TOOL_CHOICE_FORCADO) else "any"
    return {"type": tipo, "disable_parallel_tool_use": True}


def _com_nota(mensagem: dict, nota: str) -> dict:
    """A mesma mensagem da pessoa com a nota do sistema no fim."""
    conteudo = mensagem["content"]
    blocos = (
        [{"type": "text", "text": conteudo}]
        if isinstance(conteudo, str)
        else list(conteudo)
    )
    return {**mensagem, "content": [*blocos, {"type": "text", "text": nota}]}


def _eventos_de_politica(ctx: AgentContext, antes: int) -> list[Evento]:
    """As decisões de política gravadas no audit desde `antes`, como eventos."""
    return [
        Evento(
            "policy",
            {
                "regra": entrada.tool,
                "allowed": entrada.output.get("allowed"),
                "reason_code": entrada.reason_code,
                "rules_version": entrada.rules_version,
            },
        )
        for entrada in ctx.audit[antes:]
        if entrada.kind == "policy_decision"
    ]


def _com_contexto(mensagem: str, ctx: AgentContext, alvo: str) -> str:
    """Prende o relógio, os profissionais e o idioma ao turno — depois do cacheado.

    "Amanhã" só significa alguma coisa se o modelo souber que dia é hoje. A
    tentação é escrever isso no system prompt, e é o erro clássico: o prompt
    é o começo do prefixo, então uma data ali muda os bytes a cada turno e
    invalida o cache de TUDO que vem depois. Aqui o contexto entra no turno do
    usuário, onde não há prefixo nenhum para estragar.

    Entra como bloco marcado justamente porque o prompt manda tratar conteúdo
    de dado como dado: é contexto operacional, não uma instrução nova.

    O bloco sai no idioma da SESSÃO, e a linha do idioma da resposta diz o
    idioma decidido pelo código para este turno (`alvo`). Antes ela dizia "o
    MESMO da mensagem abaixo", e o modelo tinha de adivinhar: num "ok" ou num
    "yes" curto, o português do bloco, do prompt e das ferramentas vencia.

    O cadastro de pacientes NÃO entra aqui. Com a lista no turno, o agente
    dizia a quem pedia "Maria Teste" que ela não existia, "mas temos Ana, João
    e Marta". O id de `marcar_consulta` sai de `buscar_paciente`.
    """
    local = ctx.at.astimezone(BAHIA)
    agora = local.strftime(i18n.texto("contexto.formato_agora", ctx.idioma))
    bloco = i18n.texto(
        "contexto.bloco",
        ctx.idioma,
        agora=f"{agora} ({local.tzname()})",
        idioma=i18n.nome_do_idioma(alvo, ctx.idioma),
    )
    return f"{bloco}{mensagem}"


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
