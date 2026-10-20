"""As cinco regras que o modelo não decide.

Funções puras: sem rede, sem relógio interno, sem banco. Todo dado necessário
chega por parâmetro — inclusive `at`, que é o que torna cada regra testável em
qualquer horário sem congelar o relógio do processo.

P-1 cancelamento em cima da hora exige motivo    CANCEL_WINDOW_REQUIRES_REASON
P-2 não remarca para o passado                   SLOT_IN_THE_PAST
P-3 não remarca para horário ocupado             SLOT_CONFLICT
P-4 não agenda ninguém fora do expediente        OUTSIDE_BUSINESS_HOURS
P-5 teto de escritas por conversa                WRITE_QUOTA_EXCEEDED
P-6 uma consulta por paciente por dia            PATIENT_DAILY_LIMIT

P-4 vale para as DUAS escritas que põem alguém na agenda — abrir vaga e
remarcar. Só ligá-la ao abrir_vaga foi um buraco real: a demo aceitava mover
uma consulta para domingo às 3h da manhã e o log de decisão dizia "permitido",
que é exatamente o oposto do que esta demo existe para provar.

P-3 vale também ao marcar e ao abrir vaga. Antes o horário ocupado só aparecia
como 409 do ERP, contado como falha do ERP: na segunda vez a conversa encerrava
dizendo "o ERP falhou duas vezes", quando o que houve foi uma regra.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .decision import ALLOWED, Decision

RULES_PATH = Path(__file__).with_name("rules.yaml")


@dataclass(frozen=True)
class Janela:
    """Intervalo ocupado na agenda de um profissional."""

    consulta_id: str
    profissional_id: str
    inicio: datetime
    fim: datetime
    paciente_id: str = ""
    """Vazio numa vaga sem titular, que por isso não conta para a P-6."""


class PolicyEngine:
    def __init__(self, rules: dict[str, Any] | None = None) -> None:
        self._rules = (
            rules
            if rules is not None
            else yaml.safe_load(RULES_PATH.read_text("utf-8"))
        )
        self.version: str = self._rules["version"]
        self._tz = ZoneInfo(self._rules["expediente"]["timezone"])

    # -- P-4, compartilhada por toda escrita que ocupa a agenda ------------------

    def _fora_do_expediente(self, inicio: datetime, fim: datetime) -> str | None:
        """Chave da dica de recusa se o atendimento não cabe no expediente; None se cabe."""
        local = inicio.astimezone(self._tz)
        fim_local = fim.astimezone(self._tz)
        dia = self._rules["expediente"]["dias"].get(str(local.weekday()))

        if dia is None:
            return "dica.dia_fechado"

        abre = time.fromisoformat(dia["abre"])
        fecha = time.fromisoformat(dia["fecha"])
        # O atendimento inteiro precisa caber no expediente, não só o começo:
        # abrir 17h50 de 60 minutos deixaria a clínica aberta até 18h50.
        cabe = (
            abre <= local.time()
            and fim_local.time() <= fecha
            and fim_local.date() == local.date()
        )
        if cabe:
            return None
        return "dica.fora_do_expediente"

    # -- P-1 ---------------------------------------------------------------------

    def pode_cancelar(self, *, inicio: datetime, motivo: str, at: datetime) -> Decision:
        cfg = self._rules["cancelamento"]
        antecedencia = inicio - at
        dentro_da_janela = antecedencia < timedelta(hours=cfg["janela_horas"])
        motivo_ok = len(motivo.strip()) >= cfg["motivo_min_chars"]

        payload = {
            "inicio": inicio.isoformat(),
            "at": at.isoformat(),
            "horas_de_antecedencia": round(antecedencia.total_seconds() / 3600, 2),
            "motivo_len": len(motivo.strip()),
        }

        if dentro_da_janela and not motivo_ok:
            return Decision(
                allowed=False,
                reason_code="cancel_window_requires_reason",
                explanation_hint="dica.pedir_motivo",
                audit_payload=payload,
            )
        return Decision(
            allowed=True,
            reason_code=ALLOWED,
            audit_payload=payload,
            explanation_hint="dica.confirmar_cancelamento",
        )

    # -- P-2 e P-3 ---------------------------------------------------------------

    def pode_remarcar(
        self,
        *,
        consulta_id: str,
        profissional_id: str,
        novo_inicio: datetime,
        duracao: timedelta,
        agenda: list[Janela],
        at: datetime,
    ) -> Decision:
        novo_fim = novo_inicio + duracao
        payload = {
            "consulta_id": consulta_id,
            "novo_inicio": novo_inicio.isoformat(),
            "at": at.isoformat(),
        }

        if novo_inicio <= at:
            return Decision(
                allowed=False,
                reason_code="slot_in_the_past",
                explanation_hint="dica.horario_passou",
                audit_payload=payload,
            )

        hint = self._fora_do_expediente(novo_inicio, novo_fim)
        if hint:
            return Decision(
                allowed=False,
                reason_code="outside_business_hours",
                explanation_hint=hint,
                audit_payload=payload,
            )

        for janela in agenda:
            if (
                janela.consulta_id == consulta_id
                or janela.profissional_id != profissional_id
            ):
                continue
            if novo_inicio < janela.fim and janela.inicio < novo_fim:
                return Decision(
                    allowed=False,
                    reason_code="slot_conflict",
                    explanation_hint="dica.horario_ocupado",
                    audit_payload={**payload, "conflita_com": janela.consulta_id},
                )

        return Decision(
            allowed=True,
            reason_code=ALLOWED,
            value=novo_inicio,
            explanation_hint="dica.confirmar_remarcacao",
            audit_payload=payload,
        )

    # -- P-2, P-4, P-3 e P-6, para quem ocupa a agenda -----------------------------

    def pode_marcar(
        self,
        *,
        paciente_id: str | None,
        profissional_id: str,
        inicio: datetime,
        duracao_min: int,
        agenda: list[Janela],
        at: datetime,
    ) -> Decision:
        """Marcar consulta (com `paciente_id`) ou abrir vaga (com None).

        Ordem: passado, expediente, horário ocupado, limite do paciente no dia.
        A mais útil para a pessoa vem primeiro: "já passou" antes de "fora do
        horário", e "ocupado" antes de "ela já tem consulta nesse dia".
        """
        fim = inicio + timedelta(minutes=duracao_min)
        payload: dict[str, Any] = {
            "paciente_id": paciente_id,
            "profissional_id": profissional_id,
            "inicio": inicio.isoformat(),
            "inicio_local": inicio.astimezone(self._tz).isoformat(),
            "duracao_min": duracao_min,
            "at": at.isoformat(),
        }

        if inicio <= at:
            return Decision(
                allowed=False,
                reason_code="slot_in_the_past",
                explanation_hint="dica.horario_passou",
                audit_payload=payload,
            )

        hint = self._fora_do_expediente(inicio, fim)
        if hint:
            return Decision(
                allowed=False,
                reason_code="outside_business_hours",
                explanation_hint=hint,
                audit_payload=payload,
            )

        for janela in agenda:
            if janela.profissional_id != profissional_id:
                continue
            if inicio < janela.fim and janela.inicio < fim:
                return Decision(
                    allowed=False,
                    reason_code="slot_conflict",
                    explanation_hint="dica.horario_ocupado",
                    audit_payload={**payload, "conflita_com": janela.consulta_id},
                )

        if paciente_id:
            dia = inicio.astimezone(self._tz).date()
            no_dia = [
                j.consulta_id
                for j in agenda
                if j.paciente_id == paciente_id
                and j.inicio.astimezone(self._tz).date() == dia
            ]
            if len(no_dia) >= self._rules["consultas"]["max_por_paciente_dia"]:
                return Decision(
                    allowed=False,
                    reason_code="patient_daily_limit",
                    explanation_hint="dica.paciente_ja_tem_consulta",
                    audit_payload={**payload, "consultas_no_dia": len(no_dia)},
                )

        return Decision(
            allowed=True,
            reason_code=ALLOWED,
            value=inicio,
            explanation_hint="dica.confirmar_abertura",
            audit_payload=payload,
        )

    # -- P-5 ---------------------------------------------------------------------

    def pode_escrever(self, *, escritas_na_conversa: int) -> Decision:
        teto = self._rules["conversa"]["max_escritas"]
        payload = {"escritas_na_conversa": escritas_na_conversa, "teto": teto}
        if escritas_na_conversa >= teto:
            return Decision(
                allowed=False,
                reason_code="write_quota_exceeded",
                explanation_hint="dica.passar_para_recepcao",
                audit_payload=payload,
            )
        return Decision(
            allowed=True,
            reason_code=ALLOWED,
            explanation_hint="dica.seguir",
            audit_payload=payload,
        )
