"""A superfície pública: isolamento de sessão, rate limit e teto.

Estes são os testes que importam numa demo aberta — não provam que o agente é
inteligente, provam que um visitante mal-intencionado não custa caro.
"""

from __future__ import annotations

import importlib

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

ERP = "http://erp:8080"


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("BUDGET_PATH", str(tmp_path / "budget.json"))
    # US$ 0,03 de teto com US$ 0,01 por turno: três turnos cabem no dia.
    monkeypatch.setenv("DAILY_BUDGET_USD", "0.03")
    monkeypatch.setenv("TURN_RESERVE_USD", "0.01")
    monkeypatch.setenv("DEMO_KILL_SWITCH", "0")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-teste")
    import src.budget
    import src.api

    importlib.reload(src.budget)
    mod = importlib.reload(src.api)

    class AgenteFalso:
        # O turno precisa GASTAR, senão não exercita o teto: agora o dia é
        # debitado pelo custo reconciliado, e um agente que não chama nada não
        # consome orçamento nenhum — que é o comportamento certo, e tem teste
        # próprio abaixo.
        custo_por_turno = 10_000  # US$ 0,01 em micro-dólares

        def __init__(self) -> None:
            # O que o turno ENCONTROU confirmável, para provar que a vista nova
            # descarta a proposta pendente antes de o turno começar.
            self.confirmacoes_vistas: list[set[str]] = []

        async def turno(self, conv, texto, ctx):
            from src.agent import Evento

            self.confirmacoes_vistas.append(set(ctx.confirmacoes))
            ctx.escritas_realizadas += 1
            ctx.custo_micros += self.custo_por_turno
            yield Evento("message", {"texto": f"ok: {texto}"})

    from src.agent import Saude

    falso = AgenteFalso()
    falso.saude = Saude()
    mod.agente = falso
    return mod


def cliente(api) -> TestClient:
    # https e não http: o cookie de sessão é `Secure`, e um cliente falando
    # http simplesmente não o guarda nem o devolve. Em produção só existe
    # HTTPS (Traefik), então o teste precisa falar a mesma coisa — senão ele
    # testa um caminho que ninguém percorre.
    return TestClient(api.app, base_url="https://testserver")


def test_health_prova_que_o_processo_esta_de_pe(api):
    r = cliente(api).get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_diz_quando_o_agente_nao_consegue_responder(api):
    """O processo de pé não é o agente respondendo: a queda de 2026-09-22
    passou horas com /health "ok". O status HTTP continua 200 de propósito,
    porque reiniciar o container não conserta provedor."""
    api.agente.saude.registrar_falha("provider_billing")
    r = cliente(api).get("/health")
    assert r.status_code == 200
    assert r.json()["agente"] == "indisponivel"
    # O código fino fica no log do servidor, não numa rota aberta.
    assert "provider_billing" not in r.text

    api.agente.saude.registrar_ok()
    assert cliente(api).get("/health").json()["agente"] == "ok"


def test_idioma_da_interface_chega_ao_contexto(api):
    c = cliente(api)
    with c.stream("POST", "/api/chat", json={"mensagem": "hi", "idioma": "en"}) as r:
        r.read()
    sessao = next(iter(api._sessoes.values()))
    assert sessao.ctx.idioma == "en"
    # Valor fora da lista não passa: cai no português.
    with c.stream("POST", "/api/chat", json={"mensagem": "oi", "idioma": "xx"}) as r:
        r.read()
    assert sessao.ctx.idioma == "pt"


def test_mensagem_vazia_e_gigante_sao_recusadas(api):
    c = cliente(api)
    assert c.post("/api/chat", json={"mensagem": "   "}).status_code == 400
    assert c.post("/api/chat", json={"mensagem": "a" * 5000}).status_code == 400


def test_cada_visitante_recebe_o_proprio_tenant(api):
    with respx.mock:
        rota = respx.get(f"{ERP}/admin/estado").mock(
            return_value=httpx.Response(200, json={"down": False, "consultas": []})
        )
        cliente(api).get("/api/estado")
        cliente(api).get("/api/estado")  # cliente novo = cookie novo

    tenants = {c.request.headers["X-Tenant-Id"] for c in rota.calls}
    assert len(tenants) == 2


def test_conversa_continua_na_mesma_sessao(api):
    c = cliente(api)
    with respx.mock:
        respx.get(f"{ERP}/admin/estado").mock(
            return_value=httpx.Response(200, json={"down": False, "consultas": []})
        )
        c.post("/api/chat", json={"mensagem": "oi"})
        estado = c.get("/api/estado").json()
    assert estado["conversa"]["escritas"] == 1


def test_teto_diario_bloqueia_com_recado_humano(api):
    c = cliente(api)
    for _ in range(3):
        assert c.post("/api/chat", json={"mensagem": "oi"}).status_code == 200
    r = c.post("/api/chat", json={"mensagem": "oi"})
    assert r.status_code == 503
    # Recado humano, não um código: a interface é em inglês, e quem estoura o
    # teto tem de entender que é limite da demo e não defeito do agente.
    assert "tomorrow" in r.json()["detail"]


