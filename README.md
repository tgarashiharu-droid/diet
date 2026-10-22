# diet

コンビニ献立バランサー(`index.html`)。

## 商品データの更新

AIが考える価格は実際とずれることがあるため、コンビニ3社(セブン-イレブン / ローソン / ファミリーマート)の
公式サイトから最新の商品名・税込価格・栄養成分を取得して `data/products.json` に保存するスクリプトがあります。

```sh
pip install -r scripts/requirements.txt
python scripts/fetch_products.py                 # 全店舗(詳細ページも見るので数十分かかります)
python scripts/fetch_products.py --store lawson  # 1店舗だけ
python scripts/fetch_products.py --no-detail     # 一覧ページだけ(価格のみ・高速)
```

GitHub Actions(`.github/workflows/update-products.yml`)が毎週火曜の早朝に自動で実行してコミットします。
Actions タブの「商品データ更新」→「Run workflow」で手動実行もできます。

アプリは `data/products.json` を読み込めた場合、

- AIへのプロンプトに公式の商品リストを渡し、その中から選ばせる
- AIの回答の商品名を公式データと照合し、価格・栄養成分を公式の値で上書きして合計を再計算する
- 公式データに見つからなかった商品は価格の横に `*` を付けて推定値であることを示す

という動きになります。`file://` で直接開いた場合など読み込めないときは従来どおりAIの推定値を使います。
