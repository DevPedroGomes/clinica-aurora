"""O que o servidor MCP garante, e o que ele explicitamente não garante.

Estes testes não exercitam o protocolo MCP (isso é o SDK, e testá-lo seria
testar biblioteca dos outros). Eles exercitam o que é NOSSO e o que a demo
afirma: a política decide, o teto vale, o gate exige token, e o log conta a
história. Tudo isso rodando sob a resolução de dependências DESTE serviço, que é
diferente da do agente — se pydantic 2.13 e 2.10 divergirem no domínio
compartilhado, quebra aqui.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ.parent / "agent"))

import server  # noqa: E402
from src.adapters.erp import ConsultaView  # noqa: E402
from src.adapters.fake import ErpFake  # noqa: E402
from src.tools.registry import REGISTRY  # noqa: E402

BAHIA = timezone(timedelta(hours=-3))


class SessaoFalsa:
    """Só precisa ser um objeto distinto e weakref-ável, como a ServerSession."""


class CtxFalso:
    def __init__(self, sessao: SessaoFalsa) -> None:
        self.session = sessao


@pytest.fixture
def agora() -> datetime:
    return datetime(2026, 8, 10, 9, 0, tzinfo=BAHIA)


@pytest.fixture
def ctx(monkeypatch: pytest.MonkeyPatch, agora: datetime) -> CtxFalso:
    """Uma sessão MCP com ERP falso e relógio parado."""
    c = CtxFalso(SessaoFalsa())
    erp = ErpFake(
        [
            ConsultaView(
                id="cons_1",
                paciente_nome="Ana Ribeiro",
                profissional_id="prof_bruno",
                profissional_nome="Dr. Bruno Salles",
                inicio=datetime(2026, 8, 11, 9, 0, tzinfo=BAHIA),
                fim=datetime(2026, 8, 11, 9, 30, tzinfo=BAHIA),
                status="agendada",
            ),
            ConsultaView(
                id="cons_2",
                paciente_nome="João Meireles",
                profissional_id="prof_bruno",
                profissional_nome="Dr. Bruno Salles",
                inicio=datetime(2026, 8, 10, 15, 0, tzinfo=BAHIA),
                fim=datetime(2026, 8, 10, 15, 30, tzinfo=BAHIA),
                status="agendada",
            ),
        ]
    )
    sessao = server._sessao(c)  # type: ignore[arg-type]
    sessao.ctx.erp = erp  # type: ignore[assignment]
    sessao.ctx.at = agora
    # `_sessao` reencosta o relógio no tempo real a cada chamada; aqui ele fica
    # parado, senão a regra da janela de cancelamento mediria contra "agora" e o
    # teste passaria ou falharia dependendo da hora em que rodasse.
    monkeypatch.setattr(server, "_sessao", lambda _c: sessao)
    return c


async def chamar(nome: str, ctx: CtxFalso, **campos: Any) -> dict[str, Any]:
    confirmacao = campos.pop("confirmacao", None)
    return await server._executar(nome, campos, ctx, confirmacao)  # type: ignore[arg-type]


# -- o gate -------------------------------------------------------------------


async def test_escrita_sem_token_nao_grava_e_devolve_proposta(ctx: CtxFalso):
    r = await chamar(
        "remarcar_consulta",
        ctx,
        consulta_id="cons_1",
        novo_inicio="2026-08-11T16:00:00-03:00",
    )
    assert r["ok"] is False
    assert r["erro"] == "confirmation_required"
    assert r["confirmacao"]
    assert r["proposta"]["dados"]["consulta_id"] == "cons_1"
    assert server._sessao(ctx).ctx.escritas_realizadas == 0  # type: ignore[arg-type]


async def test_token_inventado_e_recusado(ctx: CtxFalso):
    r = await chamar(
        "remarcar_consulta",
        ctx,
        consulta_id="cons_1",
        novo_inicio="2026-08-11T16:00:00-03:00",
        confirmacao="nao-existe",
    )
    assert r["erro"] == "confirmacao_invalida"


async def test_token_nao_vale_para_operacao_diferente(ctx: CtxFalso):
    """O buraco que o gate existe para fechar.

    Confirmar "remarca para 16h" não pode autorizar executar "remarca para 17h".
    Sem esta checagem, o token viraria um passe livre para a tool inteira.
    """
    p = await chamar(
        "remarcar_consulta",
        ctx,
        consulta_id="cons_1",
        novo_inicio="2026-08-11T16:00:00-03:00",
    )
    r = await chamar(
        "remarcar_consulta",
        ctx,
        consulta_id="cons_1",
        novo_inicio="2026-08-11T17:00:00-03:00",
        confirmacao=p["confirmacao"],
    )
    assert r["erro"] == "confirmacao_nao_corresponde"
    assert server._sessao(ctx).ctx.escritas_realizadas == 0  # type: ignore[arg-type]


async def test_token_certo_executa_e_nao_serve_duas_vezes(ctx: CtxFalso):
    campos = {"consulta_id": "cons_1", "novo_inicio": "2026-08-11T16:00:00-03:00"}
    p = await chamar("remarcar_consulta", ctx, **campos)

    ok = await chamar("remarcar_consulta", ctx, **campos, confirmacao=p["confirmacao"])
    assert ok["ok"] is True
    assert server._sessao(ctx).ctx.escritas_realizadas == 1  # type: ignore[arg-type]

    de_novo = await chamar(
        "remarcar_consulta", ctx, **campos, confirmacao=p["confirmacao"]
    )
    assert de_novo["erro"] == "confirmacao_invalida"


# -- a política ---------------------------------------------------------------


async def test_politica_recusa_e_devolve_o_codigo_da_regra(ctx: CtxFalso):
    """O ponto da demo: quem recusa é este processo, não o modelo do outro lado.

    A recusa vem já na primeira chamada: a prévia roda a política antes de
    emitir o token, e não há o que confirmar numa escrita que a regra barra.
    """
    campos = {"consulta_id": "cons_2"}
    r = await chamar("cancelar_consulta", ctx, **campos)

    assert r["ok"] is False
    assert r["erro"] == "cancel_window_requires_reason"
    regras = {(d["regra"], d["codigo"]) for d in r["decisoes"]}
    assert ("cancelar_consulta", "cancel_window_requires_reason") in regras
    assert all(d["versao_da_politica"] for d in r["decisoes"])


async def test_proposta_recusada_pela_politica_nao_emite_token(ctx: CtxFalso):
    r = await chamar(
        "remarcar_consulta",
        ctx,
        consulta_id="cons_1",
        novo_inicio="2026-08-16T10:00:00-03:00",  # domingo
    )
    assert r["ok"] is False
    assert r["erro"] == "outside_business_hours"
    assert "confirmacao" not in r
    assert server._sessao(ctx).confirmacoes == {}  # type: ignore[arg-type]


async def test_a_mesma_escrita_passa_quando_a_regra_e_satisfeita(ctx: CtxFalso):
    campos = {"consulta_id": "cons_2", "motivo": "paciente avisou que nao vem"}
    p = await chamar("cancelar_consulta", ctx, **campos)
    r = await chamar("cancelar_consulta", ctx, **campos, confirmacao=p["confirmacao"])
    assert r["ok"] is True


async def test_id_fora_do_dominio_fechado_e_recusado_antes_de_qualquer_coisa(
    ctx: CtxFalso,
):
    """O schema é a primeira barreira, e ela roda antes da política e do gate.

    `consulta_id` é `^cons_[0-9]+$`. Um id inventado pelo modelo do outro lado
    morre aqui, sem token emitido e sem linha no log.
    """
    r = await chamar("cancelar_consulta", ctx, consulta_id="cons_inventado")
    assert r["erro"] == "input_invalido"
    assert "confirmacao" not in r
    assert server._sessao(ctx).ctx.audit == []  # type: ignore[arg-type]


# -- o log --------------------------------------------------------------------


async def test_proposta_nao_polui_o_log_de_decisao(ctx: CtxFalso):
    """Uma escrita deixa UMA linha de teto, não uma por tentativa.

    O teto é consultado depois do gate justamente por isto: checando antes, cada
    proposta e cada token inválido deixavam uma linha `write_quota`, e quem lesse
    o log não conseguia dizer qual decisão foi a que valeu.
    """
    campos = {"consulta_id": "cons_1", "novo_inicio": "2026-08-11T16:00:00-03:00"}
    await chamar("remarcar_consulta", ctx, **campos)
    await chamar("remarcar_consulta", ctx, **campos, confirmacao="invalido")
    p = await chamar("remarcar_consulta", ctx, **campos)
    await chamar("remarcar_consulta", ctx, **campos, confirmacao=p["confirmacao"])

    audit = server._sessao(ctx).ctx.audit  # type: ignore[arg-type]
    assert [e.tool for e in audit].count("write_quota") == 1


# -- nomes de paciente --------------------------------------------------------


async def test_buscar_paciente_devolve_um_id_e_nunca_a_lista(ctx: CtxFalso):
    # `chamar` usa `nome` para a tool; aqui o campo também se chama `nome`.
    async def buscar(nome: str) -> dict[str, Any]:
        return await server._executar("buscar_paciente", {"nome": nome}, ctx)  # type: ignore[arg-type]

    achado = await buscar("joao")
    assert achado == {
        "ok": True,
        "resultado": {"paciente_id": "pac_joao", "nome": "João Meireles"},
        "decisoes": [],
    }
    ausente = await buscar("Maria Teste")
    assert ausente["erro"] == "patient_not_found"
    assert "Ana" not in str(ausente) and "Marta" not in str(ausente)


async def test_agenda_sem_paciente_nao_diz_de_quem_e(ctx: CtxFalso):
    r = await chamar("listar_agenda", ctx)
    assert r["ok"] is True
    assert "Ana Ribeiro" not in str(r) and "João Meireles" not in str(r)


# -- isolamento ---------------------------------------------------------------


def test_cada_sessao_recebe_o_proprio_tenant():
    a = server._sessao(CtxFalso(SessaoFalsa()))  # type: ignore[arg-type]
    b = server._sessao(CtxFalso(SessaoFalsa()))  # type: ignore[arg-type]
    assert a.ctx.tenant_id != b.ctx.tenant_id
    assert a.ctx.tenant_id.startswith("mcp-")


# -- o guarda contra drift ----------------------------------------------------


def test_assinatura_de_cada_tool_bate_com_o_input_model():
    """As assinaturas MCP são escritas à mão; este teste impede que envelheçam.

    O FastMCP deriva da assinatura da função o schema que o cliente enxerga. Se
    um input model ganhar campo e a função aqui não, o cliente nunca saberá que
    o campo existe, e nada mais quebraria — falha silenciosa, do tipo que só
    aparece quando alguém reclama que a tool "ignora" um parâmetro.
    """
    import inspect

    for nome, spec in REGISTRY.items():
        fn = getattr(server, f"{nome}_tool")
        params = set(inspect.signature(fn).parameters) - {"ctx"}
        esperado = set(spec.input_model.model_fields)
        if spec.scope == "write":
            esperado.add("confirmacao")
        assert params == esperado, f"{nome}: assinatura {params} != modelo {esperado}"


def test_toda_tool_do_registry_esta_exposta():
    for nome in REGISTRY:
        assert hasattr(server, f"{nome}_tool"), f"{nome} não foi exposta no MCP"


def test_protecao_de_host_esta_ligada_e_conhece_o_dominio_publico():
    """Regressao de um `421 Invalid Host header` que so apareceu em producao.

    O SDK liga protecao contra DNS rebinding por padrao e, com a allowlist
    vazia, aceita so localhost. Atras do Traefik isso derruba TODO cliente. A
    protecao continua ligada de proposito; o que mudou e ela saber qual e o
    nosso host.

    O sufixo `:*` importa: a comparacao do SDK e exata, entao `127.0.0.1` sozinho
    nao casa com `127.0.0.1:8080`, que e o que o healthcheck manda.
    """
    seguranca = server.servidor.settings.transport_security
    assert seguranca is not None
    assert seguranca.enable_dns_rebinding_protection is True
    assert "aurora.pgdev.com.br" in seguranca.allowed_hosts

    validador = server.TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["aurora.pgdev.com.br", "127.0.0.1:*"],
        allowed_origins=[],
    )
    from mcp.server.transport_security import TransportSecurityMiddleware

    m = TransportSecurityMiddleware(validador)
    assert m._validate_host("aurora.pgdev.com.br") is True
    assert m._validate_host("127.0.0.1:8080") is True
    assert m._validate_host("127.0.0.1") is False
    assert m._validate_host("atacante.com") is False


def test_dominio_compartilhado_carrega_sob_as_deps_deste_servico():
    """pydantic aqui é 2.13; no agente é 2.10. Este teste vigia a diferença."""
    from src.policy.engine import PolicyEngine
    from src.tools.registry import schema_para_anthropic

    assert PolicyEngine().version
    schemas = schema_para_anthropic()
    assert len(schemas) == len(REGISTRY)
    for s in schemas:
        assert s["input_schema"]["type"] == "object"
