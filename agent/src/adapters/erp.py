"""Adapter do clinica-aurora-erp.

A integração vem antes de qualquer prompt, porque é onde o projeto morre. Regras
aplicadas aqui:

  1. timeout curto e explícito (5s) — sistema de cliente cai
  2. retry só em LEITURA — repetir escrita gera consulta duplicada
  3. circuit breaker: 3 falhas seguidas abre por 60s e o agente escala
  4. erro cru nunca chega ao usuário final
  5. modo `fake` obrigatório, senão a suíte depende do sistema do cliente
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx

TIMEOUT_S = 5.0
RETRY_LEITURA = 2
BREAKER_FALHAS = 3
BREAKER_ABERTO_S = 60


class ErpIndisponivel(Exception):
    """O sistema do cliente não respondeu. Nunca vira texto para o usuário final."""


class ErpRecusou(Exception):
    """O sistema respondeu e disse não (409, 404, 422). Tem tratamento específico."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass
class ConsultaView:
    """O mínimo que a conversa precisa. Nunca o registro inteiro."""

    id: str
    paciente_nome: str
    profissional_id: str
    profissional_nome: str
    inicio: datetime
    fim: datetime
    status: str
    paciente_id: str = ""
    """Vazio numa vaga sem titular. A P-6 conta consultas por paciente com ele."""

    @classmethod
    def de_json(cls, raw: dict[str, Any]) -> "ConsultaView":
        return cls(
            id=raw["id"],
            # `or "—"` aqui apagava a distinção que o ERP acabou de fazer:
            # string vazia é vaga sem titular, e virava "—" de novo. Só o campo
            # AUSENTE merece o traço.
            paciente_nome=raw.get("paciente_nome", "—"),
            profissional_id=raw["profissional_id"],
            profissional_nome=raw.get("profissional_nome") or "—",
            inicio=datetime.fromisoformat(raw["inicio"]),
            fim=datetime.fromisoformat(raw["fim"]),
            status=raw["status"],
            paciente_id=raw.get("paciente_id", ""),
        )


@dataclass
class PacienteView:
    """Id e nome, e nada mais: o telefone não serve à conversa."""

    id: str
    nome: str


class ErpPort(Protocol):
    async def listar(
        self, profissional_id: str | None, data: datetime | None
    ) -> list[ConsultaView]: ...
    async def obter(self, consulta_id: str) -> ConsultaView | None: ...
    async def remarcar(
        self, consulta_id: str, novo_inicio: datetime
    ) -> ConsultaView: ...
    async def cancelar(self, consulta_id: str, motivo: str) -> ConsultaView: ...
    async def abrir_vaga(
        self,
        profissional_id: str,
        inicio: datetime,
        duracao_min: int,
        paciente_id: str = "",
    ) -> ConsultaView: ...


class CircuitBreaker:
    def __init__(self, agora: datetime | None = None) -> None:
        self.falhas = 0
        self.aberto_ate: datetime | None = None

    def aberto(self, at: datetime) -> bool:
        return self.aberto_ate is not None and at < self.aberto_ate

    def registrar_falha(self, at: datetime) -> None:
        self.falhas += 1
        if self.falhas >= BREAKER_FALHAS:
            self.aberto_ate = at + timedelta(seconds=BREAKER_ABERTO_S)

    def registrar_sucesso(self) -> None:
        self.falhas = 0
        self.aberto_ate = None


