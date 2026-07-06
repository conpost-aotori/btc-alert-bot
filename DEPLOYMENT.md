# Deployment Guide — Phase 3 (WebSocket realtime mode)

**WebSocket 常時接続版** (`realtime.py`) を **AWS Lightsail** にデプロイする
手順。24/7 稼働で **急変検知が秒単位**になります（Lightsail は対象プランが
3ヶ月無料、以降 約$5/月。その他の API/配信は引き続き $0）。

> **現行構成**: この WS 版が一次系で、Phase 1 cron 版 (`main.py`,
> GitHub Actions) は `alert.yml` の `schedule` をコメントアウトして手動
> (`workflow_dispatch`) のみに変更済み。両方を同時に走らせると Discord 通知が
> 重複するため、cron は無効のまま運用します。

## アーキテクチャ

```
[AWS Lightsail instance] (1GB RAM / 2 vCPU プラン目安)
        │
        ├─ docker-compose up -d
        │       │
        │       └─ btc-alert-bot コンテナ
        │              │
        │              ├─ python -m btc_alert_bot.realtime
        │              ├─ OKX WSS (5min candle channel) を購読
        │              ├─ candle close ごとに同一の検知/分析/配信
        │              └─ /app/data/{state.json,history.sqlite} に永続化
        │
        └─ ./data は host 側の bind mount → docker 再起動で消えない
```

## 前提条件

- AWS アカウント（Lightsail を利用。対象プランは3ヶ月無料、以降 約$5/月）
- ローカル端末で `ssh` と `git` が使える
- Discord webhook URL / Gemini API key / FRED key (任意) が手元にある

## 手順

### Step 1. AWS Lightsail で インスタンスを作成

1. https://lightsail.aws.amazon.com/ にログイン
2. **Create instance**
3. **Instance location**: 近いリージョンを選択
4. **Platform / Blueprint**: Linux/Unix → **OS Only → Ubuntu 22.04 LTS**
5. **Instance plan**: **1 GB RAM / 2 vCPU / 40GB SSD**（$5/月・対象プランは
   3ヶ月無料）。`mem_limit: 512m` 運用なので 512MB プランでも動くが余裕を見て 1GB 推奨。
6. **SSH key pair**: 既定鍵をダウンロード、または自分の公開鍵をアップロード
7. **Create instance** → 数分で起動。必要なら **Networking → Static IP** を割当
   （アタッチ中は無料）。

### Step 2. ファイアウォール設定

このボットは **アウトバウンドのみ** (OKX WS / Discord webhook / etc) を使うので、
インバウンドは SSH (22) だけで十分。Lightsail の **Networking** タブで SSH(22)
は既定で開いており追加設定は不要。SSH ポートを変える場合だけ Ingress を調整。

### Step 3. VM に SSH 接続 + Docker インストール

```bash
ssh ubuntu@<your-vm-public-ip>

# ホストキーが追加されたら以下を実行
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose-plugin git

# 自分を docker グループに追加 (sudo なしで docker コマンドを使うため)
sudo usermod -aG docker $USER

# 一度ログアウトして再接続
exit
ssh ubuntu@<your-vm-public-ip>

# Verify
docker --version
docker compose version
```

### Step 4. リポジトリを clone + .env を設定

```bash
git clone https://github.com/<your-username>/btc-alert-bot.git
cd btc-alert-bot

# .env をその場で作る (commit しない)
cat > .env <<'EOF'
GEMINI_API_KEY=your-gemini-key
DISCORD_WEBHOOK_URL=your-webhook-url
FRED_API_KEY=your-fred-key
NITTER_ACCOUNTS=WatcherGuru,WuBlockchain,lookonchain,DeItaone

# X 投稿は Phase 3 でも opt-in
ENABLE_X_POST=false
DRY_RUN=false
LOG_LEVEL=INFO

# 年初来最安値の「緊急速報だけ」をXにも出す (通常スパイクはDiscordのまま)。
# X APIキー(下の4種)が必要。詳細は「年初来最安値の緊急速報をXへ」節。
# ENABLE_X_YTD_LOW=true
# YTD_ONESHOT=true   # 年初来最安値の緊急速報を「一度だけ」にする (再アームしない)

# イベントモード (FOMC 等の高インパクト時のみ感度を上げ、窓を抜けたら自動復帰)。
# 未設定なら完全に無効 = 通常挙動と byte 単位で同一。詳細は「イベントモード」節。
# EVENT_MODE_WINDOWS=2026-06-17T17:45:00Z/2026-06-17T21:00:00Z
# EVENT_MODE_THRESHOLD_MULT=0.6   # 発火しきい値の倍率 (既定 0.6)
# EVENT_MODE_COOLDOWN_MULT=0.5    # 同方向クールダウン/デバウンスの倍率 (既定 0.5)
# EVENT_MODE_OPP_DIR_MULT=0.1     # 逆方向(反転)クールダウンの倍率 (既定 0.1=6分)

# X 投稿を有効にする場合のみ:
# X_API_KEY=...
# X_API_SECRET=...
# X_ACCESS_TOKEN=...
# X_ACCESS_SECRET=...
EOF

chmod 600 .env
```

