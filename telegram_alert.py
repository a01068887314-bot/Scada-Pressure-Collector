# -*- coding: utf-8 -*-
"""
관말압력 Low / High 알람 — 판정은 여기 한 곳에서만 한다.

collect_all.py 가 15분마다 압력을 수집한 직후 check_and_notify() 를 호출한다.
  1) 기기별 "이번에 수집된 가장 최근 값 1개"로 판정
  2) 판정 결과 = "현재 알람 목록"을 Firestore  alarm_state/current  에 저장 (매 실행)
  3) 신규 알람 또는 해소가 있을 때만 텔레그램 발송 — 이때 "계속 중"인 알람도 함께 보냄
앱 ④ 수신 압력의 "압력 알람" 칸은 이 alarm_state/current 를 그대로 읽어서 같은 내용으로 표시한다.

판정 기준 (앱과 공통 — 바뀌면 여기만 수정, 앱은 저장된 기준값을 그대로 표시)
  - 오류값 : 0.5 kPa 미만        → 판정 제외 (기존 상태 유지)
  - Low    : 0.5 이상 1.7 미만
  - High   : 2.3 초과
  - 정상   : 1.7 이상 2.3 이하

알람 목록 규칙
  - 신규   : 정상 → Low/High, 또는 Low ↔ High 로 바뀐 경우 (발생 시각 = 그 값의 수신 시각)
  - 계속 중: 이전에도 같은 상태 → 최근값·수신 시각만 갱신, 발생 시각 유지
  - 해소   : Low/High → 정상  → 목록에서 빠지고 "해소"로 1번 알림
  - 이번에 값이 안 들어온 기기(수신 끊김)는 목록에 그대로 두고 "수신 끊김"으로 표시

alarm_state/current 문서 구조
  {
    "alarms": { "2208": {"status": "high", "pressure": 2.35, "timestamp": "2026-11-27T13:00:00",
                         "since": "2026-11-27T10:30:00", "location": "서원구 ..."} , ... },
    "judged_at": "2026-11-27T13:03:12",           # 판정 시각 (한국 시각)
    "criteria": {"error_min": 0.5, "low": 1.7, "high": 2.3},
    "updated_at": <server timestamp>
  }

환경변수 (GitHub Secrets 또는 로컬 .env)
  TELEGRAM_BOT_TOKEN  - 봇 토큰 (BotFather에서 발급)
  TELEGRAM_CHAT_ID    - 받을 대화방 번호. 여러 곳이면 콤마로 구분
  둘 중 하나라도 없으면 텔레그램 발송만 건너뜀 (알람 목록 저장은 계속 → 앱은 정상 표시)

PC에서 시험하기 (scada(techpalm) 폴더에서 명령창)
  python telegram_alert.py           → 시험 메시지 1개 발송 (텔레그램 연결 확인)
  python telegram_alert.py --status  → 지금 저장된 "현재 알람 목록"을 발송 (앱 ④ 알람 칸과 같은 내용)
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests

# ---------------- 판정 기준 (바뀌면 여기만 수정) ----------------
ERROR_MIN = 0.5
LOW = 1.7
HIGH = 2.3

STALE_MINUTES = 60  # 최근값 수신이 이보다 오래되면 "수신 끊김" 표시

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


# ---------------- 시각 표기 (앱과 같은 형식) ----------------
def _parse(ts_iso):
    try:
        return datetime.fromisoformat(str(ts_iso))
    except Exception:
        return None


def _fmt(ts_iso):
    """'2026-11-27T09:15:03' → '11/27 09:15'"""
    dt = _parse(ts_iso)
    return dt.strftime("%m/%d %H:%M") if dt else str(ts_iso or "-")


def _duration(since_iso, now):
    """발생 시각부터 지금까지 → '2시간 30분째' / '15분째' / '1일 3시간째'"""
    dt = _parse(since_iso)
    if not dt:
        return ""
    mins = max(0, int((now - dt).total_seconds() // 60))
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    if d:
        return f"{d}일 {h}시간째"
    if h:
        return f"{h}시간 {m}분째" if m else f"{h}시간째"
    return f"{m}분째"


def _is_stale(ts_iso, now):
    dt = _parse(ts_iso)
    return bool(dt) and (now - dt) > timedelta(minutes=STALE_MINUTES)


ICON = {"low": "🔴 Low ", "high": "🟠 High"}


def _p(v):
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "-"


def _alarm_lines(key, a, now, kind):
    """kind: "new" | "ongoing" """
    head = f"{ICON[a['status']]}  {key} {a.get('location') or ''}".rstrip()
    detail = f"   {_p(a.get('pressure'))} kPa · {_fmt(a.get('timestamp'))} 수신"
    if kind == "ongoing":
        detail += f" · {_fmt(a.get('since'))}부터 ({_duration(a.get('since'), now)})"
    lines = [head, detail]
    if _is_stale(a.get("timestamp"), now):
        lines.append(f"   ⚠ 수신 끊김 (마지막 {_fmt(a.get('timestamp'))})")
    return lines


def _resolved_lines(key, r):
    head = f"🟢 {key} {r.get('location') or ''}".rstrip()
    was = "Low" if r.get("was") == "low" else "High"
    period = f"{_fmt(r.get('since'))}~{_fmt(r.get('timestamp'))}" if r.get("since") else "해소"
    detail = f"   {_p(r.get('pressure'))} kPa · {_fmt(r.get('timestamp'))} 수신 · {was} {period}"
    return [head, detail]


def _sort_key(item):
    key, a = item
    p = a.get("pressure")
    try:
        p = float(p)
    except (TypeError, ValueError):
        p = 0.0
    # Low 먼저(낮은 순), 그다음 High(높은 순)
    return (0, p) if a.get("status") == "low" else (1, -p)


def build_message(alarms, new_keys, resolved, judged_at):
    """텔레그램 메시지 본문 (앱 ④ 알람 칸과 같은 내용·표현)"""
    now = _parse(judged_at) or now_kst()
    lines = [f"⚠ 관말압력 알람 ({_fmt(judged_at)} 판정)"]

    new_items = sorted([(k, alarms[k]) for k in new_keys if k in alarms], key=_sort_key)
    ongoing = sorted([(k, a) for k, a in alarms.items() if k not in new_keys], key=_sort_key)

    if new_items:
        lines += ["", "[신규]"]
        for k, a in new_items:
            lines += _alarm_lines(k, a, now, "new")
    if ongoing:
        lines += ["", "[계속 중]"]
        for k, a in ongoing:
            lines += _alarm_lines(k, a, now, "ongoing")
    if resolved:
        lines += ["", "[해소]"]
        for k, r in sorted(resolved.items()):
            lines += _resolved_lines(k, r)

    lows = sum(1 for a in alarms.values() if a.get("status") == "low")
    highs = sum(1 for a in alarms.values() if a.get("status") == "high")
    lines += [
        "",
        f"현재 알람: Low {lows} · High {highs}" if alarms else "✅ 현재 알람 없음",
        f"기준: Low {ERROR_MIN}~{LOW} 미만 / High {HIGH} 초과 (kPa)",
    ]
    return "\n".join(lines)


# ---------------- 상태 보관 (Firestore) ----------------
def _load_alarms(db):
    snap = db.collection(STATE_COLLECTION).document(STATE_DOC).get()
    if not snap.exists:
        return {}
    data = snap.to_dict() or {}
    if isinstance(data.get("alarms"), dict):
        return dict(data["alarms"])
    # 예전 형식({"status": {"2208": "high"}}) → 새 형식으로 옮김 (발생 시각은 알 수 없어 비워 둠)
    old = data.get("status") or {}
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
        latest_by_device: { "2188": {"pressure": 1.65, "timestamp": "2026-11-27T09:15:03", "location": "흥덕구 ..."} }
                          이번 수집에서 받은 기기별 가장 최근 값 (값이 없던 기기는 넣지 않음)
        db: firestore client (upload_firestore.get_firestore_client())
    Returns:
        신규 + 해소 건수
    """
    judged_at = now_kst().strftime("%Y-%m-%dT%H:%M:%S")
    prev = _load_alarms(db)
    alarms = {k: dict(v) for k, v in prev.items()}
    new_keys = []
    resolved = {}

    for key, info in latest_by_device.items():
        cur = classify(info.get("pressure"))
        if cur in (None, "error"):
            continue  # 값 없음 / 오류값 → 기존 상태 그대로
        before = prev.get(key)

        if cur in ("low", "high"):
            entry = {
                "status": cur,
                "pressure": info.get("pressure"),
                "timestamp": info.get("timestamp"),
                "location": info.get("location"),
            }
            if before and before.get("status") == cur:
                entry["since"] = before.get("since") or info.get("timestamp")  # 계속 중 → 발생 시각 유지
            else:
                entry["since"] = info.get("timestamp")  # 신규 (정상→Low/High, Low↔High)
                new_keys.append(key)
            alarms[key] = entry
        elif before:  # cur == "ok" 인데 이전에 알람 → 해소
            resolved[key] = {
                "was": before.get("status"),
                "since": before.get("since"),
                "pressure": info.get("pressure"),
                "timestamp": info.get("timestamp"),
                "location": info.get("location") or before.get("location"),
            }
            alarms.pop(key, None)

    # 판정 결과는 매번 저장 (앱 ④가 같은 내용을 표시하도록)
    _save_alarms(db, alarms, judged_at)

    if not new_keys and not resolved:
        print(f"[알람] 신규·해소 없음 (현재 알람 {len(alarms)}건)")
        return 0

    if not telegram_ready():
        print("[알람] 텔레그램 설정이 없어 발송을 건너뜁니다. (알람 목록 저장은 완료)")
        return len(new_keys) + len(resolved)

    sent = send_telegram(build_message(alarms, new_keys, resolved, judged_at))
    print(f"[알람] 신규 {len(new_keys)} · 해소 {len(resolved)} · 현재 {len(alarms)} → 발송 {sent}회")
    return len(new_keys) + len(resolved)


# ---------------- PC 시험용 ----------------
def _status_report():
    """지금 저장된 '현재 알람 목록'을 발송 (앱 ④ 알람 칸과 같은 내용). 상태는 바꾸지 않음."""
    from upload_firestore import get_firestore_client

    db = get_firestore_client()
    snap = db.collection(STATE_COLLECTION).document(STATE_DOC).get()
    data = (snap.to_dict() or {}) if snap.exists else {}
    alarms = _load_alarms(db)
    judged_at = data.get("judged_at") or now_kst().strftime("%Y-%m-%dT%H:%M:%S")
    text = build_message(alarms, [], {}, judged_at).replace("⚠ 관말압력 알람", "📋 관말압력 현재 알람 목록", 1)
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
