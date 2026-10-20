"""O cadastro de pacientes do ERP público: atrás de sessão, e claramente fictício.

`GET /pacientes` respondia sem `X-Tenant-Id`, com nome e um telefone mascarado
cada um de um jeito ("(71) 9****-**415" tinha um dígito a mais). O ERP é aberto
para quem quiser inspecionar a integração, e o cadastro não precisa ser: o
agente lê com a sessão dele. As rotas `/admin` saem da documentação pública,
mas continuam atendendo a página e o MCP.
"""

from __future__ import annotations

import importlib
import re

import pytest
from fastapi.testclient import TestClient

DE_FORA = {"X-Real-Ip": "203.0.113.7"}


@pytest.fixture
def erp():
    import erp.app.store
    import erp.app.main

    importlib.reload(erp.app.store)
    return importlib.reload(erp.app.main)


def _emitir(c: TestClient) -> str:
    r = c.post("/sessions", headers=DE_FORA)
    assert r.status_code == 201, r.text
    return r.json()["tenant_id"]


def test_pacientes_com_sessao_devolve_mascara_uniforme(erp):
    c = TestClient(erp.app)
    t = _emitir(c)
    r = c.get("/pacientes", headers={**DE_FORA, "X-Tenant-Id": t})
    assert r.status_code == 200
    tels = [p["telefone"] for p in r.json()]
    assert len(tels) == 3
    assert all(re.fullmatch(r"\+55 71 9\*{4}-\*{2}\d{2}", x) for x in tels), tels


def test_pacientes_com_o_erp_derrubado_responde_503(erp):
    c = TestClient(erp.app)
    t = _emitir(c)
    h = {**DE_FORA, "X-Tenant-Id": t}
    assert c.post("/admin/chaos?ligado=true", headers=h).status_code == 200
    assert c.get("/pacientes", headers=h).status_code == 503


def test_admin_fora_do_schema_publico_mas_ainda_atende(erp):
    c = TestClient(erp.app)
    paths = c.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if p.startswith("/admin")]
    assert "/pacientes" in paths

    t = _emitir(c)
    estado = c.get("/admin/estado", headers={**DE_FORA, "X-Tenant-Id": t})
    assert estado.status_code == 200


def test_descricao_diz_que_tudo_e_ficticio(erp):
    descricao = TestClient(erp.app).get("/openapi.json").json()["info"]["description"]
    assert "fictional" in descricao.lower()
    assert "Patients, practitioners and schedule are fictional." in descricao


@pytest.mark.parametrize(
    ("numero", "mascara"),
    [
        ("+55 71 90000-0001", "+55 71 9****-**01"),
        ("+55 71 90000-0002", "+55 71 9****-**02"),
        ("+55 71 91234-5678", "+55 71 9****-**78"),
    ],
)
def test_mascara_preserva_o_ddd_o_nove_e_os_dois_finais(numero, mascara):
    from erp.app.store import mascarar_telefone

    assert mascarar_telefone(numero) == mascara
