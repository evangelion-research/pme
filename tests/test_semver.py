import pytest

from pme.semver import Constraint, Version


def test_semver_precedence():
    values = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-beta", "1.0.0"]
    assert sorted(map(Version.parse, reversed(values))) == list(map(Version.parse, values))


@pytest.mark.parametrize("bad", ["v1.2.3", "1.2", "01.2.3", "1.2.3+meta", "1.0.0-01"])
def test_strict_semver(bad):
    with pytest.raises(ValueError):
        Version.parse(bad)


def test_constraints():
    assert Constraint.parse("^1.2.3").allows("1.9.0")
    assert not Constraint.parse("^1.2.3").allows("2.0.0")
    assert not Constraint.parse("~1.2.3").allows("1.3.0")
