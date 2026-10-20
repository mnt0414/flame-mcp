# flame-mcp

[English](./README.md) | 日本語

**Autodesk Flame のプロジェクトを、Model Context Protocol (MCP) を通して AI アシスタントに読み取らせ、編集させるためのツールです。**

flame-mcp は、Claude Code などの MCP クライアントを、起動中の Flame に接続します。やりたいことを普通の言葉で頼むと、アシスタントが合う操作 (op) を探し、Flame で実行します。flame-mcp は、アシスタントが実行した操作を記録します。ほとんどの変更はあとから元に戻せますが、元に戻せないものもあります ([元に戻す](#元に戻す) を参照)。

> [!NOTE]
> **現状: 公開前の版です。** 動作確認は **macOS 上の Flame 2025.2.4 と 2026.2.3** で行っています。他の版の Flame でも警告を表示して起動しますが、動作は未確認です。本プロジェクトは個人による独立したツールです。Autodesk が作成・承認・支援したものではなく、同じ名前の [abrahamADSK/flame-mcp](https://github.com/abrahamADSK/flame-mcp) とも関係はありません。

[できること](#できること) · [使い始める前に](#使い始める前に) · [クイックスタート](#クイックスタート) · [安全とプライバシー](#安全とプライバシー) · [元に戻す](#元に戻す) · [設定](#設定) · [トラブルシューティング](#トラブルシューティング) · [制約](#制約) · [仕組み](#仕組み) · [ドキュメント](#ドキュメント) · [質問・要望・不具合の報告](#質問要望不具合の報告)

## できること

flame-mcp は、プロジェクトの整理や、日常的に決まった手順で行う作業が得意です。

| 動作 | 例 |
| --- | --- |
| 読み取り | ライブラリ・reel・クリップ・シーケンスの一覧、セグメント(映像と音声)・Batch のノードとその属性の読み取り、2 つのシーケンスの比較 |
| 整理 | reel・クリップ・シーケンスの作成・複製・移動・改名、クリップとシーケンスの削除 |
| 編集 | セグメントの trim・slip・replace・削除(詰める／詰めない)、cut・insert・overwrite、映像トラックの移動、シーケンスの短縮、セグメント接続の読み書き、TimelineFX の追加と設定 |
| Batch | ノードの作成・接続・設定、setup の保存と読み込み、render |
| まとめて実行 | 複数の op を 1 つの手順として実行する (`plan.run`) |

op は 13 のカテゴリに分かれており、全部で 93 個あります。一覧は [docs/ops.md](docs/ops.md) を参照してください。

## 使い始める前に

> [!CAUTION]
> **アシスタントは、Flame のプロジェクトを直接操作します。** まずテスト用のプロジェクトで試してください。大事なプロジェクトをアシスタントに変更させる前には、アーカイブを作成してください。
>
> アシスタントが読み取った内容は、クライアントが使う AI サービスに送信されます。送信される内容は [AI サービスに送信される情報](#ai-サービスに送信される情報) を参照してください。NDA のある案件で使う前に、その AI サービスのデータ保持ポリシーと、クライアントのログを確認してください。

## クイックスタート

### 動作要件

| | |
| --- | --- |
| OS | macOS |
| Flame | 2025.2.4、2026.2.3 |
| Python | 3.11 以上 (サーバー側) |
| MCP クライアント | ローカルの stdio サーバーを実行できるもの。Claude Code で動作確認済みです。 |

flame-mcp が Flame に送信するのは ASCII の文字だけです。アシスタントに使わせる名前やファイルパスには、ASCII の文字を使ってください。アシスタントには、どの言語でも頼むことができます。

**1. コードを取得し、サーバーをインストールします。**

```bash
git clone https://github.com/mnt0414/flame-mcp.git
cd flame-mcp
python3.12 -m venv .venv
.venv/bin/pip install -e .
```

`python3.12` は、インストールされている 3.11 以上の Python に置き換えてください。macOS に付属している `/usr/bin/python3` は、3.11 より古い場合があります。

**2. Flame を終了してから、ブリッジを Flame にインストールします。**

インストーラは、実ファイルを 2 つコピーします。

- `flame_mcp_bridge.py`: Flame が起動時に読み込む入口です。`/opt/Autodesk/shared/python/` にインストールします。
- `impl.py`: ブリッジの本体です。`~/Library/Application Support/flame-mcp/bridge/` にインストールします。

同じ名前のファイルがすでにあれば、上書きする前にバックアップを作成します。

```bash
scripts/install_bridge.sh --dry-run
scripts/install_bridge.sh
scripts/doctor.sh
```

`--dry-run` を付けると、何も書き込まずに変更内容だけを表示します。問題がなければ `install_bridge.sh` を実行してください。`doctor.sh` は、インストールが正常に行われたかを診断します。

**3. MCP クライアントにサーバーを登録します。**

Claude Code の場合は、次のコマンドを実行してください。

```bash
claude mcp add flame -- /path/to/flame-mcp/scripts/flame_mcp_server.sh
```

<details>
<summary>JSON で設定する場合</summary>

JSON ファイルで設定するクライアントでは、`flame_mcp_server.sh` を `command` に書きます。ファイルを編集したら、クライアントを完全に終了してから再起動してください。

```json
{
  "mcpServers": {
    "flame": {
      "command": "/path/to/flame-mcp/scripts/flame_mcp_server.sh"
    }
  }
}
```

</details>

**4. 接続を確認します。**

Flame を起動してプロジェクトを開き、変更を伴わない簡単な指示で確認してください。

> 「Flame で開いているプロジェクト名を確認してください。プロジェクトの変更は行わないでください。」

アシスタントがプロジェクト名を答えれば、接続できています。答えない場合は [トラブルシューティング](#トラブルシューティング) を参照してください。

### 頼み方の例

まず読み取りから始めることを推奨します。その後、結果を確認しやすい変更で動作を確認してください。

> 「デスクトップの reel と、それぞれの中のクリップを一覧にして」

> 「このシーケンスの V1 の 3 番目のセグメントを、さっき取り込んだクリップに差し替えて。ソース TC は元に合わせて」

> 「このシーケンスの各セグメントを、ハンドル 10 フレーム付きで reel に取り出して」

> 「今の Batch group にこの setup を読み込んで、render する前に範囲を教えて」

「良くして」のような抽象的な頼み方よりも、対象・操作・結果の確認方法がはっきりした頼み方のほうが、うまくいくことが多いです。

### アンインストール

Flame を終了してから、次を実行します。

```bash
scripts/uninstall_bridge.sh --dry-run
scripts/uninstall_bridge.sh
```

このスクリプトは、`flame_mcp_bridge.py` と `impl.py` の 2 つのファイルを削除します。削除する前にバックアップを作成します。

変更の記録・取り消し用のファイル・ログは削除されずに残ります。残るものと保存場所は [保存されるファイルと保存期間](#保存されるファイルと保存期間) を参照してください。不要であれば、手動で削除してください。

MCP クライアントへの登録も残ります。Claude Code では、次のコマンドで削除できます。

```bash
claude mcp remove flame
```

## 安全とプライバシー

### AI サービスに送信される情報

flame-mcp 自体は、インターネットに何も送信しません。利用状況の収集 (テレメトリ) もありません。

アシスタントが flame-mcp から受け取った内容は、会話の一部として、MCP クライアントから AI サービスに送信されます。次のような内容が含まれます。

- プロジェクト・ライブラリ・reel・クリップ・シーケンス・ノードの名前
- Batch のノードのパラメータの値 (数値のほか、ノード名や Write File ノードの出力先パスなどの文字も含みます)
- ファイルパス (ユーザー名が含まれることがあります)
- タイムコードなどのメタデータ
- Flame のログの行

flame-mcp の op に、画像を読み取るものはありません (`eval.run` を除く)。`eval.run` を有効にすると、画像を含む全てのファイルを読み込むことが可能になります。

ローカルで動くモデルを使うクライアントであれば、会話の内容を外部に送信せずに使えます。ただし、クライアントがほかの情報を送信するかどうかは、クライアントとその設定に依存します。

### 操作の前の確認

操作の前にユーザーへ確認を求めるかどうかは、MCP クライアントの設定で決まります。flame-mcp の側では、アシスタントが使える op を [設定](#設定) で制限できます。また、Flame に送信する前に、すべての呼び出しを検査します。

### プロジェクトの中の文章による誘導

クリップ名・メタデータ・setup の中身・Flame のログは、そのままアシスタントに渡されます。これらの中に AI への指示のような文章が書かれていると、アシスタントがその指示に従ってしまうことがあります (プロンプトインジェクション)。他の人から受け取ったプロジェクトを調べるときは、[読み取り専用モード](#読み取り専用モード) を使ってください。

### flame-mcp の安全対策

- アシスタントの変更は、Flame に送信する前に記録します。
- ファイルを上書きする操作 (`overwrite=true`、既存のフレームへの render) は、破壊的な操作として扱います。`FLAME_MCP_ALLOW_EFFECTS` に `destructive` を含めなければ、これらは拒否されます。また flame-mcp は、上書きする操作や元に戻せない操作の前にユーザーへ確認するよう、アシスタントに指示しています。ただし、ユーザーが同意したかどうかを flame-mcp は確かめられないため、確認が行われるかはアシスタントとクライアントに依存します。
- 取り消し用の複製を削除する `journal.purge_checkpoints` は、先に dry run を行い、そのとき表示された token を渡した場合だけ削除します。
- 呼び出しが応答なしで終わった場合 (`TIMEOUT`、`BRIDGE_LOST`) は、アシスタントが実際の状態を確認し終えるまで、変更を拒否します。
- Flame に送信するのは ASCII の文字だけです。名前・パス・コードに ASCII 以外の文字が含まれていれば、何も送信せずに拒否します。
- 任意の Python を実行する `eval.run` は、既定で無効です ([任意の Python の実行](#任意の-python-の実行-evalrun) を参照)。

### 同じユーザーで動くほかのプログラム

ブリッジは、Flame を起動したユーザーだけが接続できる方法で待ち受け、受け取ったコードを実行します。flame-mcp の設定による制限が働くのは、MCP クライアントを通した操作だけです。同じユーザーで動くほかのプログラムが、ブリッジに直接送信した操作は制限できません。シェル (ターミナル) を操作できるアシスタントも、これに含まれます。信頼できないアシスタントには、Flame が起動しているコンピューターでシェルを使わせないでください。

### 複数のユーザーで使うコンピューター

`flame_mcp_bridge.py` は `/opt/Autodesk/shared/python/` にインストールされ、そのコンピューターのすべてのユーザーと、すべてのバージョンの Flame が読み込みます。このフォルダに他のユーザーが書き込めると、`flame_mcp_bridge.py` を差し替えられてしまいます。フォルダの権限を確認してください。なお `flame_mcp_bridge.py` は、本体の `impl.py` やそのフォルダが他のユーザーに書き換えられる状態であれば、本体を読み込みません。

### 保存されるファイルと保存期間

flame-mcp がディスクに作成するファイルは、Flame を起動したユーザーだけが読み取れるように作成します (ファイルは 0600、フォルダは 0700)。すでにあるフォルダの権限は変更しません。

| 内容 | 保存場所 | 保存期間 |
| --- | --- | --- |
| アシスタントが行った変更の記録 | `~/Library/Application Support/flame-mcp/journal/` | 30 日 |
| 取り消し用に保存した Batch の setup ファイル | `journal/checkpoints/` | 自動では削除しません |
| アシスタントがクリップやシーケンスを削除・編集する前に作成した、取り消し用の複製 | 各プロジェクトの `flame_mcp_checkpoints` ライブラリ | `journal.purge_checkpoints` で削除するまで |
| op の結果のうち、大きすぎてアシスタントに直接返せなかったもの (約 60 KB を超えたもの。アシスタントには保存先だけを返します) | `~/Library/Application Support/flame-mcp/spill/` | 7 日 (サーバーの起動時に、7 日より古いものを削除します) |
| ブリッジのログ | `~/Library/Application Support/flame-mcp/logs/` | 最大 4 ファイル (1 ファイルが 2 MB に達すると新しいファイルに切り替え、古いものから削除します) |

## 元に戻す

変更を元に戻したいときは、アシスタントに頼んでください (「さっきの変更を元に戻して」)。アシスタントは `journal.revert` を実行し、変更の記録をもとに元の状態に戻します。値を 1 つずつ元に戻せない変更は、取り消し用の setup ファイルや複製を使って元に戻します。変更のあとで対象が変わっていれば、上書きせずに止まります。

1 回の `plan.run` で行った変更は、まとめて元に戻ります。

> [!WARNING]
> **アシスタントが変更したあとは、Ctrl+Z を押さないでください。** 改名や属性の変更などは、Flame の Undo 履歴に記録されないことがあります。そのため Ctrl+Z を押すと、以前の別の操作が取り消されることがあります。

次のものは元に戻せません。元に戻せるかどうかは、各 op の説明 ([docs/ops.md](docs/ops.md)) に書いてあります。

- render が書き出したファイルと、作成したクリップ
- 保存した setup ファイル
- 作成した shelf reel と Batch の iteration
- `journal.purge_checkpoints` で削除した取り消し用の複製
- 取り消し用の setup ファイルに含まれない変更 (別の Batch group や Desktop など)

また、取り消し用の setup ファイルから元に戻すときは、あとから手で変えた属性の値やノードの位置を検出できません。そのため、元に戻すとその手作業の変更も失われることがあります。

詳しくは [docs/safety.md](docs/safety.md) (英語) を参照してください。

## 設定

通常は、設定は不要です。環境変数は、MCP クライアントの設定でサーバーに渡します。

| 変数 | 既定 | 意味 |
| --- | --- | --- |
| `FLAME_MCP_READONLY` | 無効 | `1`: 変更や書き込みの操作のない op だけを使う |
| `FLAME_MCP_ALLOW_CATEGORIES` | すべて | 使えるカテゴリをカンマ区切りで指定する (例 `library,desktop,timeline,segment,journal`) |
| `FLAME_MCP_ALLOW_EFFECTS` | すべて | 使える効果をカンマ区切りで指定する (`read`・`reversible`・`create`・`destructive`) |
| `FLAME_MCP_ENABLE_EVAL` | 無効 | `1`: `eval.run` を使えるようにする |
| `FLAME_MCP_CHECKPOINT_LIBRARY` | `flame_mcp_checkpoints` | 取り消し用の複製を保存するライブラリ |

- `FLAME_MCP_ALLOW_EFFECTS` には `read` を含めてください。含めないと、読み取りもすべて拒否されます。
- `FLAME_MCP_ALLOW_CATEGORIES` に `journal` を含めないと、アシスタントは変更の確認も、元に戻すこともできません。
- 設定が不正な場合 (例 `FLAME_MCP_READONLY=true`、カンマだけの一覧)、サーバーは起動せず、理由を表示します。

### 読み取り専用モード

プロジェクトを一切変更せずに、内容を確認・監査するときに使います。

```json
{
  "mcpServers": {
    "flame": {
      "command": "/path/to/flame-mcp/scripts/flame_mcp_server.sh",
      "env": { "FLAME_MCP_READONLY": "1" }
    }
  }
}
```

何も変更しない op だけが一覧に表示され、実行できます。`eval.run` は拒否されます。閉じているライブラリを名前で指定して読み取るときは、サーバーがいったん開き、読み取り後に閉じます。

### 任意の Python の実行 (`eval.run`)

`eval.run` は、どの op でもできないことのための最後の手段です。`FLAME_MCP_ENABLE_EVAL=1` を設定しない限り使えず、読み取り専用モードでは常に使えません。

> [!CAUTION]
> **`eval.run` は、Flame の中で任意の Python を、Flame を起動したユーザーの権限で実行します。** ファイルの読み書き・削除、ネットワークへのアクセス、プロセスの起動もできます。静的な検査はよくある誤りを止めますが、サンドボックスではないため、意図して書かれたコードは検査を通過します。タイムアウトはコードの完了を待つのをやめるだけで、コード自体は止めません。止まらないコードは、Flame を応答しない状態にすることがあります。アシスタントとプロジェクトを信頼できる場合だけ、有効にしてください。

## トラブルシューティング

### アシスタントが Flame に接続できない (`BRIDGE_UNAVAILABLE`)

1. Flame を起動して、プロジェクトを開いてください。ブリッジは、プロジェクトを開いてから接続を受け付けます。
2. Flame のメインメニューの **MCP Bridge > Show status** で、ブリッジが動いているかを確認してください。`Bridge is not running.` と表示された場合は、**Show last error** で理由を確認できます。
3. メインメニューに **MCP Bridge** がない場合は、ブリッジが読み込まれていません。Flame を終了してから `install_bridge.sh` を実行し、Flame を起動し直してください。インストールの状態は `scripts/doctor.sh` で確認できます。

Batch の render 中は、render が終わるまで接続できないことがあります。

### Flame を 2 つ以上起動している (`BRIDGE_AMBIGUOUS`)

ブリッジが動いている Flame が 2 つ以上あると、flame-mcp はどれにも接続しません。違う Flame を変更しないためです。使わない Flame を終了してください。

### 操作の途中で応答がなくなった (`TIMEOUT`・`BRIDGE_LOST`)

このエラーが表示された時点では、変更が反映されたかどうかは分かりません。アシスタントは、実際の状態を確認するまで、次の変更を行いません。

`BRIDGE_LOST` は、Flame がクラッシュした可能性を示します。Flame が終了していた場合は、Flame を再起動してから、アシスタントに状態の確認を頼んでください。

### 変更が反映されなかった (`SILENT_NOOP`)

Flame でダイアログが開いていないか確認してください。ダイアログが開いている間は、Media Panel への変更の一部が反映されないことがあります。ダイアログを閉じてから、もう一度アシスタントに頼んでください。

### クライアントに flame のツールが表示されない

- クライアントを完全に終了してから (メニューバーやトレイに残るプロセスも終了します)、再起動してください。
- 設定に書いた `flame_mcp_server.sh` のパスを確認してください。
- 環境変数の値が不正な場合、サーバーは起動しません。[設定](#設定) を確認してください。

### `non-ASCII character` と表示されて拒否された

Flame に送信できるのは ASCII の文字だけです。アシスタントには、どの言語でも頼むことができます。クリップやノードの名前、ファイルパスには ASCII の文字を使ってください。

### `the journal directory` の `non-ASCII character` と表示されてサーバーが起動しない

変更の記録を保存するフォルダのパスに、ASCII 以外の文字が含まれています。このパスは Flame に送信するコードに入るため、ASCII 以外の文字を含むと使えません。環境変数 `FLAME_MCP_JOURNAL_DIR` に、ASCII の文字だけのフォルダのパスを設定してください。環境変数の渡し方は [設定](#設定) を参照してください。

### アシスタントが「その操作はできない」と答える

- [設定](#設定) で使える op を制限している場合、制限した op はアシスタントに表示されません。
- アシスタントが op を探さずに答えていることがあります。`flame_catalog` で探すように頼んでください。
- 合う op がない場合、その作業は flame-mcp では行えません。op の一覧は [docs/ops.md](docs/ops.md) にあります。

### 解決しない場合

GitHub の Issues で報告してください。次の情報があると、原因を調べやすくなります。

- Flame と macOS の版
- `scripts/doctor.sh` の出力
- **MCP Bridge > Show last error** の内容
- `~/Library/Application Support/flame-mcp/logs/bridge.log` の該当部分 (ファイルパスやプロジェクト名が含まれます。公開してよい部分だけを貼ってください)

脆弱性は Issues ではなく、[SECURITY.md](.github/SECURITY.md) の手順で報告してください。

## 制約

- 未対応: transition・marker・audio・connected segments・reel や folder の削除・Linux。
- ライブラリの中身は、削除することも、ライブラリの外へ移動することもできません。ライブラリに保存された過去の作業データを、誤って変更しないためです。取り出すときは `desktop.copy` を使います (元は残ります)。
- 元に戻したシーケンス・クリップは、reel の末尾に入ります。Flame の API には、reel の中の並び順を指定する方法がありません。シーケンスを元に戻すときは、変更前に保存しておいた複製と差し替えます。変更後のシーケンスは削除せず、取り消し用のライブラリ (`flame_mcp_checkpoints`) に移します。
- 既知の落とし穴は、`flame_context` の `pitfalls` に記載されています。

## 仕組み

```
MCP クライアント  <-- stdio -->  flame-mcp サーバー  <-- Unix ソケット -->  Flame 内のブリッジ  -->  Flame Python API
```

サーバーは、アシスタントが使うツールを 4 つ用意しています。どれもアシスタントが自分で呼び出すもので、ユーザーが直接操作するものではありません。作業のほとんどは、`flame_catalog` と `flame_do` の 2 つで行います。アシスタントは `flame_catalog` で op を探し、`flame_do` で実行します。

| ツール | アシスタントがこれを使って行うこと | プロジェクトの変更 |
| --- | --- | --- |
| `flame_catalog` | 使える op を探し、その説明を読み取る | なし |
| `flame_do` | op を 1 つ実行する | op に依存 |
| `flame_context` | 作業の決まりと、Flame の今の状態を把握する | なし |
| `flame_version_info` | 接続と設定の状態を確認する | なし |

詳しくは [docs/architecture.md](docs/architecture.md) (英語) を参照してください。

## ドキュメント

| 文書 | 内容 |
| --- | --- |
| [docs/ops.md](docs/ops.md) | すべての op と、その効果・元に戻せるかどうか (自動生成) |
| [docs/architecture.md](docs/architecture.md) | サーバー・ブリッジ・変更の記録の仕組み (英語) |
| [docs/safety.md](docs/safety.md) | 何をどう元に戻すか、何が元に戻せないか、各エラーの読み方 (英語) |
| [docs/testing.md](docs/testing.md) | テストの実行方法と、実機の回帰テストがそれぞれ確かめること (英語) |

## 開発

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

`tests/` の単体テストは、Flame がなくても実行できます。実機の回帰テストは `scripts/regress.sh` です。Flame に変更を加えるため、専用のテストプロジェクトでだけ実行できます。準備と実行の手順は [docs/testing.md](docs/testing.md) (英語) を参照してください。

## 質問・要望・不具合の報告

質問・要望・不具合の報告は、GitHub の Issues で受け付けています。コードの変更を提案する場合は、プルリクエストを作る前に Issue で相談してください。

## 謝辞

- catalog と do に分ける設計と、registry・引数の検査・policy は、kumo.productions の [mcp-aftereffects](https://github.com/kumoproductions/mcp-aftereffects) (MIT) を移植したものです。[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) を参照してください。
- [abrahamADSK/flame-mcp](https://github.com/abrahamADSK/flame-mcp) と [dgirondi/FlameMCP](https://github.com/dgirondi/FlameMCP) を調査の参考にしました。
- flame-mcp は [Claude Code](https://claude.com/claude-code) を使って開発しました。設計の判断、コードのレビュー、Flame での動作確認は作者が行っています。

## 商標

Autodesk および Flame は、Autodesk, Inc. の登録商標または商標です。本プロジェクトは独立した非公式のツールであり、Autodesk, Inc. とは関係がなく、Autodesk による承認・支援を受けたものではありません。

## ライセンス

MIT。[LICENSE](LICENSE) を参照してください。
