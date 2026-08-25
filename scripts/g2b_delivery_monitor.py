"""
키그린 업무포털 - 종합쇼핑몰 잔디보호매트 납품요구 신규건 모니터링

매일 GitHub Actions로 실행되어:
1. 공공데이터포털 "조달청_나라장터쇼핑몰 품목정보 서비스" API에서
   잔디보호매트 물품식별번호(PRODUCT_IDS) 목록의 최근 조달내역을 조회
2. 이전에 이미 알림 보낸 건(seen_ids)은 제외하고 신규건만 추림
3. 신규건이 있으면 텔레그램으로 알림 + Firestore(keygreen-63efc)에 저장
   -> 업무포털 앱에서 자동으로 노출됨. 신규건이 없어도 "점검했다"는 짧은
   확인 메시지를 보낸다 - 하루 3번(09/13/17시) 도는데, 조용하면 실행 자체가
   안 됐는지 정말 신규가 없는 건지 구분이 안 되기 때문.
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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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
OPERATION = "getSpcifyPrdlstPrcureInfoList"  # 쇼핑몰 특정품목 조달내역
# 세부품명 "잔디보호매트" 한 번으로 전 업체 납품요구를 받아온 뒤 PRODUCT_IDS로 거른다.
# 물품식별번호로 31번 나눠 부르면 그만큼 타임아웃을 만날 확률이 올라간다.
DTIL_PRDCT_CLSFC_NO = "3012189301"
# 서비스키는 포털의 "디코딩" 키를 써야 합니다. 인코딩 키(%2B 등)를 넣어도
# 아래 unquote로 한 번 풀어주므로 둘 다 동작합니다.
G2B_SERVICE_KEY = urllib.parse.unquote(os.environ.get("G2B_SERVICE_KEY", ""))

# GitHub Actions(미국 리전)에서 apis.data.go.kr로 직접 붙으면 간헐적으로
# ConnectTimeout이 난다. 두 값이 다 설정돼 있으면 서울 리전에서 도는 프록시
# (kg-dashboard/pages/api/g2b-proxy.js)를 거쳐서 호출한다. 둘 중 하나라도
# 없으면(로컬 실행 등) 예전처럼 apis.data.go.kr을 직접 부른다.
G2B_PROXY_URL = os.environ.get("G2B_PROXY_URL", "")
PROXY_SECRET = os.environ.get("PROXY_SECRET", "")

# apis.data.go.kr은 해외(GitHub Actions) IP에서 접속이 간헐적으로 끊긴다.
# 재시도 없이는 이 한 번의 실패로 그날 알림이 통째로 날아간다.
_session = requests.Session()
_session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=4, connect=4, read=2, backoff_factor=3,   # 대기 0s, 6s, 12s, 24s
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)))

# ── 텔레그램 설정 ──────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── Firebase 설정 ──────────────────────────────────────────────────────
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
FIRESTORE_COLLECTION = "deliveryRequests"
FIRESTORE_STATE_DOC = "g2bMonitorState/seenIds"  # 처리 이력을 담아둘 문서 경로


def _fetch_page(begin_date: str, end_date: str, page: int, debug: bool = False):
    """한 페이지를 조회해 (items, totalCount)를 돌려준다."""
    params = {
        "serviceKey": G2B_SERVICE_KEY,
        "pageNo": str(page),
        "numOfRows": "100",
        "type": "json",
        "inqryDiv": "1",            # 1=계약납품요구일자 기준 (최대 12개월)
        "inqryBgnDate": begin_date,  # yyyyMMdd
        "inqryEndDate": end_date,    # yyyyMMdd
        "inqryPrdctDiv": "2",       # 2=세부품명 조회
        "dtilPrdctClsfcNo": DTIL_PRDCT_CLSFC_NO,
    }
    use_proxy = bool(G2B_PROXY_URL and PROXY_SECRET)
    if use_proxy:
        url = G2B_PROXY_URL
        resp = _session.get(
            url,
            params={**params, "op": OPERATION},
            headers={"x-proxy-key": PROXY_SECRET},
            timeout=(15, 30),
        )
    else:
        url = f"{G2B_API_BASE}/{OPERATION}"
        resp = _session.get(url, params=params, timeout=(15, 30))

    if debug:
        print(f"\n[DEBUG] GET {resp.url}")
        print(f"[DEBUG] status={resp.status_code}")
        print(resp.text[:3000])

    # 오픈API는 잘못된 엔드포인트/서비스키일 때 4xx + 에러 본문을 돌려주므로
    # raise_for_status 전에 본문을 남겨야 원인을 알 수 있다.
    try:
        data = resp.json()
    except ValueError:
        print(f"[ERROR] JSON이 아닌 응답 (status={resp.status_code}, page={page}):")
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

    body = data.get("response", {}).get("body", {})
    total = int(body.get("totalCount") or 0)

    items = body.get("items")
    if not items:
        return [], total
    if isinstance(items, dict):
        item = items.get("item")
        if not item:
            return [], total
        return (item if isinstance(item, list) else [item]), total
    if isinstance(items, list):
        return items, total
    return [], total


def fetch_delivery_requests(begin_date: str, end_date: str, debug: bool = False):
    """세부품명 '잔디보호매트' 전체 조달내역을 페이지 끝까지 모아서 돌려준다."""
    # 성공한 실행의 로그만 봐서는 프록시를 탔는지 직접 호출했는지 알 방법이
    # 없었다(에러가 나야만 URL을 찍었음). 매번 찍어서 눈으로 바로 확인되게 한다.
    if G2B_PROXY_URL and PROXY_SECRET:
        print(f"[INFO] G2B API 호출 경로: 프록시 ({G2B_PROXY_URL})")
    else:
        print(f"[INFO] G2B API 호출 경로: 직접 호출 ({G2B_API_BASE}) - "
              f"G2B_PROXY_URL/PROXY_SECRET 미설정")
    collected, page = [], 1
    while True:
        items, total = _fetch_page(begin_date, end_date, page, debug=(debug and page == 1))
        collected.extend(items)
        if not items or len(collected) >= total:
            return collected
        page += 1


def load_seen_ids(db):
    doc = db.document(FIRESTORE_STATE_DOC).get()
    if doc.exists:
        return set(doc.to_dict().get("ids", []))
    return set()


def save_seen_ids(db, seen_ids):
    # Firestore 문서 하나에 너무 많이 쌓이지 않도록 최근 3000개만 유지
    trimmed = sorted(seen_ids)[-3000:]
    db.document(FIRESTORE_STATE_DOC).set({"ids": trimmed, "updatedAt": datetime.datetime.utcnow().isoformat()})


def send_telegram(text: str) -> bool:
    """전송 성공 여부를 돌려준다. 실패를 삼키면 알림이 안 온 걸 알 수 없다."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=10)
    if resp.status_code != 200:
        print(f"[ERROR] 텔레그램 전송 실패: {resp.status_code} {resp.text}")
        return False
    return True


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _fmt_num(v, signed: bool = False) -> str:
    """천 단위 구분자를 넣는다. 숫자가 아니면 원본을 그대로 돌려준다."""
    n = _to_int(v)
    if n is None:
        return str(v) if v not in (None, "") else "-"
    return f"{n:+,}" if signed else f"{n:,}"


