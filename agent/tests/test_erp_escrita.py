"""Escrita no ERP real, sem dublê: é o caminho que o agente usa para marcar, remarcar e cancelar.

POR QUE ISTO EXISTE
Em 27/09 o teto por IP virou uma instância de classe usada como dependência
(`Depends(_limitar)`). Com `from __future__ import annotations`, o FastAPI lê a
anotação `request: Request` do `__call__` como texto e, sem os globais do módulo,
não resolve: o parâmetro virou um `request` de query obrigatório. Toda escrita
passou a responder 422, e o `/openapi.json` (de que o `/docs` depende) a responder
500. Os testes do adaptador do agente usam um ERP falso, e os do ERP só cobriam
sessão e `/admin`, então nada disso apareceu.
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


def _sessao(c: TestClient) -> dict[str, str]:
    r = c.post("/sessions", headers=DE_FORA)
    assert r.status_code == 201, r.text
    return {**DE_FORA, "X-Tenant-Id": r.json()["tenant_id"]}


@pytest.mark.parametrize(
    ("metodo", "caminho"),
    [
        ("patch", "/consultas/qualquer"),
        ("post", "/consultas/qualquer/cancelamento"),
        ("post", "/consultas"),
    ],
)
def test_escrita_nao_pede_parametro_request_na_query(erp, metodo, caminho):
    c = TestClient(erp.app)
    r = getattr(c, metodo)(caminho, headers=_sessao(c), json={})
    faltando = [e["loc"] for e in r.json().get("detail", []) if isinstance(e, dict)]
    assert ["query", "request"] not in faltando, r.text


def test_remarcar_de_verdade_numa_sessao_isolada(erp):
    c = TestClient(erp.app)
    h = _sessao(c)
    consultas = c.get("/consultas", headers=h).json()
    assert consultas, "a sessão nova nasce com agenda semeada"
    r = c.patch(f"/consultas/{consultas[0]['id']}", headers=h, json={})
    # o corpo vazio é recusado pela validação do corpo, nunca por falta de `request`
    assert r.status_code == 422
    assert all(e["loc"][0] == "body" for e in r.json()["detail"]), r.text


def test_openapi_gera_e_nenhuma_operacao_pede_request(erp):
    esquema = TestClient(erp.app).get("/openapi.json")
    assert esquema.status_code == 200, esquema.text
    for caminho, operacoes in esquema.json()["paths"].items():
        for metodo, op in operacoes.items():
            nomes = [p["name"] for p in op.get("parameters", [])]
            assert "request" not in nomes, f"{metodo.upper()} {caminho}"
