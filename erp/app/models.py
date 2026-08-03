"""Modelos do clinica-aurora-erp.

Este serviço representa o SISTEMA DO CLIENTE, não o agente. Ele existe para que
a demo prove uma escrita real sobre HTTP contra um serviço separado, com estado
próprio e documentação pública — em vez de simular a integração dentro do agente.

Por isso ele não conhece nada de LLM, política ou conversa.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ConsultaStatus(str, Enum):
    AGENDADA = "agendada"
    CANCELADA = "cancelada"


class Profissional(BaseModel):
    id: str = Field(examples=["prof_bruno"])
    nome: str = Field(examples=["Dr. Bruno Salles"])
    especialidade: str = Field(examples=["Clínico geral"])


class Paciente(BaseModel):
    id: str = Field(examples=["pac_ana"])
    nome: str = Field(examples=["Ana Ribeiro"])
    telefone: str = Field(
        examples=["(71) 9****-**32"],
        description="Mascarado de propósito: a demo é pública e o agente nunca precisa do número inteiro.",
    )


class Consulta(BaseModel):
    id: str = Field(examples=["cons_8812"])
    paciente_id: str
    profissional_id: str
    inicio: datetime
    fim: datetime
    status: ConsultaStatus = ConsultaStatus.AGENDADA
    motivo_cancelamento: str | None = None


class ConsultaDetalhada(Consulta):
    """O que a listagem devolve: junta o nome para a tela, sem expor o registro inteiro."""

    paciente_nome: str
    profissional_nome: str


class RemarcarRequest(BaseModel):
    novo_inicio: datetime = Field(description="Início novo, ISO 8601 com timezone.")


class CancelarRequest(BaseModel):
    motivo: str = Field(min_length=1, max_length=200)


class AbrirVagaRequest(BaseModel):
    profissional_id: str
    inicio: datetime
    duracao_min: int = Field(description="30, 45 ou 60.")


class ErroResposta(BaseModel):
    detail: str
