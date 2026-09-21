"""The ORM mapping must configure cleanly, with no warnings.

This is a cheap unit test guarding an expensive failure mode. SQLAlchemy resolves
relationships lazily, at the first ORM operation, inside `configure_mappers()`. A
misconfigured relationship is reported there as a `SAWarning` -- and the suite runs
with `filterwarnings = error`, so the warning becomes an exception *and* leaves the
registry permanently marked as failed, which then makes every later ORM call in the
process fail with a confusing "one or more mappers failed to initialize".

That is exactly how a latent defect in the `Faculty` self-relationship stayed
invisible: the schema tests speak raw SQL, so nothing configured the mappers until
the target-list importer became the first code to use ORM constructs. The failure
then surfaced as thirty unrelated integration tests erroring out, in a different
module, depending on test order.

Asserting it here means the next such mistake is one obvious red test at the point
of the mistake.
"""

from __future__ import annotations

import warnings


def test_every_relationship_configures_without_warnings() -> None:
    from sqlalchemy.orm import configure_mappers

    import app.db.all_models  # noqa: F401 - registers every mapper

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        configure_mappers()


def test_every_model_module_is_registered_in_all_models() -> None:
    """A model module nobody imports is invisible to autogenerate and to this test."""
    from pathlib import Path

    import app.db.all_models as all_models
    from app.core.db import Base

    domains_root = Path(all_models.__file__).resolve().parents[1] / "domains"
    on_disk = {path.parent.name for path in domains_root.glob("*/models.py")}
    imported = {
        name.removesuffix("_models") for name in all_models.__all__ if name.endswith("_models")
    }
    assert on_disk == imported, (
        f"model modules on disk but not imported by all_models: {sorted(on_disk - imported)}; "
        f"imported but absent: {sorted(imported - on_disk)}"
    )
    assert Base.metadata.tables, "metadata is empty; all_models did not register anything"
