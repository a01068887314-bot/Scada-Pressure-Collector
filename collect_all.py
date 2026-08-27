# -*- coding: utf-8 -*-
"""
73개소 SCADA 압력 데이터 자동 수집 메인 스크립트.

흐름:
  1. Playwright로 techpalm 로그인 (캡차 우회 검증 완료된 방식)
  2. 세션 쿠키(connect.sid) 획득
  3. device_list.json에 있는 73개소를 순회하며 다운로드 API 호출
  4. 각 CSV를 파싱 -> Firestore 업로드
  5. 실패한 기기는 목록에 모아서 마지막에 로그로 출력 (한 기기 실패해도 전체가 죽지 않게)

환경변수 (GitHub Secrets 또는 로컬 .env에서 로드):
  SCADA_USERNAME               - techpalm 로그인 아이디
  SCADA_PASSWORD                - techpalm 로그인 비밀번호
  GOOGLE_APPLICATION_CREDENTIALS - Firebase 서비스 계정 키(json) 파일 경로

실행 예:
  python collect_all.py
  python collect_all.py --minutes 20   # 최근 20분치만 수집 (기본값: 20분, 15분 주기 실행 + 여유분)
"""

import os
import sys
import json
import argparse
import tempfile
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright
import requests

from upload_firestore import parse_and_upload_csv

KST = timezone(timedelta(hours=9))  # 한국 표준시 (GitHub Actions 서버는 UTC라서 명시적으로 변환 필요)

BASE_URL = "https://scada.techpalm.co.kr"
LOGIN_URL = f"{BASE_URL}/scada/homes"  # test_login.py에서 검증된 실제 로그인 페이지 주소
DEVICE_LIST_PATH = os.path.join(os.path.dirname(__file__), "device_list.json")


def load_device_list() -> list[dict]:
    with open(DEVICE_LIST_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def login_and_get_session_cookie() -> str:
    """
    Playwright로 실제 브라우저를 띄워 techpalm에 로그인하고
    connect.sid 쿠키 값을 반환.
    (test_login.py에서 검증 완료된 방식과 동일)
    """
    username = os.environ["SCADA_USERNAME"]
    password = os.environ["SCADA_PASSWORD"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(LOGIN_URL)

        page.fill('input[name="userName"]', username)
        page.fill('input[name="userPasswd"]', password)
        page.click("#btn_login")  # 텍스트가 아닌 id로 클릭 (기존에 확인된 이슈)

        page.wait_for_load_state("networkidle")

        cookies = page.context.cookies()
        browser.close()

    session_cookie = next((c for c in cookies if c["name"] == "connect.sid"), None)
    if not session_cookie:
        raise RuntimeError("로그인 실패: connect.sid 쿠키를 찾지 못했습니다.")

    return session_cookie["value"]


def download_device_csv(session_cookie: str, device_key: str, date_fr: str, date_to: str) -> str:
    """
    로그인 세션 쿠키로 특정 deviceKey의 CSV를 다운로드해서
    임시 파일 경로를 반환.
    """
    url = (
        f"{BASE_URL}/scada/devices"
        f"?action=download&record=1&site=18&type=5"
        f"&deviceKey={device_key}"
        f"&interfaceVersion=1.00"
        f"&dateFr={date_fr}&dateTo={date_to}"
        f"&download_limit=31622500&download_limit_text=1%EB%85%84"
    )

    resp = requests.get(
        url,
        cookies={"connect.sid": session_cookie},
        timeout=30,
    )
    resp.raise_for_status()

    tmp = tempfile.NamedTemporaryFile(
        mode="wb", suffix=".csv", delete=False, prefix=f"scada_{device_key}_"
    )
    tmp.write(resp.content)
    tmp.close()
    return tmp.name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--minutes",
        type=int,
        default=20,
        help="현재 시각 기준 최근 몇 분치를 수집할지 (기본 20분, GitHub Actions 15분 주기 + 여유분)",
    )
    args = parser.parse_args()

    now = datetime.now(KST)
    date_to = now.strftime("%Y%m%d%H%M%S")
    date_fr = (now - timedelta(minutes=args.minutes)).strftime("%Y%m%d%H%M%S")

    devices = load_device_list()
    print(f"총 {len(devices)}개소 수집 시작 ({date_fr} ~ {date_to})")

    print("techpalm 로그인 중...")
    session_cookie = login_and_get_session_cookie()
    print("로그인 성공, 세션 쿠키 획득 완료")

    success_count = 0
    failed_devices = []

    for device in devices:
        device_key = device["device_key"]
        location = device["location"]
        try:
            csv_path = download_device_csv(session_cookie, device_key, date_fr, date_to)
            uploaded = parse_and_upload_csv(
                csv_path,
                device_key,
                extra_fields={"region": device.get("region"), "location": location},
            )
            os.remove(csv_path)
            print(f"[OK] {device_key} ({location}) - {uploaded}건 업로드")
            success_count += 1
        except Exception as e:
            print(f"[실패] {device_key} ({location}) - {e}")
            failed_devices.append({"device_key": device_key, "location": location, "error": str(e)})

    print("\n===== 수집 완료 =====")
    print(f"성공: {success_count}/{len(devices)}")
    if failed_devices:
        print(f"실패: {len(failed_devices)}건")
        for f in failed_devices:
            print(f"  - {f['device_key']} ({f['location']}): {f['error']}")
        # 실패 건이 있으면 GitHub Actions에서 이 실행을 "실패"로 표시하고 싶을 때 아래 줄 활성화
        # sys.exit(1)


if __name__ == "__main__":
    main()
