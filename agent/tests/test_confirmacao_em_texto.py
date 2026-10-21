"""A régua que reconhece "confirma?" em texto (`src/confirmacao_em_texto.py`).

As frases de cima são as que o modelo escreveu na verificação pós-deploy de
20/10 (linhas a, c, d e e1 em português) e os equivalentes em inglês. As de
baixo são perguntas legítimas: um falso positivo faria o modelo chamar uma
ferramenta quando devia esperar um dado da pessoa.
"""

from __future__ import annotations

import pytest

from src.confirmacao_em_texto import pede_confirmacao, perguntas

PEDEM_CONFIRMACAO = [
    "Ana tem uma consulta amanhã (21/10) às 9h com o Dr. Bruno Salles. Você quer "
    "remarcar essa para as 16h do mesmo dia?",
    "João Meireles tem uma consulta hoje às 20h com a Dra. Helena Costa. É essa "
    "que você quer cancelar?",
    'Não encontrei "Maria Teste" no cadastro.\n\nSobre o cancelamento do João: '
    "confirma que é a consulta de hoje às 20h com a Dra. Helena?",
    "Ainda preciso da sua confirmação sobre o cancelamento do João: a consulta de "
    "hoje às 20h com a Dra. Helena, confirma?",
    "Quer que eu marque a Marta amanhã às 10h com a Dra. Helena?",
    "Do you want me to move it to 4pm?",
    "Is that the one you want to cancel?",
    "Shall I book Marta with Dr. Helena tomorrow at 10am?",
    "¿Quieres que cancele la cita de João?",
]

NAO_PEDEM = [
    # pedem um dado ou uma escolha
    "Para cancelar em cima da hora preciso de um motivo. Qual é?",
    "Pode me dizer o motivo do cancelamento?",
    "Could you tell me the reason for cancelling?",
    "Since this is a short-notice cancellation, the rule requires a reason. "
    "What should I put down?",
    "A Ana tem duas consultas. Você quer remarcar a de amanhã ou a de sexta?",
    "Want me to pick another day or time instead?",
    # oferecem algo que não é alteração
    "A clínica não atende aos domingos, então não dá pra marcar nesse dia. Quer "
    "escolher outro horário, de segunda a sábado?",
    "Quer que eu olhe a agenda do Dr. Bruno?",
    "Anything else on the schedule?",
    "Pronto, remarquei para as 16h. Posso ajudar com mais alguma coisa?",
    "Pode ser em março?",
    # não perguntam nada
    "Done, Ana is now at 4pm.",
    "Só consigo ajudar com a agenda da clínica.",
    "",
]


@pytest.mark.parametrize("texto", PEDEM_CONFIRMACAO)
def test_reconhece_confirmacao_de_alteracao(texto: str):
    assert pede_confirmacao(texto)


@pytest.mark.parametrize("texto", NAO_PEDEM)
def test_nao_confunde_pergunta_legitima_com_confirmacao(texto: str):
    assert not pede_confirmacao(texto)


def test_ponto_de_abreviacao_nao_parte_a_pergunta():
    """Partida em "Dr.", a pergunta perdia o verbo e passava despercebida."""
    assert perguntas("Você quer remarcar com o Dr. Bruno para as 16h?") == [
        "voce quer remarcar com o dr bruno para as 16h?"
    ]
