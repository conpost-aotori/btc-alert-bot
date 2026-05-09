# BTC Alert Bot

5分おきに BTC 価格を監視し、急変時に**要因分析つき要約**を Discord に投稿する BOT。
GitHub Actions の cron で動く完全無料運用 (public repo 前提)。

## アーキテクチャ

```
GitHub Actions cron (*/5 min)
      ↓
①CoinGecko で価格取得 + ②Bybit で OHLC/Ticker/OI/Funding 取得
      ↓
③特徴量計算 (ATR / 5-15-60-1440min returns / move-per-ATR / OI Δ / funding)
      ↓
④Ring buffer に追記 (data/state.json, 最大 ~48h 分)
      ↓
⑤合成スコア判定 (robust z-score × 5 features)
       (履歴 <30 件なら hard fallback のみ)
      ↓ 発火時のみ
⑥要因分析 (並列実行)
   ├─ News              : The Block / CoinDesk / CoinTelegraph RSS
   ├─ Exchange announcements : Binance / Coinbase / Bybit / Kraken RSS
   ├─ Funding context   : Bybit Perp Funding Rate
   └─ Macro events      : ForexFactory (USD/CNY High-impact, ±2h, 4h cache)
      ↓
⑦Gemini 2.5-flash-lite で日本語3行要約 (新 google-genai SDK)
      ↓
⑧Discord Webhook + ローソク足チャート PNG (mplfinance)
      ↓
⑨配信成功時のみ cooldown 状態を更新 (方向別: 同方向90分 / 逆方向30分)
```

## 急変判定の合成スコア (Phase 1)

```
score =
  0.30 × move_per_atr_z   # 15min 移動 / ATR の z-score
+ 0.25 × atr_pct_z         # ATR% の異常度
+ 0.20 × volume_5bar_z     # 直近5本出来高の異常度
+ 0.15 × oi_drop_z         # OI 急減 (ロング清算プロキシ)
+ 0.10 × funding_abs_z     # |funding rate| の偏り

fire if:
  score ≥ 2.6
  AND |return_15m| ≥ 0.8%
  AND (atr_z ≥ 1.2 OR vol_z ≥ 1.5)

OR hard fallback:
  |return_15m| ≥ 1.5%
  OR |return_1h| ≥ 2.0%
  OR |return_24h| ≥ 5.0%
```

## コスト

| 項目 | 月額 |
|---|---|
| GitHub Actions (public repo) | $0 |
| CoinGecko / Bybit / RSS | $0 |
| Gemini 2.5-flash-lite | $0 (1500 RPD 無料枠、別バケット) |
| X API (現在無効化中) | $0 |
| Discord Webhook | $0 |
| **合計** | **$0** |

## ディレクトリ構成

```
src/btc_alert_bot/
├── main.py          # エントリポイント (orchestrator)
├── price.py         # CoinGecko 価格取得
├── market.py        # Bybit kline / ticker / OI / funding 取得
├── features.py      # ATR + robust z-score + 合成特徴量
├── detector.py      # 合成スコア判定 + cooldown + ring buffer 管理
├── analyzers.py     # 並列要因分析 (News / Exchange / Macro / Funding)
├── summarizer.py    # Gemini 日本語要約
├── chart.py         # mplfinance ローソク足 PNG
└── publishers.py    # Discord webhook + X tweepy

data/
├── state.json       # cooldown + feature_history (commit 対象)
└── macro_cache.json # ForexFactory レスポンスキャッシュ (4h TTL)

scripts/
└── test_discord.py  # Discord 単体 E2E テスト

.github/workflows/
└── alert.yml        # 5分おき cron
```

## セットアップ

### 1. リポジトリ作成 & push

```bash
gh repo create btc-alert-bot --public --source=. --remote=origin
git add .
git commit -m "Initial commit"
git push -u origin main
```

### 2. GitHub Secrets 登録

`.env` から一括登録:

```bash
gh secret set -f .env
```

### 3. 動作確認

GitHub の Actions タブから **"BTC Alert Bot"** → **Run workflow** で手動実行。
最初の数時間は feature_history が30件貯まらないので **hard fallback のみ**で動作します
(±1.5% / 15min, ±2% / 1h, ±5% / 24h)。

## ローカル動作確認

```bash
pip install -e .

# 急変なしの正常パス確認 (履歴を貯めるだけ)
python -m btc_alert_bot.main

# 強制発火 + Discord 投稿テスト (X はスキップ)
python scripts/test_discord.py
```

`.env` の `DRY_RUN=true` で投稿せずログ確認のみ可能。

## チューニング

`src/btc_alert_bot/detector.py` の定数で挙動を調整:

- `SCORE_WEIGHTS` ... 各特徴量の重み (合計 1.0)
- `FIRE_SCORE_MIN = 2.6` ... 合成スコア発火閾値
- `FIRE_RETURN_15M_MIN_PCT = 0.8` ... 15分リターン下限
- `HARD_FALLBACK_*` ... 履歴不足時の単純閾値
- `COOLDOWN_SAME_DIR_MIN = 90` / `COOLDOWN_OPP_DIR_MIN = 30` ... 方向別cooldown

## 設計上の割り切り (Phase 1)

- **WebSocket 不採用**: GitHub Actions は常時接続できないため、5〜15分遅延を受容
- **CoinPost 除外**: 翻訳遅延が大きく一次ソースとしては使わない
- **CoinGlass 清算データ無し**: 無料枠で実用不可 → OI drop / volume / wick から推定 (清算プロキシ)
- **Nansen / X Search 未統合**: API有料化で現状コスト無料の範疇外
- **オンチェーン分析未統合**: 無料源が信頼できないため Phase 2 以降で検討

## Phase 2 以降の候補

- Deribit options で IV / term structure / skew を要因分析に追加
- SQLite で alert/factor 履歴を蓄積、類似パターン検索
- FRED API でマクロ背景情報 (DXY/US金利) を日次更新
- WebSocket 化 (Oracle Cloud Always Free 移行)
