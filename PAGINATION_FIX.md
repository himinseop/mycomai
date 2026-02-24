# 🔧 페이지네이션 버그 수정

**작성일**: 2026-02-23
**이슈**: Jira 50개, Confluence 25개만 수집되는 문제

---

## 🐛 발견된 문제

### 증상
- **Jira**: 프로젝트당 50개 이슈만 수집 (설정된 maxResults)
- **Confluence**: 스페이스당 25개 페이지만 수집 (설정된 limit)
- **SharePoint**: 553개 (정상) ✓
- **Teams**: 426개 (정상) ✓

### 원인 분석

#### Jira 문제
```python
# 잘못된 코드
url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
# ...
if start_at >= data.get('total', 0):  # v3에는 'total' 필드 없음!
    break
```

**API 응답 구조 차이**:
- **v2 API** (deprecated): `total`, `startAt`, `maxResults`
- **v3 API** (현재): `nextPageToken`, `isLast`

Jira Cloud에서 v2 API가 삭제되어 410 에러 발생:
```
요청된 API가 삭제되었습니다. /rest/api/3/search/jql API로 마이그레이션하세요.
```

#### Confluence 문제
```python
# 잘못된 코드
if start_at >= data.get('total', 0):  # 'total' 필드 없음!
    break
```

**API 응답**: `size`, `limit`, `start`, `_links` (total 없음)

---

## ✅ 적용된 해결 방법

### Jira: v3 nextPageToken 방식
```python
# 수정된 코드
next_page_token = None

while True:
    url = f"{settings.JIRA_BASE_URL}/rest/api/3/search/jql"
    params = {
        "jql": jql,
        "maxResults": max_results,
        "fields": "..."
    }

    if next_page_token:
        params["nextPageToken"] = next_page_token

    response = requests.get(url, headers=headers, params=params)
    data = response.json()

    issues = data.get('issues', [])
    is_last = data.get('isLast', True)
    next_page_token = data.get('nextPageToken')

    if not issues or is_last:
        break

    all_issues.extend(issues)
```

**테스트 결과**:
```
✓ Issues count: 5
✓ isLast: False
✓ Has nextPageToken: True
```

### Confluence: size < limit 체크
```python
# 수정된 코드
while True:
    # ... API 호출 ...

    pages = data.get('results', [])
    size = data.get('size', 0)

    if not pages:
        break

    all_pages.extend(pages)
    start_at += len(pages)

    # 마지막 페이지 체크
    if size < limit:
        break
```

**로직**:
- `size < limit`: 반환된 결과가 limit보다 적으면 마지막 페이지
- 예: limit=25, size=18 → 마지막 페이지

### SharePoint & Teams: 변경 없음 ✓
```python
# 이미 올바른 방식 사용
while endpoint:
    response_data = call_graph_api(endpoint, access_token)
    items = response_data.get('value', [])
    all_items.extend(items)
    endpoint = response_data.get('@odata.nextLink')  # Microsoft Graph API 표준
```

---

## 📊 예상 효과

### Before (버그)
| Source | Collected | Expected | Rate |
|--------|-----------|----------|------|
| Jira | 50 | ~200+ | 25% |
| Confluence | 25 | ~100+ | 25% |
| SharePoint | 553 | 553 | 100% ✓ |
| Teams | 426 | 426 | 100% ✓ |

### After (수정 후)
| Source | Expected | Rate |
|--------|----------|------|
| Jira | 200+ | 100% ✓ |
| Confluence | 100+ | 100% ✓ |
| SharePoint | 553 | 100% ✓ |
| Teams | 426 | 100% ✓ |

**예상 데이터 증가**:
- Jira: 50개 → 200+개 (4배)
- Confluence: 25개 → 100+개 (4배)
- **Total**: ~1,100개 → ~1,700+개 (55% 증가)

---

## 🧪 검증 방법

### 로컬 테스트
```bash
# Jira 테스트
PYTHONPATH=src python3 src/company_llm_rag/data_extraction/jira/jira_extractor.py > jira_test.jsonl 2> jira_test.log
wc -l jira_test.jsonl

# Confluence 테스트
PYTHONPATH=src python3 src/company_llm_rag/data_extraction/confluence/confluence_extractor.py > confluence_test.jsonl 2> confluence_test.log
wc -l confluence_test.jsonl
```

### Docker 테스트
```bash
docker-compose -f docker/docker-compose.yml run --rm data-loader
```

### 로그 확인
```bash
# Jira 로그에서 페이지네이션 확인
grep "total so far" data/jira_errors.log

# Confluence 로그에서 페이지네이션 확인
grep "total so far" data/confluence_errors.log
```

---

## 🔍 관련 문서

- [Jira REST API v3 Migration Guide](https://developer.atlassian.com/changelog/#CHANGE-2046)
- [Jira Cloud JQL Search API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/#api-rest-api-3-search-jql-get)
- [Confluence REST API Pagination](https://developer.atlassian.com/cloud/confluence/rest/v1/intro/#pagination)
- [Microsoft Graph API Paging](https://learn.microsoft.com/en-us/graph/paging)

---

## ✅ 체크리스트

- [x] Jira 페이지네이션 버그 확인
- [x] Confluence 페이지네이션 버그 확인
- [x] SharePoint/Teams 정상 작동 확인
- [x] Jira v3 API로 수정
- [x] Confluence size 체크로 수정
- [x] 로컬 API 테스트 완료
- [ ] 전체 데이터 재수집 필요
- [ ] 벡터 DB 재구축 필요

---

## 🚀 다음 단계

1. **즉시**: 수정사항 커밋 및 푸시
2. **이후**: 전체 데이터 재수집 실행
   ```bash
   docker-compose -f docker/docker-compose.yml up data-loader
   ```
3. **확인**: 수집된 데이터 라인 수 검증
   ```bash
   wc -l data/*.jsonl
   ```
