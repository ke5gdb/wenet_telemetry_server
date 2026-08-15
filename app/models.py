"""Ingest request models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class IngestPacket(BaseModel):
    kind: Literal["sensor", "gps"] = "sensor"
    received_at: str | None = None
    #: Hex of the bytes as they came off the air. Optional but preferred: it
    #: makes the archive lossless and lets a decoder fix be replayed later.
    raw: str | None = None
    #: Decoded packet. Required when `raw` is absent.
    data: dict[str, Any] | None = None
    #: Callsign of the Wenet transmitter that sent this packet, as named by the
    #: receiver. Three identities meet in one batch and none can stand in for
    #: another: `reporter_id` is the ground station that *heard* it, this is the
    #: transmitter on the balloon string that *sent* it, and `data["id"]` names
    #: a sensor board on one of the BLE payloads talking to that transmitter.
    #:
    #: Per item, not per batch: batching is by time, so one POST can carry
    #: packets from two transmitters and a batch-level field would mislabel some
    #: of them.
    #:
    #: Absent means "the receiver never told us" -- older receiver firmware, or
    #: a batch spooled to disk before the field existed and drained hours later.
    #: That is a different fact from an unidentified transmitter, so it stays
    #: None rather than being coerced to a placeholder.
    payload_callsign: str | None = None

    @field_validator("payload_callsign", mode="before")
    @classmethod
    def _blank_is_absent(cls, value):
        """Normalise blank and non-string to absent.

        The forwarder already does this, but flights are keyed on the value: an
        empty string arriving from a hand-rolled build would become a callsign
        of its own and split a flight in two.
        """
        if value is None:
            return None
        text = value.strip() if isinstance(value, str) else str(value).strip()
        return text or None

    @model_validator(mode="after")
    def _need_something(self):
        if self.raw is None and not self.data:
            raise ValueError("packet needs at least one of `raw` or `data`")
        return self


class IngestBatch(BaseModel):
    reporter_id: str = Field(default="UNKNOWN", max_length=64)
    packets: list[IngestPacket]
