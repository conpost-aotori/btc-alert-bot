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

## ロールバック

WS 版を止めて Phase 1 cron に戻すだけ:

```bash
docker compose down
```

GitHub Actions 側は何も変更不要。すぐ次の cron が走ります。