def test_o_dia_e_debitado_pelo_gasto_real_e_nao_pela_reserva(api):
    """Reserva é estimativa; o que fica no dia é o que o `usage` disse.

    Antes a unidade era turno, então qualquer turno custava exatamente um,
    fossem seis chamadas pagas ou nenhuma. Um agente barato esgotava o dia no
    mesmo ritmo de um caro.
    """
    import src.budget as budget

    api.agente.custo_por_turno = 1_000  # US$ 0,001, um décimo da reserva
    c = cliente(api)
    for _ in range(3):
        assert c.post("/api/chat", json={"mensagem": "oi"}).status_code == 200

    assert budget.usado() == 3_000
    # Com a unidade antiga o dia já teria acabado aqui.
    assert c.post("/api/chat", json={"mensagem": "oi"}).status_code == 200


def test_turno_que_nao_chama_nada_nao_consome_o_dia(api):
    import src.budget as budget

    api.agente.custo_por_turno = 0
    c = cliente(api)
    for _ in range(10):
        c.post("/api/chat", json={"mensagem": "oi"})
    assert budget.usado() == 0


def test_rate_limit_por_ip_real(api, monkeypatch):
    monkeypatch.setattr(api, "RATE_POR_IP", 2)
    c = cliente(api)
    cab = {"x-real-ip": "203.0.113.7"}
    assert c.post("/api/chat", json={"mensagem": "a"}, headers=cab).status_code == 200
    assert c.post("/api/chat", json={"mensagem": "b"}, headers=cab).status_code == 200
    assert c.post("/api/chat", json={"mensagem": "c"}, headers=cab).status_code == 429


def test_sem_x_real_ip_todos_caem_no_mesmo_balde(api, monkeypatch):
    """Requisição sem o header do Traefik não ganha cota nova — bucket hostil."""
    monkeypatch.setattr(api, "RATE_POR_IP", 1)
    assert cliente(api).post("/api/chat", json={"mensagem": "a"}).status_code == 200
    assert cliente(api).post("/api/chat", json={"mensagem": "b"}).status_code == 429


def test_chaos_vai_para_o_tenant_da_sessao(api):
    c = cliente(api)
    with respx.mock:
        rota = respx.post(f"{ERP}/admin/chaos").mock(
            return_value=httpx.Response(200, json={"down": True})
        )
        respx.get(f"{ERP}/admin/estado").mock(
            return_value=httpx.Response(200, json={"down": True, "consultas": []})
        )
        c.get("/api/estado")
        c.post("/api/chaos?ligado=true")
    assert rota.calls.last.request.headers["X-Tenant-Id"].startswith("demo-")


def test_criar_sessao_em_laco_nao_despeja_a_conversa_de_outro_visitante(
    api, monkeypatch
):
    """`_sessoes` despeja a mais antiga quando enche. Sem teto de sessão NOVA por
    IP, um laço de GET sem cookie em /api/estado enchia o dicionário e apagava a
    conversa de quem estava no meio de uma."""
    monkeypatch.setattr(api, "NOVAS_SESSOES_POR_IP", 3)
    monkeypatch.setattr(api, "MAX_SESSOES", 4)  # o visitante e as 3 que cabem
    visitante = cliente(api)
    with respx.mock:
        respx.get(f"{ERP}/admin/estado").mock(
            return_value=httpx.Response(200, json={"down": False, "consultas": []})
        )
        visitante.post(
            "/api/chat", json={"mensagem": "oi"}, headers={"x-real-ip": "198.51.100.4"}
        )

        abuso = {"x-real-ip": "203.0.113.7"}
        codigos = [
            cliente(api).get("/api/estado", headers=abuso).status_code for _ in range(6)
        ]
        assert codigos == [200, 200, 200, 429, 429, 429]

        # A sessão do visitante sobreviveu: a escrita da conversa continua lá.
        estado = visitante.get("/api/estado", headers={"x-real-ip": "198.51.100.4"})
    assert estado.json()["conversa"]["escritas"] == 1


def test_health_nao_conta_quanto_sobrou_do_teto(api):
    """Saldo do teto diário é número operacional — e dica de abuso.

    Publicar "restam 111" numa rota aberta diz a qualquer visitante exatamente
    quanto falta para derrubar a demo do dia.
    """
    corpo = cliente(api).get("/health").json()
    assert set(corpo) == {"status", "model", "agente"}


def test_meta_sem_proof_json_devolve_vazio_em_vez_de_inventar(api, monkeypatch):
    """Sem o arquivo gerado no CI, a barra some — nao mostra numero chutado.

    O case study do portfolio dizia "124 tests" enquanto a suite rodava 134.
    Ninguem mentiu: o numero foi digitado uma vez e a suite continuou crescendo.
    """
    monkeypatch.setattr(api, "_PROVAS", None)
    assert cliente(api).get("/api/meta").json() == {}


def test_meta_nao_vaza_o_saldo_do_teto(api, monkeypatch):
    """Mesmo motivo do /health: saldo e dica de quanto falta para derrubar."""
    monkeypatch.setattr(api, "_PROVAS", {"testes": 151, "cobertura_total": 94.0})
    corpo = cliente(api).get("/api/meta").json()
    assert corpo["testes"] == 151
    assert not any("teto" in k or "restante" in k or "usado" in k for k in corpo)


