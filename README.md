# herdr-gekiatsu-plugin 🎰

**Claude Code の使用回数が分かる herdr プラグイン** ——ただし見た目はパチスロ台。

herdr のサイドバー（agents パネル）の最下段にパチスロ筐体が住み着きます。
Claude にメッセージを送るたびレバーが引かれ、Claude が考えている間リールが回り、
回答が終わった瞬間にリールが止まる。**回転数 = その日の Claude Code 使用回数**です。

```
╔═══════════════╗
║  1揃い/42回転 ║   ← 今日の 揃い数/使用回数（毎朝10時リセット）
╠═══════════════╣
║  かんがえ中…  ║   ← モニター（リーチ・大当り演出つき）
║   　　≫　　   ║
╟───────────────╢
║┌──┐ ┌──┐ ┌──┐ ║
║│７│ │７│ │４│ ║   ← リール（偶数=青/奇数=赤、揃うと色が変わる）
║└──┘ └──┘ └──┘ ║
║               ╠══○ ← レバー（スピン開始でガコッと下がる）
║  ●    ●    ●  ║   ← 停止ボタン
╚═══════════════╝
```

## 仕様

- **1やりとり = 1回転**。Claude ペインが working になった瞬間に回り始め、回答完了（あなたの番）で停止
- **回転数カウンタは毎日 日本時間 朝10:00 にリセット**（開店）
- **大当たり確率: 1/99**
  - 大当たりの内訳: **777 = 1%** / **奇数揃い(1,3,5,9) = 49%** / **偶数揃い(0,2,4,6,8) = 50%**
- 演出:
  - リーチ（左2つ揃い）: モニター点滅 + 3リール目じらし
  - 奇数揃い → 「**1500 RUSH突入！**」（赤点滅）
  - 偶数揃い → 「**300ぼーなすにゃ**」（青点滅）
  - **777** → 確定ポップアップ（リーチ中に開いた時点で当たり確定）+ 虹色の王冠「**3000 FEVER**」、10秒で自動クローズ
  - 当たり演出は次のスピンまで動き続けます

## 音声について

777 が揃った瞬間に鳴らす効果音は**同梱していません**。
お好きな音声ファイルを **`777.mp3`** という名前でこのプラグインのフォルダに置いてください。
置いてあれば揃った瞬間に `afplay` で再生されます（macOS）。無ければ無音のままです。

## 必要なもの

- herdr >= 0.7.0
- macOS または Linux（音声再生は macOS のみ）
- PATH に `python3`

## インストール

```sh
herdr plugin install yuuta1219/herdr-pachisuro-plugin --yes
```

`~/.config/herdr/config.toml` に以下を追加：

```toml
# agents パネルをスペース順に（筐体を最下段に固定するため）
[ui]
agent_panel_sort = "spaces"

# 筐体レイアウト（色違いトークンは出し分け式。行内は常に1トークンだけ値を持つ）
[ui.sidebar.agents]
rows = [
  ["state_icon", "workspace"],
  [{ token = "$s_top", fg = "#e987ae" }],
  [{ token = "$s_marq", fg = "#f5c2e7" }],
  [{ token = "$s_sep1", fg = "#e987ae" }],
  [{ token = "$s1_w", fg = "#ffffff", bold = true }, { token = "$s1_b", fg = "#74a8fc", bold = true }, { token = "$s1_r", fg = "#f8719d", bold = true }, { token = "$s1_y", fg = "#f9e2af", bold = true }, { token = "$s1_c", fg = "#94e2d5", bold = true }, { token = "$s1_m", fg = "#cba6f7", bold = true }],
  [{ token = "$s2_w", fg = "#f5c2e7" }, { token = "$s2_b", fg = "#74a8fc" }, { token = "$s2_r", fg = "#f8719d" }, { token = "$s2_y", fg = "#f9e2af" }, { token = "$s2_c", fg = "#94e2d5" }, { token = "$s2_m", fg = "#cba6f7" }],
  [{ token = "$s_sep2", fg = "#e987ae" }],
  [{ token = "$s_rl_t", fg = "#e987ae" }],
  [{ token = "$rl_b", fg = "#74a8fc", bold = true }, { token = "$rl_r", fg = "#f8719d", bold = true }, { token = "$rl_y", fg = "#f9e2af", bold = true }, { token = "$rl_c", fg = "#94e2d5", bold = true }, { token = "$rl_m", fg = "#cba6f7", bold = true }],
  [{ token = "$s_rl_b", fg = "#e987ae" }],
  ["$s_gap"],
  [{ token = "$s_btn", fg = "#f5c2e7" }],
  [{ token = "$s_bot", fg = "#e987ae" }],
]
```

設定を反映して起動：

```sh
herdr server reload-config
herdr plugin action invoke start --plugin miko.claude-slot
```

以降は herdr サーバー起動時に自動で立ち上がります。

## 操作

```sh
# 手動テストスピン（引く→回る→止まる）
herdr plugin action invoke spin --plugin miko.claude-slot

# 停止（筐体ワークスペースごと消える）
herdr plugin action invoke stop --plugin miko.claude-slot
```

デバッグ用に、状態ディレクトリ（`~/.local/state/herdr/plugins/miko.claude-slot/`）へ
`force.json` に `[7, 7, 7]` を書いておくと**次の1回だけ**その出目で着地します。

## 仕組み

- ミニワークスペース（🎰）の pane を `herdr pane report-agent` で疑似エージェント化し、pane メタデータトークンで筐体を1行ずつ描画
- Claude の状態変化は socket API `agent.list` の1秒ポーリングで検知
- 色は「同じ行に色違いトークンを並べ、使う色だけに値を入れる」方式（herdr のトークン色は config 固定のため）
- 777 の確定ポップアップはプラグインペイン（64x32 セル、popup 配置）。プロセス終了で自動クローズ
- 姉妹プラグイン: [claude-usage](https://github.com/yuuta1219/claude-usage)（使用量%をサイドバーに常時表示）
