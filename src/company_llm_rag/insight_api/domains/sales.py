"""
매출 분석 도메인 (1차, #56)

기간별 매출 데이터를 받아:
- preprocess: 총액/추이/전기 대비/차원별 집계/이상치(z-score)를 결정적으로 계산
- LLM: 계산된 통계표만 보고 요약(summary)·포인트(highlights)·이상치 해석(anomalies) 생성
"""

import json
import statistics
from collections import defaultdict
from datetime import date
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from company_llm_rag.insight_api.domains.base import InsightDomain, parse_llm_json

MAX_RECORDS = 10_000
_ANOMALY_Z = 2.5          # 일매출 z-score 이상치 기준
_ANOMALY_MIN_DAYS = 7     # 이상치 탐지 최소 표본 일수
_WEEKDAYS_KO = ["월", "화", "수", "목", "금", "토", "일"]


class Period(BaseModel):
    from_: date = Field(alias="from")
    to: date
    granularity: Literal["daily", "weekly", "monthly"] = "daily"

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _check_order(self):
        if self.to < self.from_:
            raise ValueError("period 'to' must be >= 'from'")
        return self


class SalesRecord(BaseModel):
    date: date
    amount: float
    count: Optional[int] = None
    dimension: Optional[Dict[str, str]] = None


class SalesOptions(BaseModel):
    focus: List[str] = Field(default_factory=list)
    language: str = "ko"


class SalesInsightRequest(BaseModel):
    period: Period
    compare_period: Optional[Period] = None
    records: List[SalesRecord] = Field(min_length=1, max_length=MAX_RECORDS)
    options: SalesOptions = Field(default_factory=SalesOptions)


def _format_krw(v: float) -> str:
    """금액을 한국어 단위 문자열로 포맷 (LLM 단위 변환 오류 방지 — 서버가 확정)."""
    v = round(v)
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 100_000_000:
        eok = a / 100_000_000
        s = f"{eok:,.2f}".rstrip("0").rstrip(".")
        return f"{sign}{s}억 원"
    if a >= 10_000:
        return f"{sign}{a / 10_000:,.0f}만 원"
    return f"{sign}{a:,.0f}원"


def _format_pct(ratio: Optional[float]) -> Optional[str]:
    """증감률(0.5=+50%)을 퍼센트 문자열로 포맷."""
    if ratio is None:
        return None
    return f"{'+' if ratio >= 0 else ''}{ratio * 100:,.1f}%"


def _daily_series(records: List[SalesRecord]) -> Dict[str, float]:
    daily: Dict[str, float] = defaultdict(float)
    for r in records:
        daily[r.date.isoformat()] += r.amount
    return dict(sorted(daily.items()))


def _dimension_stats(records: List[SalesRecord], total: float) -> Dict:
    """records[].dimension의 각 키(매장/채널 등)별 합계·점유율 집계."""
    dims: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in records:
        for key, value in (r.dimension or {}).items():
            dims[key][value] += r.amount
    out = {}
    for key, values in dims.items():
        ranked = sorted(values.items(), key=lambda x: -x[1])
        out[key] = [
            {"value": v, "amount": round(a, 2), "amount_display": _format_krw(a),
             "share": round(a / total, 4) if total else 0}
            for v, a in ranked
        ]
    return out


def _detect_anomalies(daily: Dict[str, float]) -> List[Dict]:
    """일매출 z-score 기반 이상치. 표본 7일 미만이면 탐지하지 않음."""
    if len(daily) < _ANOMALY_MIN_DAYS:
        return []
    values = list(daily.values())
    mean = statistics.mean(values)
    stdev = statistics.pstdev(values)
    if stdev == 0:
        return []
    out = []
    for d, v in daily.items():
        z = (v - mean) / stdev
        if abs(z) >= _ANOMALY_Z:
            out.append({
                "date": d, "amount": round(v, 2), "z_score": round(z, 2),
                "direction": "spike" if z > 0 else "drop",
            })
    return out


