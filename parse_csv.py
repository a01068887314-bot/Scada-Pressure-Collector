# -*- coding: utf-8 -*-
"""
techpalm SCADA에서 다운로드한 CSV(devicedata_{deviceKey}.csv)를
Firestore에 저장할 수 있는 형태로 파싱하는 모듈.

확인된 CSV 스펙:
- 인코딩: EUC-KR (CP949)
- 구분자: 콤마(,)
- 줄바꿈: CRLF (\r\n)
- 컬럼: 발생일자, 압력, 장치 온도, 배터리 전압, 수신 감도, 데이터 모드, 수집 주기, 통신 주기
- 발생일자 포맷: "YYYY-MM-DD HH:mm:ss" (문자열)
"""

import pandas as pd
from datetime import datetime


# CSV 컬럼명 -> Firestore 필드명 매핑 (영문 필드명으로 통일해서 저장)
COLUMN_MAP = {
    "발생일자": "timestamp_str",
    "압력": "pressure",
    "장치 온도": "device_temp",
    "배터리 전압": "battery_voltage",
    "수신 감도": "signal_strength",
    "데이터 모드": "data_mode",
    "수집 주기": "collect_interval",
    "통신 주기": "comm_interval",
}


def parse_scada_csv(file_path: str, device_key: str) -> list[dict]:
    """
    techpalm에서 다운로드한 CSV 파일 1개를 파싱해서
    Firestore에 저장 가능한 dict 리스트로 반환.

    Args:
        file_path: 다운로드된 csv 파일 경로
        device_key: 이 데이터가 어느 기기(deviceKey)의 데이터인지 (예: "2161")

    Returns:
        [
          {
            "device_key": "2161",
            "timestamp": "2026-08-20T11:00:03",   # ISO 포맷 (Firestore 정렬/쿼리에 유리)
            "pressure": 1.23,
            "device_temp": 25.4,
            "battery_voltage": 3.6,
            "signal_strength": -70.0,
            "data_mode": 1.0,
            "collect_interval": 15.0,
            "comm_interval": 15.0,
          },
          ...
        ]
    """
    df = pd.read_csv(file_path, encoding="cp949")
    df = df.rename(columns=COLUMN_MAP)

    records = []
    for _, row in df.iterrows():
        # 발생일자가 비어있는 행은 스킵 (혹시 모를 빈 줄 방지)
        if pd.isna(row.get("timestamp_str")):
            continue

        try:
            # 실제 파일에서 발생일자 값 앞에 공백이 섞여있는 경우가 확인되어 strip() 처리
            dt = datetime.strptime(str(row["timestamp_str"]).strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            # 포맷이 예상과 다른 행은 건너뛰고 나중에 확인할 수 있게 출력만
            print(f"[경고] 날짜 파싱 실패, 스킵: {row.get('timestamp_str')}")
            continue

        record = {
            "device_key": str(device_key),
            "timestamp": dt.isoformat(),
        }

        for field in [
            "pressure",
            "device_temp",
            "battery_voltage",
            "signal_strength",
            "data_mode",
            "collect_interval",
            "comm_interval",
        ]:
            value = row.get(field)
            # NaN은 None으로 변환 (Firestore는 NaN을 그대로 못 씀)
            record[field] = None if pd.isna(value) else float(value)

        records.append(record)

    return records


def make_firestore_doc_id(device_key: str, timestamp_iso: str) -> str:
    """
    중복 저장을 막기 위한 Firestore 문서 ID 생성.
    같은 기기의 같은 시각 데이터는 항상 같은 문서 ID -> 재실행해도 덮어쓰기만 되고 중복 안 생김.

    예: "2161_2026-08-20T11:00:03"
    """
    return f"{device_key}_{timestamp_iso}"


if __name__ == "__main__":
    # 간단 테스트 실행 예시
    import sys

    if len(sys.argv) < 3:
        print("사용법: python parse_csv.py <csv파일경로> <deviceKey>")
        sys.exit(1)

    path, dkey = sys.argv[1], sys.argv[2]
    result = parse_scada_csv(path, dkey)
    print(f"총 {len(result)}건 파싱됨")
    for r in result[:5]:
        print(r)
