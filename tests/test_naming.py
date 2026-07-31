import re
from dataclasses import replace

import pytest

from screening_report.naming import (
    DEFAULT_NAMING_PROFILE,
    NamingProfileError,
)


def test_profile_rejects_missing_required_named_group() -> None:
    with pytest.raises(NamingProfileError, match="project"):
        replace(
            DEFAULT_NAMING_PROFILE,
            project_pattern=re.compile(r"^LAB\d+$"),
        )


def test_profile_rejects_invalid_slot_range() -> None:
    with pytest.raises(NamingProfileError, match="slot range"):
        replace(DEFAULT_NAMING_PROFILE, minimum_slot=12, maximum_slot=2)
