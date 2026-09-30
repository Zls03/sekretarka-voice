"""Odmiana imion i nazwisk (dopełniacz) oraz rozpoznawanie rodzaju."""

import pytest

from app.polish.grammar import detect_gender, odmien_imie


@pytest.mark.parametrize(
    ("name", "genitive"),
    [
        ("Ania", "Ani"),
        ("Kasia", "Kasi"),
        ("Basia", "Basi"),
        ("Daria", "Darii"),
        ("Maria", "Marii"),
        ("Julia", "Julii"),
        ("Klaudia", "Klaudii"),
        ("Tomek", "Tomka"),
        ("Paweł", "Pawła"),
        ("Kasia Nowak", "Kasi Nowak"),
        ("Anna Kowalska", "Anny Kowalskiej"),
        ("Jan Kowalski", "Jana Kowalskiego"),
        ("Tomek Nowak", "Tomka Nowaka"),
        ("Piotr Mazur", "Piotra Mazura"),
    ],
)
def test_genitive(name, genitive):
    assert odmien_imie(name) == genitive


@pytest.mark.parametrize(
    ("name", "form"), [("Kasia Nowak", "Pani"), ("Jan Kowalski", "Pana"), ("Kuba", "Pana"), ("Ewa", "Pani")]
)
def test_detect_gender_uses_first_name(name, form):
    assert detect_gender(name) == form
