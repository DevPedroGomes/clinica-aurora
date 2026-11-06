"""ERP falso, em memória: o modo `fake` obrigatório do adapter.

Sem isto a suíte depende do sistema do cliente estar de pé, e uma suíte que
depende de rede não roda no CI nem no laptop de ninguém.

Espelha o contrato do ErpAdapter, incluindo as duas formas de dizer não:
`ErpRecusou` (o sistema respondeu e negou) e `ErpIndisponivel` (não respondeu).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .erp import ConsultaView, ErpIndisponivel, ErpRecusou, PacienteView

CADASTRO = [
    PacienteView(id="pac_ana", nome="Ana Ribeiro"),
    PacienteView(id="pac_joao", nome="João Meireles"),
    PacienteView(id="pac_marta", nome="Marta Lins"),
]


class ErpFake:
    def __init__(
        self,
        consultas: list[ConsultaView] | None = None,
        pacientes: list[PacienteView] | None = None,
    ) -> None:
        self.consultas: dict[str, ConsultaView] = {c.id: c for c in (consultas or [])}
        self.pacientes: list[PacienteView] = list(
            CADASTRO if pacientes is None else pacientes
        )
        self.down = False
        self.chamadas: list[tuple[str, str]] = []
        self._proximo_id = 9000

    def _guarda(self, op: str, alvo: str = "") -> None:
        self.chamadas.append((op, alvo))
        if self.down:
            raise ErpIndisponivel("fake down")

    async def listar(
        self,
        profissional_id: str | None = None,
        data: datetime | None = None,
        at: datetime | None = None,
    ) -> list[ConsultaView]:
        self._guarda("listar", profissional_id or "")
        itens = [c for c in self.consultas.values() if c.status == "agendada"]
        if profissional_id:
            itens = [c for c in itens if c.profissional_id == profissional_id]
        if data:
            itens = [c for c in itens if c.inicio.date() == data.date()]
        return sorted(itens, key=lambda c: c.inicio)

    async def listar_pacientes(self, at: datetime | None = None) -> list[PacienteView]:
        self._guarda("listar_pacientes")
        return list(self.pacientes)

    async def obter(self, consulta_id: str, at: datetime) -> ConsultaView | None:
        self._guarda("obter", consulta_id)
        return self.consultas.get(consulta_id)

    async def remarcar(
        self, consulta_id: str, novo_inicio: datetime, at: datetime
    ) -> ConsultaView:
        self._guarda("remarcar", consulta_id)
        c = self.consultas.get(consulta_id)
        if c is None:
            raise ErpRecusou(404, "não encontrada")
        duracao = c.fim - c.inicio
        c.inicio = novo_inicio
        c.fim = novo_inicio + duracao
        return c

    async def cancelar(
        self, consulta_id: str, motivo: str, at: datetime
    ) -> ConsultaView:
        self._guarda("cancelar", consulta_id)
        c = self.consultas.get(consulta_id)
        if c is None:
            raise ErpRecusou(404, "não encontrada")
        c.status = "cancelada"
        return c

    async def abrir_vaga(
        self,
        profissional_id: str,
        inicio: datetime,
        duracao_min: int,
        at: datetime,
        paciente_id: str = "",
    ) -> ConsultaView:
        self._guarda("abrir_vaga", profissional_id)
        self._proximo_id += 1
        cid = f"cons_{self._proximo_id}"
        c = ConsultaView(
            id=cid,
            paciente_nome=next(
                (p.nome for p in self.pacientes if p.id == paciente_id), ""
            ),
            profissional_id=profissional_id,
            profissional_nome="Dr. Bruno Salles"
            if profissional_id == "prof_bruno"
            else "Dra. Helena Costa",
            inicio=inicio,
            fim=inicio + timedelta(minutes=duracao_min),
            status="agendada",
            paciente_id=paciente_id,
        )
        self.consultas[cid] = c
        return c
