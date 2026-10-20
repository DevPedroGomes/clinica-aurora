"""O adapter HTTP — timeout, retry, circuit breaker e a fronteira 4xx/5xx.

Esta é a peça que decide se o agente escala ou insiste quando o sistema do
cliente vacila, então ela é testada contra respostas reais de HTTP, não contra
o fake.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from src.adapters.erp import (
    BREAKER_ABERTO_S,
    ErpAdapter,
    ErpIndisponivel,
    ErpRecusou,
)

BAHIA = ZoneInfo("America/Bahia")
AGORA = datetime(2026, 8, 10, 9, 0, tzinfo=BAHIA)
BASE = "http://erp.local"

CONSULTA_JSON = {
    "id": "cons_1",
    "paciente_id": "pac_ana",
    "paciente_nome": "Ana Ribeiro",
    "profissional_id": "prof_bruno",
    "profissional_nome": "Dr. Bruno Salles",
    "inicio": "2026-08-11T09:00:00-03:00",
    "fim": "2026-08-11T09:30:00-03:00",
    "status": "agendada",
}


def adapter(client: httpx.AsyncClient) -> ErpAdapter:
    return ErpAdapter(BASE, tenant_id="t1", client=client)


@pytest.fixture
async def client():
    async with httpx.AsyncClient(timeout=5.0) as c:
        yield c


@respx.mock
async def test_tenant_vai_no_header_de_todo_request(client: httpx.AsyncClient):
    rota = respx.get(f"{BASE}/consultas").mock(
        return_value=httpx.Response(200, json=[CONSULTA_JSON])
    )
    await adapter(client).listar(at=AGORA)
    assert rota.calls.last.request.headers["X-Tenant-Id"] == "t1"


@respx.mock
async def test_leitura_repete_uma_vez_em_5xx(client: httpx.AsyncClient):
    rota = respx.get(f"{BASE}/consultas").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=[CONSULTA_JSON])]
    )
    consultas = await adapter(client).listar(at=AGORA)
    assert len(consultas) == 1
    assert rota.call_count == 2


@respx.mock
async def test_escrita_nunca_repete(client: httpx.AsyncClient):
    """Retry em escrita gera consulta duplicada — docs/06-adapters §2."""
    rota = respx.patch(f"{BASE}/consultas/cons_1").mock(
        return_value=httpx.Response(503)
    )
    with pytest.raises(ErpIndisponivel):
        await adapter(client).remarcar("cons_1", AGORA + timedelta(days=1), at=AGORA)
    assert rota.call_count == 1


@respx.mock
async def test_4xx_e_recusa_do_sistema_nao_indisponibilidade(client: httpx.AsyncClient):
    respx.patch(f"{BASE}/consultas/cons_1").mock(
        return_value=httpx.Response(
            409, json={"detail": "Horário já ocupado para este profissional."}
        )
    )
    a = adapter(client)
    with pytest.raises(ErpRecusou) as exc:
        await a.remarcar("cons_1", AGORA + timedelta(days=1), at=AGORA)
    assert exc.value.status == 409
    # 4xx não conta para o breaker: o sistema está de pé, ele só disse não.
    assert a._breaker.falhas == 0


@respx.mock
async def test_404_em_obter_vira_none(client: httpx.AsyncClient):
    respx.get(f"{BASE}/consultas/cons_x").mock(
        return_value=httpx.Response(404, json={"detail": "não encontrada"})
    )
    assert await adapter(client).obter("cons_x", at=AGORA) is None


@respx.mock
async def test_tres_falhas_abrem_o_circuito(client: httpx.AsyncClient):
    respx.patch(f"{BASE}/consultas/cons_1").mock(return_value=httpx.Response(503))
    a = adapter(client)
    for _ in range(3):
        with pytest.raises(ErpIndisponivel):
            await a.remarcar("cons_1", AGORA + timedelta(days=1), at=AGORA)
    assert a._breaker.aberto(AGORA) is True


@respx.mock
async def test_circuito_aberto_falha_sem_bater_no_sistema(client: httpx.AsyncClient):
    rota = respx.patch(f"{BASE}/consultas/cons_1").mock(
        return_value=httpx.Response(503)
    )
    a = adapter(client)
    for _ in range(3):
        with pytest.raises(ErpIndisponivel):
            await a.remarcar("cons_1", AGORA + timedelta(days=1), at=AGORA)
    chamadas_ate_abrir = rota.call_count

    with pytest.raises(ErpIndisponivel, match="circuito aberto"):
        await a.remarcar("cons_1", AGORA + timedelta(days=1), at=AGORA)
    assert rota.call_count == chamadas_ate_abrir  # não bateu de novo


@respx.mock
async def test_circuito_reabre_depois_da_janela(client: httpx.AsyncClient):
    respx.patch(f"{BASE}/consultas/cons_1").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, json=CONSULTA_JSON),
        ]
    )
    a = adapter(client)
    for _ in range(3):
        with pytest.raises(ErpIndisponivel):
            await a.remarcar("cons_1", AGORA + timedelta(days=1), at=AGORA)

    depois = AGORA + timedelta(seconds=BREAKER_ABERTO_S + 1)
    consulta = await a.remarcar("cons_1", AGORA + timedelta(days=1), at=depois)
    assert consulta.id == "cons_1"
    assert a._breaker.falhas == 0


@respx.mock
async def test_timeout_vira_indisponivel(client: httpx.AsyncClient):
    respx.patch(f"{BASE}/consultas/cons_1").mock(
        side_effect=httpx.ConnectTimeout("timeout")
    )
    with pytest.raises(ErpIndisponivel):
        await adapter(client).remarcar("cons_1", AGORA + timedelta(days=1), at=AGORA)


@respx.mock
async def test_sucesso_zera_as_falhas_acumuladas(client: httpx.AsyncClient):
    respx.get(f"{BASE}/consultas").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=[])]
    )
    a = adapter(client)
    await a.listar(at=AGORA)
    assert a._breaker.falhas == 0


@respx.mock
async def test_abrir_vaga_manda_o_payload_que_o_erp_documenta(
    client: httpx.AsyncClient,
):
    rota = respx.post(f"{BASE}/consultas").mock(
        return_value=httpx.Response(201, json=CONSULTA_JSON)
    )
    await adapter(client).abrir_vaga(
        "prof_bruno", AGORA + timedelta(days=3), 45, at=AGORA
    )
    body = rota.calls.last.request.content.decode()
    assert '"profissional_id":"prof_bruno"' in body.replace(", ", ",").replace(
        '": "', '":"'
    )
    assert '"duracao_min":45' in body.replace(", ", ",").replace('": ', '":')


@respx.mock
async def test_cancelar_manda_o_motivo(client: httpx.AsyncClient):
    rota = respx.post(f"{BASE}/consultas/cons_1/cancelamento").mock(
        return_value=httpx.Response(200, json={**CONSULTA_JSON, "status": "cancelada"})
    )
    c = await adapter(client).cancelar("cons_1", "paciente adoeceu", at=AGORA)
    assert c.status == "cancelada"
    assert "paciente adoeceu" in rota.calls.last.request.content.decode()


@respx.mock
async def test_listar_pacientes_le_o_cadastro_da_sessao(client: httpx.AsyncClient):
    rota = respx.get(f"{BASE}/pacientes").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "pac_ana",
                    "nome": "Ana Ribeiro",
                    "telefone": "+55 71 9****-**01",
                }
            ],
        )
    )
    pacientes = await adapter(client).listar_pacientes(at=AGORA)
    assert [(p.id, p.nome) for p in pacientes] == [("pac_ana", "Ana Ribeiro")]
    assert rota.calls.last.request.headers["X-Tenant-Id"] == "t1"
