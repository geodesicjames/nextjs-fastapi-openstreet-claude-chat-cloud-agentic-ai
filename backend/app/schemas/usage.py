"""Schemas for the admin usage report (Claude spend from the llm_usage ledger)."""

from datetime import date

from pydantic import BaseModel

from app.services.usage_report import Totals, UsageReport


class TotalsOut(BaseModel):
    cost_usd: float
    calls: int
    input_tokens: int  # uncached input + cache writes + cache reads
    output_tokens: int  # includes thinking

    @classmethod
    def fields_from(cls, t: Totals) -> dict:
        return {
            # Floats for JSON, rounded well below a cent so sums still add up.
            "cost_usd": round(float(t.cost_usd), 6),
            "calls": t.calls,
            "input_tokens": t.input_tokens,
            "output_tokens": t.output_tokens,
        }


class DayOut(TotalsOut):
    day: date


class UserOut(TotalsOut):
    email: str


class ModelOut(TotalsOut):
    model: str


class UsageReportOut(BaseModel):
    start: date
    end: date
    total: TotalsOut
    by_day: list[DayOut]
    by_user: list[UserOut]
    by_model: list[ModelOut]

    @classmethod
    def from_report(cls, r: UsageReport) -> "UsageReportOut":
        return cls(
            start=r.start,
            end=r.end,
            total=TotalsOut(**TotalsOut.fields_from(r.total)),
            by_day=[DayOut(day=d.day, **TotalsOut.fields_from(d)) for d in r.by_day],
            by_user=[UserOut(email=u.email, **TotalsOut.fields_from(u)) for u in r.by_user],
            by_model=[ModelOut(model=m.model, **TotalsOut.fields_from(m)) for m in r.by_model],
        )
