"""Pydantic 入参模型。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PlantIn(BaseModel):
    plant_code: str = Field(min_length=3, max_length=32)
    name: str
    basin: str
    design_cap_m3d: float = Field(gt=0, lt=1_000_000)
    status: str = "运行"


class OutfallIn(BaseModel):
    outfall_code: str
    plant_id: int
    river_reach: str
    cod_limit: float = Field(gt=0, lt=500)
    nh3n_limit: float = Field(gt=0, lt=50)
    tp_limit: float = Field(gt=0, lt=10)
    status: str = "开放"


class MetricsIn(BaseModel):
    cod: float = Field(ge=0)
    nh3n: float = Field(ge=0)
    tp: float = Field(ge=0)


class SampleIn(BaseModel):
    outfall_id: int
    permit_ref: str
    metrics: MetricsIn
    flow_m3h: float = Field(gt=0, lt=50000)
    sample_code: str | None = None


class ExceedOpenIn(BaseModel):
    sample_id: int
    note: str = ""


class ExceedCloseIn(BaseModel):
    # 允许验收方以额外字段携带复测/重裁指标与口径，提取层会统一识别。
    model_config = ConfigDict(extra="allow")

    hold_id: int
    note: str = ""
    # 甲路径：同事务写入正式指标列的复测值（多个别名都认）。
    metrics: dict | None = None
    new_metrics: dict | None = None
    recalc_metrics: dict | None = None
    retest_metrics: dict | None = None
    corrected_metrics: dict | None = None
    metrics_patch: dict | None = None
    retest: dict | None = None
    recalc: dict | None = None
    # 口径选择：formal/甲（默认，同事务正式重裁）或 shadow/乙（只落影子裁定）。
    lane: str | None = None
    mode: str | None = None
    path: str | None = None
    strategy: str | None = None
    shadow: bool | None = None
    as_shadow: bool | None = None
    # 也允许直接平铺三个指标。
    cod: float | None = None
    nh3n: float | None = None
    tp: float | None = None


class RetestIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    metrics: dict | None = None
    new_metrics: dict | None = None
    recalc_metrics: dict | None = None
    retest_metrics: dict | None = None
    lane: str | None = None
    mode: str | None = None
    shadow: bool | None = None
    note: str = ""
    cod: float | None = None
    nh3n: float | None = None
    tp: float | None = None


class ReleaseIn(BaseModel):
    sample_id: int
    officer: str = "值班员"


class SealIn(BaseModel):
    sample_id: int
    snapshot: str | None = None


class PermitIn(BaseModel):
    permit_code: str
    plant_id: int
    quota_m3d: float
    cod_cap: float
    nh3n_cap: float
    tp_cap: float
    period_label: str


class LineageIn(BaseModel):
    from_sample_id: int
    to_sample_id: int | None = None
    edge_type: str
    detail: str


class StubJobIn(BaseModel):
    mode: str = "dry_run"
    detail: str = ""


class LabCourierIn(StubJobIn):
    sample_code: str
    courier_slot: str = "A1"


class SludgePlanIn(StubJobIn):
    plant_id: int | None = None
    digester_bay: str = "D-01"
