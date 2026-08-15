"""The dossier schema is the contract with T1/T2 and the Go CLI."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from schema import (
    Disambiguation,
    Identity,
    NameCollision,
    PreprocessRequest,
    ProductDossier,
    Provenance,
    Vocabulary,
    What,
)


def _dossier(**overrides) -> ProductDossier:
    base = dict(
        identity=Identity(canonical_name="Acme", slug="acme"),
        what=What(),
        vocabulary=Vocabulary(),
        disambiguation=Disambiguation(ambiguity_score=0.5),
        provenance=Provenance(generated_at=datetime.now(timezone.utc), runtime_ms=10),
    )
    return ProductDossier(**{**base, **overrides})


def test_minimal_dossier_validates() -> None:
    dossier = _dossier()

    assert dossier.identity.canonical_name == "Acme"
    assert dossier.vocabulary.feature_jargon == []


def test_roundtrips_through_json() -> None:
    dossier = _dossier()

    assert ProductDossier.model_validate_json(dossier.model_dump_json()) == dossier


@pytest.mark.parametrize("bad", [-0.1, 1.1])
def test_ambiguity_score_is_bounded(bad) -> None:
    with pytest.raises(ValidationError):
        Disambiguation(ambiguity_score=bad)


def test_collision_requires_evidence_url() -> None:
    with pytest.raises(ValidationError):
        NameCollision(name="Acme Bank", what_it_is="a bank")


def test_identity_requires_a_name() -> None:
    with pytest.raises(ValidationError):
        Identity(slug="acme")


def test_every_request_field_is_optional() -> None:
    req = PreprocessRequest(website="https://acme.dev")
    assert req.repo is None
    assert req.form is None
    assert req.name is None

    req = PreprocessRequest(repo="acme/acme")
    assert req.website is None
