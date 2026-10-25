"""Bounded, strict user-entered context; text is data, never instructions."""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vitalforge_dashboard.metric_metadata import METRIC_METADATA

DAILY_METRICS = frozenset(name for name, item in METRIC_METADATA.items() if item["expected_cadence"] == "daily")


def canonical_date(value: str) -> str:
    if len(value) != 10 or date.fromisoformat(value).isoformat() != value:
        raise ValueError("Expected a calendar date")
    return value


def canonical_tag(value: str) -> str:
    value = " ".join(value.split()).casefold()
    if not 1 <= len(value) <= 40:
        raise ValueError("Invalid tag length")
    return value


class DailyContextInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    note: str = Field(max_length=1000)
    tags: list[str] = Field(max_length=12)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(canonical_tag(value) for value in values))


class ExperimentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = Field(min_length=1, max_length=100)
    tag: str = Field(min_length=1, max_length=40)
    metric: str
    start_date: str
    end_date: str

    @field_validator("title")
    @classmethod
    def nonempty_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A title is required")
        return value

    @field_validator("tag")
    @classmethod
    def normalize_tag(cls, value: str) -> str:
        return canonical_tag(value)

    @field_validator("metric")
    @classmethod
    def daily_metric(cls, value: str) -> str:
        if value not in DAILY_METRICS:
            raise ValueError("Choose a daily metric")
        return value

    @field_validator("start_date", "end_date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        return canonical_date(value)

    @model_validator(mode="after")
    def bounded_period(self):
        span = (date.fromisoformat(self.end_date) - date.fromisoformat(self.start_date)).days
        if not 0 <= span <= 364:
            raise ValueError("Choose an inclusive period of 1 to 365 days")
        return self
