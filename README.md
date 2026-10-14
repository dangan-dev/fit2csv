# Garmin FIT to CSV Converter (`fit2csv.py`)

**English** | [日本語](README_ja.md)

A high-fidelity Python tool leveraging Garmin's official **`garmin-fit-sdk`** to decode Garmin `.fit` binary activity files and export clean, structured, and highly accurate CSV data.

---

## 🛠 Key Features

### 1. Garmin Connect Conforming Mode (Default)
Running without options (`python fit2csv.py activity.fit`) generates `<filename>_connect.csv`, mirroring the exact schema, column headers, formatting rules, and row hierarchies produced by the Garmin Connect web export.

- **Workout Step Hierarchy Reconstruction**:
  - Automatically groups multi-lap segments split by auto-lap (e.g., 1km auto-laps during a 5km tempo run or long warmup).
  - Emits parent interval header rows (e.g., `Lap: 1 - 2`, `Lap: 1 - 9`) followed by indented child lap rows.
  - Retains single-lap steps as standalone rows without distortion.
  - Appends an activity-wide summary row at the bottom (`Interval: Summary`, `Step Type: --`, `Lap: --`).
- **Advanced Running Dynamics (HRM-600 / Running Dynamics Pod Support)**:
  - **`Avg Step Speed Loss (cm/s)`**: Ground braking deceleration per step.
  - **`Avg Step Speed Loss (%)`**: Deceleration speed loss ratio relative to running velocity.
- **High-Fidelity Display & Calculations**:
  - **`Avg GAP` (Grade Adjusted Pace)**: Combines 21-second moving-average smoothing on barometric elevation (`enhanced_altitude`) with the **Minetti (2002) biomechanical energy cost model** to estimate flat-equivalent pace, aligning with Garmin Connect web GAP within seconds (0–2s variance).
  - **100.0% Guaranteed Ground Contact Time Balance**: Derives right-foot balance from the measured left-foot percentage ($L\% + R\% \equiv 100.0\%$), eliminating rounding artifacts present in native Garmin exports (e.g. `49.8% L / 50.3% R`).
  - Strict arithmetic half-up rounding, `M:SS` pace representations, and custom user weight overrides (`-w / --weight`) for W/kg power calculations.
- **Arbitrary Interval Re-Lap Recalculation (`--re-lap`)**:
  - Re-aggregates activities recorded under default auto-lap settings (e.g., 1km) into custom target distances (e.g., `2000`, `2km`, `500m`).
  - **Preserves workout step boundaries**: Protects warmup, cooldown, and interval sprint/recovery segments from being broken.
  - Recomputes all 31 key performance metrics from 1Hz timeseries records (`record`), including HR, Power, Coggan Normalized Power (NP), Cadence, Vertical Oscillation, Stride Length, Step Speed Loss, and GAP.
  - Automatically handles fractional leftover distances at the end of segments.

### 2. Raw FIT Data Extraction Mode (`--type record`, `lap`, `split`, `session`, `all`)
Extracts clean, unformatted raw data tables directly from FIT binary messages, stripping UI-centric formatting strings while preserving analytical purity:
- `record`: 1-second timeseries records.
- `lap`: Native lap summary messages.
- `split`: Interval steps and auto-detected walk/run splits.
- `session`: Overall session summary metadata.
- `all`: Exports every present message type into individual CSV files.
- Automatically normalizes vendor-specific Step Speed Loss fields to standard physical units (`cm/s`, `%`) and converts semicircles to decimal degrees (`latitude`, `longitude`).

### 3. Smart Output Naming (`-n / --workout-name`)
Extracts the workout title and activity start date directly from the FIT file metadata to generate structured filenames:
- Output format: `{WorkoutName}_{Date}_{type}.csv` (e.g., `[W8]Intervals(1kmx5)_2026-10-06_connect.csv`).
- Sanitizes whitespace and filesystem-reserved characters (`\ / : * ? " < > |`) automatically.

