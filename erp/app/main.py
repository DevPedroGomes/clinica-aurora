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
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated

from collections import defaultdict, deque

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

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
from .store import (
    BAHIA,
    MAX_CONSULTAS_POR_TENANT,
    PACIENTES,
    PROFISSIONAIS,
    Tenant,
    store,
)

DURACOES_VALIDAS = {30, 45, 60}

# Rate limit por IP nas rotas que mudam estado. Le o peer real que o Traefik
# poe em x-real-ip; quem chega sem o header cai num balde comum em vez de
# ganhar cota nova a cada requisicao.
ESCRITAS_POR_IP = 60
JANELA_S = 600
_hits: dict[str, deque[float]] = defaultdict(deque)


def _limitar(request: Request) -> None:
    ip = request.headers.get("x-real-ip") or "sem-ip"
    agora = time.monotonic()
    janela = _hits[ip]
    while janela and agora - janela[0] > JANELA_S:
        janela.popleft()
    if not janela:
        _hits.pop(ip, None)  # nao deixa o dicionario crescer com IPs ociosos
        janela = _hits[ip]
    if len(janela) >= ESCRITAS_POR_IP:
        raise HTTPException(429, "Too many changes. Please wait a few minutes.")
    janela.append(agora)


app = FastAPI(
    title="Clínica Aurora ERP",
    version="1.0.0",
    description=(
        "Scheduling system of Clínica Aurora — a **fictional** clinic.\n\n"
        "Published alongside the agent demo so anyone can see the other side of "
        "the integration: the agent talks to this API over HTTP, exactly as it "
        "would talk to a real client's ERP.\n\n"
        "Each demo session gets its own `X-Tenant-Id` and its own schedule."
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
    x_tenant_id: Annotated[
        str, Header(description="Demo session identifier.")
    ] = "demo",
) -> Tenant:
    t = store.tenant(x_tenant_id[:64], agora())
    if t.down:
        # 503 e não 500: é indisponibilidade declarada, e é o que o adapter do
        # agente trata como "sistema fora do ar" para escalar.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ERP unavailable (chaos mode is on for this session).",
        )
    return t


TenantDep = Annotated[Tenant, Depends(get_tenant)]


def _detalhar(c: Consulta) -> ConsultaDetalhada:
    pac = next((p for p in PACIENTES if p.id == c.paciente_id), None)
    prof = next((p for p in PROFISSIONAIS if p.id == c.profissional_id), None)
    # Sem paciente devolve string VAZIA, e não "—". São coisas diferentes: vazio
    # é uma vaga livre, que não tem titular por definição; "—" é um id que
    # aponta para um cadastro que não existe, e isso é defeito de dado. Colapsar
    # os dois fazia a vaga aberta aparecer no painel como uma consulta cujo
    # nome do paciente sumiu.
    return ConsultaDetalhada(
        **c.model_dump(),
        paciente_nome=(pac.nome if pac else "—") if c.paciente_id else "",
        profissional_nome=prof.nome if prof else "—",
    )


# A raiz existe porque o dominio e publico: a demo linka /docs, mas quem digita
# o dominio nu caia num {"detail":"Not Found"} cru do FastAPI — pessimo cartao de
# visita para o "servico separado que voce pode inspecionar".
@app.get("/", include_in_schema=False)
def raiz() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/health", tags=["infra"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/profissionais", response_model=list[Profissional], tags=["records"])
def listar_profissionais() -> list[Profissional]:
    return PROFISSIONAIS


@app.get("/pacientes", response_model=list[Paciente], tags=["records"])
def listar_pacientes() -> list[Paciente]:
    return PACIENTES


@app.get("/consultas", response_model=list[ConsultaDetalhada], tags=["schedule"])
def listar_consultas(
    tenant: TenantDep,
    profissional_id: Annotated[str | None, Query()] = None,
    data: Annotated[
        datetime | None, Query(description="Filter by day (America/Bahia).")
    ] = None,
) -> list[ConsultaDetalhada]:
    return [_detalhar(c) for c in store.listar(tenant, profissional_id, data)]


@app.get(
    "/consultas/{consulta_id}",
    response_model=ConsultaDetalhada,
    tags=["schedule"],
    responses={404: {"description": "No such appointment in this session."}},
)
def obter_consulta(consulta_id: str, tenant: TenantDep) -> ConsultaDetalhada:
    c = tenant.consultas.get(consulta_id)
    if c is None:
        # Id de outro tenant cai aqui: 404, nunca o dado do vizinho.
        raise HTTPException(status_code=404, detail="Appointment not found.")
    return _detalhar(c)


