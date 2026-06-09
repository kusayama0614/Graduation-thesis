# 開発ワークフロー

このリポジトリでの基本的な作業手順をまとめます。

## 1. 起動

バックエンド:

```bash
cd backend
env FLASK_APP=run.py venv/bin/python -m flask run --host=0.0.0.0
```

フロントエンド:

```bash
python3 -m http.server 8000 --directory /workspaces/Graduation-thesis
```

ブラウザ:

- ログイン画面: http://127.0.0.1:8000/screens/login/login.html
- ホーム画面: http://127.0.0.1:8000/screens/home/home.html

## 2. 開発の流れ

1. 変更対象の画面またはAPIを1つに絞る
2. フロントとバックエンドの入出力を合わせる
3. ローカルで画面遷移とAPI応答を確認する
4. セッションやファイル保存などの副作用を確認する
5. 変更点を文書化する

## 3. 主要な確認項目

- ログイン後にホームへ遷移できること
- ホームから各機能画面へ移動できること
- アップロード画面からAPIへ送信できること
- ログアウト後に保護画面へ入れないこと

## 4. 便利なAPI確認

```bash
curl http://127.0.0.1:5000/health
curl http://127.0.0.1:5000/api/auth/session
```

## 5. 現在の次の候補

1. 解答データ確認画面をAPI連携する
2. 実データ一覧を返す `backend/app/routes/analysis.py` を強化する
3. 自動採点やレポート生成の処理を実装する