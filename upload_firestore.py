# -*- coding: utf-8 -*-
"""
파싱된 SCADA 데이터를 Firestore에 업로드.

전제:
- 기존 "관말압력측정장치 App." 프로젝트(Gwanmal)와 같은 Firebase 프로젝트를 재사용
- 서비스 계정 키(JSON)는 절대 코드/공개 저장소에 넣지 않고 GitHub Secrets에 저장
- 로컬 테스트 시에는 서비스 계정 키 파일 경로를 환경변수로 지정해서 사용

컬렉션 구조 제안:
  pressure_readings/{device_key}_{timestamp}
    - device_key: str
    - timestamp: str (ISO)
    - pressure, device_temp, battery_voltage, signal_strength,
      data_mode, collect_interval, comm_interval: float | None
    - uploaded_at: server timestamp (언제 수집됐는지 추적용)

문서 ID를 "{device_key}_{timestamp}"로 고정하는 이유:
  같은 시각 데이터가 중복 저장되지 않고, 재실행해도 덮어쓰기만 됨 (idempotent)
"""

import os
import firebase_admin
from firebase_admin import credentials, firestore

from parse_csv import parse_scada_csv, make_firestore_doc_id


COLLECTION_NAME = "pressure_readings"

_db = None


def get_firestore_client():
    """
    Firebase 앱을 (한 번만) 초기화하고 Firestore 클라이언트를 반환.

    서비스 계정 키 위치:
    - 로컬 테스트: 환경변수 GOOGLE_APPLICATION_CREDENTIALS 에 json 파일 경로 지정
    - GitHub Actions: 워크플로우에서 GitHub Secrets 값을 파일로 써준 뒤 같은 방식으로 지정
    """
    global _db
    if _db is not None:
        return _db

    if not firebase_admin._apps:
        cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not cred_path:
            raise RuntimeError(
                "GOOGLE_APPLICATION_CREDENTIALS 환경변수가 설정되지 않았습니다. "
                "Firebase 서비스 계정 키(.json) 경로를 지정해주세요."
            )
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred)

    _db = firestore.client()
    return _db


def upload_records(records: list[dict]) -> int:
    """
    파싱된 레코드 리스트를 Firestore에 배치(batch)로 업로드.
    Firestore 배치는 최대 500건까지 지원하므로 500건 단위로 쪼개서 커밋.

    Returns:
        업로드된 문서 수
    """
    if not records:
        return 0

    db = get_firestore_client()
    collection_ref = db.collection(COLLECTION_NAME)

    BATCH_LIMIT = 500
    total_uploaded = 0

    for i in range(0, len(records), BATCH_LIMIT):
        chunk = records[i : i + BATCH_LIMIT]
        batch = db.batch()

        for record in chunk:
            doc_id = make_firestore_doc_id(record["device_key"], record["timestamp"])
            doc_ref = collection_ref.document(doc_id)
            data = dict(record)
            data["uploaded_at"] = firestore.SERVER_TIMESTAMP
            batch.set(doc_ref, data, merge=True)

        batch.commit()
        total_uploaded += len(chunk)
        print(f"  -> {total_uploaded}/{len(records)}건 업로드 완료")

    return total_uploaded


def parse_and_upload_csv(file_path: str, device_key: str) -> int:
    """
    CSV 파일 하나를 파싱해서 바로 Firestore에 업로드하는 헬퍼.
    """
    records = parse_scada_csv(file_path, device_key)
    print(f"[{device_key}] {len(records)}건 파싱됨, 업로드 시작...")
    return upload_records(records)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("사용법: python upload_firestore.py <csv파일경로> <deviceKey>")
        sys.exit(1)

    path, dkey = sys.argv[1], sys.argv[2]
    count = parse_and_upload_csv(path, dkey)
    print(f"총 {count}건 Firestore 업로드 완료")
