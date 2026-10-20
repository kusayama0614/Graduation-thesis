"""テスト全体の設定。"""
import os

# sympy の検算を別プロセスに分ける仕組みは test_sympy_isolation.py で確かめる。
# それ以外のテストは同じプロセスで直接実行して速くする（作業プロセスの起動に数秒かかるため）
os.environ.setdefault('SYMPY_ISOLATION', 'off')
