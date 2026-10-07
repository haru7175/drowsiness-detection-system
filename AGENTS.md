# プロジェクト開発ガイドライン（AGENTS.md）

このリポジトリは **AI居眠り検知＆マルチモーダル対話型運転支援システム**（`mobility_new`）です。

## 技術スタック
- **言語**: Python 3.x
- **画像解析**: OpenCV, MediaPipe Face Mesh
- **AI / LLM**: Ollama (LLaMA 3)
- **音声処理**: SpeechRecognition (Google STT), pyttsx3 (TTS)
- **Webサーバー**: Flask, HTML5, Tailwind CSS, Leaflet.js
- **データ管理**: SQLite3 (`mobility_advanced.db`)

## 開発・改修時の重要ルール
1. **メインスレッド・FPSの保護**: カメラ処理（`generate_frames`）内で長時間のブロッキング処理（LLM・STT・TTS）を行わないこと。
2. **スレッドセーフ**: Windows環境におけるCOM（`pythoncom`）競合に留意し、音声処理を安全に非同期化すること。
3. **専用スキルの活用**: 詳細な課題解決や設計指針については `.agents/skills/mobility-ai-dev/SKILL.md` を参照すること。
