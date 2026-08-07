"""
키그린 업무포털 - 종합쇼핑몰 잔디보호매트 납품요구 신규건 모니터링

매일 GitHub Actions로 실행되어:
1. 공공데이터포털 "조달청_나라장터쇼핑몰 품목정보 서비스" API에서
   잔디보호매트 물품식별번호(PRODUCT_IDS) 목록의 최근 조달내역을 조회
2. 이전에 이미 알림 보낸 건(seen_ids)은 제외하고 신규건만 추림
3. 신규건이 있으면 텔레그램으로 알림 + Firestore(keygreen-63efc)에 저장
   -> 업무포털 앱에서 자동으로 노출됨
4. 이번에 처리한 납품요구번호 목록을 Firestore에 저장해서 다음 실행 때 중복 방지

API 명세: https://www.data.go.kr/data/15129471/openapi.do
  (참고문서: 조달청_OpenAPI참고자료_조달청_나라장터쇼핑몰품목정보서비스_1.3.docx)
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
G2B_API_BASE = "https://apis.data.go.kr/1230000/at/ShoppingMallPrdctInfoService"
OPERATION = "getSpcifyPrdlstPrcureInfoList"  # 쇼핑몰 특정품목 조달내역 (물품식별번호로 조회 가능)
# 서비스키는 포털의 "디코딩" 키를 써야 합니다. 인코딩 키(%2B 등)를 넣어도
# 아래 unquote로 한 번 풀어주므로 둘 다 동작합니다.
G2B_SERVICE_KEY = urllib.parse.unquote(os.environ.get("G2B_SERVICE_KEY", ""))

# ── 텔레그램 설정 ──────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Firebase 설정 ──────────────────────────────────────────────────────
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
FIRESTORE_COLLECTION = "deliveryRequests"
FIRESTORE_STATE_DOC = "g2bMonitorState/seenIds"  # 처리 이력을 담아둘 문서 경로


def fetch_delivery_requests(product_id: str, begin_date: str, end_date: str, debug: bool = False):
    """물품식별번호 하나에 대한 조달(납품요구) 내역을 조회한다."""
    params = {
        "serviceKey": G2B_SERVICE_KEY,
        "pageNo": "1",
        "numOfRows": "100",
        "type": "json",
        "inqryDiv": "1",            # 1=계약납품요구일자 기준 (최대 12개월)
        "inqryBgnDate": begin_date,  # yyyyMMdd
        "inqryEndDate": end_date,    # yyyyMMdd
        "inqryPrdctDiv": "3",       # 3=물품규격명 조회 (prdctIdntNo 사용 가능)
        "prdctIdntNo": product_id,
    }
    url = f"{G2B_API_BASE}/{OPERATION}"
    resp = requests.get(url, params=params, timeout=20)

    if debug:
        print(f"\n[DEBUG] GET {resp.url}")
        print(f"[DEBUG] status={resp.status_code}")
        print(resp.text[:3000])

    # 오픈API는 잘못된 엔드포인트/서비스키일 때 4xx + 에러 본문을 돌려주므로
    # raise_for_status 전에 본문을 남겨야 원인을 알 수 있다.
    try:
        data = resp.json()
    except ValueError:
        print(f"[ERROR] JSON이 아닌 응답 (status={resp.status_code}, product_id={product_id}):")
        print(resp.text[:1000])
        resp.raise_for_status()
        raise

    if "OpenAPI_ServiceResponse" in data:
        hdr = data["OpenAPI_ServiceResponse"].get("cmmMsgHeader", {})
        raise RuntimeError(
            f"오픈API 오류: {hdr.get('errMsg')} / {hdr.get('returnAuthMsg')} "
            f"(code={hdr.get('returnReasonCode')})"
        )

    header = data.get("response", {}).get("header", {})
    if header.get("resultCode") not in ("00", None):
        raise RuntimeError(f"오픈API 오류: {header.get('resultCode')} {header.get('resultMsg')}")

    items = data.get("response", {}).get("body", {}).get("items")
    if not items:
        return []
    if isinstance(items, dict):
        item = items.get("item")
        if not item:
            return []
        return item if isinstance(item, list) else [item]
    if isinstance(items, list):
        return items
    return []


def load_seen_ids(db):
    doc = db.document(FIRESTORE_STATE_DOC).get()
    if doc.exists:
        return set(doc.to_dict().get("ids", []))
    return set()


def save_seen_ids(db, seen_ids):
    # Firestore 문서 하나에 너무 많이 쌓이지 않도록 최근 3000개만 유지
    trimmed = sorted(seen_ids)[-3000:]
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
    name = item.get("prdctIdntNoNm") or item.get("dtilPrdctClsfcNoNm") or "품명미상"
    org = item.get("dminsttNm") or "-"
    region = item.get("dminsttRgnNm") or ""
    company = item.get("corpNm") or "-"
    qty = item.get("prdctQty") or "-"
    unit = item.get("prdctUnit") or ""
    amt = item.get("prdctAmt") or "-"
    date = item.get("cntrctDlvrReqDate") or "-"
    no = item.get("cntrctDlvrReqNo") or "-"
    title = item.get("cntrctDlvrReqNm") or "-"
    return (
        f"🌱 <b>잔디보호매트 신규 납품요구</b>\n"
        f"품목: {name}\n"
        f"계약명: {title}\n"
        f"수요기관: {org} ({region})\n"
        f"업체: {company}\n"
        f"수량/금액: {qty}{unit} / {amt}원\n"
        f"납품요구일자: {date}\n"
        f"납품요구번호: {no}"
    )


def get_item_key(item: dict) -> str:
    """중복 판단용 고유키 (납품요구번호 + 변경차수 + 물품식별번호)"""
    no = item.get("cntrctDlvrReqNo") or ""
    ord_ = item.get("cntrctDlvrReqChgOrd") or ""
    pid = item.get("prdctIdntNo") or ""
    return f"{no}-{ord_}-{pid}"


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
