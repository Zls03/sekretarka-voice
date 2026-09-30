"""Wspólne zwroty asystenta w rozmowie o rezerwacji."""

import random

_CLOSING_QUESTIONS = ["W czymś jeszcze mogę pomóc?", "Czy mogę jeszcze w czymś pomóc?", "Czy jest coś jeszcze?"]


def _closing_question() -> str:
    """Losowe pytanie zamykające do doklejenia w say_exactly PO udanej akcji (odwołanie/
    zapisanie/przełożenie). Musi być wpisane na sztywno w tekst, bo say_exactly wprost
    ZAKAZUJE modelowi dodawania czegokolwiek przed/po (żeby nie psuł gramatyki sklejając
    fragmenty — patrz "Coś jeszcze mogę pomóc?" złapane wcześniej na żywym telefonie) —
    bez tego rozmowa po udanej rezerwacji urywała się bez zaproszenia do dalszych pytań."""
    return random.choice(_CLOSING_QUESTIONS)
