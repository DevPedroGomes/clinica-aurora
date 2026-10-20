"""O detector barato que decide o idioma da resposta e confere o que o modelo escreveu."""

from __future__ import annotations

import pytest

from src.detector_idioma import detectar, idioma_da_resposta, pontuar


@pytest.mark.parametrize(
    "texto",
    [
        "I'll move Ana's appointment from 9:00 to 4:00 PM tomorrow with Dr. Bruno "
        "Salles. Shall I go ahead?",
        "Done, the appointment is now at 4 PM.",
        "That time is outside opening hours. Would Monday at 10 work?",
        "I can only help with the clinic's schedule.",
        "João Meireles's appointment with Dra. Helena Costa was cancelled.",
    ],
)
def test_ingles(texto: str):
    assert detectar(texto) == "en"


@pytest.mark.parametrize(
    "texto",
    [
        "Vou remarcar a consulta da Ana das 9h para as 16h de amanhã com o Dr. "
        "Bruno. Posso seguir?",
        "Pronto, remarquei para as 16h.",
        "Esse horário está fora do expediente. Pode ser na segunda às 10h?",
        "Posso remarcá-la para as 16h? Ela fica com a Dra. Helena.",
        "Não encontrei esse nome no cadastro.",
    ],
)
def test_portugues(texto: str):
    assert detectar(texto) == "pt"


@pytest.mark.parametrize(
    "texto",
    [
        "En la agenda hay 3 citas para mañana.",
        "¿Qué hay en la agenda del Dr. Bruno?",
        "La cita de Ana es el martes a las 9.",
    ],
)
def test_espanhol(texto: str):
    assert detectar(texto) == "es"


@pytest.mark.parametrize(
    "texto", ["ok", "16h", "Ana", "👍", "", "cons_8812", "Dr. Bruno"]
)
def test_sem_sinal_nao_decide(texto: str):
    assert detectar(texto) is None
    assert detectar(texto, minimo=1) is None


@pytest.mark.parametrize(
    ("texto", "idioma"),
    [("yes", "en"), ("sim", "pt"), ("sí", "es"), ("go ahead", "en")],
)
def test_confirmacao_de_uma_palavra_basta_para_a_mensagem(texto: str, idioma: str):
    assert detectar(texto, minimo=1) == idioma
    # Para a resposta do modelo, uma palavra só não basta.
    assert detectar(texto) in (None, idioma)


def test_nomes_do_cadastro_nao_contam():
    assert pontuar("João Meireles · Dra. Helena Costa · Dr. Bruno Salles") == {
        "pt": 0,
        "en": 0,
        "es": 0,
    }


def test_clitico_com_hifen_nao_vira_espanhol():
    assert pontuar("remarcá-la")["es"] == 0


def test_empate_nao_decide():
    # "the" (en) e "com" (pt) com o mesmo peso: nenhum vence com folga.
    assert detectar("the com", minimo=1) is None


def test_idioma_da_resposta_segue_a_mensagem_clara():
    assert idioma_da_resposta("¿Qué hay en la agenda?", "en", None) == "es"
    assert idioma_da_resposta("remarque a da Ana", "en", None) == "pt"


def test_mensagem_sem_sinal_segue_a_conversa_e_depois_a_sessao():
    assert idioma_da_resposta("ok", "en", "es") == "es"
    assert idioma_da_resposta("ok", "en", None) == "en"
    assert idioma_da_resposta("16h", "pt", None) == "pt"