class ErpAdapter:
    """Cliente HTTP do ERP, com o tenant da sessão em todo request."""

    def __init__(
        self, base_url: str, tenant_id: str, client: httpx.AsyncClient | None = None
    ) -> None:
        self._base = base_url.rstrip("/")
        self._tenant = tenant_id
        self._client = client
        self._breaker = CircuitBreaker()

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Tenant-Id": self._tenant}

    async def _request(
        self,
        metodo: str,
        caminho: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        at: datetime,
    ) -> Any:
        if self._breaker.aberto(at):
            raise ErpIndisponivel("circuito aberto")

        leitura = metodo == "GET"
        tentativas = RETRY_LEITURA if leitura else 1
        ultimo_erro: Exception | None = None

        for tentativa in range(tentativas):
            try:
                client = self._client or httpx.AsyncClient(timeout=TIMEOUT_S)
                fechar = self._client is None
                try:
                    resp = await client.request(
                        metodo,
                        f"{self._base}{caminho}",
                        json=json,
                        params=params,
                        headers=self._headers,
                    )
                finally:
                    if fechar:
                        await client.aclose()

                if resp.status_code >= 500:
                    # 5xx é indisponibilidade: conta para o breaker e, em leitura,
                    # ainda vale uma segunda tentativa.
                    ultimo_erro = ErpIndisponivel(f"HTTP {resp.status_code}")
                    if leitura and tentativa + 1 < tentativas:
                        await asyncio.sleep(0.2 * (tentativa + 1))
                        continue
                    self._breaker.registrar_falha(at)
                    raise ultimo_erro

                if resp.status_code >= 400:
                    # O sistema respondeu e disse não. Isso não é falha de
                    # infraestrutura: não abre o circuito.
                    self._breaker.registrar_sucesso()
                    detalhe = ""
                    try:
                        detalhe = resp.json().get("detail", "")
                    except Exception:
                        detalhe = resp.text[:200]
                    raise ErpRecusou(resp.status_code, detalhe)

                self._breaker.registrar_sucesso()
                return resp.json() if resp.content else None

            except (httpx.TimeoutException, httpx.TransportError) as exc:
                ultimo_erro = ErpIndisponivel(str(exc))
                if leitura and tentativa + 1 < tentativas:
                    await asyncio.sleep(0.2 * (tentativa + 1))
                    continue
                self._breaker.registrar_falha(at)
                raise ultimo_erro from exc

        raise ultimo_erro or ErpIndisponivel("falha desconhecida")

    async def listar(
        self,
        profissional_id: str | None = None,
        data: datetime | None = None,
        at: datetime | None = None,
    ) -> list[ConsultaView]:
        params: dict[str, str] = {}
        if profissional_id:
            params["profissional_id"] = profissional_id
        if data:
            params["data"] = data.isoformat()
        raw = await self._request(
            "GET",
            "/consultas",
            params=params,
            at=at or datetime.now(data.tzinfo if data else None),
        )
        return [ConsultaView.de_json(item) for item in raw]

    async def listar_pacientes(self, at: datetime) -> list[PacienteView]:
        """O cadastro de pacientes da sessão (`GET /pacientes`, exige o tenant)."""
        raw = await self._request("GET", "/pacientes", at=at)
        return [PacienteView(id=p["id"], nome=p["nome"]) for p in raw]

    async def obter(self, consulta_id: str, at: datetime) -> ConsultaView | None:
        try:
            raw = await self._request("GET", f"/consultas/{consulta_id}", at=at)
        except ErpRecusou as exc:
            if exc.status == 404:
                return None
            raise
        return ConsultaView.de_json(raw)

    async def remarcar(
        self, consulta_id: str, novo_inicio: datetime, at: datetime
    ) -> ConsultaView:
        raw = await self._request(
            "PATCH",
            f"/consultas/{consulta_id}",
            json={"novo_inicio": novo_inicio.isoformat()},
            at=at,
        )
        return ConsultaView.de_json(raw)

    async def cancelar(
        self, consulta_id: str, motivo: str, at: datetime
    ) -> ConsultaView:
        raw = await self._request(
            "POST",
            f"/consultas/{consulta_id}/cancelamento",
            json={"motivo": motivo},
            at=at,
        )
        return ConsultaView.de_json(raw)

    async def abrir_vaga(
        self,
        profissional_id: str,
        inicio: datetime,
        duracao_min: int,
        at: datetime,
        paciente_id: str = "",
    ) -> ConsultaView:
        raw = await self._request(
            "POST",
            "/consultas",
            json={
                "profissional_id": profissional_id,
                "inicio": inicio.isoformat(),
                "duracao_min": duracao_min,
                "paciente_id": paciente_id,
            },
            at=at,
        )
        return ConsultaView.de_json(raw)
