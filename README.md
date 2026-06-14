# BTC Alert Bot

BTC の急変を検知して **要因分析つき日本語要約** を Discord (および任意で X) に
配信する BOT。完全無料運用 (GitHub Actions cron + public repo) を前提に、
Phase 1 で土台を作り、Phase 2 で要因分析 / 履歴蓄積 / 動的閾値 / 類似検索 /
個人アカウント監視 まで広げました。

## アーキテクチャ全体像

```
GitHub Actions cron (*/5 min) ─► python -m btc_alert_bot.main
       │
       ├─ ① CoinGecko で価格スナップショット
       ├─ ② OKX 公開 API で OHLCV / OI / funding
       ├─ ③ ATR + 多重リターン + robust z-score で特徴量
       ├─ ④ data/state.json リングバッファに追記 (~48h 保持)
       │
       ├─ ⑤ 合成スコア判定 + 動的閾値
       │     score = 0.30·move_per_atr + 0.25·atr_pct
       │           + 0.20·volume_5bar + 0.15·oi_drop + 0.10·funding_abs
       │     fire if score≥2.6 AND |return_15m|≥adaptive_floor
       │              AND (atr_z≥1.2 OR vol_z≥1.5)
       │     hard fallback: |return_15m|≥1.5% / |return_1h|≥2% / |return_24h|≥5%
       │
       ▼ 発火時のみ
       │
       ├─ ⑥ 要因分析 (7並列、合計30s デッドライン)
       │     ├─ News           : The Block / CoinDesk / CoinTelegraph RSS
       │     ├─ X Monitor      : Nitter RSS (高シグナル個人アカウント)
       │     ├─ Exchange       : Binance / Coinbase / Bybit / Kraken
       │     ├─ Macro Event    : ForexFactory High-impact ±2h (4h cache)
       │     ├─ Derivatives    : OKX funding context
       │     ├─ Options        : Deribit ATM IV / Term Structure / 25Δ Skew / RV
       │     └─ Macro Background : FRED daily DXY/yields/VIX/FedFunds (12h cache)
       │
       ├─ ⑦ 類似パターン検索 (history.sqlite, top 3)
       ├─ ⑧ Gemini 2.5-flash-lite で日本語3行要約
       ├─ ⑨ Discord webhook + ローソク足チャート (mplfinance)
       └─ ⑩ history.sqlite に alert + factors を記録
```

## ディレクトリ構成

```
src/btc_alert_bot/
├── main.py          # エントリポイント (orchestrator)
├── price.py         # CoinGecko 価格取得
├── market.py        # OKX kline / ticker / OI / funding
├── features.py      # ATR + robust z-score + 動的閾値
├── detector.py      # 合成スコア判定 + 方向別cooldown + ring buffer
├── analyzers.py     # 7並列ファンアウト (デッドライン付き)
├── deribit.py       # Deribit options aggregates
├── fred.py          # FRED マクロ背景 (opt-in)
├── x_monitor.py     # Nitter RSS X account monitor (opt-in)
├── history.py       # SQLite alert DB + 類似検索 + CLI
├── summarizer.py    # Gemini 日本語要約
├── chart.py         # mplfinance ローソク足 PNG
└── publishers.py    # Discord webhook + X tweepy

data/
├── state.json       # cooldown + feature_history (commit対象)
├── macro_cache.json # ForexFactory レスポンスキャッシュ (4h TTL)
├── fred_cache.json  # FRED レスポンスキャッシュ (12h TTL)
└── history.sqlite   # alerts + factors (commit対象)

scripts/
└── test_discord.py  # Discord 単体 E2E テスト

.github/workflows/
└── alert.yml        # 5分おき cron
```

## 月額コスト

| 項目 | 月額 |
|---|---|
| GitHub Actions (public repo) | $0 (無制限) |
| CoinGecko / OKX / Bybit / Deribit / RSS | $0 |
| Gemini 2.5-flash-lite | $0 (1500 RPD 無料枠) |
| FRED API | $0 (無料登録のみ) |
| Discord Webhook / X API (free) | $0 |
| **合計** | **$0** |

## セットアップ

### 1. リポジトリ作成 + push

```bash
gh repo create btc-alert-bot --public --source=. --remote=origin
git add .
git commit -m "Initial commit"
git push -u origin main
```

### 2. Secrets 登録

```bash
gh secret set -f .env
```

または個別に:
```bash
gh secret set GEMINI_API_KEY --body "..."
gh secret set DISCORD_WEBHOOK_URL --body "..."
gh secret set FRED_API_KEY --body "..."         # 任意
gh secret set NITTER_ACCOUNTS --body "user1,user2"  # 任意
# X 投稿を有効化したい場合のみ:
gh secret set X_API_KEY --body "..."
gh secret set X_API_SECRET --body "..."
gh secret set X_ACCESS_TOKEN --body "..."
gh secret set X_ACCESS_SECRET --body "..."
```

### 3. 動作確認