---

## 🚀 Installation & Prerequisites

### Prerequisites
- Python 3.9 or higher

### Setup
```bash
# Clone repository
git clone https://github.com/dangan-dev/fit2csv.git
cd fit2csv

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install required packages
pip install -r requirements.txt
```

---

## 📖 Usage Examples

### 1. Standard Garmin Connect CSV Export (Default)
```bash
python fit2csv.py 24109883492_ACTIVITY.fit
# Generates: 24109883492_ACTIVITY_connect.csv
```

### 2. Override Weight for W/kg Power Calculations
```bash
python fit2csv.py 24109883492_ACTIVITY.fit -w 60.5
```

### 3. Extract Raw 1-Second Timeseries Records
```bash
python fit2csv.py 24109883492_ACTIVITY.fit --type record
# Generates: 24109883492_ACTIVITY_record.csv
```

### 4. Extract Raw Lap Messages
```bash
python fit2csv.py 24109883492_ACTIVITY.fit --type lap
# Generates: 24109883492_ACTIVITY_lap.csv
```

### 5. Export All FIT Message Tables
```bash
python fit2csv.py 24109883492_ACTIVITY.fit --type all
```

### 6. Auto-Name with Workout Title & Date
```bash
python fit2csv.py 24627552877_ACTIVITY.fit -n
# Generates: [W8]Intervals(1kmx5425-_2026-10-06_connect.csv
```

### 7. Re-Aggregate Auto-Laps into Custom Distances
```bash
# Resample auto-laps into 2km blocks
python fit2csv.py 24651552170_ACTIVITY.fit --re-lap 2km

# Resample into 500m blocks
python fit2csv.py 24651552170_ACTIVITY.fit --re-lap 500m
```

### 8. Batch Convert Multiple FIT Files
```bash
python fit2csv.py *.fit
```

### 9. Inspect Available Message Types in a FIT File
```bash
python fit2csv.py sample.fit --list
```

---

## ⚙️ CLI Options Reference

| Option | Description |
|---|---|
| `-t, --type <TYPE...>` | Output mode / message types (`connect` (default), `record`, `lap`, `split`, `session`, `activity`, `all`, etc.) |
| `--re-lap <DIST>` | Re-aggregates auto-lap intervals by target distance (e.g. `2000`, `2km`, `500m`) |
| `-n, --workout-name` | Sets output filename using workout name and date: `{WorkoutName}_{Date}_{type}.csv` |
| `-w, --weight <KG>` | Specifies athlete body weight in kg (overrides FIT profile weight for W/kg calculations) |
| `-o, --output <FILE>` | Custom output destination path (valid when converting single file and single type) |
| `-l, --list` | Lists all message types and message counts present in the FIT file without converting |
| `--include-unknown` | Retains raw unresolved FIT fields (`unknown_*`) in raw export mode |

---

## 🔬 Technical Implementation Details

### Minetti (2002) Energy Cost Model for GAP
Garmin's web interface calculates Grade Adjusted Pace (GAP) using metabolic cost equations. `fit2csv` smooths barometric altitude with a 21-second rolling window to filter out running gait vibration noise, computes instantaneous grade $i$, and evaluates metabolic cost $C(i)$ relative to flat running:
$$C_r(i) = \frac{155.4 i^5 - 30.4 i^4 - 32.8 i^3 + 43.7 i^2 + 19.1 i + 3.6}{3.6}$$
Equivalent flat pace is derived via:
$$v_{\text{flat}} = v_{\text{actual}} \times C_r(i)$$

### Ground Contact Time Balance Guarantee
Because Garmin FIT files record only the left-side percentage (`avg_stance_time_balance`), separate rounding can create sum mismatches ($100.1\%$). `fit2csv` rounds the left value to 1 decimal place and computes the right value strictly as:
$$\text{Right}\% = 100.0\% - \text{Left}\%$$
guaranteeing mathematical consistency across every lap and summary row.

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
