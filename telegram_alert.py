# -*- coding: utf-8 -*-
"""
관말압력 Low / High 텔레그램 알람.

collect_all.py 가 15분마다 압력을 수집한 직후 이 모듈의 check_and_notify() 를 호출한다.
기기별 "이번에 수집된 가장 최근 값 1개"로 판정하고, 상태가 바뀐 기기만 텔레그램으로 보낸다.

판정 기준 (앱 ④ 수신 압력과 동일)
  - 오류값 : 0.5 kPa 미만        → 알람 판정에서 제외 (상태 유지)
  - Low    : 0.5 이상 1.7 미만
  - High   : 2.3 초과
  - 정상   : 1.7 이상 2.3 이하

보내는 경우 (같은 알람이 15분마다 반복되지 않도록 "바뀔 때만")
  - 정상 → Low / High           : 🔴 / 🟠 알람
  - Low ↔ High                  : 해당 알람
  - Low / High → 정상           : 🟢 정상 복귀

직전 상태 보관: Firestore  alarm_state/current  문서 1건 ({ "2188": "low", ... })
  → 실행마다 읽기 1건, 상태가 바뀐 경우에만 쓰기 1건

환경변수 (GitHub Secrets 또는 로컬 .env)
  TELEGRAM_BOT_TOKEN  - 봇 토큰 (BotFather에서 발급)
  TELEGRAM_CHAT_ID    - 받을 대화방 번호. 여러 곳이면 콤마로 구분 (예: 9192269480,-4912345678)
  둘 중 하나라도 없으면 알람은 건너뛰고 수집은 정상 진행

PC에서 시험하기 (scada(techpalm) 폴더에서 명령창)
  python telegram_alert.py           → 시험 메시지 1개 발송 (텔레그램 연결 확인)
  python telegram_alert.py --status  → 지금 Low/High 인 기기 목록을 발송 (Firestore 읽기, 상태 기록은 안 바꿈)
"""

import os
import sys
import json
from datetime import datetime, timedelta, timezone

import requests

# ---------------- 판정 기준 (바뀌면 여기만 수정) ----------------
ERROR_MIN = 0.5
LOW = 1.7
HIGH = 2.3

KST = timezone(timedelta(hours=9))  # GitHub 서버는 세계 표준시(UTC, 한국보다 9시간 느림)라 한국 시각으로 고정


def now_kst():
    return datetime.now(KST).replace(tzinfo=None)


STATE_COLLECTION = "alarm_state"
STATE_DOC = "current"
DEVICE_LIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "device_list.json")


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
    if p < ERROR_MIN:
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


# ---------------- 메시지 문구 ----------------
def _fmt_time(ts_iso):
    """'2026-11-27T09:15:03' → '11/27 09:15'"""
    try:
        dt = datetime.fromisoformat(str(ts_iso))
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return str(ts_iso)


def _line(kind, key, info):
    head = {
        "low": "🔴 [Low]",
        "high": "🟠 [High]",
        "ok": "🟢 [정상 복귀]",
    }[kind]
    loc = info.get("location") or ""
    p = info.get("pressure")
    p_txt = f"{float(p):.2f}" if p is not None else "-"
    return f"{head} {key} {loc}\n   압력 {p_txt} kPa ({_fmt_time(info.get('timestamp'))} 수신)"


# ---------------- 상태 보관 (Firestore) ----------------
def _load_state(db):
    snap = db.collection(STATE_COLLECTION).document(STATE_DOC).get()
    if snap.exists:
        data = snap.to_dict() or {}
        return dict(data.get("status") or {})
    return {}


def _save_state(db, status):
    from firebase_admin import firestore

    db.collection(STATE_COLLECTION).document(STATE_DOC).set(
        {"status": status, "updated_at": firestore.SERVER_TIMESTAMP}
    )


# ---------------- 메인: 수집 직후 호출 ----------------
def check_and_notify(latest_by_device, db):
    """
    Args:
        latest_by_device: { "2188": {"pressure": 1.65, "timestamp": "2026-11-27T09:15:03", "location": "흥덕구 ..."} }
                          이번 수집에서 받은 기기별 가장 최근 값 (값이 없던 기기는 넣지 않음)
        db: firestore client (upload_firestore.get_firestore_client())
    Returns:
        보낸 알람 줄 수
    """
    if not telegram_ready():
        print("[알람] 텔레그램 설정이 없어 알람 확인을 건너뜁니다.")
        return 0

    prev = _load_state(db)
    new_state = dict(prev)
    lines_alarm = []
    lines_recover = []

    for key, info in sorted(latest_by_device.items()):
        cur = classify(info.get("pressure"))
        if cur in (None, "error"):
            continue  # 값 없음 / 오류값 → 상태 그대로 유지
        before = prev.get(key, "ok")
        if cur == before:
            continue
        new_state[key] = cur
        if cur in ("low", "high"):
            lines_alarm.append(_line(cur, key, info))
        elif before in ("low", "high"):
            lines_recover.append(_line("ok", key, info))

    lines = lines_alarm + lines_recover
    if lines:
        header = f"⚠ 관말압력 알람 ({now_kst().strftime('%m/%d %H:%M')} 확인)"
        footer = f"기준: Low {ERROR_MIN}~{LOW} 미만 / High {HIGH} 초과 (kPa)"
        sent = send_telegram("\n".join([header, ""] + lines + ["", footer]))
        print(f"[알람] 상태 변화 {len(lines)}건 → 발송 {sent}회")
    else:
        print("[알람] 상태 변화 없음")

    if new_state != prev:
        _save_state(db, new_state)
    return len(lines)


# ---------------- PC 시험용 ----------------
def _load_devices():
    with open(DEVICE_LIST_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _status_report():
    """지금 Low/High 인 기기 목록을 Firestore에서 읽어 발송 (상태 기록은 바꾸지 않음)."""
    from google.cloud.firestore_v1.field_path import FieldPath
    from upload_firestore import get_firestore_client, COLLECTION_NAME

    db = get_firestore_client()
    devices = _load_devices()
    since = (now_kst() - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    col = db.collection(COLLECTION_NAME)

    lows, highs, got = [], [], 0
    for d in devices:
        key = str(d["device_key"])
        q = (
            col.where(FieldPath.document_id(), ">=", col.document(f"{key}_{since}"))
            .where(FieldPath.document_id(), "<=", col.document(f"{key}_"))
        )
        last = None
        for doc in q.stream():
            v = doc.to_dict()
            if last is None or str(v.get("timestamp")) > str(last.get("timestamp")):
                last = v
        if not last:
            continue
        got += 1
        info = {"pressure": last.get("pressure"), "timestamp": last.get("timestamp"), "location": d.get("location")}
        st = classify(info["pressure"])
        if st == "low":
            lows.append(_line("low", key, info))
        elif st == "high":
            highs.append(_line("high", key, info))

    header = f"📋 관말압력 현재 상태 ({now_kst().strftime('%m/%d %H:%M')})"
    body = lows + highs if (lows or highs) else ["✅ 기준을 벗어난 기기가 없습니다"]
    footer = f"최근 3시간 수신 {got}/{len(devices)}개소 · Low {len(lows)} · High {len(highs)}"
    print("\n".join([header] + body + [footer]))
    send_telegram("\n".join([header, ""] + body + ["", footer]))


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
