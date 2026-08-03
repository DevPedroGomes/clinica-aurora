"""clinica-aurora-erp — o "sistema do cliente" da demo.

API pequena de propósito: profissionais, pacientes e consultas. Ela é publicada
junto com a demo, com OpenAPI em /docs, para que o visitante possa inspecionar o
outro lado da integração e confirmar que a escrita aconteceu de verdade.

Duas coisas aqui existem só por causa da demo, e estão declaradas na página:
  - `X-Tenant-Id`: isola a agenda de cada visitante.
  - `/admin/chaos`: derruba o ERP sob demanda, para provar que o agente escala
    em vez de inventar uma confirmação quando a escrita falha.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware

from .models import (
    AbrirVagaRequest,
    CancelarRequest,
    Consulta,
    ConsultaDetalhada,
    ConsultaStatus,
    Paciente,
    Profissional,
    RemarcarRequest,
)
from .store import BAHIA, PACIENTES, PROFISSIONAIS, Tenant, store

DURACOES_VALIDAS = {30, 45, 60}

app = FastAPI(
    title="Clínica Aurora ERP",
    version="1.0.0",
    description=(
        "Sistema de agenda da Clínica Aurora — uma clínica **fictícia**.\n\n"
        "Publicado junto com a demo do agente para que qualquer pessoa possa ver "
        "o outro lado da integração: o agente fala com esta API por HTTP, como "
        "falaria com o ERP de um cliente real.\n\n"
        "Cada sessão da demo recebe seu próprio `X-Tenant-Id` e sua própria agenda."
    ),
    docs_url="/docs",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ERP_CORS_ORIGINS", "*").split(","),
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)


def agora() -> datetime:
    return datetime.now(timezone.utc)


def get_tenant(
    x_tenant_id: Annotated[str, Header(description="Identificador da sessão da demo.")] = "demo",
) -> Tenant:
    t = store.tenant(x_tenant_id[:64], agora())
    if t.down:
        # 503 e não 500: é indisponibilidade declarada, e é o que o adapter do
        # agente trata como "sistema fora do ar" para escalar.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ERP indisponível (modo caos ligado nesta sessão).",
        )
    return t


TenantDep = Annotated[Tenant, Depends(get_tenant)]


def _detalhar(c: Consulta) -> ConsultaDetalhada:
    pac = next((p for p in PACIENTES if p.id == c.paciente_id), None)
    prof = next((p for p in PROFISSIONAIS if p.id == c.profissional_id), None)
    return ConsultaDetalhada(
        **c.model_dump(),
        paciente_nome=pac.nome if pac else "—",
        profissional_nome=prof.nome if prof else "—",
    )


@app.get("/health", tags=["infra"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/profissionais", response_model=list[Profissional], tags=["cadastro"])
def listar_profissionais() -> list[Profissional]:
    return PROFISSIONAIS


@app.get("/pacientes", response_model=list[Paciente], tags=["cadastro"])
def listar_pacientes() -> list[Paciente]:
    return PACIENTES


@app.get("/consultas", response_model=list[ConsultaDetalhada], tags=["agenda"])
def listar_consultas(
    tenant: TenantDep,
    profissional_id: Annotated[str | None, Query()] = None,
    data: Annotated[datetime | None, Query(description="Filtra pelo dia (America/Bahia).")] = None,
) -> list[ConsultaDetalhada]:
    return [_detalhar(c) for c in store.listar(tenant, profissional_id, data)]


@app.get(
    "/consultas/{consulta_id}",
    response_model=ConsultaDetalhada,
    tags=["agenda"],
    responses={404: {"description": "Consulta inexistente nesta sessão."}},
)
def obter_consulta(consulta_id: str, tenant: TenantDep) -> ConsultaDetalhada:
    c = tenant.consultas.get(consulta_id)
    if c is None:
        # Id de outro tenant cai aqui: 404, nunca o dado do vizinho.
        raise HTTPException(status_code=404, detail="Consulta não encontrada.")
    return _detalhar(c)


@app.patch(
    "/consultas/{consulta_id}",
    response_model=ConsultaDetalhada,
    tags=["agenda"],
    responses={404: {"description": "Consulta inexistente nesta sessão."}, 409: {"description": "Horário ocupado."}},
)
def remarcar_consulta(
    consulta_id: str, body: RemarcarRequest, tenant: TenantDep
) -> ConsultaDetalhada:
    c = tenant.consultas.get(consulta_id)
    if c is None or c.status is ConsultaStatus.CANCELADA:
        raise HTTPException(status_code=404, detail="Consulta não encontrada.")

    duracao = c.fim - c.inicio
    novo_inicio = body.novo_inicio
    novo_fim = novo_inicio + duracao

    # O ERP também recusa conflito. A política do agente é a primeira barreira,
    # esta é a segunda: um sistema real não confia em quem chama.
    for outra in tenant.consultas.values():
        if outra.id == c.id or outra.status is ConsultaStatus.CANCELADA:
            continue
        if outra.profissional_id != c.profissional_id:
            continue
        if novo_inicio < outra.fim and outra.inicio < novo_fim:
            raise HTTPException(status_code=409, detail="Horário já ocupado para este profissional.")

    c.inicio = novo_inicio
    c.fim = novo_fim
    return _detalhar(c)


@app.post(
    "/consultas/{consulta_id}/cancelamento",
    response_model=ConsultaDetalhada,
    tags=["agenda"],
    responses={404: {"description": "Consulta inexistente nesta sessão."}},
)
def cancelar_consulta(
    consulta_id: str, body: CancelarRequest, tenant: TenantDep
) -> ConsultaDetalhada:
    c = tenant.consultas.get(consulta_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Consulta não encontrada.")
    c.status = ConsultaStatus.CANCELADA
    c.motivo_cancelamento = body.motivo
    return _detalhar(c)


@app.post(
    "/consultas",
    response_model=ConsultaDetalhada,
    status_code=201,
    tags=["agenda"],
    responses={409: {"description": "Horário ocupado."}, 422: {"description": "Duração inválida."}},
)
def abrir_vaga(body: AbrirVagaRequest, tenant: TenantDep) -> ConsultaDetalhada:
    if body.duracao_min not in DURACOES_VALIDAS:
        raise HTTPException(status_code=422, detail="Duração deve ser 30, 45 ou 60 minutos.")
    if not any(p.id == body.profissional_id for p in PROFISSIONAIS):
        raise HTTPException(status_code=404, detail="Profissional não encontrado.")

    fim = body.inicio + timedelta(minutes=body.duracao_min)
    for outra in tenant.consultas.values():
        if outra.status is ConsultaStatus.CANCELADA or outra.profissional_id != body.profissional_id:
            continue
        if body.inicio < outra.fim and outra.inicio < fim:
            raise HTTPException(status_code=409, detail="Horário já ocupado para este profissional.")

    cid = store.novo_id()
    consulta = Consulta(
        id=cid,
        paciente_id="",
        profissional_id=body.profissional_id,
        inicio=body.inicio,
        fim=fim,
    )
    tenant.consultas[cid] = consulta
    return _detalhar(consulta)


# -- superfície da demo, não do ERP ------------------------------------------


@app.post("/admin/chaos", tags=["demo"], summary="Liga ou desliga o modo caos desta sessão")
def chaos(
    ligado: bool,
    x_tenant_id: Annotated[str, Header()] = "demo",
) -> dict[str, bool]:
    """Com o caos ligado, todo endpoint de agenda responde 503 para esta sessão.

    Existe para o visitante ver o agente falhando com honestidade: ele diz que
    não conseguiu gravar e escala, em vez de devolver uma confirmação inventada.
    """
    t = store.tenant(x_tenant_id[:64], agora())
    t.down = ligado
    return {"down": t.down}


@app.post("/admin/reset", tags=["demo"], summary="Recria a agenda inicial desta sessão")
def reset(x_tenant_id: Annotated[str, Header()] = "demo") -> Response:
    store.reset(x_tenant_id[:64], agora())
    return Response(status_code=204)


@app.get("/admin/estado", tags=["demo"], summary="Agenda inteira desta sessão, para o painel")
def estado(x_tenant_id: Annotated[str, Header()] = "demo") -> dict[str, object]:
    t = store.tenant(x_tenant_id[:64], agora())
    consultas = sorted(t.consultas.values(), key=lambda c: c.inicio)
    return {
        "down": t.down,
        "timezone": "America/Bahia",
        "agora": agora().astimezone(BAHIA).isoformat(),
        "consultas": [_detalhar(c).model_dump(mode="json") for c in consultas],
    }