def test_schema_da_api_interna_nao_fica_publico(api):
    """Desligar /docs não desliga /openapi.json — e ele entrega /api/chaos,
    /api/reset e todos os parâmetros de bandeja."""
    assert cliente(api).get("/openapi.json").status_code == 404


# -- vista: recarregar a página começa conversa nova --------------------------
#
# O "sim" cego: a conversa e as confirmações vivem no servidor, presas ao
# cookie, enquanto o chat da página volta vazio depois do F5. Quem recarregava
# e digitava "sim" executava uma proposta que não estava mais na tela.

VISTA_A = {"X-Aurora-Vista": "vista-aaaa-1"}
VISTA_B = {"X-Aurora-Vista": "vista-bbbb-2"}
ESTADO_OK = httpx.Response(200, json={"down": False, "consultas": []})


def test_recarregar_a_pagina_descarta_a_confirmacao_pendente(api):
    c = cliente(api)
    c.post("/api/chat", json={"mensagem": "cancela"}, headers=VISTA_A)
    sessao = next(iter(api._sessoes.values()))
    sessao.ctx.confirmacoes.add("cancelar_consulta:{}")
    with respx.mock:
        respx.get(f"{ERP}/admin/estado").mock(return_value=ESTADO_OK)
        c.get("/api/estado", headers=VISTA_B)
    assert sessao.ctx.confirmacoes == set()
    assert sessao.conversa.state.value == "greeting"


def test_mesma_vista_nao_zera_a_conversa(api):
    c = cliente(api)
    c.post("/api/chat", json={"mensagem": "cancela"}, headers=VISTA_A)
    sessao = next(iter(api._sessoes.values()))
    conversa = sessao.conversa
    sessao.ctx.confirmacoes.add("cancelar_consulta:{}")
    with respx.mock:
        respx.get(f"{ERP}/admin/estado").mock(return_value=ESTADO_OK)
        c.get("/api/estado", headers=VISTA_A)
    assert sessao.conversa is conversa
    assert sessao.ctx.confirmacoes == {"cancelar_consulta:{}"}


def test_outra_aba_nao_confirma_a_proposta_desta(api):
    c = cliente(api)
    c.post("/api/chat", json={"mensagem": "cancela"}, headers=VISTA_A)
    sessao = next(iter(api._sessoes.values()))
    sessao.ctx.confirmacoes.add("cancelar_consulta:{}")
    c.post("/api/chat", json={"mensagem": "sim"}, headers=VISTA_B)
    assert api.agente.confirmacoes_vistas[-1] == set()


def test_recarregar_nao_devolve_o_teto_de_escritas(api):
    c = cliente(api)
    c.post("/api/chat", json={"mensagem": "remarca"}, headers=VISTA_A)
    sessao = next(iter(api._sessoes.values()))
    assert sessao.ctx.escritas_realizadas == 1
    with respx.mock:
        respx.get(f"{ERP}/admin/estado").mock(return_value=ESTADO_OK)
        estado = c.get("/api/estado", headers=VISTA_B).json()
    assert estado["conversa"]["escritas"] == 1


def test_vista_fora_do_formato_nao_muda_nada(api):
    c = cliente(api)
    c.post("/api/chat", json={"mensagem": "cancela"}, headers=VISTA_A)
    sessao = next(iter(api._sessoes.values()))
    sessao.ctx.confirmacoes.add("cancelar_consulta:{}")
    with respx.mock:
        respx.get(f"{ERP}/admin/estado").mock(return_value=ESTADO_OK)
        for estranha in ("curta", "com espaco e !", "x" * 65):
            c.get("/api/estado", headers={"X-Aurora-Vista": estranha})
        c.get("/api/estado")
    assert sessao.ctx.confirmacoes == {"cancelar_consulta:{}"}


def test_segunda_mensagem_com_turno_em_andamento_recebe_409(api):
    c = cliente(api)
    c.post("/api/chat", json={"mensagem": "oi"})
    sessao = next(iter(api._sessoes.values()))
    sessao.lock.locked = lambda: True  # turno anterior ainda em andamento
    assert c.post("/api/chat", json={"mensagem": "sim"}).status_code == 409


# -- ERP inalcançável não vira 500 --------------------------------------------


@pytest.mark.parametrize(
    ("metodo", "caminho", "rota"),
    [
        ("get", "/api/estado", ("GET", "/admin/estado")),
        ("post", "/api/chaos?ligado=true", ("POST", "/admin/chaos")),
        ("post", "/api/reset", ("POST", "/admin/reset")),
    ],
)
def test_estado_com_erp_inalcancavel_responde_503(api, metodo, caminho, rota):
    c = cliente(api)
    with respx.mock:
        respx.route(method=rota[0], url=f"{ERP}{rota[1]}").mock(
            side_effect=httpx.ConnectError("sem rota")
        )
        r = getattr(c, metodo)(caminho)
    assert r.status_code == 503
    assert r.json() == {"detail": "ERP unreachable."}