### Step 5. ビルド + 起動

```bash
docker compose build
docker compose up -d

# ログ確認
docker compose logs -f bot

# 期待される出力:
#   Realtime bot starting...
#   WS connected to wss://ws.okx.com:8443/ws/v5/public
#   WS event: {'event': 'subscribe', 'arg': {...}}
#   5m candle closed: ts=... close=...
#   No spike. (5min ごとに繰り返し)
```

`Ctrl+C` でログ追跡を抜けても、コンテナは `restart: unless-stopped` で動き続けます。

### Step 6. 状態の引き継ぎ (任意)

GitHub Actions cron 版で蓄積した `data/state.json` と `data/history.sqlite` を
そのまま再利用したい場合:

```bash
# ローカルから VM へコピー
scp data/state.json data/history.sqlite ubuntu@<vm-ip>:~/btc-alert-bot/data/

# VM 側で再起動
ssh ubuntu@<vm-ip> "cd btc-alert-bot && docker compose restart bot"
```

これで feature_history が継続、cooldown も引き継がれます。

### Step 7. 自動更新 (オプション)

git pull でコード更新を反映:

```bash
cd ~/btc-alert-bot
git pull
docker compose build
docker compose up -d
```

cron で自動化したい場合 (例: 毎日 04:00 UTC に更新):
```bash
crontab -e
# 追加:
0 4 * * * cd ~/btc-alert-bot && git pull && docker compose build && docker compose up -d >> ~/auto-update.log 2>&1
```

## 監視 / トラブルシューティング

### 生存確認

```bash
docker compose ps         # bot コンテナが running かつ healthy か
docker compose logs --tail 100 bot
```

`healthcheck` は `data/state.json` が直近 30分以内に更新されているかを見ます。
WS が長時間沈黙すれば `unhealthy` になります。

### 再接続が頻発する場合

`docker compose logs bot | grep "WS error"` で原因を確認。よくあるのは:
- VM の発信 IP が OKX に rate-limit されている → 5分以上待つ
- DNS 解決失敗 → `dig api.okx.com` で確認

### メモリ使用量

```bash
docker stats btc-alert-bot
```

通常 100〜200MB。`mem_limit: 512m` 上限で OOM しても自動再起動。

### ログの保管

`docker-compose.yml` で `max-size: 10m / max-file: 5` に設定済み (合計 50MB)。
ログを長期保管したい場合は journald や Loki に送る。

## 既知の注意点

- **OKX WS は 24時間ごとに切断**することがあります。`realtime.py` は
  exponential backoff で自動再接続するので問題なし。
- **AWS Lightsail** は対象プランが3ヶ月無料、以降 約$5/月の課金が発生。
  代替: AWS t4g.nano (月 $3.5)、Hetzner CX11 (月 €4)、あるいは元の
  GitHub Actions cron に戻る。
- **Phase 1 (GitHub Actions cron) と並行運用すると Discord通知が重複**。
  片方を停止する or cooldown を伸ばす or webhook を分ける、で対処。

## イベントモード（FOMC / CPI 等の高インパクト時のみ感度を上げる）

`EVENT_MODE_WINDOWS` に UTC の時間窓を入れておくと、その窓の間だけ発火
しきい値とクールダウンを一時的に下げ、窓を抜けると**自動で通常設定へ復帰**
します（コード変更不要、`.env` だけ）。スケジュールで感度を上げるのではなく、
窓の間に**価格が実際に動いたら**より小さい変動でも拾えるようにする仕組みです。

```bash
# 例: 2026-06-17 FOMC（政策金利発表 18:00 UTC、議長会見 18:30 UTC）。
#     17:45–21:00 UTC = 02:45–06:00 JST(6/18) をカバー。
EVENT_MODE_WINDOWS=2026-06-17T17:45:00Z/2026-06-17T21:00:00Z
EVENT_MODE_THRESHOLD_MULT=0.6   # 発火しきい値 ×0.6（例: 1h 2.0%→1.2%、1m 0.6%→0.36%）
EVENT_MODE_COOLDOWN_MULT=0.5    # 同方向CD/デバウンス ×0.5（例: 60min→30min、debounce 15min→7.5min）
EVENT_MODE_OPP_DIR_MULT=0.1     # 逆方向(反転)CD ×0.1（60min→6min。FOMCの急反転を拾うため意図的に強め）
```

