# 神奈川県ダッシュボード

e-Stat API を月次で取得し、神奈川県の人口・経済・事業者動向をまとめた静的HTMLを GitHub Pages に自動公開するプロジェクト。

## 公開URL

GitHub Pages 有効化後に以下で参照可能:
`https://<your-github-user>.github.io/kanagawa/`

## 構成

```
.
├── config/stats.yaml          # 取得対象指標の定義(編集して指標を増減)
├── scripts/
│   ├── fetch_estat.py         # e-Stat APIからデータ取得 → data/*.json
│   └── build_site.py          # docs/index.html を生成
├── templates/index.html.j2    # ダッシュボードのJinja2テンプレ
├── docs/                      # GitHub Pages公開ディレクトリ
│   ├── index.html             # 自動生成
│   └── assets/style.css
├── data/                      # 取得済みJSON(コミット対象)
└── .github/workflows/update.yml  # 月次cron + 手動実行
```

## ローカル実行

```bash
pip install -r requirements.txt
echo "ESTAT_APP_ID=xxxxxxxxxxxxxxxxxxxx" > .env  # e-Stat appId
python scripts/fetch_estat.py
python scripts/build_site.py
open docs/index.html
```

## 自動更新の仕組み

GitHub Actions の `update.yml` が以下のタイミングで動きます:
- 毎月1日 00:00 JST(`cron: "0 15 1 * *"`)
- 手動実行(Actions タブから `Run workflow`)
- `main` への push(scripts/templates/config 変更時)

ジョブは `fetch_estat.py` → `build_site.py` を実行し、生成された `docs/` を GitHub Pages にデプロイします。

## 指標を追加・修正する

`config/stats.yaml` の `indicators:` リストに追記すれば次回更新で反映されます。

各指標は以下のいずれかで取得対象を指定:

| 方法 | 説明 |
|------|------|
| `statsDataId` | 統計表IDを直接指定(最も確実) |
| `statsCode` + `titleMatch` | 統計コード配下の表をタイトル正規表現で発見(最終更新が新しいもの優先) |

`filters:` には e-Stat の `cdArea`/`cdCat01`/`cdTab`/`cdTime` 等を指定。神奈川県は通常 `cdArea: "14000"`(古い表では `cdCat01: "14000"` の場合あり)。

統計表の構造を調べるには:
```bash
python3 -c "
import os, requests; from dotenv import load_dotenv; load_dotenv()
appid = os.environ['ESTAT_APP_ID']
sid = '0003411564'  # 調べたい統計表ID
r = requests.get('https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData',
  params={'appId': appid, 'statsDataId': sid, 'cdArea': '14000', 'metaGetFlg': 'Y', 'limit': 5}).json()
for c in r['GET_STATS_DATA']['STATISTICAL_DATA']['CLASS_INF']['CLASS_OBJ']:
    cls = c.get('CLASS', [])
    if isinstance(cls, dict): cls = [cls]
    print(f\"{c['@id']} ({c.get('@name')}):\")
    for x in cls[:10]: print(f\"  {x.get('@code')}: {x.get('@name')}\")
"
```

## トラブルシューティング

- **「該当データはありませんでした」**: `filters` のコード(特に `cdArea`)が表に存在しないことが原因。表によっては地域が `cat01` に入っているので `cdCat01: "14000"` を試す
- **時系列が1点しかない**: 経済センサスなど5年に1度の調査は単発。複数年版の statsDataId を別 indicator として並べる
- **`titleMatch` がヒットしない**: 統計コードのテーブル一覧をAPIで確認し、正規表現を調整(失敗時は最終更新が新しい表に自動フォールバックして警告を出す)

## データ出典

- 政府統計の総合窓口 e-Stat: https://www.e-stat.go.jp/
- 神奈川県統計センター: https://www.pref.kanagawa.jp/docs/x6z/index.html

数値はAPI取得時点のものです。最新の正確な値は各出典でご確認ください。