GitHub の Actions タブから **"BTC Alert Bot"** → **Run workflow** で手動実行。
最初の数時間は feature_history (30件未満) のため hard fallback のみで動作します。

## ローカル開発

```bash
# 依存インストール
pip install -e .

# 通常の急変判定パスを実行 (発火しない予定)
python -m btc_alert_bot.main

# 強制発火して Discord 投稿テスト (X はスキップ)
python scripts/test_discord.py

# 履歴 DB 操作
python -m btc_alert_bot.history list                # 最新20件
python -m btc_alert_bot.history list --limit 100    # 100件
python -m btc_alert_bot.history show <id>           # 詳細表示
python -m btc_alert_bot.history init                # 空 DB 作成
```

`.env` の `DRY_RUN=true` で投稿なしのログ確認のみ可能。

## チューニングポイント

### `src/btc_alert_bot/detector.py`
- `SCORE_WEIGHTS` ... 各特徴量の重み (合計 1.0)
- `FIRE_SCORE_MIN = 2.6` ... 合成スコア発火閾値
- `FIRE_RETURN_15M_MIN_PCT = 0.8` ... 15分リターン下限のベース値
  - 実行時に `adaptive_return_floor()` で 0.5×〜2.0× にスケール
- `HARD_FALLBACK_*` ... 履歴不足時の単純閾値 (静的)
- `COOLDOWN_SAME_DIR_MIN = 90` / `COOLDOWN_OPP_DIR_MIN = 30` ... 方向別cooldown

### `src/btc_alert_bot/features.py`
- `ADAPTIVE_REFERENCE_ATR_PCT = 0.10` ... 動的閾値の基準ボラ
- `ADAPTIVE_SCALE_MIN/MAX` ... 動的閾値のクランプ範囲

### `src/btc_alert_bot/analyzers.py`
- `GATHER_FACTORS_DEADLINE_S = 30` ... 並列要因分析の wall-time 上限

### `src/btc_alert_bot/x_monitor.py`
- `TOTAL_BUDGET_S = 15` ... Nitter 全体予算
- `NITTER_TIMEOUT = 5` ... per-request タイムアウト

## 設計上の割り切り

### Phase 1 (土台)
- **WebSocket 不採用**: GitHub Actions 常時接続不可のため 5〜15分遅延を受容
- **CoinPost 除外**: 翻訳遅延が大きく一次ソースに不適
- **Bybit → OKX**: Bybit が AWS/GCP の IP を 403 ブロックするため OKX に切替
- **CoinGlass 清算データなし**: 無料枠で実用不可 → OI drop / volume / wick で清算プロキシ

### Phase 2 (要因分析強化)
- **Deribit Options**: ATM IV / 25Δ Skew (proxy) / Term Structure / RV を要因分析に追加
- **FRED マクロ背景**: opt-in、12h cache、API key 未設定時は silent skip
- **SQLite 履歴**: 発火時のみコミット (5min cron のノイズなし)
- **動的閾値 (Phase 2.5)**: 直近 ATR%中央値で `FIRE_RETURN_15M_MIN_PCT` を自動スケール
- **類似パターン検索 (Phase 2.5)**: Euclidean距離で過去アラート top3 を Gemini に渡す
- **Nitter RSS X監視**: opt-in、複数 instance fallback + sticky + 15s 総予算

### Phase 3 (常時接続化、現行の本番系)
- **AWS Lightsail** に WebSocket 版をデプロイ (詳細は `DEPLOYMENT.md`)
- 5min cron 版と同一の検知/分析/配信コードを再利用 (cron は現在 schedule 無効・手動のみ)
- 5〜15分遅延 → 数秒以内の即時検知へ

## トラブルシューティング

### Geminiクオータ枯渇
別モデルに切替: `.env` に `GEMINI_MODEL=gemini-1.5-flash` を追加。

### Discord 投稿が届かない
- ワークフローログの `Discord OK` を確認
- webhook URL の正当性 → Discord Server 設定で再生成可能

### feature_history が貯まらない
- OKX 取得失敗の可能性 → ワークフローログで `OKX market fetch failed` を検索
- フォールバック (CoinGecko-only legacy) で動作するが composite score は使えない

### Nitter が常に空
- インスタンス全死亡が常態化 → `NITTER_INSTANCES` で別インスタンスを指定
- セルフホスト Nitter なら確実
- 諦めるなら `NITTER_ACCOUNTS=` で空にする

### state.json のコミットがスパム的
- 5分ごと自動コミット (`[skip ci]` 付き) が仕様
- 気になる場合は `actions/cache` 経由に切替（要 workflow 改修）

## 参考リンク

- [Bybit Public API](https://bybit-exchange.github.io/docs/v5/intro)
- [OKX Public API](https://www.okx.com/docs-v5/en/)
- [Deribit Public API](https://docs.deribit.com/)
- [Gemini API](https://ai.google.dev/)
- [FRED API](https://fred.stlouisfed.org/docs/api/fred/)
- [GitHub Actions cron](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#schedule)
