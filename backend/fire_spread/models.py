"""Request / response models shared by the router, pipeline and CLI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

PipelineMode = Literal["base", "tuned"]


@dataclass(frozen=True)
class Ignition:
    """Initial fire state: a point, or an active perimeter (GeoJSON Polygon/MultiPolygon in
    lon/lat) whose boundary is lit at t 0. ``lat``/``lon`` is the reference point - the
    ignition itself for a point fire, the perimeter centroid otherwise (it anchors the
    domain, the weather point and the response grid)."""

    lat: float
    lon: float
    perimeter: dict | None = None

    @property
    def is_perimeter(self) -> bool:
        return self.perimeter is not None


@dataclass
class SimulationRequest:
    ignition: Ignition
    duration_hours: int = 24
    ensemble_members: int = 16
    start_time: datetime | None = None
    seed: int | None = None
    spotting: bool | None = None  # None -> settings.spotting_default
    mode: PipelineMode | None = None  # None -> settings.pipeline_mode
    output_cell_m: float | None = None  # response grid cell (m); None -> the simulation's native cell
    debug: bool = False

    def to_json(self) -> dict:
        return {
            "lat": self.ignition.lat,
            "lon": self.ignition.lon,
            "perimeter": self.ignition.perimeter,
            "durationHours": self.duration_hours,
            "ensembleMembers": self.ensemble_members,
            "startTime": self.start_time.isoformat() if self.start_time else None,
            "seed": self.seed,
            "spotting": self.spotting,
            "mode": self.mode,
            "cellSizeM": self.output_cell_m,
        }


class PointIgnition(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class FireStateRequest(BaseModel):
    """Body of ``POST /fire/arrival-grid``: a point ignition and/or an active perimeter."""

    ignition: PointIgnition | None = None
    perimeter: dict[str, Any] | None = Field(
        None, description="GeoJSON Polygon or MultiPolygon (WGS84 lon/lat) of the currently burning area."
    )
    durationHours: int = Field(24, ge=1, le=48)
    ensembleMembers: int = Field(16, ge=1, le=64)
    startTime: datetime | None = None
    seed: int | None = Field(None, ge=1)
    spotting: bool | None = None
    mode: PipelineMode | None = None
    cellSizeM: float = Field(100.0, ge=30, le=1000, description="Output grid cell size in metres.")
    debug: bool = False

    @model_validator(mode="after")
    def _one_fire_state(self) -> "FireStateRequest":
        if self.ignition is None and self.perimeter is None:
            raise ValueError("provide 'ignition' (lat/lon) and/or 'perimeter' (GeoJSON polygon)")
        if self.perimeter is not None:
            t = self.perimeter.get("type")
            if t not in ("Polygon", "MultiPolygon") or "coordinates" not in self.perimeter:
                raise ValueError("perimeter must be a GeoJSON Polygon or MultiPolygon geometry")
        return self


class WeatherSummary(BaseModel):
    source: str
    windSpeedAvgMs: float
    windDirectionAvg: float
    windSpeedSigmaMs: float | None = None
    windDirectionSigmaDeg: float | None = None
    fuelMoisture1hAvgPct: float | None = None
    fuelMoisture100hAvgPct: float | None = None
    liveHerbaceousPct: float | None = None
    liveWoodyPct: float | None = None
    foliarMoisturePct: float | None = None
    weatherMembers: int | None = None  # NWP ensemble members driving the cases (1 = perturbed deterministic)


class ZoneInfo(BaseModel):
    name: str | None = None
    dominantFireType: str | None = None
    designFires: dict[str, float] = Field(default_factory=dict)
    properties: dict = Field(default_factory=dict)


class DebugInfo(BaseModel):
    runId: str
    runDir: str
    timings: dict[str, float]
    elmfireStdoutTail: str


class LegacyArrivalGrid(BaseModel):
    """What ``GET /fire/arrival-grid`` returns by default: the contract of the original
    (Deepfire-backed) route. Origin is the south-west corner of cell [0][0]; rows run S->N,
    columns W->E; 0 = ignition, null = not reached within the horizon."""

    originLat: float
    originLon: float
    cellDegLat: float
    cellDegLon: float
    arrivalHours: list[list[int | None]]


LEGACY_KEYS = tuple(LegacyArrivalGrid.model_fields)


class ArrivalGrid(BaseModel):
    originLat: float
    originLon: float
    cellDegLat: float
    cellDegLon: float
    cellSizeM: float
    durationMinutes: int
    ensembleMembers: int
    arrivalHours: list[list[int | None]]
    arrivalMinutes: list[list[float | None]]
    arrivalMinutesP10: list[list[float | None]]
    arrivalMinutesP90: list[list[float | None]]
    burnProbability: list[list[float | None]]
    weather: WeatherSummary
    physics: dict = Field(default_factory=dict)  # switches that shaped this run (spotting, barriers, diurnal...)
    zone: ZoneInfo = Field(default_factory=ZoneInfo)
    debug: DebugInfo | None = None


# --- errors -------------------------------------------------------------------


class PipelineError(Exception):
    status_code = 500


class OutsideCoverage(PipelineError):
    """Ignition outside the static data, or on a non-burnable cell."""

    status_code = 422


class WeatherProviderError(PipelineError):
    status_code = 502


class ElmfireTimeout(PipelineError):
    status_code = 504


class ElmfireFailed(PipelineError):
    status_code = 500
