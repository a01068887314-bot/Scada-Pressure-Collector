# SCADA 압력데이터 자동화 프로젝트

techpalm SCADA 사이트(scada.techpalm.co.kr)에서 73개소 압력 데이터를 15분마다 자동 수집해서
Firestore에 저장하는 프로젝트. (기존 "관말압력측정장치 App."과 같은 Firebase 프로젝트 재사용)

## 파일 구성

| 파일 | 역할 |
|---|---|
| `parse_csv.py` | 다운로드된 CSV(EUC-KR 인코딩)를 파싱해서 Firestore용 dict로 변환 |
| `upload_firestore.py` | 파싱된 데이터를 Firestore에 배치 업로드 |
| `collect_all.py` | 로그인 → 73개소 순회 다운로드 → 파싱 → 업로드까지 전체 실행하는 메인 스크립트 |
| `device_list.json` | 73개소 deviceKey ↔ 위치 매핑 (양식3.xlsx에서 추출) |
| `.github/workflows/collect.yml` | 15분마다 자동 실행하는 GitHub Actions 워크플로우 |
| `requirements.txt` | Python 의존성 목록 |

## 로컬 테스트 방법

```bash
pip install -r requirements.txt
playwright install chromium

# 환경변수 설정 (Windows PowerShell 기준)
$env:SCADA_USERNAME="아이디"
$env:SCADA_PASSWORD="비밀번호"
$env:GOOGLE_APPLICATION_CREDENTIALS="C:\path\to\firebase-key.json"

python collect_all.py --minutes 60
```

## 아직 확인/수정이 필요한 부분 (중요)

1. **`LOGIN_URL` 확인 필요**
   `collect_all.py`의 `LOGIN_URL = f"{BASE_URL}/login"`은 추정값입니다.
   기존에 검증된 `test_login.py`에서 실제로 사용한 로그인 페이지 URL로 반드시 교체해주세요.

2. **날짜 파싱 시 공백 이슈**
   실제 CSV의 `발생일자` 값 앞에 공백이 한 칸 섞여 있는 것을 확인했습니다 (`parse_csv.py`에서 `.strip()` 처리로 이미 대응해뒀습니다). 혹시 다른 이상한 공백/문자가 더 있는지, 실제 값이 채워진 CSV로 한 번 더 테스트해보시는 걸 추천드립니다.

3. **`record` 파라미터**
   `download_device_csv()`에서 `record=1`로 고정했습니다 (기존 확인사항: 아무 값이나 넣어도 정상 동작).

4. **GitHub Secrets 등록 필요** (저장소 Settings → Secrets and variables → Actions)
   - `SCADA_USERNAME`
   - `SCADA_PASSWORD`
   - `FIREBASE_SERVICE_ACCOUNT_JSON` (Firebase 서비스 계정 키 json 파일 전체 내용을 통째로 붙여넣기)

5. **공개 저장소 사용 시 주의**
   워크플로우 코드 자체는 공개되지만 위 3개 비밀값은 GitHub Secrets에 암호화 저장되어 코드에는 노출되지 않습니다.

6. **Firestore 요금**
   15분마다 73개소 write → 하루 약 7,000건. Firestore 무료 티어(일 20,000 write)로 충분히 커버됩니다.

## 다음 단계 (예정)

- [ ] 웹 대시보드 제작 (Render/Vercel 무료 티어)
- [ ] 압력 알람 기준값 설계 및 모바일 앱에 "압력 알람" 화면 추가
- [ ] type 값별 의미 확인 (type=5=압력 외 온도/배터리/수신감도 번호)
