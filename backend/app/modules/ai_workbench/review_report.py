"""供产品展示的结构化复盘，避免长摘要代替发现与规则决定。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReportTable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    columns: list[str] = Field(min_length=1, max_length=12)
    rows: list[list[str]] = Field(max_length=50)

    @model_validator(mode="after")
    def rectangular(self):
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("报告表格每行必须与表头列数相同")
        if any(len(cell) > 1000 for row in [self.columns, *self.rows] for cell in row):
            raise ValueError("报告单元格最多1000字符")
        return self


class ReportSection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=30)
    table: ReportTable | None = None


class RuleDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    status: Literal["PROPOSED", "NO_CHANGE", "INSUFFICIENT_EVIDENCE"]
    reason: str = Field(min_length=1, max_length=2000)


class ReportDriver(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    finding: str = Field(min_length=1, max_length=160)
    impact: str = Field(min_length=1, max_length=350)
    action: str = Field(min_length=1, max_length=250)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=30)


class ReportExecutive(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    assessment: str = Field(min_length=1, max_length=300)
    drivers: list[ReportDriver] = Field(min_length=1, max_length=3)
    next_review: str = Field(min_length=1, max_length=250)


class ReviewReport(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    executive: ReportExecutive | None = None
    headline: str = Field(min_length=1, max_length=160)
    verdict: str = Field(min_length=1, max_length=1000)
    sections: list[ReportSection] = Field(min_length=1, max_length=12)
    rule_decision: RuleDecision
