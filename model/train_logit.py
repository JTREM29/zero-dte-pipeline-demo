import os
import sqlite3

import joblib
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

DB_PATH = os.getenv("DB_PATH", "db/tnt.db")
MODEL_DIR = os.getenv("MODEL_DIR", "model/artifacts")
HORIZON_MIN = int(os.getenv("MODEL_HORIZON_MIN", "5"))
TF = os.getenv("MODEL_TF", "1m")
SYMBOLS = os.getenv("MODEL_SYMBOLS", "SPY,QQQ,IWM").split(",")


def load_prices(symbol: str, limit: int = 8000) -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as conn:
        query = (
            """
            SELECT ts, close
            FROM prices
            WHERE symbol=? AND tf=?
            ORDER BY ts DESC
            LIMIT ?
            """
        )
        df = pd.read_sql_query(query, conn, params=(symbol, TF, limit))
    if df.empty:
        return df
    df = df.sort_values("ts")
    df["close"] = df["close"].astype(float)
    return df


def make_features(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    px = df["close"]
    ret1 = px.pct_change()
    ret5 = px.pct_change(5)
    ret15 = px.pct_change(15)

    vol10 = ret1.rolling(10).std()
    vol30 = ret1.rolling(30).std()

    mom10 = px / px.rolling(10).mean() - 1.0
    mom30 = px / px.rolling(30).mean() - 1.0

    up = ret1.clip(lower=0).rolling(14).mean()
    down = (-ret1.clip(upper=0)).rolling(14).mean()
    rs = up / (down + 1e-9)
    rsi = 100 - (100 / (1 + rs))

    features = pd.DataFrame(
        {
            "ret1": ret1,
            "ret5": ret5,
            "ret15": ret15,
            "vol10": vol10,
            "vol30": vol30,
            "mom10": mom10,
            "mom30": mom30,
            "rsi": rsi,
        }
    )

    fwd = px.shift(-HORIZON_MIN)
    target = (fwd > px).astype(int)

    data = features.join(target.rename("y")).dropna()
    return data.drop(columns=["y"]), data["y"]


def main() -> None:
    os.makedirs(MODEL_DIR, exist_ok=True)

    x_list: list[pd.DataFrame] = []
    y_list: list[pd.Series] = []

    for sym in [s.strip().upper() for s in SYMBOLS]:
        df = load_prices(sym)
        if df.empty:
            continue
        X, y = make_features(df)
        if len(X) < 500:
            continue
        x_list.append(X)
        y_list.append(y)

    if not x_list:
        raise SystemExit("Not enough data to train.")

    X = pd.concat(x_list, axis=0)
    y = pd.concat(y_list, axis=0)

    pipeline = Pipeline(
        [
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(max_iter=2000)),
        ]
    )

    pipeline.fit(X, y)

    path = os.path.join(MODEL_DIR, "logit.joblib")
    joblib.dump(pipeline, path)
    print(f"[OK] trained logistic model on {len(X)} rows; saved to {path}")


if __name__ == "__main__":
    main()
