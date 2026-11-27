# -*- coding: utf-8 -*-
"""
관말압력 Low / High 알람 — 판정은 여기 한 곳에서만 한다.

collect_all.py 가 15분마다 압력을 수집한 직후 check_and_notify() 를 호출한다.
  1) 기기별 "이번에 수집된 가장 최근 값 1개"로 판정 → Low/High 기기 목록
  2) 판정 결과를 Firestore  alarm_state/current  에 저장 (앱 ④ "압력 알람" 칸이 그대로 표시)
  3) 텔레그램 발송
       - Low/High 기기가 있으면     → 매번 목록 발송 (같은 내용이 계속되어도 발송)
       - 없는데 직전에 알람이 있었으면 → "✅ 이상 없음" 1회 발송
       - 없고 직전에도 없었으면      → 보내지 않음
  ※ 이번 수집에서 값을 하나도 못 받았으면(SCADA 접속 실패 등) 판정하지 않고 직전 결과 유지

판정 기준 (앱과 공통 — 바뀌면 여기만 수정. 앱은 저장된 기준값을 그대로 표시)
  - 오류값 : 0.5 kPa 이하   → 판정 제외 (오류값을 거르기 위한 것으로, 화면·메시지에는 표시하지 않음)
  - Low    : 0.5 초과 1.7 미만  → 표시는 "Low 1.7 미만"
  - High   : 2.4 초과
  - 정상   : 1.7 이상 2.4 이하
  ※ 같은 기기가 계속 Low/High로 반복 발송된다면 기준값을 조정한다.

alarm_state/current 문서 구조
  {
    "alarms": { "2208": {"status": "high", "pressure": 2.35, "timestamp": "2026-11-27T13:30:00",
                         "location": "서원구 ..."}, ... },     # 이번 판정의 Low/High 기기만
    "judged_at": "2026-11-27T13:33:12",                       # 판정 시각 (한국 시각)
    "criteria": {"error_min": 0.5, "low": 1.7, "high": 2.4},
    "updated_at": <server timestamp>
  }

환경변수 (GitHub Secrets 또는 로컬 .env)
  TELEGRAM_BOT_TOKEN  - 봇 토큰 (BotFather에서 발급)
  TELEGRAM_CHAT_ID    - 받을 대화방 번호. 여러 곳이면 콤마로 구분
  둘 중 하나라도 없으면 텔레그램 발송만 건너뜀 (판정 결과 저장은 계속 → 앱은 정상 표시)

PC에서 시험하기 (scada(techpalm) 폴더에서 명령창)
  python telegram_alert.py           → 시험 메시지 1개 발송 (텔레그램 연결 확인)
  python telegram_alert.py --status  → 지금 저장된 판정 결과를 발송 (앱 ④ 알람 칸과 같은 내용)
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests

# ---------------- 판정 기준 (바뀌면 여기만 수정) ----------------
ERROR_MIN = 0.5
LOW = 1.7
HIGH = 2.4

KST = timezone(timedelta(hours=9))  # GitHub 서버는 세계 표준시(UTC, 한국보다 9시간 느림)라 한국 시각으로 고정


def now_kst():
    return datetime.now(KST).replace(tzinfo=None)


STATE_COLLECTION = "alarm_state"
STATE_DOC = "current"


# ---------------- .env 읽기 (PC 시험용, 추가 설치 없이) ----------------
def load_dotenv_simple(path=None):
    """같은 폴더의 .env 파일에서 KEY=VALUE 줄을 읽어 환경변수로 넣는다 (이미 있는 값은 그대로 둠)."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


# ---------------- 판정 ----------------
def classify(pressure):
    """압력값 → "error" | "low" | "high" | "ok" | None(값 없음)"""
    if pressure is None:
        return None
    try:
        p = float(pressure)
    except (TypeError, ValueError):
        return None
    if p != p:  # NaN
        return None
    if p <= ERROR_MIN:  # 0.5 이하 = 오류값
        return "error"
    if p < LOW:
        return "low"
    if p > HIGH:
        return "high"
    return "ok"


# ---------------- 텔레그램 발송 ----------------
def _chat_ids():
    raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    return [c.strip() for c in raw.replace(";", ",").split(",") if c.strip()]


def telegram_ready():
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN")) and bool(_chat_ids())


