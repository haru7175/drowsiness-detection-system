# アーキテクチャ分離とリファクタリング設計

現在のモノリシックな `main.py` を、保守性・テスタビリティ・並行処理の安定性に優れた疎結合アーキテクチャへと段階的にリファクタリングするための設計指針です。

---

## 推奨ディレクトリ構成

```text
mobility_new/
├── src/
│   ├── __init__.py
│   ├── config.py              # 環境変数、定数、閾値の一元管理
│   ├── core/                  # システム状態・イベントキュー
│   │   ├── __init__.py
│   │   ├── state.py           # SystemState（共有データ・スレッドロック）
│   │   └── event_bus.py       # キューによるイベント発行・購読
│   ├── sensing/               # 映像解析・居眠り検知
│   │   ├── __init__.py
│   │   ├── detector.py        # MediaPipe FaceMesh & ランドマーク処理
│   │   ├── metrics.py         # EAR計算、Pitch/Roll頭部姿勢推定
│   │   └── calibration.py     # ベースライン自動調整
│   ├── dialog/                # 音声認識・対話・LLM
│   │   ├── __init__.py
│   │   ├── stt.py             # 音声認識ワーカー
│   │   ├── tts.py             # 音声合成ワーカー（単一スレッド常駐）
│   │   └── llm_agent.py       # Ollama LLaMA 3連携・プロンプト管理
│   ├── navigation/            # 位置情報・スポット提案
│   │   ├── __init__.py
│   │   └── spot_finder.py     # 周辺スポット検索ロジック
│   ├── storage/               # データベース・ログ
│   │   ├── __init__.py
│   │   └── db.py              # SQLite操作、時系列ログ記録
│   └── web/                   # Webダッシュボード
│       ├── __init__.py
│       └── server.py          # Flaskアプリ定義・APIエンドポイント
├── templates/
│   └── index.html             # ダッシュボード画面
├── main.py                    # 各モジュールの起動・オーケストレーション
└── tests/                     # 単体テスト・シミュレーション
```

---

## 非同期イベントフロー

カメラのループ（映像解析）と音声・LLM推論を `event_bus`（キュー）で仲介します。

```text
[カメラ映像ループ (60fps)]
        │
        ▼ (眠気スコア判定)
[Score > 40検知] ───(イベント発行)───► [Event Queue]
                                             │
                       ┌─────────────────────┴─────────────────────┐
                       ▼                                           ▼
          [緊急ブザー音 / 即時警告]                     [対話エージェント (別スレッド)]
          (遅延 0.05秒で再生)                           ・STT 音声聞き取り
                                                       ・Ollama LLM 推論
                                                       ・TTS 音声再生
```

---

## 主な実装パターン

### 1. TTSワーカーの常駐化（Windows COM競合防止）
`pyttsx3` をメインループや毎回のスレッドで初期化せず、専用キューを監視する単一スレッドで常駐させます。

```python
import queue
import threading
import pyttsx3
import pythoncom

tts_queue = queue.Queue()

def tts_worker():
    pythoncom.CoInitialize()
    engine = pyttsx3.init()
    engine.setProperty('rate', 140)
    
    while True:
        text = tts_queue.get()
        if text is None: break
        try:
            engine.say(text)
            engine.runAndWait()
        except Exception as e:
            print(f"TTS Error: {e}")
        finally:
            tts_queue.task_done()
    pythoncom.CoUninitialize()

# 起動時に1度だけ実行
threading.Thread(target=tts_worker, daemon=True).start()

def speak(text):
    tts_queue.put(text)
```
