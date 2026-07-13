"""
키그린 업무포털 - 종합쇼핑몰 잔디보호매트 납품요구 신규건 모니터링

매일 GitHub Actions로 실행되어:
1. 공공데이터포털 "조달청_종합쇼핑몰 품목정보서비스" API에서
   잔디보호매트 물품식별번호(PRODUCT_IDS) 목록의 최근 납품요구 내역을 조회
2. 이전에 이미 알림 보낸 건(seen_ids)은 제외하고 신규건만 추림
3. 신규건이 있으면 텔레그램으로 알림 + Firestore(keygreen-63efc)에 저장
   -> 업무포털 앱에서 자동으로 노출됨
4. 이번에 처리한 납품요구번호 목록을 Firestore에 저장해서 다음 실행 때 중복 방지

※ 최초 실행 전 확인 필요:
   - G2B_API_URL / OPERATION 이름, 파라미터명은 실제 Swagger 문서 기준으로
     한 번 검증이 필요합니다. --debug 옵션으로 raw 응답을 먼저 확인하세요.
   - python scripts/g2b_delivery_monitor.py --debug
"""

import os
import sys
import json
import argparse
import datetime
import urllib.parse
import requests

# ── 잔디보호매트 물품식별번호 (앱 index.html의 DATA 배열에서 추출) ──────────
# 앱에 새 제품이 추가되면 이 리스트도 같이 업데이트해야 신규 제품의
# 납품요구 건도 잡힙니다.
PRODUCT_IDS = [
    "23260735", "23267132", "23267133", "23311790", "23390341",
    "23647621", "23682803", "24118913", "24276012", "24382494",
    "24384511", "24422058", "24436951", "24436953", "24673829",
    "24707999", "24817304", "24902279", "25032194", "25032195",
    "25032196", "25223859", "25232574", "25232575", "25409697",
    "25417838", "25678010", "25678011", "25705742", "25731041",
    "26193811",
]

# ── 공공데이터포털 API 설정 ────────────────────────────────────────────
G2B_API_BASE = "http://apis.data.go.kr/1230000/ScshdPrdlstInfoService"
OPERATION = "getDlvrReqInfoList"  # ※ 실제 오퍼레이션명 검증 필요 (Swagger 확인)
G2B_SERVICE_KEY = os.environ.get("G2B_SERVICE_KEY", "")

# ── 텔레그램 설정 ──────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Firebase 설정 ──────────────────────────────────────────────────────
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
FIRESTORE_COLLECTION = "deliveryRequests"
FIRESTORE_STATE_DOC = "g2bMonitorState/seenIds"  # 처리 이력을 담아둘 문서 경로


def fetch_delivery_requests(product_id: str, begin_date: str, end_date: str, debug: bool = False):
    """물품식별번호 하나에 대한 납품요구 목록을 조회한다."""
    params = {
        "serviceKey": G2B_SERVICE_KEY,
        "pageNo": "1",
        "numOfRows": "100",
        "type": "json",
        "inqryDiv": "2",          # 조회구분: 2=납품요구접수일자 기준 (요검증)
        "inqryBgnDt": begin_date,  # yyyyMMdd
        "inqryEndDt": end_date,    # yyyyMMdd
        "prdctIdntNo": product_id,
    }
    url = f"{G2B_API_BASE}/{OPERATION}"
    resp = requests.get(url, params=params, timeout=20)

    if debug:
        print(f"\n[DEBUG] GET {resp.url}")
        print(f"[DEBUG] status={resp.status_code}")
        print(resp.text[:3000])

    resp.raise_for_status()
    data = resp.json()

    # 응답 구조는 검증 후 아래 경로를 실제 키로 맞춰야 합니다.
    try:
        items = data["response"]["body"]["items"]
        if items is None or items == "":
            return []
        if isinstance(items, dict) and "item" in items:
            item = items["item"]
            return item if isinstance(item, list) else [item]
        if isinstance(items, list):
            return items
        return []
    except (KeyError, TypeError) as e:
        print(f"[WARN] 응답 구조 파싱 실패 (product_id={product_id}): {e}")
        print(json.dumps(data, ensure_ascii=False)[:1000])
        return []


def load_seen_ids(db):
    doc = db.document(FIRESTORE_STATE_DOC).get()
    if doc.exists:
        return set(doc.to_dict().get("ids", []))
    return set()