@app.patch(
    "/consultas/{consulta_id}",
    response_model=ConsultaDetalhada,
    tags=["schedule"],
    responses={
        404: {"description": "No such appointment in this session."},
        409: {"description": "Slot already taken."},
    },
)
def remarcar_consulta(
    consulta_id: str,
    body: RemarcarRequest,
    tenant: TenantDep,
    _: None = Depends(_limitar),
) -> ConsultaDetalhada:
    c = tenant.consultas.get(consulta_id)
    if c is None or c.status is ConsultaStatus.CANCELADA:
        raise HTTPException(status_code=404, detail="Appointment not found.")

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
            raise HTTPException(
                status_code=409, detail="Slot already taken for this practitioner."
            )

    c.inicio = novo_inicio
    c.fim = novo_fim
    return _detalhar(c)


@app.post(
    "/consultas/{consulta_id}/cancelamento",
    response_model=ConsultaDetalhada,
    tags=["schedule"],
    responses={404: {"description": "No such appointment in this session."}},
)
def cancelar_consulta(
    consulta_id: str,
    body: CancelarRequest,
    tenant: TenantDep,
    _: None = Depends(_limitar),
) -> ConsultaDetalhada:
    c = tenant.consultas.get(consulta_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Appointment not found.")
    c.status = ConsultaStatus.CANCELADA
    c.motivo_cancelamento = body.motivo
    return _detalhar(c)


@app.post(
    "/consultas",
    response_model=ConsultaDetalhada,
    status_code=201,
    tags=["schedule"],
    responses={
        409: {"description": "Slot already taken."},
        422: {"description": "Invalid duration."},
    },
)
def abrir_vaga(
    body: AbrirVagaRequest, tenant: TenantDep, _: None = Depends(_limitar)
) -> ConsultaDetalhada:
    if len(tenant.consultas) >= MAX_CONSULTAS_POR_TENANT:
        raise HTTPException(
            status_code=429,
            detail="This demo schedule hit its appointment limit.",
        )
    if body.duracao_min not in DURACOES_VALIDAS:
        raise HTTPException(
            status_code=422, detail="Duration must be 30, 45 or 60 minutes."
        )
    if not any(p.id == body.profissional_id for p in PROFISSIONAIS):
        raise HTTPException(status_code=404, detail="Practitioner not found.")
    # Vazio é válido (vaga sem titular). Preenchido tem que existir: aceitar um
    # id qualquer criaria uma consulta apontando para um paciente inventado, que
    # é exatamente o tipo de escrita que esta demo existe para tornar impossível.
    if body.paciente_id and not any(p.id == body.paciente_id for p in PACIENTES):
        raise HTTPException(status_code=404, detail="Patient not found.")

    fim = body.inicio + timedelta(minutes=body.duracao_min)
    for outra in tenant.consultas.values():
        if (
            outra.status is ConsultaStatus.CANCELADA
            or outra.profissional_id != body.profissional_id
        ):
            continue
        if body.inicio < outra.fim and outra.inicio < fim:
            raise HTTPException(
                status_code=409, detail="Slot already taken for this practitioner."
            )

    cid = store.novo_id()
    consulta = Consulta(
        id=cid,
        paciente_id=body.paciente_id,
        profissional_id=body.profissional_id,
        inicio=body.inicio,
        fim=fim,
    )
    tenant.consultas[cid] = consulta
    return _detalhar(consulta)


# -- superfície da demo, não do ERP ------------------------------------------


@app.post(
    "/admin/chaos", tags=["demo"], summary="Turn chaos mode on or off for this session"
)
def chaos(
    ligado: bool,
    x_tenant_id: Annotated[str, Header()] = "demo",
) -> dict[str, bool]:
    """With chaos on, every schedule endpoint answers 503 for this session.

    It exists so the visitor can watch the agent fail honestly: it says it could
    not write and escalates, instead of returning a made-up confirmation.
    """
    t = store.tenant(x_tenant_id[:64], agora())
    t.down = ligado
    return {"down": t.down}


@app.post(
    "/admin/reset", tags=["demo"], summary="Rebuild this session's initial schedule"
)
def reset(x_tenant_id: Annotated[str, Header()] = "demo") -> Response:
    store.reset(x_tenant_id[:64], agora())
    return Response(status_code=204)


@app.get(
    "/admin/estado",
    tags=["demo"],
    summary="This session's full schedule, for the panel",
)
def estado(x_tenant_id: Annotated[str, Header()] = "demo") -> dict[str, object]:
    t = store.tenant(x_tenant_id[:64], agora())
    consultas = sorted(t.consultas.values(), key=lambda c: c.inicio)
    return {
        "down": t.down,
        "timezone": "America/Bahia",
        "agora": agora().astimezone(BAHIA).isoformat(),
        "consultas": [_detalhar(c).model_dump(mode="json") for c in consultas],
    }
