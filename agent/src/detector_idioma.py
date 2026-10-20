"""Em que idioma um texto parece estar, e em qual o agente deve responder.

Detecção barata de propósito: conta palavras funcionais que só existem numa das
três línguas que a demo vê (português, inglês e espanhol). Não é classificador
de uso geral, e não precisa ser: ele só tem de dizer, sem chamada paga, se uma
resposta saiu em português numa sessão em inglês, e se uma mensagem curta da
pessoa ("yes", "sim", "sí") diz alguma coisa sobre o idioma dela.

Palavra que existe em mais de uma das três (para, que, no, do, a, de, agenda,
consulta, cancelar, paciente) fica de fora: ela não separa nada e só criaria
empate. Nomes do cadastro (João, Dra. Helena) também não entram, e por isso não
se usa acento como sinal: "João" aparece numa resposta em inglês.
"""

from __future__ import annotations

import re

PALAVRAS: dict[str, frozenset[str]] = {
    "pt": frozenset(
        """
        não nao você voce vocês estão estou são é foi foram
        um uma uns umas com da dos das na nas ao aos à às pelo pela pelos pelas
        isso isto essa esse nessa nesse neste nesta dessa desse deste
        ela ele eles elas eu meu minha seu sua seus suas dele dela
        também já ainda então mas ou mais muito pra pro
        tem têm há posso pode podem vou vai quer quero
        hoje amanhã agora depois aqui ali quando qual quais nenhuma nenhum
        sim obrigado obrigada certo beleza
        horário horários marquei remarquei cancelei abri ficou fica ficaria
        """.split()
    ),
    "en": frozenset(
        """
        the an and of to is are was were be been being it its it's this that
        these those with for from on at by i i'm i'll i've you you're your
        we our they them there here what which who when where how why
        can could would should will shall does did don't can't won't not
        yes yeah sure have has had just now today tomorrow
        appointment appointments schedule scheduled slot slots booked book
        moved move rescheduled reschedule cancelled canceled cancel
        ahead go please done want need only all any some after before then
        than about into out up but or if so let confirm confirmed
        patient patients opening hours closed sorry
        """.split()
    ),
    "es": frozenset(
        """
        el los las la es y del con una un unos unas al lo le les
        qué cuál cuáles cómo dónde cuándo hay están estoy son fue
        sí gracias pero muy también ahora hoy mañana aquí
        usted ustedes su sus tiene tienes tengo quiere quieres puedo puede
        cita citas ningún ninguna eso ese esos estos hasta entonces doctora
        """.split()
    ),
}

# Pares que só existem numa das três. "para as 16h" é português ("para las" em
# espanhol), e é exatamente o fim de uma confirmação curta ("Pronto, remarquei
# para as 16h."), que sem eles passava com uma palavra só e escapava da checagem.
BIGRAMAS: dict[str, frozenset[tuple[str, str]]] = {
    "pt": frozenset(
        {
            ("para", "as"),
            ("para", "o"),
            ("para", "os"),
            ("para", "a"),
            ("a", "consulta"),
            ("o", "horário"),
        }
    ),
    "en": frozenset(),
    "es": frozenset({("para", "el"), ("para", "las"), ("a", "las")}),
}

# O hífen e o apóstrofo ficam DENTRO da palavra: "remarcá-la" não pode virar
# "remarcá" + "la" (que contaria como espanhol), nem "I'll" virar "i" + "ll".
_TOKEN = re.compile(r"[a-záàâãéêíóôõúüçñ]+(?:[-'][a-záàâãéêíóôõúüçñ]+)*")


def pontuar(texto: str) -> dict[str, int]:
    """Quantas palavras (e pares de palavras) exclusivas de cada idioma o texto tem."""
    tokens = _TOKEN.findall(texto.lower().replace("’", "'"))
    pares = list(zip(tokens, tokens[1:]))
    return {
        idioma: sum(t in lista for t in tokens)
        + sum(par in BIGRAMAS[idioma] for par in pares)
        for idioma, lista in PALAVRAS.items()
    }


def detectar(texto: str, minimo: int = 2) -> str | None:
    """O idioma do texto, ou None quando não dá para dizer.

    Exige `minimo` palavras do vencedor e pelo menos o dobro das do segundo
    colocado. Resposta do modelo usa o padrão (2): ela tem frases inteiras, e um
    "ok" solto não pode disparar reescrita. Mensagem da pessoa usa 1, porque a
    confirmação típica é uma palavra só ("yes", "sim", "sí").
    """
    pontos = sorted(pontuar(texto).items(), key=lambda kv: kv[1], reverse=True)
    (melhor, primeiro), (_, segundo) = pontos[0], pontos[1]
    if primeiro < minimo or primeiro < 2 * segundo:
        return None
    return melhor


def idioma_da_resposta(
    mensagem: str, idioma_da_sessao: str, idioma_da_conversa: str | None
) -> str:
    """Em que idioma o agente responde a esta mensagem.

    A mensagem decide quando ela é clara: quem escreve em espanhol recebe
    espanhol, é o que a tela promete. Quando ela não diz nada ("ok", "16h",
    "Ana"), vale o último idioma que a pessoa usou nesta conversa e, antes de
    ela usar algum, o da sessão. Nunca o idioma do prompt, das ferramentas ou
    dos dados do ERP, que era o que vencia antes.
    """
    return detectar(mensagem, minimo=1) or idioma_da_conversa or idioma_da_sessao
