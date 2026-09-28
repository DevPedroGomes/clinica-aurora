"""Um visitante não alcança a sessão de outro pelo ERP público.

O ERP fica aberto em aurora-erp.gomio.com.br para quem quiser inspecionar o
outro lado da integração, e isso incluía `/admin/chaos` e `/admin/reset`. O id
da sessão vinha do header que quem chama escolhe, e três coisas deixavam um
visitante atrapalhar outro:

- sem header, todo mundo caía no mesmo tenant `demo`;
- qualquer id inventado criava um tenant na hora;
- com 500 tenants o ERP despejava o mais antigo, então 500 ids inventados
  apagavam a agenda de todo visitante da página.

Aqui, "de fora" é request com `X-Real-Ip`, que o Traefik sempre escreve; o
agente e o servidor MCP falam pela rede interna e chegam sem ele.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

DE_FORA = {"X-Real-Ip": "203.0.113.7"}


@pytest.fixture
def erp():
    import erp.app.store
    import erp.app.main

    importlib.reload(erp.app.store)
    return importlib.reload(erp.app.main)


def _cliente(erp) -> TestClient:
    return TestClient(erp.app)


def _emitir(c: TestClient, ip: str = "203.0.113.7") -> str:
    r = c.post("/sessions", headers={"X-Real-Ip": ip})
    assert r.status_code == 201, r.text
    return r.json()["tenant_id"]


@pytest.mark.parametrize(
    ("metodo", "caminho"),
    [
        ("post", "/admin/chaos?ligado=true"),
        ("post", "/admin/reset"),
        ("get", "/admin/estado"),
        ("get", "/consultas"),
    ],
)
@pytest.mark.parametrize("origem", [DE_FORA, {}])
def test_sem_header_nao_existe_tenant_padrao(erp, metodo, caminho, origem):
    r = getattr(_cliente(erp), metodo)(caminho, headers=origem)
    assert r.status_code == 401
    assert "POST /sessions" in r.json()["detail"]


def test_de_fora_nao_se_alcanca_a_sessao_da_pagina(erp):
    c = _cliente(erp)
    # A sessão de um visitante da página, como o agente a cria.
    vizinho = {"X-Tenant-Id": "demo-3f9a1c2b7d4e"}
    antes = c.get("/admin/estado", headers=vizinho).json()["consultas"]

    for caminho in ("/admin/chaos?ligado=true", "/admin/reset"):
        r = c.post(caminho, headers={**vizinho, **DE_FORA})
        assert r.status_code == 401
    # Nem ler: o 401 é o mesmo de um id inventado, e não confirma que ele existe.
    assert c.get("/admin/estado", headers={**vizinho, **DE_FORA}).status_code == 401

    depois = c.get("/admin/estado", headers=vizinho).json()
    assert depois["down"] is False
    assert depois["consultas"] == antes


@pytest.mark.parametrize(
    "inventado", ["demo", "qualquer-coisa", "mcp-abc", "api-" + "0" * 32]
)
def test_id_que_ninguem_emitiu_e_recusado_de_fora(erp, inventado):
    r = _cliente(erp).post(
        "/admin/chaos?ligado=true", headers={"X-Tenant-Id": inventado, **DE_FORA}
    )
    assert r.status_code == 401


def test_sessao_emitida_derruba_e_religa_so_o_proprio_erp(erp):
    c = _cliente(erp)
    minha = {"X-Tenant-Id": _emitir(c), **DE_FORA}
    outra = {"X-Tenant-Id": _emitir(c, ip="198.51.100.4"), **DE_FORA}
    assert minha["X-Tenant-Id"] != outra["X-Tenant-Id"]
    assert minha["X-Tenant-Id"].startswith("api-")

    assert c.post("/admin/chaos?ligado=true", headers=minha).json() == {"down": True}
    assert c.get("/consultas", headers=minha).status_code == 503
    assert c.get("/consultas", headers=outra).status_code == 200

    assert c.post("/admin/chaos?ligado=false", headers=minha).json() == {"down": False}
    assert c.get("/consultas", headers=minha).status_code == 200
    assert c.post("/admin/reset", headers=minha).status_code == 204


def test_botao_derrubar_o_erp_da_pagina_continua_funcionando(erp):
    """O caminho do botão: agente, rede interna, id `demo-` sorteado por ele.

    Ele nasce na hora, e é isso que mantém a sessão do visitante de pé depois
    de um restart do ERP, que esvazia a memória."""
    c = _cliente(erp)
    sessao = {"X-Tenant-Id": "demo-a1b2c3d4e5f6"}
    assert c.post("/admin/chaos?ligado=true", headers=sessao).json() == {"down": True}
    assert c.get("/consultas", headers=sessao).status_code == 503
    assert c.get("/admin/estado", headers=sessao).json()["down"] is True
    assert c.post("/admin/chaos?ligado=false", headers=sessao).json() == {"down": False}
    assert c.get("/consultas", headers=sessao).status_code == 200
    assert c.post("/admin/reset", headers=sessao).status_code == 204


def test_emissao_de_sessao_tem_teto_por_ip(erp):
    c = _cliente(erp)
    for _ in range(erp.SESSOES_POR_IP):
        _emitir(c)
    assert c.post("/sessions", headers=DE_FORA).status_code == 429
    # O teto é de quem abusou, não de todo mundo.
    _emitir(c, ip="198.51.100.4")


def test_encher_uma_origem_nao_despeja_a_sessao_da_pagina(erp, monkeypatch):
    tetos = importlib.import_module("erp.app.store").MAX_TENANTS_POR_ORIGEM
    monkeypatch.setitem(tetos, "api", 3)
    c = _cliente(erp)
    pagina = {"X-Tenant-Id": "demo-0a1b2c3d4e5f"}
    c.post("/admin/chaos?ligado=true", headers=pagina)

    emitidas = [_emitir(c, ip=f"198.51.100.{i}") for i in range(10)]

    # A origem `api` despejou as próprias sessões mais antigas...
    primeira = {"X-Tenant-Id": emitidas[0], **DE_FORA}
    assert c.get("/admin/estado", headers=primeira).status_code == 401
    # ...e a da página continua viva, com o estado que tinha. Se ela tivesse
    # sido despejada, voltaria semeada de novo, com o ERP ligado.
    assert c.get("/admin/estado", headers=pagina).json()["down"] is True
