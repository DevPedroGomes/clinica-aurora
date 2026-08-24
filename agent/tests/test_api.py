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

        async def turno(self, conv, texto, ctx):
            from src.agent import Evento

            ctx.escritas_realizadas += 1
            ctx.custo_micros += self.custo_por_turno
            yield Evento("message", {"texto": f"ok: {texto}"})

    mod.agente = AgenteFalso()
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


def test_health_nao_conta_quanto_sobrou_do_teto(api):
    """Saldo do teto diário é número operacional — e dica de abuso.

    Publicar "restam 111" numa rota aberta diz a qualquer visitante exatamente
    quanto falta para derrubar a demo do dia.
    """
    corpo = cliente(api).get("/health").json()
    assert set(corpo) == {"status", "model"}


def test_schema_da_api_interna_nao_fica_publico(api):
    """Desligar /docs não desliga /openapi.json — e ele entrega /api/chaos,
    /api/reset e todos os parâmetros de bandeja."""
    assert cliente(api).get("/openapi.json").status_code == 404
