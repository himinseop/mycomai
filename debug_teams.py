"""
Teams Incoming Webhook 테스트 스크립트
"""

import os
import sys
sys.path.insert(0, "src")

from dotenv import load_dotenv
load_dotenv()

from company_llm_rag.teams_sender import send_inquiry_to_teams

question = "연결된 계정에서 email 주소를 못 가져오는 이슈가 있나요?"
history = [
    {"role": "user", "content": "연결된 계정에서 email 주소를 못 가져오는 이슈가 있나요?"},
    {"role": "assistant", "content": "관련 정보를 회사 지식베이스에서 찾을 수 없습니다."},
    {"role": "user", "content": "CUPPING-2372 티켓 내용이에요"},
    {"role": "assistant", "content": "해당 티켓에 대한 추가 정보가 없어 정확한 답변이 어렵습니다."},
]

result = send_inquiry_to_teams(question, history)
print("전송 결과:", "✅ 성공" if result else "❌ 실패")
