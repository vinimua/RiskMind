"""Frozen index identities, not a strategy registry or runtime selector."""

from enum import StrEnum


class IndexProfile(StrEnum):
    BASELINE = "baseline-v1"
    PARENT_CHILD = "parent-child-v1"
