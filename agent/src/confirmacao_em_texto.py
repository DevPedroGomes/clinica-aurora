"""A resposta do modelo pede, em texto, a confirmação de uma alteração?

O gate de confirmação (G-17) só conhece o que passou pela ferramenta: a escrita
é retida, vira proposta e a próxima fala da pessoa pode confirmá-la. Uma
pergunta em TEXTO ("Você quer remarcar essa para as 16h?") não registra nada.
Quando a pessoa respondia "sim", o modelo enfim chamava a ferramenta, o gate
retinha a chamada e pedia o "sim" de novo: confirmação dupla, vista na
verificação pós-deploy de 20/10 em português, com o código do gate correto.

O prompt já mandava chamar a ferramenta direto, e em inglês o modelo obedecia.
Em português, não. Este módulo é a parte do conserto que não depende do prompt:
ele reconhece a pergunta de confirmação para o loop do agente pedir ao modelo
a chamada da ferramenta, e aí a regra decide e o gate retém.

A régua é conservadora de propósito. Um falso negativo deixa tudo como era (a
pergunta vai para a tela). Um falso positivo faz o modelo chamar uma ferramenta
quando a pergunta era legítima, por exemplo pedir o motivo de um cancelamento.
Por isso só conta pergunta de sim ou não sobre uma alteração: pergunta aberta
("qual delas?", "qual o motivo?"), com alternativa ("16h ou 17h?") ou que pede
um dado à pessoa fica de fora.
"""

from __future__ import annotations

import re
import unicodedata

# Ponto de abreviação não termina frase: "o Dr. Bruno" partia a pergunta ao meio
# e o verbo ("remarcar") ficava na frase anterior.
_ABREVIACAO = re.compile(r"\b(dra|dr|sra|sr|srta|mrs|mr|ms|prof)\.", re.IGNORECASE)
_FIM_DE_FRASE = re.compile(r"(?<=[.!?])\s+|\n+")

# Pergunta aberta: a pessoa precisa dizer alguma coisa, não só "sim".
_PERGUNTA_ABERTA = re.compile(
    r"\b(qual|quais|quando|quanto|quantos|quantas|quem|onde|como|por que|porque|"
    r"que horas?|que horario|que dia|what|which|when|who|whom|where|how|why|"
    r"cual|cuales|cuando|quien|donde)\b"
)
# Alternativa: "16h ou 17h?" é escolha, não confirmação.
_ALTERNATIVA = re.compile(r"\b(ou|or)\b")
# Pedido de dado que só a pessoa tem: chamar a ferramenta seria chutar.
_PEDE_DADO = re.compile(r"\b(motivo|razao|reason|sobrenome|surname|apellido)\b")

_CONFIRMACAO = re.compile(
    r"\b(confirm\w*|posso seguir|posso prosseguir|shall i|go ahead|proceed)\b"
)
# Verbos das quatro escritas, nas formas que aparecem numa pergunta. Substantivo
# fica de fora: "pode me dizer o motivo do cancelamento?" pede um dado, "quer
# que eu olhe a agenda?" e "anything else on the schedule?" não alteram nada, e
# "março" sem acento não pode virar o verbo "marco".
_ESCRITA = re.compile(
    r"\b("
    r"(re|des)?marc(ar|a|as|amos|ada|ado)|(re|des)marco|(re|des)?marqu(e|em|emos)|"
    r"cancel(ar|o|a|as|e|em|amos|ada|ado)|agend(ar|o|e|em|ada|ado)|"
    r"mov(er|o|a)|pass(ar|o|e) (a consulta|para)|abr(ir|o|a) (a |uma )?vaga|"
    r"reschedul(e|ing)|book|move|cancel|open (a |the )?slot|"
    r"reprogram(ar|o|e)|anul(ar|o|e)"
    r")\b"
)


def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto)
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return _ABREVIACAO.sub(r"\1", sem_acento.lower()).replace("¿", " ")


def perguntas(texto: str) -> list[str]:
    """As frases do texto que terminam em interrogação, normalizadas."""
    frases = _FIM_DE_FRASE.split(_normalizar(texto))
    return [f.strip() for f in frases if f.strip().endswith("?")]


def pede_confirmacao(texto: str) -> bool:
    """Se alguma pergunta do texto é um "confirma?" de alteração na agenda."""
    for pergunta in perguntas(texto):
        if (
            _PERGUNTA_ABERTA.search(pergunta)
            or _ALTERNATIVA.search(pergunta)
            or _PEDE_DADO.search(pergunta)
        ):
            continue
        if _CONFIRMACAO.search(pergunta) or _ESCRITA.search(pergunta):
            return True
    return False
