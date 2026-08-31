# Graduation-thesis

## Firebase を使ったデータベース開発

このシステムは Firestore をデータ保存先として使えるようにしています。Firebase が未設定でも既存の SQLite で動くため、段階的に切り替えできます。

### 設定項目

- `FIREBASE_PROJECT_ID`
- `GOOGLE_APPLICATION_CREDENTIALS`
- `FIREBASE_CREDENTIALS_JSON`

### 進め方

1. Firebase Console でプロジェクトを作成します。
2. Firestore Database を有効化します。
3. サービスアカウントキーを発行します。
4. `backend/.env` に上記の環境変数を設定します。
5. `http://localhost:5001/health/firebase` で接続状態を確認します。