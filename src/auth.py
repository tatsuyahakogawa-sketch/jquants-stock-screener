"""Streamlitマルチページ間で共有するパスワード認証。

app.pyの元々の実装をそのまま切り出したもの（挙動は変更していない）。
Streamlitのpages/以下は個別にスクリプトが実行されるため、app.pyだけで
認証していると、pages/以下のURLへ直接アクセスされた場合に認証をバイパス
できてしまう。そのためapp.pyと各pages/*.pyの両方の先頭でcheck_password()
を呼ぶ。
"""
from __future__ import annotations

import streamlit as st


def check_password() -> bool:
    """クラウド公開時、無関係な人に開かれてAPIキー（レート制限）を消費されるのを
    防ぐための簡易パスワード認証。secrets.tomlにapp_passwordが設定されていない
    場合（ローカル実行等）は認証をスキップする。
    """
    try:
        correct = st.secrets["app_password"]
    except (KeyError, FileNotFoundError):
        return True
    if st.session_state.get("authenticated"):
        return True

    st.title("日本株スクリーニング")
    password = st.text_input("パスワード", type="password")
    if password:
        if password == correct:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("パスワードが違います")
    return False
