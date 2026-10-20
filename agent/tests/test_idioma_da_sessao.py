"""O idioma da sessão governa o que o código escreve, e o idioma da resposta é conferido.

Regressão do bug de 20/10: em `?lang=en`, a confirmação às vezes voltava em
português. O idioma da sessão (`ctx.idioma`) só escolhia três frases de reserva;
o prompt, o bloco de contexto, a ordem do gate, os erros das tools e as dicas da
política iam em português, e a linha de idioma pedia ao modelo que adivinhasse
pela mensagem, o que um "yes" curto não segura.

O LLM é falso: estes testes provam o que o CÓDIGO garante (o que ele manda ao
modelo e o que ele faz com uma resposta no idioma errado), não o modelo.
"""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src import i18n
from src.adapters.erp import ConsultaView
from src.adapters.fake import ErpFake
from src.agent import Agente, assinatura
from src.flows.state_machine import Conversation
from src.policy.engine import PolicyEngine
from src.prompts.system import PROMPTS
from src.tools.registry import AgentContext, schema_para_anthropic
from tests.conftest import FakeLLM, coletar, texto, tool_use, uso

BAHIA = ZoneInfo("America/Bahia")
AGORA = datetime(2026, 8, 10, 9, 0, tzinfo=BAHIA)
REMARCAR_16H = {"consulta_id": "cons_1", "novo_inicio": "2026-08-11T16:00:00-03:00"}
PEDIDO = {"pt": "remarca a da Ana pras 16h", "en": "move Ana's appointment to 4pm"}
NEUTRO = "✓"  # resposta sem sinal de idioma: não dispara a checagem
IDIOMAS = ["pt", "en"]


@pytest.fixture
def erp() -> ErpFake:
    return ErpFake(
        [
            ConsultaView(
                id="cons_1",
                paciente_nome="Ana Ribeiro",
                profissional_id="prof_bruno",
                profissional_nome="Dr. Bruno Salles",
                inicio=datetime(2026, 8, 11, 9, 0, tzinfo=BAHIA),
                fim=datetime(2026, 8, 11, 9, 30, tzinfo=BAHIA),
                status="agendada",
                paciente_id="pac_ana",
            )
        ]
    )


@pytest.fixture
def ctx(erp: ErpFake) -> AgentContext:
    return AgentContext(
        conversation_id="conv_idioma",
        tenant_id="t1",
        policy=PolicyEngine(),
        erp=erp,  # type: ignore[arg-type]
        at=AGORA,
    )


def ultimos_resultados(chamada: dict) -> list[dict]:
    """Os tool_results que o código mandou ao modelo nesta chamada."""
    conteudo = chamada["messages"][-1]["content"]
    return [json.loads(r["content"]) for r in conteudo if r.get("is_error")]


def textos_do_assistente(conv: Conversation) -> list[str]:
    achados = []
    for m in conv.historico:
        if m["role"] != "assistant":
            continue
        for b in m["content"]:
            achados.append(
                b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "")
            )
    return achados


def mensagens(eventos: list) -> list[str]:
    return [e.dado["texto"] for e in eventos if e.tipo == "message"]


# -- o prompt, o schema e o bloco de contexto ---------------------------------


