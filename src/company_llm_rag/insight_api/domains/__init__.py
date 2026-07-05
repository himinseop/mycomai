"""
인사이트 도메인 레지스트리

도메인 추가 방법:
1. domains/에 InsightDomain 구현 파일 추가
2. 아래 DOMAIN_REGISTRY에 등록
3. prompts/insights/<name>.txt 프롬프트 작성
"""

from typing import Dict

from company_llm_rag.insight_api.domains.base import InsightDomain
from company_llm_rag.insight_api.domains.sales import SalesDomain

DOMAIN_REGISTRY: Dict[str, InsightDomain] = {
    d.name: d for d in [
        SalesDomain(),
    ]
}