def save_seen_ids(db, seen_ids):
    # Firestore 문서 하나에 너무 많이 쌓이지 않도록 최근 3000개만 유지
    trimmed = list(seen_ids)[-3000:]
    db.document(FIRESTORE_STATE_DOC).set({"ids": trimmed, "updatedAt": datetime.datetime.utcnow().isoformat()})


def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] 텔레그램 설정이 없어 알림을 건너뜁니다.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=10)
    if resp.status_code != 200:
        print(f"[WARN] 텔레그램 전송 실패: {resp.status_code} {resp.text}")


def format_telegram_message(item: dict) -> str:
    name = item.get("prdlstNm") or item.get("품명") or "품명미상"
    org = item.get("dmndInsttNm") or item.get("수요기관명") or "-"
    region = item.get("dmndInsttRgnNm") or item.get("수요기관소재시도") or ""
    company = item.get("corpNm") or item.get("업체명") or "-"
    qty = item.get("dlvrQty") or item.get("납품수량") or "-"
    amt = item.get("dlvrAmt") or item.get("납품금액") or "-"
    date = item.get("dlvrReqRcptDate") or item.get("납품요구접수일자") or "-"
    no = item.get("dlvrReqNo") or item.get("납품요구번호") or "-"
    return (
        f"🌱 <b>잔디보호매트 신규 납품요구</b>\n"
        f"품명: {name}\n"
        f"수요기관: {org} ({region})\n"
        f"업체: {company}\n"
        f"수량/금액: {qty} / {amt}원\n"
        f"접수일자: {date}\n"
        f"납품요구번호: {no}"
    )


def get_item_key(item: dict) -> str:
    """중복 판단용 고유키 (납품요구번호 + 물품순번 등)"""
    no = item.get("dlvrReqNo") or item.get("납품요구번호") or ""
    seq = item.get("dlvrPrdlstSn") or item.get("납품요구물품순번") or ""
    return f"{no}-{seq}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="첫 조회의 raw 응답을 출력")
    parser.add_argument("--days", type=int, default=3, help="조회할 최근 일수 (D-1 배치 특성상 여유있게 조회)")
    parser.add_argument("--dry-run", action="store_true", help="Firestore/텔레그램에 실제로 쓰지 않고 콘솔에만 출력")
    args = parser.parse_args()

    if not G2B_SERVICE_KEY:
        print("[ERROR] G2B_SERVICE_KEY 환경변수가 없습니다.")
        sys.exit(1)

    today = datetime.date.today()
    begin_date = (today - datetime.timedelta(days=args.days)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")

    all_items = []
    for i, pid in enumerate(PRODUCT_IDS):
        items = fetch_delivery_requests(pid, begin_date, end_date, debug=(args.debug and i == 0))
        all_items.extend(items)

    print(f"[INFO] 총 {len(all_items)}건 조회됨 (물품식별번호 {len(PRODUCT_IDS)}개, {begin_date}~{end_date})")

    if args.debug:
        print("[DEBUG] --debug 모드: 여기까지만 확인하고 종료합니다.")
        return

    if args.dry_run:
        for item in all_items:
            print(format_telegram_message(item))
            print("---")
        return

    # ── Firebase 연결 ──
    import firebase_admin
    from firebase_admin import credentials, firestore

    if not FIREBASE_SERVICE_ACCOUNT_JSON:
        print("[ERROR] FIREBASE_SERVICE_ACCOUNT 환경변수가 없습니다.")
        sys.exit(1)

    cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT_JSON))
    firebase_admin.initialize_app(cred)
    db = firestore.client()

    seen_ids = load_seen_ids(db)
    new_items = [item for item in all_items if get_item_key(item) not in seen_ids]

    print(f"[INFO] 신규건 {len(new_items)}건")

    for item in new_items:
        key = get_item_key(item)
        seen_ids.add(key)

        # Firestore에 저장 -> 앱에서 표시
        db.collection(FIRESTORE_COLLECTION).add({
            **item,
            "notifiedAt": datetime.datetime.utcnow().isoformat(),
        })

        # 텔레그램 알림
        send_telegram(format_telegram_message(item))

    if new_items:
        save_seen_ids(db, seen_ids)

    print("[INFO] 완료")


if __name__ == "__main__":
    main()
