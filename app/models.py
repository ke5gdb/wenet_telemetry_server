"""Ingest request models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class IngestPacket(BaseModel):
    kind: Literal["sensor", "gps"] = "sensor"
    received_at: str | None = None
    #: Hex of the bytes as they came off the air. Optional but preferred: it
    #: makes the archive lossless and lets a decoder fix be replayed later.
    raw: str | None = None
    #: Decoded packet. Required when `raw` is absent.
    data: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _need_something(self):
        if self.raw is None and not self.data:
            raise ValueError("packet needs at least one of `raw` or `data`")
        return self


class IngestBatch(BaseModel):
    reporter_id: str = Field(default="UNKNOWN", max_length=64)
    packets: list[IngestPacket]
