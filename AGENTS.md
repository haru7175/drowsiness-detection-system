# プロジェクト開発ガイドライン（AGENTS.md）

このリポジトリは **AI居眠り検知＆マルチモーダル対話型運転支援システム**（`mobility_new`）です。

## 技術スタック
- **言語**: Python 3.x
- **画像解析**: OpenCV, MediaPipe Face Mesh
- **AI / LLM**: Ollama (Qwen2.5 7B / LLaMA 3)
- **音声処理**: SpeechRecognition (Google STT), pyttsx3 (TTS)
- **Webサーバー**: Flask, HTML5, Tailwind CSS, Google Maps API
- **データ管理**: SQLite3 (`mobility_advanced.db`)

## モジュール構成
- `core/`: 状態管理（`SystemState`）、定数、日本語フォント描画設定
- `sensing/`: MediaPipe Face Mesh、EAR/MAR幾何計算、姿勢推定、キャリブレーション、わき見検知
- `voice/`: TTS（常駐ワーカー & 自己修復）、対話エージェント（Ollamaタイムアウト保護 & STT）
- `navigation/`: スポット解決（Nominatimキャッシュ付き）、OSRMルート計算、Googleマップ連携
- `storage/`: SQLite非同期書き込みワーカー、走行生体ログ取得
- `main.py`: Flask Webサーバー、APIエンドポイント、ビデオストリーム配信

## 開発・改修時の重要ルール
1. **メインスレッド・FPSの保護**: カメラ処理（`generate_frames`）内で長時間のブロッキング処理（LLM・STT・TTS）を行わないこと。
2. **スレッドセーフ**: Windows環境におけるCOM（`pythoncom`）競合に留意し、音声処理を安全に非同期化すること。
3. **安全第一の警戒維持**: 対話終了直後にスコアを0リセットせず、POST_ALERT（警戒フェーズ）を通じて緩やかに回復させること。
4. **専用スキルの活用**: 詳細な課題解決や設計指針については `.agents/skills/mobility-ai-dev/SKILL.md` を参照すること。