@pytest.mark.parametrize("idioma", IDIOMAS)
async def test_prompt_e_schema_seguem_o_idioma_da_sessao(
    ctx: AgentContext, idioma: str
):
    ctx.definir_idioma(idioma)
    llm = FakeLLM([texto(NEUTRO)])
    await coletar(Agente(llm), Conversation(), PEDIDO[idioma], ctx)
    enviado = llm.chamadas[0]
    assert enviado["system"][0]["text"] == PROMPTS[idioma]
    assert enviado["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert enviado["tools"] == schema_para_anthropic(idioma)


@pytest.mark.parametrize(
    ("idioma", "abre", "linha"),
    [
        ("pt", "<contexto_operacional>", "idioma da resposta: português."),
        ("en", "<operational_context>", "reply language: English."),
    ],
)
async def test_bloco_de_contexto_no_idioma_da_sessao(
    ctx: AgentContext, idioma: str, abre: str, linha: str
):
    ctx.definir_idioma(idioma)
    llm = FakeLLM([texto(NEUTRO)])
    await coletar(Agente(llm), Conversation(), PEDIDO[idioma], ctx)
    primeira = llm.chamadas[0]["messages"][0]["content"]
    assert primeira.startswith(abre)
    assert linha in primeira
    assert primeira.endswith(PEDIDO[idioma])


async def test_resposta_curta_numa_sessao_em_ingles_continua_em_ingles(
    ctx: AgentContext,
):
    """O caso da gravação: "yes" ou "ok" depois da proposta. O código diz o idioma."""
    ctx.definir_idioma("en")
    llm = FakeLLM([texto(NEUTRO)], [texto(NEUTRO)])
    agente, conv = Agente(llm), Conversation()
    await coletar(agente, conv, "ok", ctx)
    await coletar(agente, conv, "16h", ctx)
    for chamada in llm.chamadas:
        assert "reply language: English." in chamada["messages"][-1]["content"]


async def test_quem_escreve_em_espanhol_recebe_espanhol_e_a_conversa_lembra(
    ctx: AgentContext,
):
    ctx.definir_idioma("en")
    llm = FakeLLM([texto(NEUTRO)], [texto(NEUTRO)], [texto(NEUTRO)])
    agente, conv = Agente(llm), Conversation()

    await coletar(agente, conv, "¿Qué hay en la agenda?", ctx)
    assert "reply language: Spanish." in llm.chamadas[0]["messages"][-1]["content"]

    await coletar(agente, conv, "ok", ctx)
    assert "reply language: Spanish." in llm.chamadas[1]["messages"][-1]["content"]

    # Trocar o idioma na tela é o sinal mais recente: a memória some.
    ctx.definir_idioma("pt")
    await coletar(agente, conv, "ok", ctx)
    assert (
        "idioma da resposta: português." in llm.chamadas[2]["messages"][-1]["content"]
    )


def test_definir_o_mesmo_idioma_nao_esquece_a_conversa(ctx: AgentContext):
    ctx.definir_idioma("en")
    ctx.idioma_da_conversa = "es"
    ctx.definir_idioma("en")
    assert ctx.idioma_da_conversa == "es"
    ctx.definir_idioma("xx")
    assert (ctx.idioma, ctx.idioma_da_conversa) == ("pt", None)


# -- cada texto que o código põe na conversa, nos dois idiomas ----------------


@pytest.mark.parametrize("idioma", IDIOMAS)
async def test_ordem_do_gate_no_idioma_da_sessao(ctx: AgentContext, idioma: str):
    ctx.definir_idioma(idioma)
    llm = FakeLLM([tool_use("remarcar_consulta", REMARCAR_16H)], [texto(NEUTRO)])
    eventos = await coletar(Agente(llm), Conversation(), PEDIDO[idioma], ctx)

    resultado = ultimos_resultados(llm.chamadas[1])
    assert resultado == [
        {
            "error": "confirmation_required",
            "message": i18n.texto("gate.confirmar", idioma),
        }
    ]
    estado = next(e for e in eventos if e.tipo == "state" and "motivo" in e.dado)
    assert estado.dado["motivo"] == i18n.texto("estado.aguardando_confirmacao", idioma)


@pytest.mark.parametrize("idioma", IDIOMAS)
async def test_teto_de_escritas_no_idioma_da_sessao(ctx: AgentContext, idioma: str):
    ctx.definir_idioma(idioma)
    ctx.escritas_realizadas = 3
    ctx.confirmacoes.add(assinatura("remarcar_consulta", REMARCAR_16H))
    llm = FakeLLM([tool_use("remarcar_consulta", REMARCAR_16H)])
    conv = Conversation()
    await coletar(Agente(llm), conv, PEDIDO[idioma], ctx)
    # Escalou na hora: a mensagem do teto foi para o histórico, não para outra chamada.
    assert len(llm.chamadas) == 1
    resultado = json.loads(conv.historico[-1]["content"][0]["content"])
    assert resultado["message"] == i18n.texto("erro.teto_de_escritas", idioma)


@pytest.mark.parametrize("idioma", IDIOMAS)
async def test_erros_de_tool_montados_pelo_agente_no_idioma_da_sessao(
    ctx: AgentContext, idioma: str
):
    ctx.definir_idioma(idioma)
    llm = FakeLLM(
        [
            tool_use("executar_sql", {"query": "DROP"}, id_="a"),
            tool_use(
                "abrir_vaga",
                {"profissional_id": "prof_bruno", "inicio": "x", "duracao_min": 600},
                id_="b",
            ),
        ],
        [texto(NEUTRO)],
    )
    await coletar(Agente(llm), Conversation(), PEDIDO[idioma], ctx)
    assert [r["message"] for r in ultimos_resultados(llm.chamadas[1])] == [
        i18n.texto("erro.tool_desconhecida", idioma),
        i18n.texto("erro.input_invalido", idioma),
    ]


@pytest.mark.parametrize("idioma", IDIOMAS)
async def test_uma_escrita_por_vez_no_idioma_da_sessao(ctx: AgentContext, idioma: str):
    ctx.definir_idioma(idioma)
    llm = FakeLLM(
        [
            tool_use("remarcar_consulta", REMARCAR_16H, id_="a"),
            tool_use("cancelar_consulta", {"consulta_id": "cons_1"}, id_="b"),
        ],
        [texto(NEUTRO)],
    )
    await coletar(Agente(llm), Conversation(), PEDIDO[idioma], ctx)
    erros = {r["error"]: r["message"] for r in ultimos_resultados(llm.chamadas[1])}
    assert erros["one_write_at_a_time"] == i18n.texto("erro.uma_por_vez", idioma)


@pytest.mark.parametrize("idioma", IDIOMAS)
async def test_recusa_da_regra_e_dica_no_idioma_da_sessao(
    ctx: AgentContext, idioma: str
):
    ctx.definir_idioma(idioma)
    domingo = {"consulta_id": "cons_1", "novo_inicio": "2026-08-16T10:00:00-03:00"}
    llm = FakeLLM([tool_use("remarcar_consulta", domingo)], [texto(NEUTRO)])
    await coletar(Agente(llm), Conversation(), PEDIDO[idioma], ctx)
    assert ultimos_resultados(llm.chamadas[1]) == [
        {
            "error": "outside_business_hours",
            "message": i18n.texto("tool.horario_indisponivel", idioma),
            "hint": i18n.texto("dica.dia_fechado", idioma),
        }
    ]


class RecusaLLM:
    def __init__(self) -> None:
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        return SimpleNamespace(content=[], stop_reason="refusal", usage=uso())


@pytest.mark.parametrize("idioma", IDIOMAS)
async def test_frases_de_reserva_no_idioma_da_sessao(ctx: AgentContext, idioma: str):
    """As frases que a tela mostra sem passar pelo modelo: "Posso seguir?" e afins."""
    ctx.definir_idioma(idioma)
    recusa = await coletar(Agente(RecusaLLM()), Conversation(), PEDIDO[idioma], ctx)
    assert mensagens(recusa) == [i18n.texto("fala.recusa", idioma)]

    llm = FakeLLM([tool_use("remarcar_consulta", REMARCAR_16H)], [texto("")])
    muda = await coletar(Agente(llm), Conversation(), PEDIDO[idioma], ctx)
    assert mensagens(muda) == [i18n.texto("fala.confirmacao", idioma)]

    llm = FakeLLM([texto("  ")])
    vazia = await coletar(Agente(llm), Conversation(), PEDIDO[idioma], ctx)
    assert mensagens(vazia) == [i18n.texto("fala.mudo", idioma)]


class QuebraDepoisDaEscrita(FakeLLM):
    """Grava e o provedor cai antes da resposta."""

    async def _create(self, **kwargs):
        if len(self.chamadas) == 1:
            self.chamadas.append(kwargs)
            raise RuntimeError("provider down")
        return await super()._create(**kwargs)


@pytest.mark.parametrize("idioma", IDIOMAS)
async def test_nota_de_escrita_sem_resposta_no_idioma_da_sessao(
    ctx: AgentContext, idioma: str
):
    ctx.definir_idioma(idioma)
    ctx.confirmacoes.add(assinatura("remarcar_consulta", REMARCAR_16H))
    llm = QuebraDepoisDaEscrita([tool_use("remarcar_consulta", REMARCAR_16H)])
    conv = Conversation()
    await coletar(Agente(llm), conv, PEDIDO[idioma], ctx)
    assert conv.historico[-1]["content"][0]["text"] == i18n.texto(
        "nota.escrita_sem_resposta", idioma
    )


# -- a checagem do idioma da resposta ------------------------------------------

PROPOSTA_PT = "Vou remarcar a consulta da Ana das 9h para as 16h. Posso seguir?"
PROPOSTA_EN = "I'll move Ana's appointment from 9 AM to 4 PM. Shall I go ahead?"


async def test_resposta_em_portugues_numa_sessao_em_ingles_e_reescrita(
    ctx: AgentContext,
):
    ctx.definir_idioma("en")
    conv = Conversation()
    llm = FakeLLM(
        [tool_use("remarcar_consulta", REMARCAR_16H)],
        [texto(PROPOSTA_PT)],
        [texto(PROPOSTA_EN)],
    )
    eventos = await coletar(Agente(llm), conv, PEDIDO["en"], ctx)

    assert mensagens(eventos) == [PROPOSTA_EN]
    assert len(llm.chamadas) == 3
    original, reescrita = llm.chamadas[1], llm.chamadas[2]
    # A reescrita não muda o prefixo cacheado nem pode chamar ferramenta.
    assert reescrita["system"] == original["system"]
    assert reescrita["tools"] == original["tools"]
    assert reescrita["tool_choice"] == {"type": "none"}
    pedido = reescrita["messages"]
    assert pedido[-2]["role"] == "assistant"
    assert pedido[-2]["content"][0].text == PROPOSTA_PT
    assert "<language_check>" in pedido[-1]["content"]
    assert "entirely in English" in pedido[-1]["content"]

    # O rascunho errado não fica no histórico; a proposta retida continua de pé.
    textos = textos_do_assistente(conv)
    assert PROPOSTA_PT not in textos and PROPOSTA_EN in textos
    assert assinatura("remarcar_consulta", REMARCAR_16H) in ctx.confirmacoes

    checagem = next(e for e in eventos if e.tipo == "language_check")
    assert checagem.dado == {"alvo": "en", "detectado": "pt", "corrigido": True}
    linha = next(a for a in ctx.audit if a.kind == "language_check")
    assert linha.reason_code == "reply_language_mismatch"
    assert linha.output == {"corrigido": True}
    assert sum(e.tipo == "usage" for e in eventos) == 3


async def test_reescrita_que_continua_errada_entrega_o_original_sem_insistir(
    ctx: AgentContext,
):
    ctx.definir_idioma("en")
    llm = FakeLLM(
        [tool_use("remarcar_consulta", REMARCAR_16H)],
        [texto(PROPOSTA_PT)],
        [texto("Remarco a consulta da Ana para as 16h. Confirma?")],
    )
    eventos = await coletar(Agente(llm), Conversation(), PEDIDO["en"], ctx)
    assert mensagens(eventos) == [PROPOSTA_PT]
    assert len(llm.chamadas) == 3  # uma reescrita, e só
    checagem = next(e for e in eventos if e.tipo == "language_check")
    assert checagem.dado["corrigido"] is False


async def test_resposta_no_idioma_certo_nao_paga_reescrita(ctx: AgentContext):
    ctx.definir_idioma("en")
    llm = FakeLLM([tool_use("remarcar_consulta", REMARCAR_16H)], [texto(PROPOSTA_EN)])
    eventos = await coletar(Agente(llm), Conversation(), PEDIDO["en"], ctx)
    assert mensagens(eventos) == [PROPOSTA_EN]
    assert len(llm.chamadas) == 2
    assert not [e for e in eventos if e.tipo == "language_check"]


async def test_sessao_em_portugues_tambem_e_conferida(ctx: AgentContext):
    llm = FakeLLM(
        [tool_use("remarcar_consulta", REMARCAR_16H)],
        [texto(PROPOSTA_EN)],
        [texto(PROPOSTA_PT)],
    )
    eventos = await coletar(Agente(llm), Conversation(), PEDIDO["pt"], ctx)
    assert mensagens(eventos) == [PROPOSTA_PT]
    assert "<verificacao_de_idioma>" in llm.chamadas[2]["messages"][-1]["content"]


async def test_pergunta_em_espanhol_respondida_em_espanhol_nao_e_reescrita(
    ctx: AgentContext,
):
    ctx.definir_idioma("en")
    llm = FakeLLM([texto("En la agenda hay una cita mañana a las 9.")])
    eventos = await coletar(Agente(llm), Conversation(), "¿Qué hay en la agenda?", ctx)
    assert len(llm.chamadas) == 1
    assert not [e for e in eventos if e.tipo == "language_check"]


class QuebraNaReescrita(FakeLLM):
    async def _create(self, **kwargs):
        if len(self.chamadas) == 2:
            self.chamadas.append(kwargs)
            raise RuntimeError("provider down")
        return await super()._create(**kwargs)


async def test_falha_do_provedor_na_reescrita_entrega_o_original(ctx: AgentContext):
    """Melhor a resposta no idioma errado do que turno nenhum."""
    ctx.definir_idioma("en")
    llm = QuebraNaReescrita(
        [tool_use("remarcar_consulta", REMARCAR_16H)], [texto(PROPOSTA_PT)]
    )
    agente = Agente(llm)
    eventos = await coletar(agente, Conversation(), PEDIDO["en"], ctx)
    assert mensagens(eventos) == [PROPOSTA_PT]
    assert not [e for e in eventos if e.tipo == "provider_error"]
    assert [a.kind for a in ctx.audit].count("provider_failure") == 1
    assert (
        next(e for e in eventos if e.tipo == "language_check").dado["corrigido"]
        is False
    )
