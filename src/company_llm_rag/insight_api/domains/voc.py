"""
VOC/피드백 분석 도메인 (2차, #56 Phase 3)

기간별 VOC(고객의 소리)/피드백 데이터를 받아:
- preprocess: 건수 추이/평점 분포/부정 비율/카테고리·채널 집계/급증 감지를 결정적으로 계산
- LLM: 통계표 + 샘플 텍스트(부정 우선)를 보고 주제(theme) 도출·해석

레지스트리 확장 검증용 2번째 도메인 — 라우터/인증/이력은 공통 파이프라인 그대로.
"""

import json
import statistics
from collections import defaultdict
from datetime import date
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from company_llm_rag.insight_api.domains.base import InsightDomain, parse_llm_json
from company_llm_rag.insight_api.domains.sales import Period, _format_pct

MAX_RECORDS = 10_000
_ANOMALY_Z = 2.5
_ANOMALY_MIN_DAYS = 7
_MAX_SAMPLES = 60          # LLM에 전달할 샘플 상한 (부정 피드백 우선)
_SAMPLE_TEXT_CHARS = 200   # 샘플 1건당 텍스트 길이 상한
_NEGATIVE_RATING_MAX = 2   # 이 값 이하 평점 = 부정


class VocRecord(BaseModel):
    date: date
    text: str = Field(min_length=1, max_length=4000)
    rating: Optional[int] = Field(None, ge=1, le=5)
    category: Optional[str] = None
    channel: Optional[str] = None


class VocOptions(BaseModel):
    focus: List[str] = Field(default_factory=list)
    language: str = "ko"


class VocInsightRequest(BaseModel):
    period: Period
    records: List[VocRecord] = Field(min_length=1, max_length=MAX_RECORDS)
    options: VocOptions = Field(default_factory=VocOptions)


def _count_share(counter: Dict[str, int], total: int) -> List[Dict]:
    ranked = sorted(counter.items(), key=lambda x: -x[1])
    return [{"value": v, "count": c, "share": round(c / total, 4) if total else 0}
            for v, c in ranked]


class VocDomain(InsightDomain):
    name = "voc"
    request_model = VocInsightRequest

    def preprocess(self, req: VocInsightRequest) -> Dict:
        cur = [r for r in req.records
               if req.period.from_ <= r.date <= req.period.to]
        if not cur:
            raise ValueError("no records within period")

        total = len(cur)

        # 일별 건수 추이 + 급증 감지 (z-score)
        daily: Dict[str, int] = defaultdict(int)
        for r in cur:
            daily[r.date.isoformat()] += 1
        daily = dict(sorted(daily.items()))
        anomalies = []
        if len(daily) >= _ANOMALY_MIN_DAYS:
            values = list(daily.values())
            mean = statistics.mean(values)
            stdev = statistics.pstdev(values)
            if stdev > 0:
                for d, v in daily.items():
                    z = (v - mean) / stdev
                    if abs(z) >= _ANOMALY_Z:
                        anomalies.append({
                            "date": d, "count": v, "z_score": round(z, 2),
                            "direction": "spike" if z > 0 else "drop",
                        })

        # 평점 통계
        rated = [r.rating for r in cur if r.rating is not None]
        rating_stats = None
        if rated:
            negative = sum(1 for v in rated if v <= _NEGATIVE_RATING_MAX)
            neg_ratio = negative / len(rated)
            dist = {str(s): sum(1 for v in rated if v == s) for s in range(1, 6)}
            rating_stats = {
                "avg": round(statistics.mean(rated), 2),
                "rated_count": len(rated),
                "distribution": dist,
                "negative_count": negative,
                "negative_ratio": round(neg_ratio, 4),
                "negative_ratio_display": _format_pct(neg_ratio).lstrip("+"),
            }

        # 카테고리/채널 집계
        cat_counter: Dict[str, int] = defaultdict(int)
        ch_counter: Dict[str, int] = defaultdict(int)
        for r in cur:
            if r.category:
                cat_counter[r.category] += 1
            if r.channel:
                ch_counter[r.channel] += 1

        # LLM 샘플: 부정 피드백 우선 → 최신순, 텍스트 truncate
        negatives = [r for r in cur
                     if r.rating is not None and r.rating <= _NEGATIVE_RATING_MAX]
        neg_ids = {id(r) for r in negatives}
        others = [r for r in cur if id(r) not in neg_ids]
        picked = (sorted(negatives, key=lambda r: r.date, reverse=True)
                  + sorted(others, key=lambda r: r.date, reverse=True))[:_MAX_SAMPLES]
        samples = [{
            "date": r.date.isoformat(),
            "rating": r.rating,
            "category": r.category,
            "text": r.text[:_SAMPLE_TEXT_CHARS],
        } for r in picked]

        return {
            "period": {"from": req.period.from_.isoformat(),
                       "to": req.period.to.isoformat()},
            "total_count": total,
            "days_observed": len(daily),
            "daily_series": daily,
            "rating": rating_stats,
            "categories": _count_share(cat_counter, total),
            "channels": _count_share(ch_counter, total),
            "anomalies": anomalies,
            "samples": samples,          # LLM 주제 도출용 (응답 stats에서는 제거)
            "sample_count": len(samples),
            "sampling_note": (
                f"전체 {total}건 중 {len(samples)}건 샘플 (부정 피드백 우선)"
                if total > len(samples) else None
            ),
        }

    def build_messages(self, req: VocInsightRequest, stats: Dict) -> List[Dict[str, str]]:
        system = self.load_prompt()
        focus = ", ".join(req.options.focus) if req.options.focus else "없음"
        user = (
            f"[분석 요청]\n"
            f"- 관심 포인트: {focus}\n"
            f"- 응답 언어: {req.options.language}\n\n"
            f"[서버 계산 통계표 + 샘플]\n{json.dumps(stats, ensure_ascii=False)}"
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

    def postprocess_stats(self, stats: Dict) -> Dict:
        """응답용 stats — 샘플 원문은 응답/이력에 노출하지 않음."""
        out = dict(stats)
        out.pop("samples", None)
        return out

    def request_summary(self, req: VocInsightRequest, stats: Dict) -> Dict:
        """이력용 요약 — VOC 원문 텍스트는 저장하지 않음."""
        return {
            "period": stats.get("period"),
            "rows": len(req.records),
            "days_observed": stats.get("days_observed"),
            "rated": bool(stats.get("rating")),
            "categories": [c["value"] for c in stats.get("categories", [])][:10],
            "focus": req.options.focus,
        }