class SalesDomain(InsightDomain):
    name = "sales"
    request_model = SalesInsightRequest
    signature_fields = {"date", "amount"}
    description = "기간별 매출/거래 금액 데이터 분석 — 총액·추이·채널/매장별 편차·전기 대비·이상치"

    def preprocess(self, req: SalesInsightRequest) -> Dict:
        cur = [r for r in req.records
               if req.period.from_ <= r.date <= req.period.to]
        if not cur:
            raise ValueError("no records within period")

        daily = _daily_series(cur)
        total = sum(daily.values())
        count_total = sum(r.count for r in cur if r.count is not None)
        days = len(daily)
        avg_daily = total / days if days else 0

        best_day = max(daily, key=daily.get)
        worst_day = min(daily, key=daily.get)

        # 요일 패턴 (일 합계의 요일별 평균)
        weekday_sums: Dict[int, List[float]] = defaultdict(list)
        for d, v in daily.items():
            weekday_sums[date.fromisoformat(d).weekday()].append(v)
        weekday_avg = {
            _WEEKDAYS_KO[wd]: round(statistics.mean(vals), 2)
            for wd, vals in sorted(weekday_sums.items())
        }

        stats: Dict = {
            "period": {"from": req.period.from_.isoformat(),
                       "to": req.period.to.isoformat(),
                       "granularity": req.period.granularity},
            "total": round(total, 2),
            "total_display": _format_krw(total),
            "days_observed": days,
            "avg_daily": round(avg_daily, 2),
            "avg_daily_display": _format_krw(avg_daily),
            "count_total": count_total or None,
            "best_day": {"date": best_day, "amount": round(daily[best_day], 2),
                         "amount_display": _format_krw(daily[best_day])},
            "worst_day": {"date": worst_day, "amount": round(daily[worst_day], 2),
                          "amount_display": _format_krw(daily[worst_day])},
            "weekday_avg": weekday_avg,
            "daily_series": {d: round(v, 2) for d, v in daily.items()},
            "dimensions": _dimension_stats(cur, total),
            "anomalies": [
                {**a, "amount_display": _format_krw(a["amount"])}
                for a in _detect_anomalies(daily)
            ],
        }

        # 전기 대비 (compare_period 구간의 records 사용)
        if req.compare_period:
            prev = [r for r in req.records
                    if req.compare_period.from_ <= r.date <= req.compare_period.to]
            prev_total = sum(r.amount for r in prev)
            growth = round((total - prev_total) / prev_total, 4) if prev_total > 0 else None
            stats["compare"] = {
                "period": {"from": req.compare_period.from_.isoformat(),
                           "to": req.compare_period.to.isoformat()},
                "prev_total": round(prev_total, 2),
                "prev_total_display": _format_krw(prev_total),
                "growth": growth,
                "growth_display": _format_pct(growth),
            }
            # 차원별 전기 대비 (양쪽 모두 있는 값만)
            prev_dims = _dimension_stats(prev, prev_total) if prev else {}
            dim_growth: Dict[str, List[Dict]] = {}
            for key, cur_items in stats["dimensions"].items():
                prev_map = {i["value"]: i["amount"] for i in prev_dims.get(key, [])}
                rows = []
                for item in cur_items:
                    pv = prev_map.get(item["value"])
                    if pv and pv > 0:
                        g = round((item["amount"] - pv) / pv, 4)
                        rows.append({"value": item["value"],
                                     "growth": g, "growth_display": _format_pct(g)})
                if rows:
                    dim_growth[key] = rows
            if dim_growth:
                stats["compare"]["dimension_growth"] = dim_growth

        return stats

    def build_messages(self, req: SalesInsightRequest, stats: Dict,
                       question: str = "") -> List[Dict[str, str]]:
        system = self.load_prompt()
        focus = ", ".join(req.options.focus) if req.options.focus else "없음"
        user = (
            f"[분석 요청]\n"
            f"- 질문: {question or '(없음 — 전반적인 분석)'}\n"
            f"- 관심 포인트: {focus}\n"
            f"- 응답 언어: {req.options.language}\n\n"
            f"[서버 계산 통계표]\n{json.dumps(stats, ensure_ascii=False)}"
        )
        return [{"role": "system", "content": system},
                {"role": "user", "content": user}]

    def parse_response(self, raw: str) -> Dict:
        data = parse_llm_json(raw)
        return {
            "summary": (data.get("summary") or "").strip(),
            "highlights": data.get("highlights") or [],
            "anomalies": data.get("anomalies") or [],
        }

    def request_summary(self, req: SalesInsightRequest, stats: Dict) -> Dict:
        """이력용 요약 — 원본 매출 데이터(records)는 저장하지 않음."""
        return {
            "period": stats.get("period"),
            "rows": len(req.records),
            "days_observed": stats.get("days_observed"),
            "dimension_keys": sorted(stats.get("dimensions", {}).keys()),
            "has_compare": req.compare_period is not None,
            "focus": req.options.focus,
        }
