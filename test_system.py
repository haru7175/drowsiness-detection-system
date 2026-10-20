import unittest
import json
import time
from main import app
from core.state import state
from navigation.spot_navigator import (
    extract_destination_query,
    is_pure_affirmation,
    find_best_spot,
    SPOT_CACHE
)
from voice.dialog_agent import sanitize_ai_response, call_ollama_with_timeout
from storage.db_manager import save_detailed_log, get_recent_logs

class SystemIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_destination_extraction(self):
        self.assertEqual(extract_destination_query("近くのセブンに行きたい"), "セブン")
        self.assertEqual(extract_destination_query("スタバに案内して"), "スタバ")
        self.assertEqual(extract_destination_query("ラーメン食べたい"), "ラーメン")
        self.assertTrue(is_pure_affirmation("はい"))
        self.assertTrue(is_pure_affirmation("お願いします"))
        self.assertTrue(is_pure_affirmation("おねがいします"))
        self.assertTrue(is_pure_affirmation("頼みます"))
        self.assertFalse(is_pure_affirmation("ファミマに行きたい"))

    def test_sanitize_ai_response(self):
        long_text = "AI: 承知しました、ドライバーさん！安全運転のためにセブン-イレブンへ案内しますね。温かいコーヒーでゆっくり休んでくださいね。無理は禁物ですよ。"
        sanitized = sanitize_ai_response(long_text)
        self.assertTrue(len(sanitized) <= 65)
        self.assertFalse(sanitized.startswith("AI:"))
        self.assertFalse(sanitized.startswith("承知しました"))

    def test_status_endpoint(self):
        state.score = 35.0
        state.is_distracted = True
        state.in_post_alert = True
        res = self.client.get('/api/status')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["score"], 35.0)
        self.assertTrue(data["is_distracted"])
        self.assertTrue(data["in_post_alert"])
        self.assertIn("score", data)
        self.assertIn("ear", data)
        self.assertIn("camera_connected", data)

    def test_logging_and_recent_logs(self):
        test_action = f"Unit Test Action {int(time.time())}"
        save_detailed_log(test_action)
        time.sleep(0.5)  # 非同期ワーカースレッドの書き込み待機
        logs = get_recent_logs(limit=5)
        self.assertTrue(len(logs) > 0)
        self.assertEqual(logs[0]["action"], test_action)

        res = self.client.get('/api/logs?limit=5')
        self.assertEqual(res.status_code, 200)
        logs_json = res.get_json()
        self.assertEqual(logs_json["status"], "success")
        self.assertTrue(len(logs_json["logs"]) > 0)

    def test_spot_cache(self):
        # 疑似キャッシュエントリー作成
        test_key = ("テストスポット", 35.37, 136.76)
        SPOT_CACHE[test_key] = (time.time(), {"name": "テスト店舗", "lat": 35.37, "lng": 136.76, "distance_km": 0.5})
        from navigation.spot_navigator import search_real_spot_nearby
        cached = search_real_spot_nearby("テストスポット", 35.37, 136.76)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["name"], "テスト店舗")

if __name__ == '__main__':
    unittest.main()
