# Garmin FIT to CSV 変換ツール (`fit2csv.py`)

[English](README.md) | **日本語**

Garmin公式の **`garmin-fit-sdk`** を使用して、GARMINデバイスが出力する `.fit` バイナリファイルを解析し、扱いやすい CSV 形式で出力するPythonスクリプトです。

---

## 🛠 特徴

### 1. Garmin Connect 準拠モード（デフォルト）
`python fit2csv.py activity.fit` のようにオプションなしで実行すると、Garmin Connect ウェブサイトからエクスポートしたCSVと同一のフォーマットで `<ファイル名>_connect.csv` を生成します。
- **ワークアウト階層構造の再現（案A）**:
  - 1kmオートラップ等で複数ラップに分割された同一ワークアウトステップは、親ステップ行（例: `Lap: 1 - 2`, `Lap: 1 - 9`）を先行出力し、その配下に子ラップ行を展開。
  - 単一ラップのステップはそのまま1行で出力。
  - 最下行にアクティビティ全体のサマリ行（`Interval: Summary`, `Step Type: --`, `Lap: --`）を付与。
- **最新ランニングダイナミクス対応 (HRM-600等)**:
  - **`Avg Step Speed Loss (cm/s)`**: 接地時の減速・ブレーキ量（cm/s）
  - **`Avg Step Speed Loss (%)`**: 走速度に対する減速損失比率（%）
- **高精度な表示整合性**:
  - **`Avg GAP` (Grade Adjusted Pace / 勾配調整ペース)**: 気圧高度計データ (`enhanced_altitude`) に対するノイズフィルタ処理と Minetti (2002) の生体力学エネルギー消費モデルに基づく推定により、Garmin Connect の Web GAP と秒単位（0〜2秒差）で整合。
  - 四捨五入（算術丸め）、M:SS形式ペース、W/kg（`-w/--weight` による体重上書き対応）、GCT Balance（`XX.X% L / YY.Y% R`）など、Garmin Connectの表示仕様と99%以上の完全一致を達成。
- **自動ラップ区間の任意間隔再計算 (`--re-lap`)**:
  - ウォッチの自動ラップ設定（通常1km）で記録されたデータを、ユーザーが指定した任意の間隔（例: `2000`, `2km`, `500m`）で再集計して出力可能。
  - ワークアウトのステップ構造（WS疾走/レストやインターバルの境界）を破壊せず、1秒ごとの時系列データ（`record`）から全31項目（心拍、パワー、Coggan NP、100.0%保証GCT Balance、上下動、歩幅、標高差、Step Speed Loss、Avg GAP等）を高精度に集約・再生成。
  - 端数ラップ（割り切れない余り区間）も末尾に保持。単位付き表記（`km`, `m`）および数値指定の両方に対応。

### 2. FIT 生データ抽出モード（`--type record`, `lap`, `split`, `session`, `all`）
FITバイナリから抽出されたメッセージを、余計な加工列を排した純粋なテーブル形式で出力します。
- `record`: 1秒ごとの時系列データ
- `lap`: ラップごとの生データ
- `split`: ウォーク/ラン検知やインターバルステップごとのスプリットデータ
- `session`: セッション概要生データ
- `all`: FITファイルに含まれる全メッセージを個別CSVに出力
- 未定義フィールドだった Step Speed Loss (`step_speed_loss`, `step_speed_loss_percent`) は適切な単位（cm/s, %）に正規化。
- 緯度経度は扱いやすい度数（degrees）に変換。

---

## 🚀 使い方

### 1. 準備 (仮想環境の有効化)
```bash
# 仮想環境をアクティベート
source .venv/bin/activate

# (初回のみ) 必要なパッケージのインストール
pip install garmin-fit-sdk pandas
```

### 2. コマンド実行例

#### ① Garmin Connect準拠のCSVを出力（デフォルト）
```bash
python fit2csv.py 24109883492_ACTIVITY.fit
# 出力: 24109883492_ACTIVITY_connect.csv
```

#### ② 体重を指定してGarmin Connect準拠のCSVを出力（W/kg計算用）
```bash
python fit2csv.py 24109883492_ACTIVITY.fit -w 57.9
```

#### ③ ラップ生データ (lap) をCSV出力
```bash
python fit2csv.py 24109883492_ACTIVITY.fit --type lap
# 出力: 24109883492_ACTIVITY_lap.csv
```

#### ④ 秒単位の時系列生データ (record) をCSV出力
```bash
python fit2csv.py 24109883492_ACTIVITY.fit --type record
# 出力: 24109883492_ACTIVITY_record.csv
```

#### ⑤ 全てのFITメッセージを生データとして一括出力
```bash
python fit2csv.py 24109883492_ACTIVITY.fit --type all
```

#### ⑥ ワークアウト名と実施日をファイル名に使用して変換
```bash
python fit2csv.py 24627552877_ACTIVITY.fit -n
# 出力形式: {ワークアウト名}_{日付}_{タイプ}.csv
# 出力例: [W8]Intervals(1kmx5425-_2026-10-06_connect.csv
# （空白およびファイル名に使用できない文字 \ / : * ? " < > | は自動除去されます）
```

#### ⑦ 自動ラップ区間を任意距離（例: 2km, 500m）で再計算して出力
```bash
# 2km間隔で再計算
python fit2csv.py 24651552170_ACTIVITY.fit --re-lap 2km
# （または --re-lap 2000）

# 500m間隔で細分化再計算
python fit2csv.py 24651552170_ACTIVITY.fit --re-lap 500m
```

#### ⑧ 出力ファイル名を直接指定する場合
```bash
python fit2csv.py sample.fit -o my_workout.csv
```

#### ⑨ フォルダ内の全FITファイルを一括変換
```bash
python fit2csv.py *.fit
```

#### ⑩ FITファイルに含まれるデータ種別（メッセージ一覧）を確認
```bash
python fit2csv.py sample.fit --list
```

---

## 📊 オプション一覧

| オプション | 説明 |
|---|---|
| `-t, --type <TYPE...>` | 出力種別を指定 (`connect` (デフォルト), `record`, `lap`, `split`, `session`, `activity`, `all` 等) |
| `--re-lap <DIST>` | Garmin Connect出力時、自動ラップ区間を指定した一定距離で再計算（例: `2000`, `2km`, `500m`） |
| `-n, --workout-name` | 出力ファイル名にワークアウト名と実施日（YYYY-MM-DD）を使用（`{ワークアウト名}_{日付}_{タイプ}.csv`） |
| `-w, --weight <KG>` | ユーザー体重(kg)を指定（FIT内部の登録体重を上書きしてW/kgを計算） |
| `-o, --output <FILE>` | 出力先CSVファイル名を指定（1ファイル・1種別変換時のみ） |
| `-l, --list` | FITファイル内のメッセージ一覧と件数を表示（変換は行わない） |
| `--include-unknown` | 生データ抽出時にFIT内部の未知フィールド (`unknown_*`) も除外せず出力 |
