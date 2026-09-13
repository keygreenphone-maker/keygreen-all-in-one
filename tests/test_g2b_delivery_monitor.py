"""
scripts/g2b_delivery_monitor.py 회귀/버그 테스트.

실제 텔레그램 봇 토큰이나 실제 채팅 ID는 절대 사용하지 않는다 - 아래의
모든 토큰/chat_id는 합성 값이며, requests.post는 전부 목(mock) 처리해
네트워크 호출이 발생하지 않는다.

실행: python -m unittest discover -s tests -v
"""

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import g2b_delivery_monitor as monitor  # noqa: E402


class FakeResponse:
    """requests.Response를 흉내내는 최소 스텁."""

    def __init__(self, status_code, json_body=None, text=None):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text if text is not None else json.dumps(json_body or {})

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body


class SendTelegramTest(unittest.TestCase):
    """텔레그램 발송 + supergroup 전환(migrate_to_chat_id) 대응."""

    def setUp(self):
        self._orig_token = monitor.TELEGRAM_BOT_TOKEN
        self._orig_chat_id = monitor.TELEGRAM_CHAT_ID
        monitor.TELEGRAM_BOT_TOKEN = "synthetic-test-token"
        monitor.TELEGRAM_CHAT_ID = "-100111222333"

    def tearDown(self):
        monitor.TELEGRAM_BOT_TOKEN = self._orig_token
        monitor.TELEGRAM_CHAT_ID = self._orig_chat_id

    def test_success_on_first_try_does_not_retry(self):
        responses = [FakeResponse(200, {"ok": True})]
        with patch("g2b_delivery_monitor.requests.post", side_effect=responses) as mock_post:
            result = monitor.send_telegram("hello")

        self.assertTrue(result)
        self.assertEqual(mock_post.call_count, 1)

    def test_supergroup_migration_retries_once_with_new_chat_id(self):
        """RED였던 케이스: 실제 실패 사례(그룹 -> 슈퍼그룹 전환)를 그대로 재현."""
        migration_response = FakeResponse(
            400,
            {
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: group chat was upgraded to a supergroup chat",
                "parameters": {"migrate_to_chat_id": -1004357899209},
            },
        )
        success_response = FakeResponse(200, {"ok": True})

        with patch(
            "g2b_delivery_monitor.requests.post",
            side_effect=[migration_response, success_response],
        ) as mock_post:
            result = monitor.send_telegram("잔디보호매트 신규 납품요구 테스트")

        self.assertTrue(result, "새 chat_id로 재시도하면 최종적으로 성공 처리되어야 한다")
        self.assertEqual(mock_post.call_count, 2, "정확히 1회만 재시도해야 한다")

        first_call, second_call = mock_post.call_args_list
        self.assertEqual(first_call.kwargs["data"]["chat_id"], "-100111222333")
        self.assertEqual(second_call.kwargs["data"]["chat_id"], "-1004357899209")

        # chat_id 외의 payload/토큰/URL은 그대로여야 한다.
        self.assertEqual(first_call.args[0], second_call.args[0])
        for key in ("text", "parse_mode", "disable_web_page_preview"):
            self.assertEqual(
                first_call.kwargs["data"][key],
                second_call.kwargs["data"][key],
            )

    def test_supergroup_migration_retry_failure_still_fails(self):
        """재시도까지 실패하면 기존 실패 처리(다음 실행 재시도 정책)를 유지해야 한다."""
        migration_response = FakeResponse(
            400,
            {"ok": False, "parameters": {"migrate_to_chat_id": -1004357899209}},
        )
        still_failing_response = FakeResponse(400, {"ok": False, "description": "still bad"})

        with patch(
            "g2b_delivery_monitor.requests.post",
            side_effect=[migration_response, still_failing_response],
        ) as mock_post:
            result = monitor.send_telegram("hello")

        self.assertFalse(result)
        self.assertEqual(mock_post.call_count, 2)

    def test_non_migration_400_does_not_retry(self):
        """migrate_to_chat_id가 없는 다른 400 오류는 기존처럼 재시도 없이 실패해야 한다."""
        bad_request = FakeResponse(400, {"ok": False, "description": "Bad Request: chat not found"})

        with patch("g2b_delivery_monitor.requests.post", side_effect=[bad_request]) as mock_post:
            result = monitor.send_telegram("hello")

        self.assertFalse(result)
        self.assertEqual(mock_post.call_count, 1)

    def test_non_json_error_body_does_not_retry(self):
        """응답 본문이 JSON이 아니면 migrate_to_chat_id를 읽을 수 없으니 재시도하지 않는다."""
        broken = FakeResponse(400, json_body=None, text="<html>502</html>")

        with patch("g2b_delivery_monitor.requests.post", side_effect=[broken]) as mock_post:
            result = monitor.send_telegram("hello")

        self.assertFalse(result)
        self.assertEqual(mock_post.call_count, 1)


class DedupAndFormattingRegressionTest(unittest.TestCase):
    """텔레그램 수정과 무관한 기존 기능(중복 방지 키, 메시지 포맷)이 안 깨졌는지 확인."""

    def test_get_item_key_includes_all_dedup_fields(self):
        item = {
            "cntrctDlvrReqNo": "20240001",
            "cntrctDlvrReqChgOrd": "00",
            "prdctIdntNo": "23260735",
            "prdctSno": "1",
        }
        self.assertEqual(monitor.get_item_key(item), "20240001-00-23260735-1")

    def test_get_item_key_distinguishes_product_lines_within_same_request(self):
        base = {"cntrctDlvrReqNo": "20240001", "cntrctDlvrReqChgOrd": "00", "prdctIdntNo": "23260735"}
        key1 = monitor.get_item_key({**base, "prdctSno": "1"})
        key2 = monitor.get_item_key({**base, "prdctSno": "2"})
        self.assertNotEqual(key1, key2)

    def test_format_telegram_message_new_item_smoke(self):
        item = {
            "prdctIdntNoNm": "잔디보호매트",
            "dminsttNm": "테스트기관",
            "dminsttRgnNm": "서울",
            "corpNm": "키그린(주)",
            "prdctUnit": "EA",
            "cntrctDlvrReqNm": "테스트 계약",
            "cntrctDlvrReqNo": "20240001",
            "cntrctDlvrReqChgOrd": "00",
            "prdctQty": "10",
            "prdctAmt": "1000000",
            "cntrctDlvrReqDate": "20240101",
        }
        msg = monitor.format_telegram_message(item)
        self.assertIn("잔디보호매트 신규 납품요구", msg)
        self.assertIn("납품요구번호: 20240001", msg)
        self.assertNotIn("[경쟁사]", msg)

    def test_format_telegram_message_competitor_tag(self):
        item = {
            "prdctIdntNoNm": "잔디보호매트",
            "corpNm": "타사(주)",
            "cntrctDlvrReqNo": "20240002",
            "cntrctDlvrReqChgOrd": "00",
        }
        msg = monitor.format_telegram_message(item)
        self.assertIn("[경쟁사]", msg)


if __name__ == "__main__":
    unittest.main()
