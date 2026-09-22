"""Import every model module so `Base.metadata` is complete.

Alembic autogenerate and the model/migration drift test both need the whole
metadata. Importing each module explicitly -- rather than walking the package -- keeps
the dependency obvious and fails loudly if a module is renamed.
"""

from __future__ import annotations

from app.core.db import Base
from app.domains.catalog import models as catalog_models
from app.domains.identity import models as identity_models
from app.domains.onboarding import models as onboarding_models
from app.domains.pilot import models as pilot_models
from app.domains.review import models as review_models
from app.domains.sources import models as sources_models
from app.domains.taxonomy import models as taxonomy_models
from app.domains.versioning import models as versioning_models

__all__ = [
    "Base",
    "catalog_models",
    "identity_models",
    "onboarding_models",
    "pilot_models",
    "review_models",
    "sources_models",
    "taxonomy_models",
    "versioning_models",
]