def _fmt_date(v) -> str:
    s = str(v or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else (s or "-")


def format_heartbeat_message(begin_date: str, end_date: str, total_checked: int, our_count: int) -> str:
    """신규건이 0건일 때 보내는 짧은 점검 완료 메시지.
    하루 3번 도는데 조용하면 "확인했는데 없었다"와 "실행이 아예 안 됐다"를
    구분할 수 없어서, 매 실행마다 결과를 남긴다."""
    kst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    return (
        f"✅ <b>납품요구 점검 완료</b> · 신규 없음\n"
        f"{kst:%Y-%m-%d %H:%M} KST\n"
        f"조회범위 {_fmt_date(begin_date)}~{_fmt_date(end_date)}"
        f" (전체 {total_checked}건 중 우리 품목 {our_count}건)"
    )


def format_telegram_message(item: dict) -> str:
    name = item.get("prdctIdntNoNm") or item.get("dtilPrdctClsfcNoNm") or "품명미상"
    org = item.get("dminsttNm") or "-"
    region = item.get("dminsttRgnNm") or ""
    company = item.get("corpNm") or "-"
    unit = item.get("prdctUnit") or ""
    title = item.get("cntrctDlvrReqNm") or "-"
    no = item.get("cntrctDlvrReqNo") or "-"
    chg = (item.get("cntrctDlvrReqChgOrd") or "00").strip()

    # 변경차수 건은 수량/금액이 "증액 후 누적"이라 그대로 보여주면 신규 발주로 오해된다.
    # 이번에 실제로 움직인 양(증감)을 앞세우고 누적은 참고로 붙인다.
    is_change = chg not in ("", "00")
    incdec = _to_int(item.get("incdecQty"))

    # 텔레그램 Bot API의 HTML 모드는 굵게·기울임·링크 정도만 지원하고
    # 글자색은 아예 못 바꾼다 - 대신 아이콘과 [경쟁사] 표시로 구분한다.
    # 폰 알림은 첫 줄(헤더)만 미리보기에 뜨므로 여기서 갈라줘야 의미가 있다.
    is_ours = "키그린" in company
    tag = "" if is_ours else "[경쟁사] "

    if not is_change:
        icon = "🌱" if is_ours else "👀"
        header = f"{icon} <b>{tag}잔디보호매트 신규 납품요구</b>"
    elif incdec is not None and incdec < 0:
        kind = "전량취소" if _to_int(item.get("prdctQty")) == 0 else "감액"
        header = f"⚠️ <b>{tag}잔디보호매트 납품요구 [변경 {chg}차 {kind}]</b>"
    elif incdec is not None and incdec > 0:
        icon = "🌱" if is_ours else "👀"
        header = f"{icon} <b>{tag}잔디보호매트 납품요구 [변경 {chg}차 증액]</b>"
    else:
        icon = "🌱" if is_ours else "👀"
        header = f"{icon} <b>{tag}잔디보호매트 납품요구 [변경 {chg}차]</b>"

    lines = [
        header,
        f"품목: {name}",
        f"계약명: {title}",
        f"수요기관: {org} ({region})" if region else f"수요기관: {org}",
        f"업체: {company}",
    ]
    if is_change:
        lines.append(f"증감: {_fmt_num(item.get('incdecQty'), signed=True)}{unit}"
                     f" / {_fmt_num(item.get('incdecAmt'), signed=True)}원")
        lines.append(f"누적: {_fmt_num(item.get('prdctQty'))}{unit}"
                     f" / {_fmt_num(item.get('prdctAmt'))}원"
                     f" (최초 {_fmt_date(item.get('IntlCntrctDlvrReqDate'))})")
    else:
        lines.append(f"수량/금액: {_fmt_num(item.get('prdctQty'))}{unit}"
                     f" / {_fmt_num(item.get('prdctAmt'))}원")
    lines.append(f"납품요구일자: {_fmt_date(item.get('cntrctDlvrReqDate'))}")
    lines.append(f"납품요구번호: {no}")
    return "\n".join(lines)


def get_item_key(item: dict) -> str:
    """중복 판단용 고유키 (납품요구번호 + 변경차수 + 물품식별번호 + 물품순번)

    같은 납품요구에 같은 물품식별번호가 규격/단가만 달리해 여러 줄 들어올 수 있어서
    물품순번까지 넣어야 한다. 빼면 두 번째 줄부터 알림이 조용히 누락된다.
    """
    no = item.get("cntrctDlvrReqNo") or ""
    ord_ = item.get("cntrctDlvrReqChgOrd") or ""
    pid = item.get("prdctIdntNo") or ""
    sno = item.get("prdctSno") or ""
    return f"{no}-{ord_}-{pid}-{sno}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true", help="첫 조회의 raw 응답을 출력")
    # API가 D-1 배치인 데다 실행이 며칠 연속 실패할 수 있어서 범위를 넉넉히 잡는다.
    # 좁게 잡으면 그 사이에 발생한 건이 범위 밖으로 밀려나 영영 알림이 안 간다.
    # 이미 보낸 건은 seen_ids가 걸러내므로 넓혀도 중복 발송은 없다.
    parser.add_argument("--days", type=int, default=10, help="조회할 최근 일수")
    parser.add_argument("--dry-run", action="store_true", help="Firestore/텔레그램에 실제로 쓰지 않고 콘솔에만 출력")
    parser.add_argument("--test-telegram", action="store_true",
                        help="텔레그램 시크릿만 점검 (테스트 메시지 1건 발송 후 종료)")
    parser.add_argument("--preview", type=int, metavar="N",
                        help="최근 N건을 텔레그램으로 미리보기 발송 "
                             "(Firestore 저장·중복이력 갱신 없음. 알림 형식 확인용)")
    args = parser.parse_args()

    # 실제로 알림을 보내는 모드에서만 텔레그램 설정을 요구한다 (--debug/--dry-run은 불필요)
    if (args.test_telegram or not (args.debug or args.dry_run)) and not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("[ERROR] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 없습니다.")
        sys.exit(1)

    if args.test_telegram:
        kst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        if not send_telegram(f"✅ 키그린 모니터링 설정 테스트\n{kst:%Y-%m-%d %H:%M} KST"):
            sys.exit(1)
        print("[INFO] 테스트 메시지 발송 성공 - 텔레그램 시크릿 정상")
        return

    if not G2B_SERVICE_KEY:
        print("[ERROR] G2B_SERVICE_KEY 환경변수가 없습니다.")
        sys.exit(1)

    today = datetime.date.today()
    begin_date = (today - datetime.timedelta(days=args.days)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")

    fetched = fetch_delivery_requests(begin_date, end_date, debug=args.debug)
    all_items = [it for it in fetched if it.get("prdctIdntNo") in PRODUCT_IDS]

    print(f"[INFO] 잔디보호매트 전체 {len(fetched)}건 중 "
          f"우리 품목 {len(all_items)}건 ({begin_date}~{end_date})")

    if args.debug:
        print("[DEBUG] --debug 모드: 여기까지만 확인하고 종료합니다.")
        return

    if args.dry_run:
        for item in all_items:
            print(format_telegram_message(item))
            print("---")
        return

    if args.preview:
        # 알림 형식만 확인하는 용도. Firestore와 중복이력을 건드리지 않으므로
        # 다음 정기 실행의 신규건 판정에 영향을 주지 않는다.
        targets = sorted(all_items, key=lambda x: x.get("cntrctDlvrReqDate") or "",
                         reverse=True)[:args.preview]
        print(f"[INFO] 미리보기 {len(targets)}건 발송 (저장/이력 갱신 없음)")
        failed = 0
        for item in targets:
            if not send_telegram(format_telegram_message(item)):
                failed += 1
        if failed:
            print(f"[ERROR] 미리보기 발송 실패 {failed}/{len(targets)}건")
            sys.exit(1)
        return

    # ── Firebase 연결 ──
    import firebase_admin
    from firebase_admin import credentials, firestore

    if not FIREBASE_SERVICE_ACCOUNT_JSON:
        print("[ERROR] FIREBASE_SERVICE_ACCOUNT 환경변수가 없습니다.")
        sys.exit(1)

    cred = credentials.Certificate(json.loads(FIREBASE_SERVICE_ACCOUNT_JSON))
    firebase_admin.initialize_app(cred)
    db = firestore.client(database_id="(default)")

    seen_ids = load_seen_ids(db)
    new_items = [item for item in all_items if get_item_key(item) not in seen_ids]

    print(f"[INFO] 신규건 {len(new_items)}건")

    failed = 0
    for item in new_items:
        key = get_item_key(item)
        seen_ids.add(key)

        # Firestore에 저장 -> 앱에서 표시
        db.collection(FIRESTORE_COLLECTION).add({
            **item,
            "notifiedAt": datetime.datetime.utcnow().isoformat(),
        })

        # 텔레그램 알림
        if not send_telegram(format_telegram_message(item)):
            failed += 1

    # 전송에 실패한 건이 있으면 seen_ids를 저장하지 않는다.
    # 저장해버리면 "이미 알림 보낸 건"으로 남아 다음 실행에서 영영 재시도되지 않는다.
    if failed:
        print(f"[ERROR] 텔레그램 전송 실패 {failed}/{len(new_items)}건 - 다음 실행에서 재시도합니다.")
        sys.exit(1)

    if new_items:
        save_seen_ids(db, seen_ids)
    else:
        # 신규건이 없어도 실행은 됐다는 걸 알려준다. 하루 3번 도는데 계속
        # 조용하면 "확인했는데 없었다"인지 "실행이 아예 안 됐다"인지 구분이
        # 안 된다. 실패해도 seen_ids와 무관해서 재시도할 게 없으니 exit(1)
        # 하지 않고 경고만 남긴다.
        if not send_telegram(format_heartbeat_message(begin_date, end_date, len(fetched), len(all_items))):
            print("[WARN] 점검 완료 메시지 전송 실패 (다음 실행에서 다시 보내집니다)")

    print("[INFO] 완료")


if __name__ == "__main__":
    main()