def send_telegram(text):
    """설정된 모든 대화방으로 메시지 발송. 텔레그램 1회 최대 4096자라 길면 나눠서 보냄.
    Returns: 성공한 발송 수"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_ids = _chat_ids()
    if not token or not chat_ids:
        print("[알람] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없어 발송을 건너뜁니다.")
        return 0

    chunks = []
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > 3800:
            chunks.append(buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        chunks.append(buf)

    ok = 0
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in chat_ids:
        for chunk in chunks:
            try:
                r = requests.post(url, data={"chat_id": chat_id, "text": chunk}, timeout=15)
                if r.status_code == 200 and r.json().get("ok"):
                    ok += 1
                else:
                    # 토큰이 로그에 남지 않도록 응답 내용만 출력
                    print(f"[알람] 발송 실패 (대화방 {chat_id}): {r.status_code} {r.text[:200]}")
            except Exception as e:
                print(f"[알람] 발송 오류 (대화방 {chat_id}): {type(e).__name__}")
    return ok


# ---------------- 메시지 (앱 ④ 알람 칸과 같은 표현) ----------------
def _fmt(ts_iso):
    """'2026-11-27T09:15:03' → '11/27 09:15'"""
    try:
        return datetime.fromisoformat(str(ts_iso)).strftime("%m/%d %H:%M")
    except Exception:
        return str(ts_iso or "-")


def _p(v):
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "-"


ICON = {"low": "🔴 Low ", "high": "🟠 High"}


def _sort_key(item):
    key, a = item
    try:
        p = float(a.get("pressure"))
    except (TypeError, ValueError):
        p = 0.0
    return (0, p) if a.get("status") == "low" else (1, -p)  # Low 먼저(낮은 순), 그다음 High(높은 순)


def _criteria_line():
    return f"기준: Low {LOW} 미만 / High {HIGH} 초과 (kPa)"


def build_message(alarms, judged_at, title="⚠ 관말압력 알람"):
    lines = [f"{title} ({_fmt(judged_at)} 판정)", ""]
    for key, a in sorted(alarms.items(), key=_sort_key):
        lines.append(f"{ICON[a['status']]}  {key} {a.get('location') or ''}".rstrip())
        lines.append(f"   {_p(a.get('pressure'))} kPa · {_fmt(a.get('timestamp'))} 수신")
    lows = sum(1 for a in alarms.values() if a.get("status") == "low")
    highs = sum(1 for a in alarms.values() if a.get("status") == "high")
    lines += ["", f"Low {lows} · High {highs}", _criteria_line()]
    return "\n".join(lines)


def build_clear_message(judged_at):
    return "\n".join([f"✅ 관말압력 이상 없음 ({_fmt(judged_at)} 판정)", "", "Low/High 기기가 없습니다.", _criteria_line()])


# ---------------- 판정 결과 보관 (Firestore) ----------------
def _load_alarms(db):
    snap = db.collection(STATE_COLLECTION).document(STATE_DOC).get()
    if not snap.exists:
        return {}
    data = snap.to_dict() or {}
    if isinstance(data.get("alarms"), dict):
        return dict(data["alarms"])
    old = data.get("status") or {}  # 예전 형식({"2208": "high"}) 호환
    return {k: {"status": v} for k, v in old.items() if v in ("low", "high")}


def _save_alarms(db, alarms, judged_at):
    from firebase_admin import firestore

    db.collection(STATE_COLLECTION).document(STATE_DOC).set(
        {
            "alarms": alarms,
            "judged_at": judged_at,
            "criteria": {"error_min": ERROR_MIN, "low": LOW, "high": HIGH},
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )


# ---------------- 메인: 수집 직후 호출 ----------------
def check_and_notify(latest_by_device, db):
    """
    Args:
        latest_by_device: { "2188": {"pressure": 1.65, "timestamp": "2026-11-27T13:30:00", "location": "흥덕구 ..."} }
                          이번 수집에서 받은 기기별 가장 최근 값 (값이 없던 기기는 넣지 않음)
        db: firestore client (upload_firestore.get_firestore_client())
    Returns:
        이번 판정의 Low/High 기기 수 (판정을 건너뛰면 -1)
    """
    if not latest_by_device:
        print("[알람] 이번 수집에서 받은 값이 없어 판정을 건너뜁니다 (직전 결과 유지).")
        return -1

    judged_at = now_kst().strftime("%Y-%m-%dT%H:%M:%S")
    had_alarm_before = bool(_load_alarms(db))

    alarms = {}
    for key, info in latest_by_device.items():
        cur = classify(info.get("pressure"))
        if cur in ("low", "high"):
            alarms[key] = {
                "status": cur,
                "pressure": info.get("pressure"),
                "timestamp": info.get("timestamp"),
                "location": info.get("location"),
            }

    _save_alarms(db, alarms, judged_at)

    if alarms:
        text = build_message(alarms, judged_at)
    elif had_alarm_before:
        text = build_clear_message(judged_at)
    else:
        print("[알람] Low/High 없음 (발송 없음)")
        return 0

    if not telegram_ready():
        print("[알람] 텔레그램 설정이 없어 발송을 건너뜁니다. (판정 결과 저장은 완료)")
        return len(alarms)

    sent = send_telegram(text)
    print(f"[알람] Low/High {len(alarms)}건{' (이상 없음 안내)' if not alarms else ''} → 발송 {sent}회")
    return len(alarms)


# ---------------- PC 시험용 ----------------
def _status_report():
    """지금 저장된 판정 결과를 발송 (앱 ④ 알람 칸과 같은 내용). 저장 내용은 바꾸지 않음."""
    from upload_firestore import get_firestore_client

    db = get_firestore_client()
    snap = db.collection(STATE_COLLECTION).document(STATE_DOC).get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    alarms = _load_alarms(db)
    judged_at = data.get("judged_at") or now_kst().strftime("%Y-%m-%dT%H:%M:%S")
    text = build_message(alarms, judged_at, "📋 관말압력 현재 판정") if alarms else build_clear_message(judged_at)
    print(text)
    send_telegram(text)


if __name__ == "__main__":
    load_dotenv_simple()
    if not telegram_ready():
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 를 .env 파일에 먼저 넣어주세요.")
        sys.exit(1)

    if "--status" in sys.argv:
        _status_report()
    else:
        n = send_telegram(
            "✅ 관말압력 알람 봇 연결 테스트입니다.\n이 메시지가 보이면 텔레그램 설정이 정상입니다."
        )
        print("시험 메시지 발송 성공!" if n else "시험 메시지 발송 실패 — 위 오류 내용을 확인해주세요.")
