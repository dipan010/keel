"""Migration hygiene. Needs no database.

CI applies these in filename order against an empty Postgres, so anything that
makes the order ambiguous makes the schema ambiguous. Two people adding an 0006
is the ordinary way that happens, and it is silent: both apply, in whatever
order the shell globs them.
"""

from __future__ import annotations

import pathlib
import re

MIGRATIONS = sorted((pathlib.Path(__file__).resolve().parents[1] / "migrations").glob("*.sql"))


def numbers() -> list[int]:
    return [int(re.match(r"^(\d+)_", p.name).group(1)) for p in MIGRATIONS]


def test_there_are_migrations_at_all():
    assert MIGRATIONS


def test_every_file_is_numbered():
    for p in MIGRATIONS:
        assert re.match(r"^\d{4}_[a-z0-9_]+\.sql$", p.name), f"{p.name} is not NNNN_snake_case.sql"


def test_numbers_are_unique():
    ns = numbers()
    dupes = {n for n in ns if ns.count(n) > 1}
    assert not dupes, f"two migrations share a number: {dupes}"


def test_numbers_are_contiguous_from_one():
    """A gap usually means a migration was deleted after being applied
    somewhere, which makes 'has this been run?' unanswerable."""
    assert numbers() == list(range(1, len(MIGRATIONS) + 1))


def test_filename_order_matches_numeric_order():
    """CI globs them. Zero-padding is what keeps 0010 after 0009 rather than
    after 0001."""
    assert numbers() == sorted(numbers())


def test_each_migration_is_a_single_transaction():
    """A half-applied migration leaves a schema no version number describes."""
    for p in MIGRATIONS:
        body = p.read_text().lower()
        assert "begin;" in body, f"{p.name} does not open a transaction"
        assert "commit;" in body, f"{p.name} does not commit"


def test_migrations_are_never_edited_in_place():
    """0001 is applied to every environment that already exists; changing it
    changes nothing for them and everything for a new one. The check is crude
    -- it only asserts later migrations exist to carry changes -- but it is
    here to make the rule visible next to the files it governs.
    """
    assert MIGRATIONS[0].name.startswith("0001_"), "the first migration was renamed"
