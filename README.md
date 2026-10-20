# AI居眠り検知＆マルチモーダル対話型運転支援システム
## Next-Gen Driver Monitoring & Multimodal Safety Assistant (`mobility_new`)

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![OpenCV](https://img.shields.io/badge/OpenCV-Computer%20Vision-green.svg)](https://opencv.org/)
[![MediaPipe](https://img.shields.io/badge/MediaPipe-Face%20Mesh-orange.svg)](https://developers.google.com/mediapipe)
[![Ollama](https://img.shields.io/badge/Ollama-Qwen2.5%207B-purple.svg)](https://ollama.com/)
[![Flask](https://img.shields.io/badge/Flask-Web%20Framework-black.svg)](https://flask.palletsprojects.com/)
[![License](https://img.shields.io/badge/License-MIT-brightgreen.svg)](#)

車載端末への実装を想定した、ドライバーモニタリングおよび対話型運転支援システムです。  
車載カメラからドライバーの表情・視線・頭部姿勢をリアルタイムにセンシングし、眠気や疲労を検知します。  
一方的な警告音のみに依存せず、ドライバーの要望（休憩スポット、コンビニ、カフェ等）をハンズフリーの音声対話でヒアリングし、現在地から最寄りの実在スポットへルート案内を行います。

---

## システム概要

```mermaid
flowchart TD
    subgraph Sensing ["1. リアルタイム生体センシング"]
        CAM["車載Webカメラ"] --> MP["MediaPipe Face Mesh (468点)"]
        MP --> EAR["EAR (開眼度・瞬目トラッキング)"]
        MP --> MAR["MAR (口開度・あくび検知)"]
        MP --> HP["Head Pose (Pitch/Roll うつむき・傾き検知)"]
        MP --> LOST["顔見失い・わき見検知 (DISTRACTION)"]
    end

    subgraph FSM ["2. ハイブリッド警戒ステートマシン"]
        EAR & MAR & HP & LOST --> Score["総合眠気スコア算出 (0〜100pt)"]
        Score --> Level{"状態判定"}
        Level -->|NORMAL| Safe["通常監視"]
        Level -->|POST_ALERT| Alert["注意警戒中 (急激リセット防止)"]
        Level -->|WARNING| Caution["疲労予兆 (HUD注意)"]
        Level -->|DANGER| Emergency["即時警告ブザー (0.05s)"]
        Emergency --> DialogQueue["非同期対話キュー"]
    end

    subgraph Dialog ["3. 音声対話エージェント"]
        DialogQueue --> Agent["対話タスク (STT / LLM / TTS)"]
        Agent --> STT["Google音声認識 (環境ノイズ適応)"]
        Agent --> LLM["Ollama / Qwen2.5 7B (タイムアウト保護 2.5s)"]
        Agent --> TTS["pyttsx3 常駐ワーカー (Windows COM管理)"]
    end

    subgraph Navigation ["4. スポット探索 & ナビゲーション連携"]
        Agent --> POI["実在店舗検索 (Nominatimキャッシュ)"]
        POI --> Route["OSRM 道路ルート算出"]
        Route --> GMaps["Google Maps ナビ連携 (ピン & 経路)"]
    end
```

---

## 主な機能

### 1. 生体センシング・姿勢推定
- **MediaPipe Face Mesh (468点)**: 3次元ランドマークからピクセル実距離を幾何計算。
- **EAR (Eye Aspect Ratio)**: 自然な瞬き（1〜8フレーム）を識別し、緩慢な瞬目や長時間の閉眼（1.2秒以上）のみを検知。
- **MAR (Mouth Aspect Ratio)**: 口角と唇の開き幅からあくびを検知し、疲労の予兆段階で介入。
- **頭部姿勢推定 (Pitch / Roll)**: 三角関係の幾何計算により、高速かつ安定してうつむきや傾きを算出。
- **動的適応キャリブレーション**: 起動時の個人基準値の学習に加え、走行中の照明変化やカメラ露出変化にEMA（指数移動平均）で追従。
- **わき見・脱力検知 (DISTRACTION)**: 顔未検出が2秒以上継続した際に前方注意を発報、4秒継続で危険警告。

### 2. 2段階ハイブリッド警告 & 警戒維持
- **即時警告ブザー**: 危険検知（DANGER）時に遅延なく高周波ブザー（2200Hz Beep）を鳴動。
- **警戒空白期間の防止 (POST_ALERT)**: 対話終了直後にスコアを0リセットせず、警戒スコア（25pt）を維持した上で開眼推移に応じて自然減衰。

### 3. 音声対話エージェント
- **ローカルLLM (Ollama / Qwen2.5 7B)**: 運転を妨げない簡潔な発話（60文字以内）を生成。
- **タイムアウト保護 (2.5秒)**: LLM推論遅延時はフォールバック応答へ即座に切り替え。
- **常駐型TTSエンジン**: Windows COM環境での競合を防ぐ専用ワーカースレッドおよび自動再初期化。
- **メインループ保護**: カメラ処理ループから対話処理をキューで分離し、映像描画のフレームレートを維持。

### 4. スポット探索 & ナビゲーション
- **発話クレンジング**: ドライバーの発話から不要語句を除去し、目的地や要望（コンビニ、道の駅、コーヒーなど）を抽出。
- **Nominatim API (キャッシュ付き)**: 現在地周辺の実店舗情報を検索。インメモリキャッシュによりAPIレート制限を回避。
- **OSRM ルート算出**: 道路ネットワークに沿った走行経路座標を取得し、地図上へ描画。

### 5. Webダッシュボード
- **テレメトリ表示**: EAR、MAR、Pitch、居眠りスコア（0〜100pt）をリアルタイムに可視化。
- **走行ログ閲覧**: SQLiteに記録された直近の生体検知・アラートログをモーダルで確認可能。

---

## モジュール構成

```
mobility_new/
├── core/                        # システム共通基盤
│   ├── config.py                # 定数・日本語フォント描画設定
│   └── state.py                 # システム状態管理（SystemState）
├── sensing/                     # 映像解析 & センシング
│   └── face_tracker.py          # Face Mesh、EAR/MAR、姿勢推定、MJPEG配信
├── voice/                       # 音声処理 & 対話エージェント
│   ├── tts_engine.py            # pyttsx3 常駐ワーカー & 警告ブザー
│   └── dialog_agent.py          # Ollama (タイムアウト保護付き) & STT対話
├── navigation/                  # ナビゲーション & 地図連携
│   └── spot_navigator.py        # スポット解決、Nominatim/OSRM検索
├── storage/                     # データ管理
│   └── db_manager.py            # SQLite非同期書き込みワーカー & ログ取得
├── templates/
│   └── index.html               # Webダッシュボード (Tailwind CSS, Google Maps)
├── main.py                      # Flaskサーバー & APIエントリーポイント
├── test_system.py               # ユニット・統合テスト
└── mobility_advanced.db         # SQLiteデータベース
```

---

## 動作要件

- **OS**: Windows 10 / 11 (64-bit)
- **Python**: 3.10 以上
- **ハードウェア**:
  - Webカメラ（またはUSBカメラ）
  - マイク・スピーカー
- **ローカルLLM**: [Ollama](https://ollama.com/)

---

## セットアップ手順

### 1. リポジトリのクローン
```bash
git clone https://github.com/haru7175/drowsiness-detection-system.git
cd drowsiness-detection-system
```

### 2. 仮想環境の作成と依存関係のインストール
```powershell
python -m venv env
.\env\Scripts\activate

pip install opencv-python mediapipe flask pyttsx3 SpeechRecognition requests numpy pillow python-dotenv ollama pywin32
```

### 3. Ollama モデルの準備
```powershell
ollama pull qwen2.5:7b
```

### 4. 環境変数の設定（任意）
Google Maps JavaScript API を利用する場合は、ルートディレクトリに `.env` を作成してください。  
（未設定の場合でも Google Maps Embed モードで動作します）

```env
GMAPS_API_KEY=your_google_maps_api_key_here
```

---

## 起動方法

### サーバー起動
```powershell
.\env\Scripts\python main.py
```

ブラウザで `http://127.0.0.1:5000` にアクセスします。  
起動直後の100フレームで基準値のキャリブレーションが行われます。

### テスト実行
```powershell
.\env\Scripts\python test_system.py
```

---

## ライセンス

本プロジェクトは MIT License のもとで公開されています。