- **逆方向(反転)クールダウンは別係数**: `EVENT_MODE_OPP_DIR_MULT`（既定 0.1=6分）。
  FOMCは「急騰→数分後に急反転」が典型で、通常の60分反転クールダウンだと
  反転側(2本目)を取りこぼす。これを意図的に強く短縮し、反転を捕捉する。
  より反応を上げたいなら 0.05(=3分) 等に。逆に反転スパムが嫌なら 0.3 等へ。
- **窓は複数可**: カンマ区切り（`A/B,C/D`）。`START/END` は ISO-8601 UTC。
  開始は含む・終了は含まない。`START < END` でない/壊れた要素は無視。
- **倍率は (0, 1]**: 範囲外や不正値は 1.0（無効化）にフォールバック。片方だけ
  使いたいときは他方を `1.0` に。
- **z-confirm(ATR/出来高)は下げない**: アンチfakeoutゲートは窓中も厳格なまま
  （発火フロアと一緒に緩めるとFOMCのチョップで誤発火が増えるため）。
- **未設定なら完全に無効** = 通常挙動と完全一致（係数は常に 1.0）。
- 反映は `.env` 更新 → `docker compose up -d`。起動ログに
  `event-mode: armed, ... [ACTIVE NOW]` / `disarmed` が出ます。窓に入ると
  検知ログに `Event-mode ACTIVE (...): fire thresholds ×0.60` が毎分出ます。
- **イベント後は窓を消す**（行をコメントアウトして `up -d`）と確実。ただし
  窓を過ぎれば自動で通常に戻るので消し忘れても害はありません。

> 実装は `src/btc_alert_bot/event_mode.py`（純関数・例外を出さない・既定オフ）。
> 単体/統合テスト: `python tests/test_event_mode.py` /
> `python tests/test_event_mode_integration.py`。

## 年初来最安値の緊急速報をXへ（一度だけ）

BTCが**年初来最安値を更新**すると、ボットは通常クールダウンを無視して
`🚨 BTC緊急暴落速報📉` を**必ず**発火し、本文先頭に
`🔴 年初来最安値を更新（$XX,XXX）` の行を付けます（配信成功まで再送＝絶対投稿）。
この緊急速報を**Xにも**出すための設定:

```bash
# .env に追記
ENABLE_X_YTD_LOW=true       # 年初来最安値の緊急速報だけをXにも投稿
YTD_ONESHOT=true            # その速報を「一度だけ」にする (再アームしない)
# X APIキー4種 (必須 — これが無いとXには出ない)
X_API_KEY=...
X_API_SECRET=...
X_ACCESS_TOKEN=...
X_ACCESS_SECRET=...
```

- **`ENABLE_X_YTD_LOW=true`**: **年初来最安値の緊急速報だけ**をXに投稿。通常スパイクは
  引き続きDiscordのみ（Xに出したくない要件向け）。**心理的節目($60k等)の破りは
  Xには出しません**（節目は頻度が高く「一度だけ」上限も無いため、対象外にしてあります）。
  全アラートをXに出したい場合は代わりに `ENABLE_X_POST=true`。
- **`YTD_ONESHOT=true`**: 年初来最安値の速報を**一度配信したらラッチして二度と出さない**。
  再アームは state.json の `ytd_emergency_fired` を消すか、この env を外す。
  既定オフ（=新安値ごと＋30日クールダウンの通常挙動）。
- 「年初来最安値を更新」の行は本文**先頭**なので、Xの文字数制限で切られても必ず残ります。
- X APIキーが無ければXには一切出ません（Discordは出ます）。
- **X優先コミット**: `ENABLE_X_YTD_LOW=true` かつ Xキーが揃っている場合、
  「一度だけ」の消費は**Xへの投稿成功が条件**。X投稿が一時失敗した場合は
  Discordに出ていても未消費のまま次の1分足で再送します（X成功までDiscordに
  同じ速報が繰り返され得ますが、X投稿が主目的のための仕様）。キー未設定の
  場合は従来どおりDiscord成功で確定（無限再送はしません）。
- **再送は5回まで**: キーが「存在するが死んでいる」（トークン失効・X無料枠
  月500件の上限超過など）場合、5回の再送後にエラーログを出して**Discord成功
  で確定にフォールバック**します（毎分の無限再送でDiscord/LLM/X APIを浪費
  しないため）。失敗時はログに `YTD-low X delivery failed` が出ます。
- ⚠️ **`DRY_RUN=true` と `YTD_ONESHOT=true` を併用しない**こと。dry-run時は実投稿
  しないのにラッチを消費しないよう、マイルストーンのcommitはdry-runでスキップします
  （＝dry-runで「一度だけ」を空打ちしません）。本番は `DRY_RUN=false` のままで。

## ロールバック

WS 版を止めて Phase 1 cron に戻すだけ:

```bash
docker compose down
```

GitHub Actions 側は何も変更不要。すぐ次の cron が走ります。
